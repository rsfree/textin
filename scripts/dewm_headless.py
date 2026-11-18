#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TextIn 去水印 headless 适配器 v2（轮换优先）

2026-11-18 实测结论（决定本适配器的设计）：
- 原生 HTTP 客户端（curl / curl_cffi / urllib）被 WAF 指纹层直接拒绝（441）→ 必须浏览器环境
- headless Chromium（Playwright）可通过；页面内 fetch 与线上前端完全同构
- 单出口 IP 额度约 3 次/天（机房 headless），超限返回 441 reach_limit / 431 今日请求超过限制
- 2088 轮换代理 + 每请求新建 context ⇒ 每次新出口 IP，实测新 IP 命中即 200
- 441 / 431 的正确反应都是「换 IP」，不是等待

策略（默认）：
- 每个任务都新建 browser context（= 从代理池取一个新出口 IP）
- 441/431 → 立即换 context 重试（最多 --max-attempts 次）
- 每次使用的出口 IP 记录到 results.jsonl

用法：
    # 轮换代理（推荐：2086 http = 每连接换出口）
    python headless_adapter.py --input-dir ./imgs --out ./out \
        --proxy 'http://poolclient:PASS@pool.livetest.cn:2086' --workers 3 --skip-existing

    # 多代理池轮询（不同端口/出口混合）
    python headless_adapter.py ... --proxy 'http://poolclient:PASS@h:2086' \
                                       --proxy 'http://cnclient:PASS@pool.s3ai.cn:11088'

    # 直连（无代理，单 IP 额度 ~3 次/天）
    python headless_adapter.py --input a.jpg --out ./out

环境变量：
    TEXTIN_PROXY  代理 URL（--proxy 的默认值，逗号分隔多个池）
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

API_HOST = "https://api.textin.com"
OCR_PATH = "/home/user_trial_ocr?service=watermark-remove&is_url=0"
REMAIN_PATH = "/home/user_trial_remain?service=watermark-remove"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
RATE_LIMIT = 441     # reach_limit（窗口/出口额度）
DAILY_LIMIT = 431    # 今日请求超过限制次数

JS_IP = ("async()=>{try{const r=await fetch('https://api.ipify.org');"
         "return (await r.text()).trim()}catch(e){return ''}}")
JS_OCR = """
async (b64) => {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const t0 = performance.now();
  const r = await fetch('%s', {
    method: 'POST',
    headers: {'Content-Type': 'application/octet-stream'},
    body: arr,
  });
  let j = {};
  try { j = await r.json(); } catch (e) { j = {code: -1, msg: 'bad json'}; }
  return {
    code: j.code,
    msg: (j.msg || '').slice(0, 40),
    ms: Math.round(performance.now() - t0),
    image: (j.data && j.data.result && j.data.result.image) || '',
    success_count: (j.data && j.data.result && j.data.result.success_count) || 0,
  };
}
""" % OCR_PATH


def log(msg: str) -> None:
    print(msg, flush=True)


def _append(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


@dataclass
class Job:
    src: Path


class Counter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ok = 0
        self.failed = 0

    def hit_ok(self) -> int:
        with self.lock:
            self.ok += 1
            return self.ok

    def hit_fail(self) -> None:
        with self.lock:
            self.failed += 1


def parse_proxy(url: str) -> dict | None:
    if not url:
        return None
    u = urlparse(url)
    if not u.hostname or not u.port:
        raise SystemExit(f"代理 URL 解析失败: {url}")
    scheme = "socks5" if u.scheme.startswith("socks") else "http"
    p: dict = {"server": f"{scheme}://{u.hostname}:{u.port}"}
    if u.username:
        p["username"] = unquote(u.username)
    if u.password:
        p["password"] = unquote(u.password)
    return p


class Session:
    """一个 browser context = 一个出口 IP。"""

    def __init__(self, browser, proxy: dict | None, timeout_ms: int = 45000):
        self.ctx = browser.new_context(
            user_agent=UA, viewport={"width": 1600, "height": 1000}, proxy=proxy
        )
        self.page = self.ctx.new_page()
        self.page.goto(f"{API_HOST}{REMAIN_PATH}",
                       wait_until="domcontentloaded", timeout=timeout_ms)

    def ip(self) -> str:
        try:
            return self.page.evaluate(JS_IP) or ""
        except Exception:  # noqa: BLE001
            return ""

    def ocr(self, b64: str) -> dict:
        return self.page.evaluate(JS_OCR, b64)

    def close(self) -> None:
        try:
            self.ctx.close()
        except Exception:  # noqa: BLE001, S110 - 关闭失败不阻塞主流程
            pass


class AccountAllocator:
    """代理池账号租约：原子递增领取，保证不同 worker 永不同 IP。

    2090 模式：poolclient-{acct} → 池内固定出口 IP。
    每个账号领取后由唯一 worker 独占（"一个 IP 只有一个并发"）。
    """

    def __init__(self, prefix: str, start: int, end: int):
        self.prefix = prefix
        self.next = start
        self.end = end
        self.lock = threading.Lock()
        self.exhausted = False

    def alloc(self) -> str | None:
        with self.lock:
            if self.next > self.end:
                self.exhausted = True
                return None
            n = self.next
            self.next += 1
            return f"{self.prefix}{n:03d}"


class GostSlot:
    """每个 worker 独占一个本地端口；换 IP = 换账号 + 重启转发（~200ms）。"""

    def __init__(self, port: int, pool_host: str, password: str):
        self.port = port
        self.pool_host = pool_host
        self.password = password
        self.proc: "subprocess.Popen | None" = None
        self.acct = ""

    def _kill(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001, S110 - 兜底关闭
                    pass
            self.proc = None
        # 清理可能残留的同端口进程
        subprocess.run(["pkill", "-f", f"127.0.0.1:{self.port}"],
                       capture_output=True, check=False)

    def bind(self, acct: str) -> bool:
        """把本 slot 的上游切到指定账号（= 指定出口 IP）。"""
        self._kill()
        up = f"socks5://poolclient-{acct}:{self.password}@{self.pool_host}"
        try:
            self.proc = subprocess.Popen(
                ["gost", "-L", f"http://127.0.0.1:{self.port}", "-F", up],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log("gost 未安装：请先 apt/下载 gost v3")
            return False
        self.acct = acct
        # 等端口就绪
        for _ in range(30):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                    return True
            except OSError:
                time.sleep(0.1)
        return False

    def close(self) -> None:
        self._kill()


def worker(
    wid: int,
    jobs: "queue.Queue[Job]",
    args: argparse.Namespace,
    proxies: list[str],
    counter: Counter,
    out_dir: Path,
    results_path: Path,
    failed_path: Path,
    allocator: "AccountAllocator | None" = None,
    slot: "GostSlot | None" = None,
) -> None:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 - 延迟导入：无需 playwright 也能 import 本模块

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    sess: Session | None = None
    rotation = 0

    def next_proxy() -> dict | None:
        nonlocal rotation
        if not proxies:
            return None
        url = proxies[rotation % len(proxies)]
        rotation += 1
        return parse_proxy(url)

    def fresh_session() -> Session:
        nonlocal sess
        if sess is not None:
            sess.close()
        if allocator is not None and slot is not None:
            acct = allocator.alloc()
            if acct is None:
                raise RuntimeError("账号池已耗尽")
            if not slot.bind(acct):
                raise RuntimeError(f"gost bind 失败: {acct} port={slot.port}")
            # 每个账号（出口 IP）只由一个 worker 独占使用
            sess = Session(browser, {"server": f"http://127.0.0.1:{slot.port}"})
            sess.acct = acct
            return sess
        sess = Session(browser, next_proxy())
        return sess

    try:
        while True:
            try:
                job = jobs.get_nowait()
            except queue.Empty:
                break
            out_path = out_dir / (job.src.stem + ".jpg")
            attempts = 0
            solved = False
            while attempts < args.max_attempts:
                attempts += 1
                try:
                    s = fresh_session()
                except Exception as exc:  # noqa: BLE001
                    log(f"[w{wid}] session error: {str(exc)[:100]}")
                    time.sleep(4)
                    continue
                ip = s.ip() if args.check_ip else ""
                # 同一 IP 内允许两发：新 IP 首投常被挑战性拒绝（441），
                # 同会话/同出口的第二发才会放行（2026-11-18 实测）
                res: dict = {}
                for shot in (1, 2):
                    try:
                        b64 = base64.b64encode(job.src.read_bytes()).decode()
                        res = s.ocr(b64)
                    except Exception as exc:  # noqa: BLE001
                        log(f"[w{wid}] eval error: {str(exc)[:100]}")
                        res = {"code": -1, "msg": str(exc)[:40], "ms": 0}
                        break
                    code = res.get("code")
                    acct = getattr(s, "acct", "") or ""
                    rec = {
                        "worker": wid, "file": str(job.src), "attempt": attempts,
                        "shot": shot, "at": time.strftime("%H:%M:%S"), "ip": ip,
                        "acct": acct,
                        "code": code, "msg": res.get("msg"), "ms": res.get("ms"),
                    }
                    if code == 200 and res.get("image"):
                        out_path.write_bytes(base64.b64decode(res["image"]))
                        rec["out"] = str(out_path)
                        rec["success_count"] = res.get("success_count")
                        _append(results_path, rec)
                        n = counter.hit_ok()
                        log(f"[w{wid}] OK {job.src.name} ip={ip or '-'} "
                            f"{res.get('ms')}ms try#{attempts}/shot{shot} (ok_total={n})")
                        solved = True
                        break
                    _append(results_path, rec)
                    if code in (RATE_LIMIT, DAILY_LIMIT) and shot == 1:
                        log(f"[w{wid}] {code} {job.src.name} 首投挑战 -> 同 IP 第二发")
                        time.sleep(3)
                        continue
                    if code in (RATE_LIMIT, DAILY_LIMIT):
                        log(f"[w{wid}] {code} {job.src.name} 第二发仍拒 -> 换 IP")
                    break
                if solved:
                    if args.rotate_every and counter.ok % args.rotate_every == 0:
                        sess.close()
                        sess = None
                    break
                sess.close()
                sess = None
            if not solved:
                counter.hit_fail()
                _append(failed_path, {"file": str(job.src), "reason": "max_attempts"})
                log(f"[w{wid}] FAIL {job.src.name} after {attempts} attempts")
            jobs.task_done()
    finally:
        if sess is not None:
            sess.close()
        if slot is not None:
            slot.close()
        try:
            browser.close()
        except Exception:  # noqa: BLE001, S110 - 关闭失败不阻塞主流程
            pass


def collect_inputs(args: argparse.Namespace) -> list[Path]:
    files: list[Path] = []
    if args.input:
        files += [Path(x) for x in args.input]
    if args.input_dir:
        d = Path(args.input_dir)
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.webp", "*.tif", "*.tiff"):
            files += sorted(d.glob(ext))
    out: list[Path] = []
    out_dir = Path(args.out)
    for f in files:
        if not f.is_file():
            log(f"skip (not file): {f}")
            continue
        if args.skip_existing and (out_dir / (f.stem + ".jpg")).exists():
            log(f"skip (done): {f.name}")
            continue
        out.append(f)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="TextIn 去水印 headless 适配器 v2（轮换优先）")
    ap.add_argument("--input", nargs="*", help="输入图片路径")
    ap.add_argument("--input-dir", help="输入目录")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--workers", type=int, default=2, help="并发 worker 数")
    ap.add_argument("--max-attempts", type=int, default=6, help="单图最大尝试（换 IP）次数")
    ap.add_argument("--rotate-every", type=int, default=1,
                    help="每成功 N 次换一次出口 IP（默认 1=每张都换）")
    ap.add_argument("--proxy", action="append", default=None,
                    help="代理 URL，可重复传多个池（轮询）；默认读 TEXTIN_PROXY")
    ap.add_argument("--check-ip", action="store_true", help="每次会话探测出口 IP 并记录")
    ap.add_argument("--pool-host", default=os.environ.get("TEXTIN_POOL_HOST", ""),
                    help="账号池 socks5 主机:端口（如 pool.livetest.cn:2090）；"
                         "启用后每张图领取独占账号=独占出口 IP，经 gost 本地转发")
    ap.add_argument("--pool-pass", default=os.environ.get("TEXTIN_POOL_PASS", ""),
                    help="账号池密码")
    ap.add_argument("--acct-prefix", default="t", help="账号前缀（poolclient-{prefix}{NNN}）")
    ap.add_argument("--acct-start", type=int, default=1, help="账号起始编号")
    ap.add_argument("--acct-end", type=int, default=600, help="账号结束编号")
    ap.add_argument("--gost-base-port", type=int, default=11000,
                    help="每 worker 独占的 gost 本地端口基址")
    ap.add_argument("--skip-existing", action="store_true", help="跳过已有输出（断点续跑）")
    ap.add_argument("--dry-run", action="store_true", help="只列出待处理文件")
    args = ap.parse_args()

    proxies: list[str] = args.proxy or []
    if not proxies:
        env = os.environ.get("TEXTIN_PROXY", "").strip()
        if env:
            proxies = [x.strip() for x in env.split(",") if x.strip()]

    files = collect_inputs(args)
    if not files:
        log("没有待处理文件")
        return 1
    log(f"待处理 {len(files)} 个文件 | workers={args.workers} | "
        f"proxy_pools={len(proxies)} | rotate_every={args.rotate_every} | "
        f"max_attempts={args.max_attempts}")
    if args.dry_run:
        for f in files:
            log(f"  {f}")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    failed_path = out_dir / "failed.jsonl"

    jobs: "queue.Queue[Job]" = queue.Queue()
    for f in files:
        jobs.put(Job(src=f))

    counter = Counter()
    threads = []
    n = max(1, min(args.workers, len(files)))
    allocator = None
    if args.pool_host:
        if not args.pool_pass:
            log("--pool-host 需要配合 --pool-pass（或 TEXTIN_POOL_PASS）")
            return 1
        allocator = AccountAllocator(args.acct_prefix, args.acct_start, args.acct_end)
        log(f"账号池模式：{args.pool_host} 账号 {args.acct_prefix}"
            f"{args.acct_start:03d}..{args.acct_prefix}{args.acct_end:03d}"
            f" | 每 worker 独占端口 from {args.gost_base_port + 1}")
    for wid in range(1, n + 1):
        slot = None
        if allocator is not None:
            slot = GostSlot(args.gost_base_port + wid, args.pool_host, args.pool_pass)
        t = threading.Thread(
            target=worker,
            args=(wid, jobs, args, proxies, counter, out_dir, results_path, failed_path,
                  allocator, slot),
            daemon=True,
        )
        t.start()
        threads.append(t)
        time.sleep(1.5)
    for t in threads:
        t.join()

    log(f"完成：ok={counter.ok} failed={counter.failed} results={results_path}")
    return 0 if counter.failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
