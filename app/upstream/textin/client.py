#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TextIn 上游 HTTP 客户端。

设计要点（每条都有理由，不是风格）：

1. **`trust_env=False`**：环境里的 `HTTP_PROXY`/`HTTPS_PROXY` 会把对外请求也代理走，
   对带鉴权的 POST 常表现为静默挂起或 broken pipe，且两侧都没有日志。
   走上游的代理**只由 `TEXTIN_PROXY_POOL` 显式决定**，不由宿主环境偶然决定。
2. **判成败只看信封 `code`（整数）**，不按 HTTP 状态猜语义 —— 上游几乎恒回 HTTP 200。
3. **请求体是文件裸字节**（httpx 用 `content=`；注意 `requests` 的同名参数是坑，
   本服务用 httpx），`Content-Type` 必须是**嗅探出的真实类型**。
4. **`dry_run` 只构造请求、绝不触网**，返回可断言的字典（翻译层验收与花钱解耦）。
5. **不自动重试**：451/431 是额度类失败，重试不会稳定变好；网络上偶发错误也没把握
   区分「已处理但丢响应」，宁可真报错（由 `app/gate.py` 记录 431 静默窗）。
6. **出口可换（`TEXTIN_PROXY_POOL`）**：配置后**每个请求新建连接**并绑定池里的下一个
   代理 —— 轮换的保证来自「每请求一个新连接」，不是代理凭据（这类池通常按 TCP 连接
   轮换出口 IP；复用连接会拿回同一个出口）。
   📌 依据（2026-09-24 归因实验，见 `docs/UPSTREAM.md §4`）：textin 的 `451` 是
   **按出口 IP 的软限** —— 同一 IP 换身份（真实 Chrome ↔ 裸客户端）行为同型；
   换新 IP（同身份）立刻恢复。⇒ 要绕只能换出口，代理池是其中更"真"的一种。
   ⚠️ 默认关：绕试用配额属对抗性规避，开与不开由**部署方**决定
   （`TEXTIN_PROXY_POOL` 为空 = 直连；配了 token 时不受影响，池仍生效）。
"""

from __future__ import annotations

import contextlib
import itertools
import random
from typing import Any, AsyncIterator

import httpx

from ...config import Settings, get_settings
from ...errors import (
    ApiError,
    UpstreamDailyQuotaError,
    UpstreamQuotaError,
    UpstreamTimeout,
    UpstreamUnavailableError,
    classify_code,
)
from ...models import Capability
from ..textin.capabilities import build_call

__all__ = ["TextinClient"]

#: 与实测 CLI（tx_tools.py）逐字一致的浏览器 UA。
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
_ORIGIN = "https://tools.textin.com"

#: 代取外链输入的独立超时（与上游处理超时无关）。
_FETCH_TIMEOUT = 30.0

#: 服务器内部识别码：200 成功；其余见 errors.classify_code
_OK = 200


class TextinClient:
    """一次构造 = 一个连接池（无代理时）/ 一个代理轮换器（有代理时）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._s = settings or get_settings()
        #: 测试注入的假传输：**只在无代理路径使用**。
        #: 🔴 httpx 同时给 `transport` 与 `proxy` 时会走代理、把假传输整个绕开
        #: （2026-09-24 实测：两个都给时它真去连了代理）⇒ 池路径的单测改用
        #: 替换 `httpx.AsyncClient` 构造器的方式（见 tests/test_client.py）。
        self._transport = transport
        self._proxies = [p.strip() for p in self._s.PROXY_POOL.split(",") if p.strip()]
        self._rr = itertools.count()
        self._client = self._build_client(None)
        self.last_request_id: str | None = None

    # ------------------------------------------------------------------ 会话

    def _build_client(self, proxy: str | None) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self._s.BASE_URL.rstrip("/"),
            "timeout": self._s.TIMEOUT,
            "headers": {"accept": "application/json"},
            "trust_env": False,
        }
        if proxy:
            kwargs["proxy"] = proxy
        elif self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def next_proxy(self) -> str | None:
        """轮询取下一个代理；池为空返回 None（直连）。"""
        if not self._proxies:
            return None
        return self._proxies[next(self._rr) % len(self._proxies)]

    def masked_proxies(self) -> list[str]:
        """池的**脱敏**视图（只留 scheme://host:port），供 `/readyz` 与日志。"""
        out: list[str] = []
        for raw in self._proxies:
            try:
                u = httpx.URL(raw)
                host = u.host or ""
                port = f":{u.port}" if u.port else ""
                out.append(f"{u.scheme}://{host}{port}")
            except Exception:  # noqa: BLE001 - 坏配置不该让健康面炸掉
                out.append("<unparsable>")
        return out

    @property
    def pool_size(self) -> int:
        return len(self._proxies)

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        """取一个会话。

        - **无池**：复用长命 client（连接池有效）；
        - **有池**：**每个请求新建一个 client 并绑定该代理**。轮换的保证来自
          「每请求一个新连接」—— 复用连接会拿回同一个出口 IP（实测）。
        """
        proxy = self.next_proxy()
        if proxy is None:
            yield self._client
            return
        client = self._build_client(proxy)
        try:
            yield client
        finally:
            await client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "TextinClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ 上游调用

    def _headers(self, content_type: str) -> dict[str, str]:
        """出站请求头。与实测 CLI 逐字段对齐（token 空串也发，那是它跑通的形态）。"""
        headers = {
            "content-type": content_type,
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "origin": _ORIGIN,
            "referer": _ORIGIN + "/",
            "user-agent": _UA,
            "token": self._s.TOKEN or "",
        }
        if self._s.ROTATE_XFF and not self._s.TOKEN:
            headers["X-Forwarded-For"] = _fresh_xff()
        return headers

    async def call(self, cap: Capability, blob_data: bytes, blob_mime: str, *,
                   dry_run: bool = False) -> dict[str, Any]:
        """发一次上游请求，返回**完整信封**（dry_run 时返回"将要发出的请求"）。"""
        call = build_call(cap, blob_data, blob_mime)
        headers = self._headers(call.content_type)
        timeout = (self._s.CONVERT_TIMEOUT if cap.family == "convert" else self._s.TIMEOUT)

        if dry_run:
            req = self._client.build_request(
                "POST", self._s.OCR_PATH, params=call.params,
                content=call.content, headers=headers,
            )
            return {
                "dry_run": True,
                "method": "POST",
                "url": str(req.url),
                "service": call.service,
                "params": call.params,
                "channel": call.channel,
                "content_type": call.content_type,
                "content_bytes": len(call.content),
                "headers": _mask_headers(dict(req.headers)),
                "via_proxy": bool(self._proxies),
            }

        try:
            async with self._session() as http_client:
                resp = await http_client.post(
                    self._s.OCR_PATH, params=call.params,
                    content=call.content, headers=headers, timeout=timeout,
                )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(
                f"textin 请求超时（service={call.service}，{timeout:.0f}s）"
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"textin 网络错误：{exc}") from exc
        return self._parse(resp, service=call.service)

    def _parse(self, resp: httpx.Response, *, service: str) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise UpstreamUnavailableError(
                f"textin 返回非 JSON（HTTP {resp.status_code}）",
                http_status=resp.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise UpstreamUnavailableError("textin 返回的信封不是对象",
                                           http_status=resp.status_code)

        request_id = payload.get("x_request_id")
        if request_id:
            self.last_request_id = str(request_id)

        code = payload.get("code")
        if code == _OK:
            return payload

        msg = str(payload.get("msg") or f"HTTP {resp.status_code}")
        if code is None:
            raise UpstreamUnavailableError(
                f"textin 信封缺 code（HTTP {resp.status_code}，msg={msg}）",
                request_id=str(request_id) if request_id else None,
                http_status=resp.status_code,
            )
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            raise UpstreamUnavailableError(
                f"textin 的 code 不是整数（{code!r}，service={service}）",
                request_id=str(request_id) if request_id else None,
            ) from None
        exc = classify_code(code_int, msg, request_id=str(request_id) if request_id else None,
                            http_status=resp.status_code)
        if isinstance(exc, (UpstreamQuotaError, UpstreamDailyQuotaError)):
            # 把 service 带上：451 是**按 service 独立计**的，排障时这个字段是关键
            exc.message = f"{exc.message}（service={service}）"
        raise exc

    # ------------------------------------------------------------------ 外链输入

    async def fetch_url(self, url: str, max_bytes: int) -> tuple[bytes, str | None]:
        """代取调用方给的外链输入（有硬字节上限）。

        预检 `Content-Length` + 读流封顶双保险；取不到就显式失败，
        **绝不把半个文件发给上游**。与上游调用共用同一套会话（含代理池）。
        """
        declared_ct: str | None = None
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._session() as http_client:
                async with http_client.stream(
                    "GET", url, timeout=_FETCH_TIMEOUT, follow_redirects=True
                ) as resp:
                    if resp.status_code >= 400:
                        raise UpstreamUnavailableError(
                            f"外链输入取回失败：HTTP {resp.status_code}（{url[:120]}）",
                            http_status=resp.status_code,
                        )
                    declared_ct = resp.headers.get("content-type")
                    declared_len = resp.headers.get("content-length")
                    if (declared_len and declared_len.isdigit()
                            and int(declared_len) > max_bytes):
                        raise ApiError(
                            400, "input_too_large",
                            f"外链输入超过上限（{declared_len} 字节 > {max_bytes} 字节）",
                        )
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ApiError(
                                400, "input_too_large",
                                f"外链输入超过上限（已读 {total} 字节 > {max_bytes} 字节）",
                            )
                        chunks.append(chunk)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(f"外链输入超时（{url[:120]}）") from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"外链输入网络错误：{exc}") from exc
        return b"".join(chunks), declared_ct


def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    """dry_run 预览里的请求头：token 一律打码（凭据不出现在报文里）。"""
    out = dict(headers)
    for key in list(out):
        if key.lower() == "token":
            out[key] = "***" if out[key] else ""
    return out


def _fresh_xff() -> str:
    """随机 X-Forwarded-For（**只在显式开启时**使用；默认路径不生成）。

    上游信任该头、换 IP 即重置匿名配额（实测）—— 那是「绕按 IP 试用配额」，
    属对抗性规避，本项目默认不做（`TEXTIN_ROTATE_XFF=0`）。
    """
    safe = ("203.0.113.", "198.51.100.", "192.0.2.")  # RFC 5737 文档保留段
    if random.random() < 0.6:
        return random.choice(safe) + str(random.randint(1, 254))
    return (f"{random.randint(20, 220)}.{random.randint(0, 255)}."
            f"{random.randint(0, 255)}.{random.randint(1, 254)}")
