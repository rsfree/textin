#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务编排：校验 → 解析输入 → 调上游 → 装配响应。

**响应的唯一出口是本文件的下半部分（`_view` / `_deliver`）** ——
改响应形状只改这里；对外契约的真相在 `docs/INTERFACE.md`。

契约要点（2026-09-24 定稿）：
- **同步直给**：上游就是同步单请求（无任务 id、无轮询），不做假异步；
- 三族端点：`/v1/images/generations`（图像）、`/v1/files/convert`（转换）、
  `/v1/files/parse`（解析）；
- 结果默认内联 `b64_json`；`response_format=url` 时落盘并提供 `/files/{name}`；
- 诊断字段 `requested` / `effective` / `warnings` / `unsupported` / `upstream` 是本层
  在标准之外的加性扩展，**不改变标准字段语义**。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import ApiError, UpstreamDailyQuotaError, UpstreamUnavailableError
from .gate import QuotaWindow
from .media import image_size, resolve_input, save_media
from .models import Capability, availability
from .observability import report, span
from .upstream.textin import TextinClient
from .upstream.textin.capabilities import extract_file, extract_images, extract_parsed

__all__ = ["run", "FAMILY_FIELDS"]

#: 各族读取的输入字段名（image 族用 `image`，文件族用 `file`）
FAMILY_FIELDS: dict[str, str] = {"image": "image", "convert": "file", "parse": "file"}

#: 各请求里允许出现的键（其余键 → 400，避免拼写错误被静默吞掉）
_COMMON_KEYS = ("model", "dry_run", "response_format", "filename")
_FAMILY_KEYS: dict[str, tuple[str, ...]] = {
    "image": ("image",),
    "convert": ("file",),
    "parse": ("file",),
}

#: 认识、但**上游/本服务不支持**的字段：不报错，进 `unsupported[]`。
#: 静默丢弃是大忌 —— 调用方会以为控制生效了。
_KNOWN_UNSUPPORTED = (
    "prompt", "n", "size", "quality", "style", "seed", "negative_prompt", "watermark",
    "user", "stream", "background", "output_format", "moderation",
    "sequential_image_generation", "sequential_image_generation_options", "extra_body",
)

_RESPONSE_FORMATS = ("b64_json", "url")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def validate_request(payload: Any, *, family: str) -> tuple[Capability, str, bool, list[str]]:
    """校验请求体的**键集与模型名**，返回 (cap, response_format, dry_run, unsupported)。

    校验顺序是刻意的：**先模型、后键集** —— 把 convert 族的模型打到 images 端点
    是最常见的一类误用，此时告诉它"该打哪个端点"比报"多了一个 file 字段"有用得多。
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ApiError(400, "missing_model", "缺少 model（如 textin:watermark-remove）")

    from .models import lookup  # noqa: PLC0415 - 避免循环导入（models 不依赖本模块，这里只是就近）

    try:
        cap = lookup(model.strip())
    except KeyError as exc:
        raise ApiError(400, "unknown_model", str(exc)) from exc
    if cap.family != family:
        raise ApiError(
            400, "model_wrong_endpoint",
            f"{cap.name} 属于 {cap.family} 族，请改打 "
            f"{'/v1/images/generations' if cap.family == 'image' else '/v1/files/' + cap.family}",
            model=cap.name, family=cap.family,
        )

    allowed = set(_COMMON_KEYS) | set(_FAMILY_KEYS[family])
    unknown = sorted(k for k in payload if k not in allowed and k not in _KNOWN_UNSUPPORTED)
    if unknown:
        raise ApiError(
            400, "unknown_field",
            f"不认识的字段：{'、'.join(unknown)}。"
            f"本端点（{family} 族）接受的字段：{'、'.join(sorted(allowed))}",
            unknown=unknown, accepted=sorted(allowed),
        )

    response_format = payload.get("response_format") or "b64_json"
    if response_format not in _RESPONSE_FORMATS:
        raise ApiError(
            400, "invalid_response_format",
            f"response_format 只支持 {'、'.join(_RESPONSE_FORMATS)}",
        )
    dry = bool(payload.get("dry_run"))
    unsupported = [k for k in _KNOWN_UNSUPPORTED if payload.get(k) is not None]
    return cap, response_format, dry, unsupported


async def run(
    *,
    settings: Settings,
    client: TextinClient,
    gate: QuotaWindow,
    cap: Capability,
    family: str,
    payload: dict[str, Any],
    response_format: str,
    dry_run: bool,
    unsupported: list[str],
) -> dict[str, Any]:
    warnings: list[str] = []

    # ---- 1) 未取证能力闸门（**必须生效在调用上游之前**；dry_run 穿透）----
    ok, reason = availability(cap, allow_unverified=settings.ALLOW_UNVERIFIED)
    if not ok and not dry_run:
        raise ApiError(503, "capability_not_verified", reason, model=cap.name)

    # ---- 2) 431 静默窗（当天不恢复 ⇒ 白打没有意义；dry_run 不触网，天然豁免）----
    if not dry_run and gate.open():
        snap = gate.snapshot()
        raise ApiError(
            429, "daily_quota_cooldown",
            f"textin 按天额度已满（code=431），静默窗剩余 {int(snap['remaining_s'])}s；"
            f"当天不恢复，窗内不再打上游",
            retry_after=snap["remaining_s"], upstream_code=431,
        )

    # ---- 3) 输入解析（四种到达形态归一；类型按真实字节嗅探）----
    field = FAMILY_FIELDS[family]
    raw_input = payload.get(field)
    filename_hint = payload.get("filename")
    if not isinstance(filename_hint, str) or not filename_hint.strip():
        filename_hint = None
    max_bytes = max(1, settings.MAX_DOWNLOAD_MB) * 1024 * 1024
    async def _fetch(url: str, limit: int) -> tuple[bytes, str | None]:
        return await client.fetch_url(url, limit)

    blob = await resolve_input(
        raw_input,
        accepts=cap.accepts,
        fetch=_fetch,
        max_bytes=max_bytes,
        filename_hint=filename_hint,
        warnings=warnings,
    )

    requested = {
        "model": cap.name,
        "response_format": response_format,
        field: _input_summary(raw_input),
    }
    if filename_hint:
        requested["filename"] = filename_hint
    effective = {
        "service": cap.service,
        "params": cap.params(),
        "input": {"kind": blob.kind, "mime": blob.mime, "bytes": blob.size,
                  "source": blob.source},
    }

    # ---- 4) 上游调用 ----
    with span("textin.call", service=cap.service, family=family, dry_run=dry_run) as rec:
        try:
            envelope = await client.call(cap, blob.data, blob.mime, dry_run=dry_run)
        except UpstreamDailyQuotaError as exc:
            cooldown = gate.trip()
            if cooldown:
                exc.retry_after = cooldown
            rec["outcome"] = "daily_quota"
            raise
        rec["outcome"] = "dry_run" if dry_run else "ok"

    # ---- 5) 装配（唯一出口）----
    if dry_run:
        report("textin.dry_run", service=cap.service, bytes=blob.size)
        return {
            "created": int(time.time()),
            "model": cap.name,
            "dry_run": True,
            "data": [],
            "usage": None,
            "requested": requested,
            "effective": effective,
            "warnings": warnings,
            "unsupported": unsupported,
            "upstream": None,
            "preview": envelope,
        }

    data_items, usage = _assemble(
        cap=cap, family=family, envelope=envelope, response_format=response_format,
        settings=settings, filename_hint=filename_hint, warnings=warnings,
    )
    report("textin.done", service=cap.service, items=len(data_items), usage=usage)
    return {
        "created": int(time.time()),
        "model": cap.name,
        "dry_run": False,
        "data": data_items,
        "usage": usage,
        "requested": requested,
        "effective": effective,
        "warnings": warnings,
        "unsupported": unsupported,
        "upstream": {
            "service": cap.service,
            "code": envelope.get("code"),
            "request_id": envelope.get("x_request_id"),
        },
    }


# ---------------------------------------------------------------------------
# 装配（唯一出口）
# ---------------------------------------------------------------------------


def _assemble(*, cap: Capability, family: str, envelope: dict[str, Any],
              response_format: str, settings: Settings, filename_hint: str | None,
              warnings: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    result = (envelope.get("data") or {}).get("result")

    if family == "image":
        images = extract_images(result)
        if not images:
            raise _silent_failure(cap, result)
        items = []
        for raw, mime, ext in images:
            item = _deliver(raw, mime=mime, ext=ext, response_format=response_format,
                            settings=settings)
            size = image_size(raw)
            if size:
                item["size"] = f"{size[0]}x{size[1]}"
            items.append(item)
        return items, {"generated_images": len(items)}

    if family == "convert":
        raw = extract_file(result, cap)
        name = _out_name(filename_hint, cap.output_ext)
        item = _deliver(raw, mime=cap.output_mime, ext=cap.output_ext,
                        response_format=response_format, settings=settings, filename=name)
        return [item], {"files": 1}

    # family == "parse"
    parsed = extract_parsed(result, cap)
    item: dict[str, Any] = {"result": parsed.result}
    if parsed.text is not None:
        item["text"] = parsed.text
    base = Path(filename_hint).stem if filename_hint else "output"
    attachments = []
    for att in parsed.attachments:
        attachments.append(_deliver(
            att.data, mime=att.mime, ext=att.ext, response_format=response_format,
            settings=settings, filename=f"{base}-{att.key}{att.ext}",
        ))
    if cap.b64_attachments and not attachments:
        warnings.append(
            f"上游未返回预期附件（{'、'.join(k for k, _, _ in cap.b64_attachments)}）—— "
            f"result 里没有对应键或解码失败"
        )
    item["attachments"] = attachments
    return [item], {"files": 1}


def _deliver(raw: bytes, *, mime: str, ext: str, response_format: str, settings: Settings,
             filename: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"mime": mime}
    if filename:
        item["filename"] = filename
    if response_format == "url":
        try:
            name = save_media(raw, settings.MEDIA_DIR, ext)
        except OSError as exc:
            raise ApiError(
                503, "media_write_failed",
                f"结果落盘失败（MEDIA_DIR={settings.MEDIA_DIR}）：{exc} —— 部署问题",
            ) from exc
        item["url"] = f"/files/{name}"
    else:
        item["b64_json"] = base64.b64encode(raw).decode("ascii")
    return item


def _silent_failure(cap: Capability, result: Any) -> UpstreamUnavailableError:
    """`code=200` 但没有可用产物 = 静默型失败，必须当显式错误处理。"""
    shape = type(result).__name__
    keys = sorted(result.keys()) if isinstance(result, dict) else None
    return UpstreamUnavailableError(
        f"textin 未返回图片（service={cap.service}，result 形态={shape}"
        f"{'，keys=' + str(keys) if keys else ''}）",
        code=None,
    )


def _out_name(filename_hint: str | None, ext: str) -> str:
    stem = Path(filename_hint).stem if filename_hint else "output"
    return f"{stem}{ext}"


def _input_summary(value: Any) -> dict[str, Any]:
    """输入摘要（**不回显原文**：可能是几 MB 的 base64）。"""
    if isinstance(value, str):
        if value.startswith("data:"):
            form = "data-uri"
        elif value[:4].lower() == "http":
            form = "url"
        else:
            form = "base64"
        return {"form": form, "length": len(value)}
    if isinstance(value, list):
        return {"form": "list", "items": len(value)}
    return {"form": type(value).__name__}
