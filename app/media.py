#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体处理：类型嗅探 / 输入解析 / 结果落盘。

三条硬规矩（都来自实测踩坑，与 reverse-proxy 的 biz-api 同源）：

  1. **按 magic bytes 嗅探，不信扩展名也不信 Content-Type。**
     真实案例：`.jpg` 结尾、Content-Type 写成非标的 `image/jpg`，实际是 PNG，
     上游按 Content-Type 校验直接拒。本服务一律用嗅探出的真实类型做出站 Content-Type。
  2. **容器类型要看内容**：docx / xlsx / pptx / ofd 都是 ZIP，靠内层目录名区分
     （`word/` `xl/` `ppt/` `OFD.xml`）；`.doc`/`.xls` 都是 OLE2，靠能力声明的期望值定。
  3. **外链一律由本服务代取并设硬上限**（预检 Content-Length + 读流封顶），
     取不到就显式失败，绝不把半个文件发给上游。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .errors import ApiError

__all__ = ["Blob", "sniff", "resolve_input", "save_media", "image_size", "DATA_URI_RE"]

#: data URI：`data:[<mime>][;base64],<payload>`。mime 只作参考，**以嗅探为准**。
DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]*)?(?P<b64>;base64)?,(?P<payload>.*)$", re.S)

_B64_RE = re.compile(r"^[A-Za-z0-9+/=\s]{32,}$")

#: kind → (mime, ext)。kind 是本服务内部的输入家族名（与 models.InputKind 对齐）。
_KIND_MIME: dict[str, tuple[str, str]] = {
    "png": ("image/png", ".png"),
    "jpeg": ("image/jpeg", ".jpg"),
    "gif": ("image/gif", ".gif"),
    "webp": ("image/webp", ".webp"),
    "bmp": ("image/bmp", ".bmp"),
    "tiff": ("image/tiff", ".tif"),
    "pdf": ("application/pdf", ".pdf"),
    "zip": ("application/zip", ".zip"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "ofd": ("application/ofd", ".ofd"),
    "doc": ("application/msword", ".doc"),
    "xls": ("application/vnd.ms-excel", ".xls"),
    "csv": ("text/csv", ".csv"),
    "ole2": ("application/x-ole-storage", ".bin"),
    "text": ("text/plain", ".txt"),
    "unknown": ("application/octet-stream", ".bin"),
}

_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)


def mime_ext(kind: str) -> tuple[str, str]:
    return _KIND_MIME.get(kind, _KIND_MIME["unknown"])


def sniff(data: bytes) -> str:
    """粗嗅探：返回内部 kind（png/jpeg/.../pdf/zip/ole2/text/unknown）。

    ZIP 与 OLE2 **不在这里细分**（要看内层内容），由 `_refine_container` 完成。
    """
    if len(data) < 4:
        return "unknown"
    for magic, kind in _IMAGE_MAGIC:
        if data.startswith(magic):
            return kind
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"%PDF"):
        return "pdf"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole2"
    if _looks_like_text(data):
        return "text"
    return "unknown"


def _looks_like_text(data: bytes) -> bool:
    """很小的纯文本启发式：只在能力接受 CSV 时才被使用（CSV 没有 magic）。"""
    head = data[:4096]
    if not head:
        return False
    if b"\x00" in head:
        return False
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return printable / max(len(text), 1) > 0.9


def _refine_container(kind: str, data: bytes, accepts: tuple[str, ...]) -> str:
    """ZIP / OLE2 的细分。

    - ZIP：`word/` → docx、`xl/` → xlsx、`ppt/` → pptx、含 `OFD.xml` → ofd；
    - OLE2：`.doc` 与 `.xls` 都是 OLE2 容器，不解析文档流就分不开 —— 按**能力声明的
      期望值**取（能力就是按 service 定的，它只会接受其中一种）。
    """
    if kind == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
        except (zipfile.BadZipFile, OSError):
            return "zip"
        for name in names:
            if name.startswith("word/"):
                return "docx"
            if name.startswith("xl/"):
                return "xlsx"
            if name.startswith("ppt/"):
                return "pptx"
        if any(n.endswith("OFD.xml") or n == "OFD.xml" for n in names):
            return "ofd"
        return "zip"
    if kind == "ole2":
        if "doc" in accepts:
            return "doc"
        if "xls" in accepts:
            return "xls"
        return "ole2"
    return kind


@dataclass(frozen=True)
class Blob:
    """一次输入：**已归一为本地字节**，类型来自嗅探。"""

    data: bytes
    kind: str
    mime: str
    ext: str
    source: str                  # data-uri / url / base64
    filename: str | None = None

    @property
    def size(self) -> int:
        return len(self.data)


def _make_blob(data: bytes, *, source: str, accepts: tuple[str, ...],
               filename: str | None, warnings: list[str]) -> Blob:
    if not data:
        raise ApiError(400, "empty_input", "输入是 0 字节（fetch/解码得到空内容）")
    kind = _refine_container(sniff(data), data, accepts)
    if kind == "text":
        # CSV 没有 magic：只有在能力明确接受 csv 时才认（否则 text 就是一种"无法识别"）
        kind = "csv" if "csv" in accepts else "text"
    if kind not in accepts:
        raise ApiError(
            400, "input_kind_not_accepted",
            f"输入是 {kind}（按真实字节嗅探；扩展名/Content-Type 不作数），"
            f"但该能力只接受：{'、'.join(accepts)}",
            sniffed_kind=kind, accepted=list(accepts),
        )
    mime, ext = mime_ext(kind)
    if kind == "csv":
        mime, ext = "text/csv", ".csv"
    return Blob(data=data, kind=kind, mime=mime, ext=ext,
                source=source, filename=filename)


async def resolve_input(
    value: str,
    *,
    accepts: tuple[str, ...],
    fetch: Callable[[str, int], Awaitable[tuple[bytes, str | None]]] | None = None,
    max_bytes: int,
    filename_hint: str | None = None,
    warnings: list[str] | None = None,
) -> Blob:
    """把 `image` / `file` 字段的四种到达形态归一成 Blob：data URI / URL / 裸 base64。

    - URL 形态需要 `fetch`（由 client 提供；测试用假 fetch，零出网）；
    - 类型一律**以嗅探为准**；data URI 里写的 mime 与真实不符时留 warning（不拒绝）。
    """
    warn = warnings if warnings is not None else []
    if not isinstance(value, str) or not value.strip():
        raise ApiError(
            400, "missing_input",
            "缺少输入（image / file 字段）。接受三种形态："
            "data URI（`data:image/png;base64,...`）、http(s) URL、裸 base64",
        )
    value = value.strip()

    m = DATA_URI_RE.match(value)
    if m:
        payload = m.group("payload") or ""
        declared = (m.group("mime") or "").strip() or None
        if m.group("b64"):
            try:
                data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ApiError(400, "invalid_base64", f"data URI 的 base64 解不开：{exc}") from exc
        else:
            # 非 base64 的 data URI（百分号编码文本，csv 场景见过）
            from urllib.parse import unquote_to_bytes  # noqa: PLC0415 - 局部使用

            data = unquote_to_bytes(payload)
        blob = _make_blob(data, source="data-uri", accepts=accepts,
                          filename=filename_hint, warnings=warn)
        if declared and declared != blob.mime:
            warn.append(f"data URI 声明的 mime（{declared}）与真实类型不符，已按真实类型 {blob.mime} 发出")
        return blob

    if value.lower().startswith(("http://", "https://")):
        if fetch is None:
            raise ApiError(400, "url_input_unsupported", "本部署未启用外链输入")
        data, declared_ct = await fetch(value, max_bytes)
        warn.append(f"已由本服务代取外链（{len(data)} 字节）")
        blob = _make_blob(data, source="url", accepts=accepts,
                          filename=filename_hint, warnings=warn)
        if declared_ct:
            declared_base = declared_ct.split(";")[0].strip().lower()
            if declared_base and declared_base not in (blob.mime, "application/octet-stream"):
                warn.append(
                    f"外链响应的 Content-Type（{declared_base}）与真实类型不符，"
                    f"已按真实类型 {blob.mime} 发出"
                )
        return blob

    if _B64_RE.match(value):
        try:
            data = base64.b64decode(re.sub(r"\s+", "", value), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ApiError(400, "invalid_base64", f"裸 base64 解不开：{exc}") from exc
        return _make_blob(data, source="base64", accepts=accepts,
                          filename=filename_hint, warnings=warn)

    raise ApiError(
        400, "invalid_input_format",
        "输入既不是 data URI、也不是 http(s) URL、也不是合法 base64。"
        "裸 base64 至少 32 字符；本地文件请自行编码后传入",
    )


def save_media(data: bytes, media_dir: str | Path, ext: str) -> str:
    """把结果落盘（`response_format=url` 时）。内容寻址：同名同内容，天然幂等。"""
    name = hashlib.sha256(data).hexdigest()[:20] + ext
    target = Path(media_dir) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    return name


def image_size(data: bytes) -> tuple[int, int] | None:
    """PNG / JPEG 的像素尺寸（只在**确实解析出来**时返回，绝不编造）。

    seedream 契约的 `data[].size` 要的是上游实际产出的像素尺寸；
    这里从结果字节里读，读不到就不给该字段。
    """
    kind = sniff(data)
    if kind == "png" and len(data) >= 24:
        import struct  # noqa: PLC0415

        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    if kind == "jpeg":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if i + 9 < n:
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return int(w), int(h)
                return None
            if seg_len <= 0:
                return None
            i += 2 + seg_len
    return None
