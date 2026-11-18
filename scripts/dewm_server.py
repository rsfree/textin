#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""体验页免费通道采集器（sidecar HTTP 服务）。

给主服务 `textin-service` 当 431（按天额度打满）时的降级通道：
主服务的 `TEXTIN_FALLBACK_URL` 指向本服务，`watermark-remove` 请求在 431 时
转发到这里，由 headless 浏览器走**体验页免费额度**出图。

接口（只此一个，刻意最小）：

    POST /v1/dewm      {"image_b64": "<b64>", "mime": "image/jpeg"}
                    →  200 {"b64_json": "<b64>", "ms": 2712, "acct": "t001", "ip": "..."}
                    →  503 {"code": 441|431|..., "msg": "..."}   本张没成（主服务回退 429）
    GET  /healthz   →  200 {"ok": true, "browser": "up", "pool": true|false}

取舍（都是有理由的，不是风格）：

1. **单线程串行**：Playwright 的 sync API 不能跨线程；HTTP handler 把任务投进队列，
   主线程逐个处理 —— 并发语义由**主服务**（单 worker 默认）决定，这里不做并发。
2. **browser 常驻、context 每请求新建**：browser 启动 ~1s 不可省；而"换出口 IP"
   必须靠**新建 context**（每个 context 一个代理/账号），复用 context 会拿回同一出口。
3. **每请求领取独占账号**（`--pool-host` 模式）：一个出口 IP 同时只有一个请求，
   用完即弃（下次换账号 = 换 IP）。不配池 = 直连（单 IP 每天数次）。
4. **不做重试**：441/431 直接如实返回，由主服务/调用方决定要不要再来一次
   —— 重试的决策权在上层，这里不制造"看起来在工作"的假象。

用法：

    # 直连（单 IP）
    python3 scripts/dewm_server.py --port 8790

    # 账号池 + IP 独占（推荐）
    export TEXTIN_POOL_PASS='***'
    python3 scripts/dewm_server.py --port 8790 \
        --pool-host pool.livetest.cn:2090 --pool-pass "$TEXTIN_POOL_PASS" \
        --acct-prefix t --acct-start 1 --acct-end 600

    # 主服务侧
    TEXTIN_FALLBACK_URL=http://127.0.0.1:8790
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dewm_headless import (  # noqa: E402  (同目录脚本，path 调整后导入)
    AccountAllocator,
    GostSlot,
    Session,
)

__all__ = ["main"]

#: 主线程处理结果与 HTTP 线程之间的单向回填。
_RESULT_TIMEOUT = 300.0


class Collector:
    """持有常驻 browser；请求由 HTTP 线程入队、主线程串行消费。"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.jobs: "queue.Queue[tuple[str, dict, queue.Queue]]" = queue.Queue()
        self.browser = None
        self.playwright = None
        self.allocator = AccountAllocator(args.acct_prefix, args.acct_start, args.acct_end) \
            if args.pool_host else None
        self.slot = GostSlot(args.gost_port, args.pool_host, args.pool_pass) \
            if args.pool_host else None
        self.media_dir = Path(args.media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)

    # ---- 主线程 ----
    def run_forever(self) -> None:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 (延迟导入：主服务环境未必装 playwright)

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        print(json.dumps({"event": "ready", "port": self.args.port,
                          "pool": bool(self.args.pool_host)}), flush=True)
        while True:
            image_b64, meta, reply = self.jobs.get()
            try:
                reply.put(self._process(image_b64, meta))
            except Exception as exc:  # noqa: BLE001
                reply.put({"status": 500, "body": {"code": -1,
                                                   "msg": f"{type(exc).__name__}: {str(exc)[:160]}"}})

    def _process(self, image_b64: str, meta: dict) -> dict:
        proxy = None
        acct = ""
        if self.allocator is not None and self.slot is not None:
            acct = self.allocator.alloc() or ""
            if not acct:
                return {"status": 503, "body": {"code": -2, "msg": "账号池已耗尽"}}
            if not self.slot.bind(acct):
                return {"status": 503, "body": {"code": -3, "msg": f"gost bind 失败: {acct}"}}
            proxy = {"server": f"http://127.0.0.1:{self.slot.port}"}
        t0 = time.time()
        sess = Session(self.browser, proxy)
        try:
            res = sess.ocr(image_b64)
            ip = sess.ip() if self.args.check_ip else ""
        finally:
            sess.close()
        ms = int((time.time() - t0) * 1000)
        code = res.get("code")
        if code != 200 or not res.get("image"):
            return {"status": 503, "body": {"code": code, "msg": res.get("msg") or "no image",
                                            "acct": acct, "ip": ip, "ms": ms}}
        # 留档（排查用；隐私边界：只是调用方自己的图）
        try:
            (self.media_dir / f"{int(time.time()*1000)}.jpg").write_bytes(
                base64.b64decode(res["image"]))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"event": "archive_failed", "err": str(exc)[:100]}), flush=True)
        return {"status": 200, "body": {"b64_json": res["image"], "ms": ms,
                                        "acct": acct, "ip": ip,
                                        "success_count": res.get("success_count", 1)}}

    def submit(self, image_b64: str, meta: dict) -> tuple[int, dict]:
        reply: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        self.jobs.put((image_b64, meta, reply))
        try:
            out = reply.get(timeout=_RESULT_TIMEOUT)
        except queue.Empty:
            return 504, {"code": -4, "msg": "采集超时"}
        return out["status"], out["body"]


def make_handler(collector: Collector):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: ANN002, ANN003
            print(json.dumps({"t": time.strftime("%H:%M:%S"), "msg": fmt % args}), flush=True)

        def _send(self, status: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self._send(200, {"ok": True,
                                 "browser": "up" if collector.browser else "down",
                                 "pool": bool(collector.args.pool_host),
                                 "acct_next": getattr(collector.allocator, "next", None)})
                return
            self._send(404, {"code": 404, "msg": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path.rstrip("/") != "/v1/dewm":
                self._send(404, {"code": 404, "msg": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 80 * 1024 * 1024:
                self._send(400, {"code": 400, "msg": "invalid body size"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                image_b64 = payload["image_b64"]
                if not isinstance(image_b64, str) or not image_b64:
                    raise ValueError("image_b64 empty")
            except Exception as exc:  # noqa: BLE001
                self._send(400, {"code": 400, "msg": f"bad request: {str(exc)[:120]}"})
                return
            status, body = collector.submit(image_b64, {"mime": payload.get("mime")})
            self._send(status, body)

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description="体验页免费通道采集器（431 降级 sidecar）")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--media-dir", default="var/dewm",
                    help="结果图留档目录（默认 var/dewm）")
    ap.add_argument("--check-ip", action="store_true", help="每次请求探测出口 IP")
    ap.add_argument("--pool-host", default=os.environ.get("TEXTIN_POOL_HOST", ""),
                    help="账号池 socks5 主机:端口（启用后每请求独占账号=独占出口）")
    ap.add_argument("--pool-pass", default=os.environ.get("TEXTIN_POOL_PASS", ""))
    ap.add_argument("--acct-prefix", default="t")
    ap.add_argument("--acct-start", type=int, default=1)
    ap.add_argument("--acct-end", type=int, default=600)
    ap.add_argument("--gost-port", type=int, default=11999,
                    help="采集器独占的 gost 本地转发端口")
    args = ap.parse_args()
    if args.pool_host and not args.pool_pass:
        print("--pool-host 需要 --pool-pass（或 TEXTIN_POOL_PASS）", file=sys.stderr)
        return 2

    collector = Collector(args)
    threading.Thread(target=collector.run_forever, name="playwright", daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(collector))
    print(json.dumps({"event": "listen", "url": f"http://{args.host}:{args.port}"}), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if collector.slot is not None:
            collector.slot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
