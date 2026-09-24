# -*- coding: utf-8 -*-
"""`/llms.txt`（[llmstxt.org](https://llmstxt.org) 约定）的**渲染器**。

单独成模块的两个理由：
1. `main.py` 只留薄路由层（还原"调用方看到的基址" + 选响应类型）；
2. 渲染是**纯函数**（入参 = base / allow_unverified）⇒ 可以不起 HTTP、不打真服务，
   直接拿去做**对账门禁**（见 `tests/test_api.py` 的两条 `test_llms_*reconcile*`）：
   - 条目 ↔ 能力注册表（`CAPABILITIES` / `available()` / `label`）**双向**对账；
   - 「## 端点」小节 ↔ FastAPI **真实路由表**双向对账（豁免项在 `EXCLUDED_ROUTES` 里逐条写理由）。

🔴 想把某个端点从索引里删掉/改名，必须同时改这里的两张表，否则门禁红 —— 这是刻意的。
"""
from __future__ import annotations

from . import __version__
from .models import CAPABILITIES, available, by_family

#: 三族 → 该族的执行端点（既是小节标题，也是"三族齐全"门禁的锚点）
FAMILY_ENDPOINT: dict[str, str] = {
    "image": "/v1/images/generations",
    "convert": "/v1/files/convert",
    "parse": "/v1/files/parse",
}

#: 三族的**中文名**（小节标题用；门禁按"中文名 + 端点"一起锚定，改名即红）
FAMILY_LABEL: dict[str, str] = {
    "image": "图像族",
    "convert": "转换族",
    "parse": "解析族",
}

#: 「## 端点」小节里**逐条列出**的路径（= 对账门禁的**应出现**集合）
DOCUMENTED_PATHS: tuple[str, ...] = (
    "/v1/models",
    "/healthz",
    "/readyz",
    "/v1/images/generations",
    "/v1/files/convert",
    "/v1/files/parse",
    "/capabilities",
    "/llms.txt",
    "/docs",
    "/openapi.json",
)

#: **刻意不写进索引**的路由 → 理由（对账门禁要求每条都有理由，禁止静默豁免）
EXCLUDED_ROUTES: dict[str, str] = {
    "/stats": "运维端点，要鉴权；不进对外索引",
    "/": "人类着陆页（回答「下一步去哪」）；索引本就是给 Agent 的入口，不必再指向自己",
    "/favicon.svg": "浏览器图标，非 API",
    "/favicon.ico": "同上（返回 204 让老客户端静默）",
    "/files": "静态取件挂载，正文以 `/files/*` 一句话说明（不逐条列）",
    "/redoc": "FastAPI 自带的 Redoc，与 /docs 重复",
    "/docs/oauth2-redirect": "Swagger 的 OAuth 回调，框架内部用",
}

_HEAD = "# textin-service"
_SUMMARY = (
    "> TextIn（tools.textin.com / api.textin.com）的**同步出口**：把"
    "「图像处理 / 文档转换 / 识别解析」三族统一成三类动作"
    "（图进图出 / 文件进文件出 / 文件进结构化），模型名走 OpenAI 兼容发现端点。"
)


def render(base: str, *, allow_unverified: bool = False) -> str:
    """渲染 `/llms.txt`。

    `base` 以 `/` 结尾（调用方看到的基址：回环访问得回环链接、经域名访问得域名链接）。
    """
    usable = sorted(available(allow_unverified=allow_unverified))
    lines: list[str] = [
        _HEAD, "", _SUMMARY, "",
        "## 端点", "",
        f"- [模型清单]({base}v1/models)：免鉴权，本部署 {len(usable)} 条（逐字四键，OpenAI 形态）",
        f"- [健康]({base}healthz)｜[就绪]({base}readyz)：后者含配额静默窗与出口形态（已脱敏）",
        f"- `POST {base}v1/images/generations`：图像族，"
        f"`{{\"model\": …, \"image\": \"<data-uri|url|base64>\"}}`",
        f"- `POST {base}v1/files/convert`：转换族，同族字段用 `file`",
        f"- `POST {base}v1/files/parse`：解析族，返回结构化 `result`",
        f"- [能力全集]({base}capabilities)：要鉴权，含未取证项的**原因**",
        f"- 交互式文档 `{base}docs`（OpenAPI：`{base}openapi.json`）",
        "",
        "## 能力（按族）", "",
    ]
    for fam, endpoint in FAMILY_ENDPOINT.items():
        lines += [f"### {FAMILY_LABEL[fam]} → `POST {endpoint}`", ""]
        for name, cap in sorted(by_family(fam).items()):
            if name not in usable:
                continue          # 与 /v1/models 同表：只列**本部署可调用**的
            out = cap.output + (cap.output_ext or "")
            accepts = "/".join(cap.accepts)
            lines.append(f"- `{name}` — {cap.label or cap.service}（输入 {accepts} → 输出 {out}）")
        lines.append("")
    gated = [n for n in sorted(CAPABILITIES) if n not in usable]
    if gated:
        lines += [
            f"- 🔒 另有 {len(gated)} 条能力因**未取证**被门禁挡住（"
            + "、".join(f"`{n}`" for n in gated)
            + "）：原因与开启方式见 `GET /capabilities`。",
            "",
        ]
    lines += [
        "## 鉴权", "",
        "`Authorization: Bearer <key>`。免鉴权：`/v1/models`、`/healthz`、`/readyz`、"
        "`/llms.txt`、`/docs`、`/openapi.json`、`/files/*`。",
        "",
        "## 形态与限制", "",
        "- **同步**接口（没有任务 id）：一次请求一次返回；输入上限 50MB（data-uri / 外链 / 附件）。",
        "- 上游是**匿名试用额度**，按「出口 IP × service × 天」计：去水印约 25~30 次成功/天/IP。",
        "  `431`＝当日额度用尽（**当天不恢复**）；`451`＝该 service 的软限（约每 4 发 1 次）"
        "—— **服务已自动换出口重试一次**（`warnings[]` 留痕）。",
        "- 典型延迟 1~3s（文档转换族更长）；`dry_run: true` 可**零成本**预演将发往上游的请求。",
        "- `response_format: \"url\"` 的产物落在本服务 `/files/<产物哈希>.<ext>`，"
        "**链接有效期约 7 天**（由部署方清理策略决定）。",
        f"- 服务版本 {__version__}；未取证能力默认 `503 capability_not_verified`。",
        "",
        "## 源码与契约", "",
        "- [仓库](https://github.com/rsfree/textin)",
        "- [接口契约 INTERFACE.md](https://github.com/rsfree/textin/blob/main/docs/INTERFACE.md)",
        "",
    ]
    return "\n".join(lines)
