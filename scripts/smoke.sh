#!/usr/bin/env bash
# 端到端冒烟：**真起服务 + 真 HTTP + 假上游**（零真实上游请求）。
#
#   zsh scripts/smoke.sh [端口]        # 默认 8699（服务）/ 8700（假上游）
#
# 为什么用假上游：本服务唯一"有代价"的动作是打上游（消耗匿名试用配额）。
# 冒烟要验证的是**我们这条链路**（鉴权、参数校验、翻译、错误信封、取件、静默窗），
# 而"上游能不能出活"由 `scripts/probe.py --live` 单独验证（那是另一件事，要花额度）。
set -euo pipefail

PORT=${1:-8699}
MOCK_PORT=$((PORT + 1))
PY=${PY:-python3}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
BASE="http://127.0.0.1:${PORT}"

cleanup() {
  [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
  [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "== 起假上游 :$MOCK_PORT（crop_enhance_image 触发 431 场景）=="
MOCK_SCENARIO="crop_enhance_image:431" "$PY" scripts/mock_upstream.py "$MOCK_PORT" &
MOCK_PID=$!

echo "== 起服务 :$PORT（上游指向假上游，配额静默窗 600s）=="
TEXTIN_BASE_URL="http://127.0.0.1:${MOCK_PORT}" TEXTIN_API_KEYS="" TEXTIN_QUOTA_COOLDOWN=600 \
  "$PY" -m uvicorn "app.main:create_app" --factory --host 127.0.0.1 --port "$PORT" \
  > /tmp/textin_smoke_app.log 2>&1 &
APP_PID=$!

for _ in $(seq 1 40); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.25
done
curl -sf "$BASE/healthz" >/dev/null || { echo "服务没起来；日志：/tmp/textin_smoke_app.log"; exit 1; }
echo "   healthz ok"

"$PY" - "$BASE" "$MOCK_PORT" <<'PY'
import base64, json, sys, urllib.error, urllib.request

base, mock = sys.argv[1], sys.argv[2]
fail = 0

def post(path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc), dict(exc.headers)

def get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.load(resp)

def check(label, ok, extra=""):
    global fail
    print(("✅ " if ok else "❌ ") + label + (f"  {extra}" if extra else ""))
    fail += 0 if ok else 1

png = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000018000000100802000000aa17c1a0"
    "0000001a49444154789c63fcffff3f0326c8280a6360148500a3280c0083b80a1d"
    "0000000049454e44ae426082")
data_uri = "data:image/png;base64," + base64.b64encode(png).decode()

# 1) 图像族 happy path（默认 b64_json）
st, body, _ = post("/v1/images/generations",
                   {"model": "textin:watermark-remove", "image": data_uri})
check("images 200 + b64_json",
      st == 200 and body["data"][0]["b64_json"] and body["data"][0]["mime"] == "image/png",
      f"status={st}")

# 2) response_format=url → 落盘 + /files 取件
st, body, _ = post("/v1/images/generations",
                   {"model": "textin:demoire", "image": data_uri, "response_format": "url"})
url = body["data"][0].get("url", "") if st == 200 else ""
ok_served = False
if url:
    with urllib.request.urlopen(base + url, timeout=10) as resp:
        ok_served = resp.status == 200 and resp.read().startswith(b"\x89PNG")
check("response_format=url → /files 取件", st == 200 and ok_served, f"url={url}")

# 3) 转换族 + 解析族各来一发
st, body, _ = post("/v1/files/convert",
                   {"model": "textin:pdf-to-word", "filename": "样本.pdf",
                    "file": "data:application/pdf;base64," +
                            base64.b64encode(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n").decode()})
check("convert 200 + docx 文件名",
      st == 200 and body["data"][0]["filename"] == "样本.docx",
      f"status={st} name={body.get('data', [{}])[0].get('filename')}")

st, body, _ = post("/v1/files/parse",
                   {"model": "textin:table-excel", "file": data_uri})
check("parse 200 + xlsx 附件",
      st == 200 and body["data"][0]["attachments"]
      and body["data"][0]["attachments"][0]["filename"].endswith(".xlsx"),
      f"status={st}")

# 4) dry_run：零上游请求
_, before = get(f"http://127.0.0.1:{mock}/mock/log")
st, body, _ = post("/v1/files/convert",
                   {"model": "textin:pdf-to-word", "dry_run": True,
                    "file": "data:application/pdf;base64,JVBERi0xLjQKJSVFT0YK"})
_, after = get(f"http://127.0.0.1:{mock}/mock/log")
check("dry_run 有 preview 且零上游请求",
      st == 200 and body["dry_run"] is True and "service=pdf-to-word" in body["preview"]["url"]
      and len(after["entries"]) == len(before["entries"]))

# 5) 假上游看到的 = 我们以为发出的（service / content-type / body / token / xff）
_, log = get(f"http://127.0.0.1:{mock}/mock/log")
first = log["entries"][0]
check("上游收到 service=watermark-remove", first["service"] == "watermark-remove", first["service"])
check("上游收到 Content-Type=image/png（真实类型）", first["content_type"] == "image/png",
      first["content_type"])
check("上游收到 PNG magic 的裸字节 body", first["body_head"].startswith("89504e47"), first["body_head"])
check("匿名发**空 token 头**（与实测 CLI 逐字一致）", first["token_header"] == "",
      repr(first["token_header"]))
check("默认不做 XFF 轮换", first["xff"] is None, str(first["xff"]))

# 6) 门禁项 503（未取证能力）
st, body, _ = post("/v1/files/convert",
                   {"model": "textin:ofd-to-image",
                    "file": "data:application/zip;base64," + base64.b64encode(b"PK\x03\x04").decode()})
check("未取证项默认 503 + 开启方式",
      st == 503 and body["error"]["code"] == "capability_not_verified"
      and "TEXTIN_ALLOW_UNVERIFIED" in body["error"]["message"], f"status={st}")

# 7) 模型打错端点 → 400 并指路
st, body, _ = post("/v1/images/generations", {"model": "textin:pdf-to-word",
                                              "file": "data:application/pdf;base64,JVBERi0="})
check("模型打错端点 → 400 + 指路",
      st == 400 and body["error"]["code"] == "model_wrong_endpoint"
      and "/v1/files/convert" in body["error"]["message"], f"status={st}")

# 8) 430/431：假上游对 crop_enhance_image 回 431 ⇒ 429 + Retry-After + 静默窗
_, cnt_before = get(f"http://127.0.0.1:{mock}/mock/log")
st1, body1, h1 = post("/v1/images/generations",
                      {"model": "textin:crop-enhance", "image": data_uri})
st2, body2, _ = post("/v1/images/generations",
                     {"model": "textin:crop-enhance", "image": data_uri})
_, cnt_after = get(f"http://127.0.0.1:{mock}/mock/log")
delta = len(cnt_after["entries"]) - len(cnt_before["entries"])
check("431 → 429（kind=daily_quota）+ Retry-After=600",
      st1 == 429 and body1["error"]["kind"] == "daily_quota"
      and h1.get("retry-after") == "600", f"status={st1} retry-after={h1.get('retry-after')}")
check("静默窗内第二次直接 429 且**不打上游**",
      st2 == 429 and body2["error"]["code"] == "daily_quota_cooldown" and delta == 1,
      f"upstream_calls=+{delta}")

print()
print("冒烟结果：" + ("全部通过" if fail == 0 else f"失败 {fail} 项"))
sys.exit(0 if fail == 0 else 1)
PY

echo "== 收尾 =="
