#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""431 降级通道：体验页免费额度采集器（`scripts/dewm_server.py`）。

主链路（httpx → api.textin.com，token/匿名）收到 `431 今日请求超过限制次数`
（按天额度打满、当天不恢复）时，若配置了 `TEXTIN_FALLBACK_URL` 且能力是
`watermark-remove`，把原始图片转发给本机 sidecar，由 headless 浏览器走体验页
免费额度出图；成功则合成与上游同形的 envelope 交回装配层（**输出契约不变**）。

三条纪律（与本项目其它层一致）：

1. **默认关**：`FALLBACK_URL` 为空 ⇒ 本模块不参与，431 行为与历史版本完全一致。
2. **只认能力面**：采集器只支持去水印 ⇒ 仅 `watermark-remove` 走降级，其余能力 431 照旧 429。
3. **失败不掩盖真因**：sidecar 不可用时抛 `FallbackUnavailable`，由 service 层回退到
   431 原语义（429 + 静默窗），原始错误不被吞。

依据（2026-11-18 实测）：体验页通道的频控是「单出口 IP 按天数次」，与主链路的
431 同源但计量独立；换出口 IP 即恢复，sidecar 负责出口调度（账号租约 + 独占转发）。
"""

from __future__ import annotations

import base64

import httpx

from .config import Settings

__all__ = ["FallbackUnavailable", "collect_dewatermark", "fallback_available"]

#: 采集器唯一支持的能力（其余能力降级无意义）。
_FALLBACK_SERVICES = frozenset({"watermark-remove"})


class FallbackUnavailable(RuntimeError):
    """采集器不可用或返回异常（service 层据此回退到 431 原语义）。"""


def fallback_available(service: str, settings: Settings) -> bool:
    """该能力 + 当前配置是否具备降级条件（纯函数，便于单测）。"""
    return bool(settings.FALLBACK_URL) and service in _FALLBACK_SERVICES


async def collect_dewatermark(image: bytes, mime: str, settings: Settings) -> str:
    """把图片交给采集器去水印，返回结果图的 base64。

    `trust_env=False`：与上游客户端同策略 —— 宿主环境代理不参与，出口只由
    采集器自身决定（它内部用账号租约 + gost 转发做 IP 调度）。
    """
    url = settings.FALLBACK_URL.rstrip("/") + "/v1/dewm"
    payload = {"image_b64": base64.b64encode(image).decode(), "mime": mime}
    try:
        async with httpx.AsyncClient(
            timeout=settings.FALLBACK_TIMEOUT, trust_env=False
        ) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 - 任何失败都归为「采集器不可用」
        raise FallbackUnavailable(
            f"采集器不可用（{url}）：{type(exc).__name__}: {str(exc)[:120]}"
        ) from exc
    b64 = data.get("b64_json")
    if not isinstance(b64, str) or not b64:
        raise FallbackUnavailable(
            f"采集器响应缺 b64_json（code={data.get('code')} msg={data.get('msg')!r}）"
        )
    return b64
