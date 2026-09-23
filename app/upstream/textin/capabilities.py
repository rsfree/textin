#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""翻译层（**纯函数**，离网可测）：出站请求构造 + 上游结果解析。

上游报文的三类形态与判据（详见 docs/UPSTREAM.md §3）：

| 形态 | 出现场景 | 解析方式 |
|---|---|---|
| `result` 是 dict，图在 `image` / `image_list[].image` | 图像族 | `extract_images` |
| `result` 是 **base64 字符串**（整份文件） | 转换族 | `extract_file` |
| `result` 是结构化 dict | 解析族 | `extract_parsed` |

纪律：**解析不出来 = 显式失败**，绝不"猜一个形态"往下走 ——
`code=200` 但没有可用产物，是静默型失败，必须当错处理。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any

from ...errors import UpstreamUnavailableError
from ...media import mime_ext, sniff
from ...models import Capability

__all__ = [
    "UpstreamCall",
    "ParsedResult",
    "Attachment",
    "build_call",
    "extract_images",
    "extract_file",
    "extract_parsed",
]

#: 结果图片可接受的 kind（上游图像族实测恒 JPEG；其余是防御性收容）
_RESULT_IMAGE_KINDS = ("png", "jpeg", "webp", "bmp", "tiff")

#: base64 候选串的最小长度：防把短文本误判成 b64（`b64decode(validate=False)` 很宽容）。
#: 取 64：一张 24x16 的合法小 PNG 其 b64 约 116 字符 —— 阈值再高就会把**真的小图**滤掉
#: （2026-09-24 实测踩到：阈值 128 时小 PNG 被静默判为"没有图"）。
_MIN_B64 = 64
_B64_LOOKSLIKE_RE = re.compile(r"^[A-Za-z0-9+/=\r\n]+$")


@dataclass(frozen=True)
class UpstreamCall:
    """一次将要发出的上游请求（已归一）。"""

    params: dict[str, str]
    content: bytes
    content_type: str
    channel: str          # "raw"（裸字节，唯一常态）| "json"（image-to-pdf 专属）

    @property
    def service(self) -> str:
        return self.params.get("service", "")


@dataclass(frozen=True)
class Attachment:
    """解析族里附带的产物文件（如 table&excel=1 的 result.excel）。"""

    key: str              # 上游 result 里的键名
    data: bytes
    ext: str
    mime: str


@dataclass(frozen=True)
class ParsedResult:
    """解析族的结果。`result` 是**上游原样**的结构化数据（不翻译、不裁剪）。"""

    result: dict[str, Any]
    text: str | None = None
    attachments: tuple[Attachment, ...] = ()


# ---------------------------------------------------------------------------
# 出站请求构造
# ---------------------------------------------------------------------------


def build_call(cap: Capability, blob_data: bytes, blob_mime: str) -> UpstreamCall:
    """构造上游请求。

    - 常态：**裸文件字节**，Content-Type = 嗅探出的真实类型；
    - 唯一例外 `image-to-pdf`：JSON 通道 `{"files":["<b64>"]}`（抓包实证）。
    """
    params = cap.params()
    if cap.service == "image-to-pdf":
        b64 = base64.b64encode(blob_data).decode("ascii")
        content = json.dumps({"files": [b64]}).encode("utf-8")
        return UpstreamCall(params=params, content=content,
                            content_type="application/json", channel="json")
    return UpstreamCall(params=params, content=blob_data,
                        content_type=blob_mime, channel="raw")


# ---------------------------------------------------------------------------
# 结果解析
# ---------------------------------------------------------------------------


def _try_b64_image(value: Any) -> tuple[bytes, str, str] | None:
    """尝试把候选值当 base64 解成图片；解出来**且 magic 是图片**才算数。"""
    if not isinstance(value, str) or len(value) < _MIN_B64:
        return None
    try:
        raw = base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    kind = sniff(raw)
    if kind not in _RESULT_IMAGE_KINDS:
        return None
    mime, ext = mime_ext(kind)
    return raw, mime, ext


def extract_images(result: Any) -> list[tuple[bytes, str, str]]:
    """从图像族 result 里取出全部图片（(bytes, mime, ext)）。

    已实测的两种 + 防御性收容：
      · `{"image": "<b64>"}`            —— watermark-remove / demoire / text_auto_removal
      · `{"image_list": [{"image": …}]}` —— crop_enhance_image
      · 裸字符串 / 列表                    —— 防御（文档曾述，未见实物）
    """
    out: list[tuple[bytes, str, str]] = []
    if isinstance(result, str):
        got = _try_b64_image(result)
        if got:
            out.append(got)
        return out
    if isinstance(result, list):
        for item in result:
            out.extend(extract_images(item))
        return out
    if isinstance(result, dict):
        for key in ("image", "image_base64", "base64"):
            got = _try_b64_image(result.get(key))
            if got:
                out.append(got)
                break
        else:
            seq = result.get("image_list") or result.get("images")
            if isinstance(seq, list):
                for item in seq:
                    if isinstance(item, dict):
                        got = _try_b64_image(item.get("image") or item.get("image_base64"))
                    else:
                        got = _try_b64_image(item)
                    if got:
                        out.append(got)
    return out


def _container_matches(data: bytes, ext: str) -> bool:
    """结果文件的容器校验：**按扩展名的期望签字**，不符即视为拿错了东西。"""
    if ext == ".pdf":
        return data.startswith(b"%PDF")
    if ext in (".zip", ".docx", ".xlsx", ".pptx"):
        return data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
    return True


def extract_file(result: Any, cap: Capability) -> bytes:
    """转换族：`result` 是整份文件的 base64。

    拿到的字节必须**签字与能力声明的产物一致**（PDF 头 / ZIP 头）——
    否则是上游行为变化或拿错了内容（例如错误页），必须显式失败。
    """
    if not isinstance(result, str) or len(result) < 16:
        raise UpstreamUnavailableError(
            f"textin 未返回文件（service={cap.service}，"
            f"result 形态={type(result).__name__}）—— 期望 base64 字符串",
            code=None,
        )
    try:
        raw = base64.b64decode(result, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise UpstreamUnavailableError(
            f"textin 返回的 result 不是合法 base64（service={cap.service}）",
        ) from exc
    if not _container_matches(raw, cap.output_ext):
        raise UpstreamUnavailableError(
            f"textin 返回的产物与期望容器不符（service={cap.service}，"
            f"期望 {cap.output_ext}，首 4 字节={raw[:4]!r}）—— 拒绝交付可疑产物",
        )
    return raw


def extract_parsed(result: Any, cap: Capability) -> ParsedResult:
    """解析族：结构化 result 原样透传 + 规范化便利字段。

    - `text`：`result.markdown`（doc-parse / finance-report）；若上游返回的
      markdown 看起来仍是 base64（`markdown_details=1` 未生效的情况），
      尝试解码并**在调用方留 warning**（由服务层做，见 app/service.py）。
    - `attachments`：`cap.b64_attachments` 声明的键（如 `excel`）解码成文件，
      容器签字不符则**跳过该附件并留 warning**（不整体失败 —— 主体结构化结果仍有效）。
    """
    if isinstance(result, str) and cap.service == "pdf_to_markdown":
        # 防御：上游若只回一段 markdown 文本，按 {"markdown": …} 归一
        result = {"markdown": result}
    if not isinstance(result, dict):
        raise UpstreamUnavailableError(
            f"textin 未返回结构化结果（service={cap.service}，"
            f"result 形态={type(result).__name__}）",
        )
    text: str | None = None
    md = result.get("markdown")
    if isinstance(md, str):
        if _looks_like_b64_text(md):
            try:
                decoded = base64.b64decode(md, validate=False).decode("utf-8")
                text = decoded
            except (binascii.Error, ValueError, UnicodeDecodeError):
                text = md
        else:
            text = md
    attachments: list[Attachment] = []
    for key, ext, mime in cap.b64_attachments:
        value = result.get(key)
        if not isinstance(value, str) or len(value) < 16:
            continue
        try:
            raw = base64.b64decode(value, validate=False)
        except (binascii.Error, ValueError):
            continue
        if _container_matches(raw, ext):
            attachments.append(Attachment(key=key, data=raw, ext=ext, mime=mime))
    return ParsedResult(result=result, text=text, attachments=tuple(attachments))


def _looks_like_b64_text(value: str) -> bool:
    """判断一段文本"是不是 base64 编码的产物"。

    用两条**同时成立**的判据（够窄，不会把中文 markdown 误伤）：
    长度 > 200、整串只含 base64 字母表（无空格/中文/换行）。
    """
    if len(value) <= 200:
        return False
    return bool(_B64_LOOKSLIKE_RE.match(value))
