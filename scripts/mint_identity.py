#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""铸造 textin 匿名「浏览器指纹身份」—— 参考 `metaso/tools/mint_identity.py` 的既定模式。

🔴 **实测结论（2026-09-24，负结果）**：textin **没有**可铸造的匿名指纹身份 ——
  站点给匿名访客只种 `acw_tc`（阿里云 WAF）+ 分析类 cookie（百度 `Hm_*`/`HMACCOUNT`、
  诸葛 `zg_did`），**没有** metaso 那套阿里 FP（`_c_WBKFRo`/`_nb_ioWEgULi`/`aliyungf_tc`/`tid`），
  也**不发**匿名 `_textin_token`；把这些 cookie（连完整浏览器头一起）注入后，
  `431` 状态**照旧 431** ⇒ 配额键只看**连接侧 IP**，身份字段被忽略。
  ⇒ 本脚本保留用于**复验**（站点日后若改发指纹身份，这里有现成工具），
  但**不要**据此给服务加 `TEXTIN_COOKIE` 之类的注入旋钮（没有素材的配置=假配置）。

背景（为什么曾做这个）：
  metaso 的差分实验证明「限流键里含**浏览器侧身份**」：同一出口 IP，纯 HTTP 秒 429，
  真浏览器（带完整指纹 cookie）正常出结果。textin 这边 2026-09-24 的归因实验只排除了
  **无 cookie 客户端**之间的差异（Chrome 页面 fetch ↔ 裸 python 行为同型），
  从未测过「带站点指纹身份」这一格 —— 本脚本补上，答案是"这一格不存在"。

做法（与 metaso 版逐条对齐）：
  Playwright 起一个**全新无痕** Chromium，访问 tools.textin.com 的工具页，
  让站点自己的指纹脚本自然运行，收集：
    · 全量 cookie（tools.textin.com + api.textin.com 两边都收）
    · `localStorage._textin_token`（如果站点给匿名访客也发一个）
  全程**不发任何处理请求** —— 铸造是零额度动作。

产物（--out，默认 <repo>/var/identity.json）：
  {"cookie": "k=v; k=v; …", "token": "…", "ua": "…", "minted_at": …, "notes": […]}
  `cookie` 直接填进 `TEXTIN_COOKIE`；`token` 填 `TEXTIN_TOKEN`（若为空串说明站点不给匿名 token）。

用法：
  python scripts/mint_identity.py                # 无头
  python scripts/mint_identity.py --headed       # 有头（无头被指纹脚本识破时用）
  python scripts/mint_identity.py --out /tmp/x.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 站点页（指纹脚本在这里运行）+ 两个 cookie 域
PAGE_URL = "https://tools.textin.com/image_processing/watermark-remove"
COOKIE_URLS = ("https://tools.textin.com", "https://api.textin.com")

#: 参考 metaso 的指纹 cookie 清单：只决定**报告哪些**，不决定怎么造
EXPECTED = ("aliyungf_tc", "_c_WBKFRo", "_nb_ioWEgULi", "tid", "acw_tc", "cna", "JSESSIONID")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

#: 本机 Playwright 缓存里的 Chromium（与 1.62 要求的 revision 不一致，直接指路径省下载）
CHROMIUM_CANDIDATES = (
    "~/Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/"
    "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "~/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/"
    "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
)


def _pick_executable() -> str | None:
    for raw in CHROMIUM_CANDIDATES:
        p = Path(raw).expanduser()
        if p.exists():
            return str(p)
    return None


def mint(*, headed: bool = False, settle_ms: int = 9000) -> dict:
    # playwright 是**可选依赖**（只在铸造时用；服务运行不需要）⇒ 延迟导入
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    exe = _pick_executable()
    notes: list[str] = []
    token: str | None = None
    with sync_playwright() as pw:
        kwargs: dict = {"headless": not headed}
        if exe:
            kwargs["executable_path"] = exe
            notes.append(f"executable_path={Path(exe).parts[-4]}")
        else:
            notes.append("executable_path 未命中缓存，用 playwright 默认浏览器")
        browser = pw.chromium.launch(**kwargs)
        try:
            ctx = browser.new_context(
                user_agent=UA, locale="zh-CN",
                timezone_id="Asia/Shanghai", viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=45_000)
            # 指纹脚本通常在加载后异步收集环境并回种 cookie：停留 + 轻微鼠标移动
            page.mouse.move(200, 200)
            page.wait_for_timeout(settle_ms // 3)
            page.mouse.move(500, 380)
            page.wait_for_timeout(settle_ms // 3)
            page.mouse.move(760, 520)
            page.wait_for_timeout(settle_ms // 3)
            try:
                token = page.evaluate("() => localStorage.getItem('_textin_token')")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"读 localStorage 失败：{type(exc).__name__}")
            cookies = ctx.cookies(list(COOKIE_URLS))
        finally:
            browser.close()

    jar = {c["name"]: c["value"] for c in cookies}
    if not jar:
        notes.append("⚠️ 站点一个 cookie 都没种（可能：指纹脚本没跑 / 无需 cookie）")
    else:
        missing = [k for k in EXPECTED if k not in jar]
        notes.append(f"cookie {len(jar)} 项；清单内命中 "
                     f"{[k for k in EXPECTED if k in jar]}；未出现 {missing}")
    notes.append(f"localStorage._textin_token："
                 f"{('有（%d 字符）' % len(token)) if token else '无（匿名不带 token）'}")

    return {
        "cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
        "cookie_names": sorted(jar),
        "token": token or "",
        "minted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ua": UA,
        "page": PAGE_URL,
        "headed": headed,
        "settle_ms": settle_ms,
        "notes": notes,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--headed", action="store_true", help="有头模式（反检测更强）")
    ap.add_argument("--settle-ms", type=int, default=9000,
                    help="页面停留时长（等指纹 JS），默认 9000")
    ap.add_argument("--out", default=str(ROOT / "var" / "identity.json"))
    args = ap.parse_args()

    t0 = time.time()
    data = mint(headed=args.headed, settle_ms=args.settle_ms)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"铸造完成 {time.time() - t0:.1f}s -> {out}")
    print(f"  cookie 项：{', '.join(data['cookie_names']) or '（无）'}")
    print(f"  token：{'有' if data['token'] else '无'}")
    for n in data["notes"]:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
