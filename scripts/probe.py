#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零成本自检：把每条能力的**出站请求形状**与**响应装配回路**打出来核对。

三个阶段，默认只跑前两个（**一个字节都不发给真实上游**）：

  `shapes` 对全部能力构造出站请求（dry_run）→ 打印 service / query / 通道 / Content-Type / 字节数
  `loop`   起本地假上游，把每条能力从 HTTP 端点打到"上游"再回装配（全链路回路）
  `live`   ⚠️ **真实调用上游**（消耗该 service 的匿名试用额度）—— 必须显式 `--live`

用法：
    python scripts/probe.py
    python scripts/probe.py --phases shapes,loop
    python scripts/probe.py --live --cap textin:watermark-remove
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402

from app.config import Settings  # noqa: E402
from app.models import CAPABILITIES, availability, lookup  # noqa: E402
from app.service import _assemble  # noqa: E402
from app.upstream.textin import TextinClient  # noqa: E402

# ---------------------------------------------------------------------------
# 代表输入（按 `accepts` 的第一项选样本）
# ---------------------------------------------------------------------------

SAMPLE_DIR = ROOT / "scripts" / "samples"
_MIME = {
    "png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp",
    "bmp": "image/bmp", "tiff": "image/tiff",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv", "ofd": "application/ofd",
}


def _tiny_png(w: int = 24, h: int = 16) -> bytes:
    import struct  # noqa: PLC0415
    import zlib  # noqa: PLC0415

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (struct.pack(">I", len(data)) + payload
                + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes((10, 120, 200)) * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _zip_with(entry: str) -> bytes:
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(entry, "<x/>")
    return buf.getvalue()


def sample_for(cap) -> tuple[bytes, str]:
    """该能力的一条代表输入（优先用研究期留在 scripts/samples/ 的真样本）。"""
    kind = cap.accepts[0]
    if kind in ("png", "jpeg", "webp", "bmp", "tiff"):
        real = SAMPLE_DIR / "wm_sample.png"
        return (real.read_bytes() if real.exists() else _tiny_png()), _MIME[kind]
    if kind == "pdf":
        real = SAMPLE_DIR / "test.pdf"
        return (real.read_bytes() if real.exists() else b"%PDF-1.4\n%%EOF\n"), "application/pdf"
    if kind in ("docx", "doc"):
        return _zip_with("word/document.xml"), _MIME["docx"]
    if kind in ("xlsx", "xls"):
        return _zip_with("xl/workbook.xml"), _MIME["xlsx"]
    if kind == "csv":
        return b"a,b\n1,2\n", "text/csv"
    if kind == "ofd":
        return _zip_with("OFD.xml"), _MIME["ofd"]
    raise AssertionError(f"未知输入家族 {kind}")


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------


def phase_shapes() -> int:
    print("=" * 104)
    print("【shapes】对每条能力构造出站请求（dry_run，**不触网**）")
    print(f"{'':2}{'模型':<32} {'service':<19} {'params':<36} {'通道':<5} {'Content-Type':<62} 字节")
    conf = Settings(_env_file=None, TOKEN="")
    client = TextinClient(conf, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"code": 200, "data": {}})))
    bad = 0
    for name in sorted(CAPABILITIES):
        cap = CAPABILITIES[name]
        data, mime = sample_for(cap)
        preview = asyncio.run(client.call(cap, data, mime, dry_run=True))
        ok, reason = availability(cap, allow_unverified=False)
        flag = "  " if ok else "🔒"
        print(f"{flag}{name:<32} {cap.service:<19} {str(dict(cap.service_params)):<36} "
              f"{preview['channel']:<5} {preview['content_type']:<62} {preview['content_bytes']}")
        if not ok:
            print(f"     └─ 门禁中：{reason[:100]}")
        if preview["content_bytes"] <= 0:
            bad += 1
    asyncio.run(client.aclose())
    print(f"\nshapes 完成：{len(CAPABILITIES)} 条能力，异常 {bad} 条")
    return bad


def phase_loop() -> int:
    print("=" * 104)
    print("【loop】假上游全链路回路（真 HTTP，**零真实上游请求**）")
    import mock_upstream  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from http.server import ThreadingHTTPServer  # noqa: PLC0415

    from app.main import create_app  # noqa: PLC0415

    server = ThreadingHTTPServer(("127.0.0.1", 0), mock_upstream.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    import tempfile  # noqa: PLC0415

    media = Path(tempfile.mkdtemp(dir="/tmp", prefix="textin_probe_"))
    conf = Settings(_env_file=None, BASE_URL=f"http://127.0.0.1:{port}", API_KEYS="",
                    MEDIA_DIR=str(media), TOKEN="")
    if not conf.BASE_URL.startswith("http://127.0.0.1"):
        raise SystemExit("回路必须指向本地假上游，拒绝执行")
    app = create_app(conf)
    endpoint = {"image": "/v1/images/generations", "convert": "/v1/files/convert",
                "parse": "/v1/files/parse"}
    failed = 0
    with TestClient(app) as c:
        for name in sorted(CAPABILITIES):
            cap = CAPABILITIES[name]
            if not availability(cap, allow_unverified=False)[0]:
                print(f"🔒 {name:<34} {cap.family:<8} 门禁（未取证，跳过）")
                continue
            data, mime = sample_for(cap)
            payload: dict = {"model": name}
            key = "image" if cap.family == "image" else "file"
            payload[key] = f"data:{mime};base64," + base64.b64encode(data).decode()
            if key == "file":
                payload["filename"] = f"input.{cap.accepts[0]}"
            r = c.post(endpoint[cap.family], json=payload)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            ok = r.status_code == 200 and bool(body.get("data"))
            detail = ("keys=" + ",".join(sorted(body["data"][0]))) if ok else json.dumps(
                body, ensure_ascii=False)[:110]
            failed += 0 if ok else 1
            print(f"{'✅' if ok else '❌'} {name:<34} {cap.family:<8} {r.status_code}  {detail}")
    mock_hits = _count_mock(port)
    server.shutdown()
    print(f"\nloop 完成：失败 {failed} 条；假上游共收到 {mock_hits} 次请求")
    return failed


def _count_mock(port: int) -> int:
    import urllib.request  # noqa: PLC0415

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/mock/log", timeout=3) as resp:
            return len(json.load(resp)["entries"])
    except Exception:  # noqa: BLE001 - 仅用于打印统计
        return -1


def phase_live(cap_name: str, input_path: str | None) -> int:
    print("=" * 104)
    print(f"【live】⚠️ 真实调用上游：{cap_name}（消耗该 service 的匿名试用额度）")
    cap = lookup(cap_name)
    if input_path:
        data = Path(input_path).read_bytes()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".pdf": "application/pdf"}.get(Path(input_path).suffix.lower(),
                                               "application/octet-stream")
    else:
        data, mime = sample_for(cap)
    conf = Settings(_env_file=None)
    client = TextinClient(conf)

    async def _run() -> dict:
        t0 = time.time()
        try:
            envelope = await client.call(cap, data, mime)
        finally:
            await client.aclose()
        dt = time.time() - t0
        items, usage = _assemble(cap=cap, family=cap.family, envelope=envelope,
                                 response_format="b64_json", settings=conf,
                                 filename_hint=None, warnings=[])
        return {"dt": dt, "envelope": envelope, "items": items, "usage": usage}

    out = asyncio.run(_run())
    outdir = ROOT / "var" / "live"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    saved: list[str] = []
    for i, item in enumerate(out["items"]):
        raw: bytes | None = None
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            raw = (Path(conf.MEDIA_DIR) / Path(item["url"]).name).read_bytes()
        if raw is not None:
            ext = (item.get("filename") or "").rsplit(".", 1)[-1] if item.get("filename") \
                else ("jpg" if item.get("mime") == "image/jpeg" else "bin")
            path = outdir / f"{stamp}-{cap_name.replace(':', '_')}-{i}.{ext}"
            path.write_bytes(raw)
            saved.append(str(path.relative_to(ROOT)))
    report = {"cap": cap_name, "service": cap.service, "seconds": round(out["dt"], 2),
              "usage": out["usage"], "saved": saved,
              "upstream_request_id": out["envelope"].get("x_request_id")}
    (outdir / f"{stamp}-{cap_name.replace(':', '_')}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print("\n⚠️ 真实调用已完成：该 service 的匿名配额已被消耗一次（见 docs/UPSTREAM.md §4）")
    return 0


def phase_pool(live: bool) -> int:
    """验池：`TEXTIN_PROXY_POOL` 里每个入口的**真实出口 IP**（每次新连接）。

    这是 wenxin 那轮换来的教训：池的"标称"不等于实际 —— 必须实测出口数与轮换粒度
    （那次号称的池其实只有 3 个出口，且复用连接会钉住同一个出口）。
    加 `--live` 时再经**服务自己的客户端**（`TextinClient`）真实调一次上游，
    证明池路径端到端可用（消耗 1 次试用额度）。
    """
    print("=" * 104)
    print("【pool】代理池实测（出口 IP / 轮换粒度）")
    conf = Settings(_env_file=None)
    pool = [p.strip() for p in conf.PROXY_POOL.split(",") if p.strip()]
    if not pool:
        print("TEXTIN_PROXY_POOL 未配置 —— 跳过（这是默认状态：直连）")
        return 0
    print(f"池入口 {len(pool)} 个：{[_mask(p) for p in pool]}")

    ips: list[str] = []
    rounds = max(3, len(pool) * 2)
    for i in range(rounds):
        proxy = pool[i % len(pool)]
        try:
            with httpx.Client(proxy=proxy, timeout=20, trust_env=False) as c:
                ip = c.get("https://ifconfig.me/ip").text.strip()[:40]
        except Exception as exc:  # noqa: BLE001
            ip = f"<FAIL {type(exc).__name__}>"
        ips.append(ip)
        print(f"  轮 {i + 1}: {_mask(proxy)} → {ip}")
    good = [ip for ip in ips if not ip.startswith("<")]
    print(f"出口 IP：{len(set(good))} 个不同的 / {len(good)} 次成功 / 共 {rounds} 轮")

    if live:
        print("\n--live：经服务客户端真实调一次 watermark-remove --")
        from app.models import lookup  # noqa: PLC0415

        client = TextinClient(conf)
        data, mime = sample_for(lookup("textin:watermark-remove"))

        async def _one():
            t0 = time.time()
            try:
                env = await client.call(lookup("textin:watermark-remove"), data, mime)
                return {"seconds": round(time.time() - t0, 2), "code": env.get("code"),
                        "request_id": env.get("x_request_id")}
            finally:
                await client.aclose()

        print(json.dumps(asyncio.run(_one()), ensure_ascii=False))
        print("⚠️ 已消耗该 service 的试用额度 1 次")
    return 0


def _mask(proxy: str) -> str:
    try:
        u = httpx.URL(proxy)
        return f"{u.scheme}://{u.host}:{u.port}"
    except Exception:  # noqa: BLE001
        return "<unparsable>"


def main() -> int:
    ap = argparse.ArgumentParser(description="textin-service 自检")
    ap.add_argument("--phases", default="shapes,loop",
                    help="逗号分隔：shapes / loop / pool（默认前两个，零真实请求）")
    ap.add_argument("--live", action="store_true",
                    help="pool 阶段真实调一次；--cap 单独用时也走真实调用（消耗试用额度）")
    ap.add_argument("--cap", default="textin:watermark-remove", help="--live 时调哪条能力")
    ap.add_argument("--input", default=None, help="--live 时的输入文件（默认用内置样本）")
    args = ap.parse_args()

    bad = 0
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    if "shapes" in phases:
        bad += phase_shapes()
    if "loop" in phases:
        bad += phase_loop()
    if "pool" in phases:
        bad += phase_pool(args.live)
    if args.live and "pool" not in phases:
        bad += phase_live(args.cap, args.input)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
