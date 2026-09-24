#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 层：对外契约的**唯一入口**（契约全文 docs/INTERFACE.md）。

三件必须做对的事：

  1. **同步直给** —— POST 一次返回结果；上游就是同步单请求，不做假异步（2026-09-24 用户拍板）。
  2. **错误信封统一** —— 我们自己的错误 `{"error":{code,message}}`；
     上游错误额外带 `kind` / `request_id` / `upstream:true`；重试语义用 `Retry-After` 表达。
  3. **鉴权语义** —— 静态 Bearer 白名单；`API_KEYS` 空 = 关闭（仅限内网，启动 WARNING）；
     `/v1/models` 刻意免鉴权（发现端点，与 jimeng 一致）；其余对外端点与运维面都要 Key。

⚠️ `gunicorn` 目标必须是**工厂**：`app.main:create_app()`（见 gunicorn_conf.py）。
"""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from . import __version__
from .config import Settings, get_settings
from .errors import UPSTREAM_KIND_STATUS, ApiError, UpstreamError
from .gate import QuotaWindow
from .llms import render as render_llms_txt
from .models import CAPABILITIES, NOT_REGISTERED, available, availability
from .observability import setup as setup_observability
from .observability import spans
from .service import run, validate_request
from .upstream.textin import TextinClient

__all__ = ["create_app"]


def _api_keys(settings: Settings) -> list[str]:
    return [k.strip() for k in settings.API_KEYS.split(",") if k.strip()]


async def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """静态 Bearer 白名单。

    白名单为空 ⇒ **鉴权整体关闭**（仅限内网），启动时打 WARNING。
    比对用 `hmac.compare_digest`（恒定时间），不用 `in`。

    ⚠️ 配置取自 `request.app.state.settings` 而**不是**全局 `get_settings()`：
    否则 `create_app(settings=...)` 传进来的配置对鉴权无效，测试会假绿。
    """
    settings: Settings = request.app.state.settings
    keys = _api_keys(settings)
    if not keys:
        return "anonymous"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "unauthorized", "缺少 Bearer 凭据")
    token = authorization.split(" ", 1)[1].strip()
    if not any(hmac.compare_digest(token, k) for k in keys):
        raise ApiError(401, "unauthorized", "凭据不在白名单")
    return "client"


def _client(request: Request) -> TextinClient:
    return request.app.state.client


def _gate(request: Request) -> QuotaWindow:
    return request.app.state.gate


def _dry_run_flag(request: Request, payload: dict[str, Any]) -> bool:
    header = (request.headers.get("x-avm-dry-run") or "").strip().lower()
    if header in ("1", "true", "yes"):
        return True
    return bool(payload.get("dry_run"))


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    setup_observability(
        level=s.LOG_LEVEL, logfire_token=s.LOGFIRE_TOKEN, service_name=s.OTEL_SERVICE_NAME,
        environment=s.LOGFIRE_ENVIRONMENT, scrubbing=s.OTEL_SCRUBBING,
        capture_upstream=s.OTEL_CAPTURE_UPSTREAM,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = s
        app.state.client = TextinClient(s)
        app.state.gate = QuotaWindow(s.QUOTA_COOLDOWN)
        Path(s.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
        if not _api_keys(s):
            # 🔴 fail-closed：空 keys 默认**拒绝启动**（2026-09-24 事故根因——当时只打 WARNING，
            #    服务在公网上无鉴权运行了数小时，谁都没看见）。要无鉴权必须显式豁免。
            if not s.ALLOW_NO_AUTH:
                logger.error(
                    "TEXTIN_API_KEYS 为空且未显式豁免 ⇒ 拒绝启动。"
                    "内网/干跑确实要无鉴权，请显式设 TEXTIN_ALLOW_NO_AUTH=1（届时 /readyz 会自报 false）")
                raise RuntimeError(
                    "TEXTIN_API_KEYS 为空：拒绝以无鉴权方式启动（确需请设 TEXTIN_ALLOW_NO_AUTH=1）")
            logger.warning("TEXTIN_API_KEYS 为空**但已显式豁免**（TEXTIN_ALLOW_NO_AUTH=1）："
                           "鉴权整体关闭 —— 只允许受信内网这样部署")
        if not s.TOKEN:
            logger.info("未配 TEXTIN_TOKEN：按**匿名**调用上游（配额按 IP 计，见 docs/UPSTREAM.md §4）")
        if s.ROTATE_XFF:
            logger.warning("TEXTIN_ROTATE_XFF=1：已开启 XFF 轮换（属对抗性规避，默认应为 0）")
        if s.PROXY_POOL.strip():
            logger.warning(
                "TEXTIN_PROXY_POOL 已配置（{} 个入口）：每个请求会换一个新出口。"
                "绕试用配额属对抗性规避，默认应为空 —— 由部署方确认这是有意为之",
                app.state.client.pool_size,
            )
        if s.ALLOW_UNVERIFIED:
            logger.warning("TEXTIN_ALLOW_UNVERIFIED=1：未取证能力已放开（后果由部署方承担）")
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(
        title="textin-service",
        version=__version__,
        summary="TextIn 工具站（tools.textin.com）的同步出口：图像处理 / 文档转换 / 识别解析",
        lifespan=lifespan,
    )

    # ---------- 错误信封 ----------

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        headers: dict[str, str] = {}
        retry_after = exc.extra.get("retry_after")
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            headers["Retry-After"] = str(int(retry_after))
        return JSONResponse(status_code=exc.status, content=exc.payload(), headers=headers)

    @app.exception_handler(UpstreamError)
    async def _upstream_error(_: Request, exc: UpstreamError) -> JSONResponse:
        status = UPSTREAM_KIND_STATUS.get(exc.kind, 502)
        body: dict[str, Any] = {
            "error": {
                "code": exc.code if exc.code is not None else exc.kind,
                "message": exc.message,
                "kind": exc.kind,
                "upstream": True,
            }
        }
        if exc.request_id:
            body["error"]["request_id"] = exc.request_id
        headers: dict[str, str] = {}
        if exc.retry_after and exc.retry_after > 0:
            headers["Retry-After"] = str(int(exc.retry_after))
        return JSONResponse(status_code=status, content=body, headers=headers)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常：{}", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": f"内部错误：{type(exc).__name__}"}},
        )

    # ---------- 健康与运维 ----------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, Any]:
        gate = _gate(request)
        allow = bool(s.ALLOW_UNVERIFIED)
        checks = {
            "api_keys_enabled": bool(_api_keys(s)),
            "allow_no_auth": bool(s.ALLOW_NO_AUTH),     # 无鉴权豁免（fail-closed 的逃生门）
            "upstream_base": f"{s.BASE_URL}{s.OCR_PATH}",
            "upstream_auth": "token" if s.TOKEN else "anonymous",
            "xff_rotation": bool(s.ROTATE_XFF),
            # 出口形态要可见（凭据已脱敏）：池配了没有、几个入口、都是谁
            "egress_proxies": _client(request).masked_proxies(),
            "allow_unverified": allow,
            "soft_limit_retry": int(s.SOFT_LIMIT_RETRY),   # 451 自动重试次数（0=关）
            "media_dir": s.MEDIA_DIR,
            "quota_window": gate.snapshot(),
            "capabilities": {
                "total": len(CAPABILITIES),
                "available": len(available(allow_unverified=allow)),
            },
        }
        degraded = checks["quota_window"]["cooling"]
        return {"status": "degraded" if degraded else "ok", "checks": checks}

    @app.get("/stats")
    async def stats(request: Request, _: str = Depends(require_api_key)) -> dict[str, Any]:
        return {
            "version": __version__,
            "quota_window": _gate(request).snapshot(),
            "spans": spans()[-50:],
        }

    # ---------- 发现端点 ----------

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        """OpenAI 兼容的**列表**端点：只列本部署可调用的模型（免鉴权，与 jimeng 一致）。

        逐字四键（`id`/`object`/`created`/`owned_by` —— 多一个键就是契约变更）；
        全集（含未取证项的**原因与开启方式**）在 `GET /capabilities`。
        """
        settings: Settings = request.app.state.settings
        caps = available(allow_unverified=settings.ALLOW_UNVERIFIED)
        from .models import MODEL_RELEASED_AT, OWNED_BY  # noqa: PLC0415

        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": MODEL_RELEASED_AT, "owned_by": OWNED_BY}
                for name in sorted(caps)
            ],
        }

    @app.get("/llms.txt")
    async def llms_txt(request: Request) -> Response:
        """`/llms.txt`（llmstxt.org 约定）：给 LLM/Agent 的**站点索引**。

        渲染在 `app.llms.render`（**纯函数**，便于对账门禁）；这里只负责一件平台相关的事：
        还原**调用方看到的基址** —— 反代后面用 `X-Forwarded-Proto` 还原对外 scheme
        （nginx 已设该头），否则 https 站点会写出 http 链接。只在本端点认它：
        全站开 uvicorn 的 proxy-headers 会顺带改日志/客户端 IP 口径。
        """
        settings: Settings = request.app.state.settings
        scheme = (request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
                  or request.url.scheme)
        host = request.headers.get("host") or request.url.netloc
        body = render_llms_txt(f"{scheme}://{host}/",
                               allow_unverified=settings.ALLOW_UNVERIFIED)
        return Response(body, media_type="text/markdown; charset=utf-8")

    @app.get("/capabilities")
    async def capabilities(request: Request,
                           _: str = Depends(require_api_key)) -> dict[str, Any]:
        settings: Settings = request.app.state.settings
        allow = settings.ALLOW_UNVERIFIED
        models = []
        not_available = []
        for name in sorted(CAPABILITIES):
            cap = CAPABILITIES[name]
            ok, reason = availability(cap, allow_unverified=allow)
            entry = {
                "id": name,
                "family": cap.family,
                "service": cap.service,
                "params": cap.params(),
                "accepts": list(cap.accepts),
                "output": cap.output,
                "output_ext": cap.output_ext or None,
                "verified": cap.verified,
                "evidence": cap.evidence,
                "notes": cap.notes,
            }
            if ok:
                models.append(entry)
            else:
                entry["reason"] = reason
                not_available.append(entry)
        return {
            "models": models,
            "not_available": not_available,
            "not_registered": NOT_REGISTERED,
        }

    # ---------- 三族执行端点（同步直给） ----------

    async def _handle(family: str, request: Request) -> JSONResponse:
        client = _client(request)
        gate = _gate(request)
        try:
            payload = await request.json()
        except ValueError as exc:
            raise ApiError(400, "invalid_json", "请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")

        dry_run = _dry_run_flag(request, payload)
        cap, response_format, body_dry, unsupported = validate_request(payload, family=family)
        dry_run = dry_run or body_dry

        body = await run(
            settings=s, client=client, gate=gate, cap=cap, family=family,
            payload=payload, response_format=response_format, dry_run=dry_run,
            unsupported=unsupported,
        )
        return JSONResponse(status_code=200, content=body)

    @app.post("/v1/images/generations")
    async def images_generations(request: Request,
                                 _: str = Depends(require_api_key)) -> JSONResponse:
        return await _handle("image", request)

    @app.post("/v1/files/convert")
    async def files_convert(request: Request,
                            _: str = Depends(require_api_key)) -> JSONResponse:
        return await _handle("convert", request)

    @app.post("/v1/files/parse")
    async def files_parse(request: Request,
                          _: str = Depends(require_api_key)) -> JSONResponse:
        return await _handle("parse", request)

    # ---------- 结果取件（response_format=url 时产生） ----------
    # 🔴 目录必须在 mount **之前**存在：StaticFiles(check_dir=True) 在挂载时就校验，
    # 否则 create_app 直接抛（而生命周期里的 mkdir 跑在挂载之后，救不了）。
    Path(s.MEDIA_DIR).mkdir(parents=True, exist_ok=True)
    app.mount("/files", StaticFiles(directory=str(Path(s.MEDIA_DIR))), name="files")

    return app


if __name__ == "__main__":  # python -m app.main
    # ⚠️ 入口必须写在这里：`python -m app.main` 跑的是本文件，不会去执行 app/__main__.py
    # （grok 轮踩过：写错位置会**静默退出 0**，看起来像起过了）。
    _s = get_settings()
    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=_s.PORT)
