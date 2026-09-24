#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图像族「多形态」矩阵测试 —— 打的是**已部署的服务**（默认 https://textin.1task.cn）。

覆盖三个维度：
  · 输入形态：`data-uri` ／ 裸 base64 ／ 外链 URL
  · 输入类型：png / jpeg / webp / bmp / tiff / 带 alpha 的 png（+ pdf，仅 crop-enhance·demoire 接受）
  · 能力：图像族 4 条（watermark-remove / crop-enhance / demoire / text-auto-removal）

**默认只跑 dry_run**（零上游调用；只有 URL 形态会"代取"一次），`--real` 才追加一小批
真实调用（走部署侧代理池，匿名试用额度）。这与仓内「默认零消耗」的纪律一致。

用法：
    TEXTIN_KEY=<key> python3 scripts/matrix_image.py                  # 零成本：dry_run 矩阵
    TEXTIN_KEY=<key> python3 scripts/matrix_image.py --real           # 追加真实调用（见 REAL_CASES）
    TEXTIN_KEY=<key> python3 scripts/matrix_image.py --base http://127.0.0.1:39015
    ... --forms-dir /path/to/imgs     # 自己备语料（不给就用 Pillow 现场造）

依赖：标准库；造语料需要 Pillow（没有就传 --forms-dir）。
⚠️ 只用 `http.client`（**不经任何代理**，语义等同 `curl --noproxy '*'`）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import pathlib
import ssl
import sys
import time
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
BASE_SAMPLE = HERE / "samples" / "wm_sample.png"
# 外链形态用一张"长期存在"的公开图；用自己人可以复现的地址更好，但这里只需要"服务代取"这一条被验到
EXTERNAL_URL = "https://www.python.org/static/img/python-logo.png"

IMAGE_CAPS = ["textin:watermark-remove", "textin:crop-enhance",
              "textin:demoire", "textin:text-auto-removal"]
#: 各能力接受 PDF（图+PDF）——用于「pdf 形态」的正例/负例对照
PDF_OK = {"textin:crop-enhance", "textin:demoire"}
#: 真实调用用例（形态 × 能力），刻意压到最少：每条=1 次上游调用
REAL_CASES = [
    ("裸base64 × 去水印", "textin:watermark-remove", "base64", "in.png", {}),
    ("外链URL × 去水印", "textin:watermark-remove", "url", EXTERNAL_URL, {}),
    ("JPEG × 去水印", "textin:watermark-remove", "data-uri", "in.jpg", {}),
    ("url交付 × 去水印", "textin:watermark-remove", "data-uri", "in.png", {"response_format": "url"}),
    ("PNG × 切边增强", "textin:crop-enhance", "data-uri", "in.png", {}),
    ("WEBP × 去屏幕纹", "textin:demoire", "data-uri", "in.webp", {}),
    ("TIFF × 擦手写", "textin:text-auto-removal", "data-uri", "in.tiff", {}),
]


def build_forms(out_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """造多格式语料（需要 Pillow）。返回 {名字: 路径}。"""
    from PIL import Image  # noqa: PLC0415 - 只有造语料时才需要

    src = Image.open(BASE_SAMPLE)
    rgb = src.convert("RGB")
    files = {"in.png": None, "in.jpg": None, "in.webp": None,
             "in.bmp": None, "in.tiff": None, "in_rgba.png": None, "in.pdf": None}
    rgb.save(out_dir / "in.png")
    rgb.save(out_dir / "in.jpg", quality=92)
    rgb.save(out_dir / "in.webp", quality=92)
    rgb.save(out_dir / "in.bmp")
    rgb.save(out_dir / "in.tiff")
    src.convert("RGBA").save(out_dir / "in_rgba.png")     # 带 alpha：验"嗅探按字节、alpha 会丢"的形态
    rgb.save(out_dir / "in.pdf")                          # PDF 形态：仅 crop-enhance/demoire 接受
    return {k: out_dir / k for k in files}


def as_payload(form: str, src, model: str, extra: dict) -> dict:
    """把"形态"翻译成请求体。"""
    if form == "url":
        p = {"model": model, "image": src}
    else:
        raw = pathlib.Path(src).read_bytes()
        if form == "data-uri":
            ext = pathlib.Path(src).suffix.lower().lstrip(".")
            mime = {"jpg": "jpeg", "tif": "tiff"}.get(ext, ext)
            p = {"model": model, "image": f"data:image/{mime};base64,{base64.b64encode(raw).decode()}"}
        else:                                   # 裸 base64
            p = {"model": model, "image": base64.b64encode(raw).decode()}
    p.update(extra)
    return p


def call(base: str, path: str, payload: dict | None, key: str | None,
         method: str = "POST", timeout: int = 180) -> tuple[int, dict | bytes, float]:
    if path.startswith("http"):                      # 绝对 URL（取 /files/ 产物）
        u = urllib.parse.urlparse(path)
        target = u.path + (("?" + u.query) if u.query else "")
    else:
        u = urllib.parse.urlparse(base)
        target = u.path.rstrip("/") + path
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    ctx = ssl.create_default_context() if u.scheme == "https" else None
    port = u.port or (443 if u.scheme == "https" else 80)
    conn = cls(u.hostname, port, timeout=timeout, context=ctx) if ctx else cls(u.hostname, port, timeout=timeout)
    headers = {"content-type": "application/json"} if payload is not None else {}
    if key:
        headers["authorization"] = f"Bearer {key}"
    t0 = time.time()
    try:
        conn.request(method, target,
                     body=json.dumps(payload).encode() if payload is not None else None,
                     headers=headers)
        r = conn.getresponse()
        body = r.read()
        dt = time.time() - t0
        if r.getheader("content-type", "").startswith("application/json"):
            return r.status, json.loads(body), dt
        return r.status, body, dt
    finally:
        conn.close()


def _is_soft_limit(st: int, body) -> bool:
    """上游软限：HTTP 429 或错误码 451（`need_register`，按 service 独立计、多节点软限）。"""
    return st == 429 or (isinstance(body, dict) and body.get("error", {}).get("code") == 451)


class Matrix:
    def __init__(self, base: str, key: str | None, retry_soft: int = 1, retry_wait: float = 8.0) -> None:
        self.base, self.key = base, key
        self.retry_soft, self.retry_wait = retry_soft, retry_wait
        self.rows: list[dict] = []

    def rec(self, group: str, case: str, ok: bool | None, detail: str) -> None:
        """ok: True=通过 / False=失败 / None=软限（上游配额，非缺陷 —— 契约里 451 是"每 4 发 1 次"）。"""
        tag = {True: "✓", False: "✗", None: "⚠"}[ok]
        self.rows.append({"group": group, "case": case, "result":
                          "pass" if ok is True else ("soft" if ok is None else "fail"), "detail": detail})
        print(f"  [{tag}] {group:12s} {case:34s} {detail}")

    # ---------- 零成本：dry_run 矩阵 ----------
    def dry_forms(self, forms: dict) -> None:
        for cap in IMAGE_CAPS:
            for form, src in (("data-uri", forms["in.png"]), ("base64", forms["in.png"]),
                              ("url", EXTERNAL_URL)):
                st, body, _ = call(self.base, "/v1/images/generations",
                                   as_payload(form, src, cap, {"dry_run": True}), self.key)
                if isinstance(body, bytes):
                    self.rec("forms", f"{cap} × {form}", False, f"非 JSON（HTTP {st}）")
                    continue
                eff = body.get("effective", {}).get("input", {})
                req = body.get("requested", {}).get("image", {})
                ok = st == 200 and req.get("form") == form
                self.rec("forms", f"{cap.split(':')[1]} × {form}", ok,
                         f"HTTP {st} ｜ requested.form={req.get('form')} ｜ "
                         f"sniffed={eff.get('kind')} ｜ source={eff.get('source')}")

    def dry_kinds(self, forms: dict) -> None:
        for cap in IMAGE_CAPS:
            for name in ("in.png", "in.jpg", "in.webp", "in.bmp", "in.tiff", "in_rgba.png", "in.pdf"):
                want = name.split("_")[-1].split(".")[-1]
                want = {"jpg": "jpeg", "tif": "tiff"}.get(want, want)   # 服务报的是 MIME 名
                label = name.rsplit(".", 1)[0].replace("in_", "") or "png"
                st, body, _ = call(self.base, "/v1/images/generations",
                                   as_payload("data-uri", forms[name], cap, {"dry_run": True}), self.key)
                if isinstance(body, bytes):
                    self.rec("kinds", f"{cap.split(':')[1]} × {label}", False, f"非 JSON（HTTP {st}）")
                    continue
                if st == 200:
                    kind = body["effective"]["input"]["kind"]
                    self.rec("kinds", f"{cap.split(':')[1]} × {label}", kind == want,
                             f"HTTP 200 ｜ 嗅探={kind}" + ("（rgba 输入）" if "rgba" in name else ""))
                else:  # 负例：pdf 不在 accepts 内 ⇒ 400 且报嗅探结果
                    err = body.get("error", {})
                    expect = "input_kind_not_accepted" if (want == "pdf" and cap not in PDF_OK) else None
                    ok = err.get("code") == expect if expect else False
                    self.rec("kinds", f"{cap.split(':')[1]} × {label}", ok,
                             f"HTTP {st} ｜ {err.get('code')}（{'负例：accepts 不含 pdf' if expect else '非预期拒绝'}）")

    def dry_negatives(self, forms: dict) -> None:
        png = forms["in.png"]
        cases = [
            ("坏 base64", {"model": "textin:watermark-remove", "image": "data:image/png;base64,!!!!"},
             "invalid_base64"),
            ("docx 打图族", {"model": "textin:watermark-remove",
                          "image": "data:application/vnd.openxmlformats-officedocument."
                                   "wordprocessingml.document;base64,UEsDBBQABgAIAAAA"},
             "input_kind_not_accepted"),
            ("convert 模型错端点", {"model": "textin:pdf-to-word", "file": "data:application/pdf;base64,JVBERi0="},
             "model_wrong_endpoint"),
            ("未知字段", {"model": "textin:watermark-remove", "image": "data:image/png;base64,"
                      + base64.b64encode(png.read_bytes()).decode(), "n": 2, "dry_run": True}, None),
        ]
        for name, payload, want_code in cases:
            st, body, _ = call(self.base, "/v1/images/generations", payload, self.key)
            if isinstance(body, bytes):
                self.rec("negatives", name, False, f"非 JSON（HTTP {st}）")
                continue
            if want_code:
                ok = st == 400 and body.get("error", {}).get("code") == want_code
                self.rec("negatives", name, ok, f"HTTP {st} ｜ {body.get('error', {}).get('code')}")
            else:  # 未知字段：应 200 + 在 unsupported[] 留痕（不被静默吞掉）
                uns = body.get("unsupported", [])
                names = [u if isinstance(u, str) else u.get("field") for u in uns]
                ok = st == 200 and "n" in names
                self.rec("negatives", name, ok, f"HTTP {st} ｜ unsupported={names}")

    # ---------- 真实调用（--real） ----------
    def real(self, forms: dict) -> None:
        for label, cap, form, src, extra in REAL_CASES:
            payload = as_payload(form, forms.get(src, src), cap, extra)
            st, body, dt = call(self.base, "/v1/images/generations", payload, self.key)
            for _ in range(self.retry_soft):        # 451/429 = 上游软限，换出口/等一拍常可过
                if not _is_soft_limit(st, body):
                    break
                print(f"     ⚠ {label}: 上游软限（451/429）→ {self.retry_wait}s 后重试一次")
                time.sleep(self.retry_wait)
                st, body, dt = call(self.base, "/v1/images/generations", payload, self.key)
            if _is_soft_limit(st, body):
                self.rec("real", label, None,
                         f"HTTP {st} ｜ 上游软限（{body.get('error', {}).get('message', '')[:60]}…）—— 非缺陷，契约 §4")
                continue
            if isinstance(body, bytes) or st != 200:
                self.rec("real", label, False,
                         f"HTTP {st} ｜ {json.dumps(body, ensure_ascii=False)[:120] if not isinstance(body, bytes) else body[:60]}")
                continue
            item = body["data"][0]
            if "b64_json" in item:
                raw = base64.b64decode(item["b64_json"])
                out = pathlib.Path("/tmp") / f"tx_real_{hashlib.md5(label.encode()).hexdigest()[:6]}.jpg"
                out.write_bytes(raw)
                magic = raw[:3].hex()
                self.rec("real", label, True,
                         f"HTTP 200 ｜ {item.get('mime')} {item.get('size')} {len(raw)}B ｜ "
                         f"magic={magic} ｜ rid={body['upstream']['request_id'][:12]} ｜ {dt:.1f}s ｜ →{out}")
            else:  # response_format=url：取回并核对字节
                url = item["url"]
                st2, blob, _ = call(self.base, url, None, self.key, method="GET")
                sha = hashlib.sha256(blob).hexdigest()[:12] if isinstance(blob, bytes) else "-"
                ok = st2 == 200 and isinstance(blob, bytes) and len(blob) > 1000
                self.rec("real", label, ok,
                         f"HTTP 200 ｜ url={url} → GET {st2} ｜ {len(blob) if isinstance(blob, bytes) else '?'}B ｜ sha256={sha}")

    def summary(self) -> int:
        g = {"pass": [], "soft": [], "fail": []}
        for r in self.rows:
            g[r["result"]].append(r)
        print(f"\n  合计 {len(self.rows)} 项 ｜ 通过 {len(g['pass'])} ｜ "
              f"软限 {len(g['soft'])}（上游配额，非缺陷）｜ 失败 {len(g['fail'])}")
        for r in g["fail"] + g["soft"]:
            mark = "✗" if r["result"] == "fail" else "⚠"
            print(f"    {mark} {r['group']}/{r['case']}: {r['detail']}")
        return 1 if g["fail"] else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://textin.1task.cn")
    ap.add_argument("--key", default=os.environ.get("TEXTIN_KEY"))
    ap.add_argument("--real", action="store_true", help="追加真实调用（默认只跑零成本 dry_run）")
    ap.add_argument("--forms-dir", type=pathlib.Path, help="自备语料目录（不给则用 Pillow 现场造）")
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("/tmp/tx_matrix.json"))
    ap.add_argument("--only", help="只跑真实用例里名字含该子串的（复跑用）")
    a = ap.parse_args()
    if not a.key:
        print("!! 需要 API key：--key 或环境变量 TEXTIN_KEY", file=sys.stderr)
        return 2

    if a.forms_dir:
        forms = {p.name: p for p in sorted(a.forms_dir.iterdir()) if p.suffix != ".pdf"}
        for p in a.forms_dir.iterdir():
            forms[p.name] = p
    else:
        tmp = pathlib.Path("/tmp/tx_forms")
        if not tmp.is_dir():        # 沙箱里 mkdir(exist_ok=True) 会报 EEXIST（shim）
            tmp.mkdir(parents=True)
        forms = build_forms(tmp)

    print(f"== 目标 {a.base} ｜ 形态语料 {len(forms)} 种 ｜ {'含真实调用' if a.real else '仅 dry_run（零上游）'} ==")
    m = Matrix(a.base, a.key)
    print("-- 输入形态（data-uri / 裸 base64 / 外链）× 能力 --"); m.dry_forms(forms)
    print("-- 输入类型（png/jpeg/webp/bmp/tiff/rgba/pdf）× 能力 --"); m.dry_kinds(forms)
    print("-- 负例与留痕 --"); m.dry_negatives(forms)
    if a.real:
        global REAL_CASES
        if a.only:
            REAL_CASES = [c for c in REAL_CASES if a.only in c[0]]
        print(f"-- 真实调用（{len(REAL_CASES)} 项，每项 1 次上游） --"); m.real(forms)
    rc = m.summary()
    a.out.write_text(json.dumps(m.rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  明细已落 {a.out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
