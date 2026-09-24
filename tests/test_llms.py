# -*- coding: utf-8 -*-
"""`/llms.txt` 的**对账门禁**（渲染器抽成 `app/llms.py` 之后就能这样测了）。

两组对账，都是**双向**的 —— 只测一边会让"漏项"或"幽灵项"溜过去：

1. **条目 ↔ 能力注册表**：每个可调用能力都在索引里、每条索引都对应一个真能力，
   且**说明文字逐字来自注册表 `label`**（防止有人往索引里手写一段会漂移的文案）。
2. **「## 端点」↔ FastAPI 真实路由表**：索引里点名的路径必须真的存在；
   路由表里的每条业务路由要么被索引提到、要么在 `EXCLUDED_ROUTES` 里**带理由**豁免
   （豁免项本身也得是真路由 —— 免得留下指向已删路由的僵尸豁免）。

纯净性：这两组都只调**纯函数** `app.llms.render`（不起 HTTP、不打真服务）；
HTTP 层（响应类型/免鉴权/反代协议还原）的门禁在 `tests/test_api.py`。
"""
from __future__ import annotations

import re

from app import __version__
from app.llms import DOCUMENTED_PATHS, EXCLUDED_ROUTES, FAMILY_ENDPOINT, FAMILY_LABEL, render
from app.main import create_app
from app.models import CAPABILITIES, available
from tests.helpers import settings

BASE = "https://unit.test/"


def _render(tmp_path, **overrides) -> str:
    return render(BASE, allow_unverified=bool(overrides.get("ALLOW_UNVERIFIED", False)))


def _bullets(body: str) -> list[str]:
    """索引里代表"一条能力"的行（`- `textin:xxx` — 说明（输入 … → 输出 …）`）。"""
    return [ln for ln in body.splitlines() if ln.startswith("- `textin:")]


def _paths_in_text(body: str) -> set[str]:
    """索引里出现的所有**本站路径**（反引号代码段 + markdown 链接）。

    🔴 三个坑（都是变异自证抓出来的，别退回去）：
    - 链接与代码段里常写**绝对 URL**，而且不一定在串首（`POST {base}v1/…`）
      ⇒ 必须**全局**把 `https?://<host>` 剥成路径，否则残留成 `//host/v1/…` 的假路径；
    - 外站链接（GitHub 的 `/rsfree/textin`）要**按 host 抹掉**，否则把外站路径误判成幽灵；
    - 只收"像路径"的 token（`/` 开头 + 白名单字符），别把 `431`、`dry_run` 这类代码段卷进来。
    """
    base_host = BASE.split("//", 1)[1].rstrip("/")

    def _strip_url(m: re.Match[str]) -> str:
        host, path = m.group(1), m.group(2) or "/"
        return path if host == base_host else " "     # 外站：整段抹掉

    found: set[str] = set()
    for raw in re.findall(r"`([^`]+)`", body) + re.findall(r"\]\(([^)]+)\)", body):
        s = re.sub(r"https?://([^/\s`)]+)(/[^\s`)]*)?", _strip_url, raw)
        for tok in re.findall(r"/[A-Za-z0-9._~/{}-]+", s):
            found.add(tok.rstrip("/") or "/")
    return found


def _route_paths(tmp_path) -> set[str]:
    app = create_app(settings(MEDIA_DIR=str(tmp_path / "media"), API_KEYS="k"))
    return {r.path.rstrip("/") or "/" for r in app.routes if hasattr(r, "path")}


# ---------------------------------------------------------------- 对账 ①：注册表
def test_llms_bullets_reconcile_with_registry(tmp_path):
    """双向：可调用能力 == 索引条目；且说明文字逐字等于注册表 `label`。"""
    body = _render(tmp_path)
    bullets = _bullets(body)
    want = sorted(available(allow_unverified=False))
    got = sorted(re.search(r"- `(textin:[^`]+)`", b).group(1) for b in bullets)
    assert got == want, f"索引条目与注册表不一致：漏 {set(want) - set(got)} / 多 {set(got) - set(want)}"
    assert len(bullets) == len(want)                      # 去重（同 id 不得出现两次）

    for b in bullets:
        mid = re.search(r"- `(textin:[^`]+)`", b).group(1)
        label = CAPABILITIES[mid].label
        assert f"`{mid}` — {label}（输入 " in b, f"{mid} 的说明不是注册表 label：{b}"


def test_llms_version_and_gated_line(tmp_path):
    """版本行来自 `__version__`；门禁项不计条目、但要被一行说明点名（开关翻转则消失）。"""
    body = _render(tmp_path)
    assert f"服务版本 {__version__}；" in body
    gated = sorted(set(CAPABILITIES) - set(available(allow_unverified=False)))
    assert all(f"`{g}`" not in "\n".join(_bullets(body)) for g in gated)
    if gated:
        assert "🔒" in body and all(f"`{g}`" in body for g in gated)

    opened = render(BASE, allow_unverified=True)
    assert "🔒" not in opened
    assert sorted(re.search(r"- `(textin:[^`]+)`", b).group(1) for b in _bullets(opened)) \
        == sorted(CAPABILITIES)


def test_llms_family_sections_cover_three_families(tmp_path):
    """三族小节必须"中文名 + 端点"成对出现（改任一半即红）。"""
    body = _render(tmp_path)
    for fam, endpoint in FAMILY_ENDPOINT.items():
        assert f"### {FAMILY_LABEL[fam]} → `POST {endpoint}`" in body, fam


def test_llms_links_use_caller_base(tmp_path):
    """链接基址 = 调用方看到的基址（回环/域名各得其所），不留硬编码域名。"""
    body = _render(tmp_path)
    for p in DOCUMENTED_PATHS:
        if p in ("/docs", "/openapi.json", "/llms.txt") or "{" in p:
            continue
        assert f"{BASE}{p.lstrip('/')}" in body, p
    assert "textin.1task.cn" not in body, "索引里不该硬编码部署域名"


# ---------------------------------------------------------------- 对账 ②：真实路由表
def test_llms_endpoints_reconcile_with_routes(tmp_path):
    """索引点名的路径 ⊆ 真实路由；真实业务路由 ⊆ 索引 ∪ 带理由的豁免（双向对账）。"""
    body = _render(tmp_path)
    routes = _route_paths(tmp_path)
    mentioned = _paths_in_text(body)

    ghost = {p for p in mentioned if p not in routes and p != "/files/*" and p.count("/") > 0
             and not p.startswith("/files/")}
    # `/files/*` 是"静态取件"的说明写法，真实路由是挂载 `/files`；其余必须真存在
    ghost = {p for p in ghost if not p.startswith("/files")}
    assert not ghost, f"索引点名了不存在的路径：{sorted(ghost)}"

    for p in DOCUMENTED_PATHS:
        assert p in routes, f"DOCUMENTED_PATHS 里的 {p} 不在真实路由表里"
        assert p in mentioned, f"DOCUMENTED_PATHS 里的 {p} 没出现在索引正文里"

    exempt = {p.rstrip("/") or "/" for p in EXCLUDED_ROUTES}
    undocumented = routes - mentioned - exempt
    assert not undocumented, f"有路由既没进索引、也没登记豁免：{sorted(undocumented)}"

    stale = exempt - routes
    assert not stale, f"豁免表里有僵尸项（路由已不存在）：{sorted(stale)}"
    assert all(reason.strip() for reason in EXCLUDED_ROUTES.values()), "豁免必须逐条写理由"
