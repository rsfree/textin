"""测试样本与配置构造（供各用例 import；**全部本地生成、零网络**）。

样本的真实性分两档，用例要按需选择：

- **格式真实**（PNG/JPEG）：真的能被解码器读出像素 —— `image_size()` 有断言价值；
- **容器真实**（PDF/DOCX/XLSX/PPTX/OFD）：只有 magic/容器结构对，内容不是真文档 ——
  本服务的翻译层只做容器判定，够用；**要验证真文档请用 scripts/probe.py --live**。
"""

from __future__ import annotations

import asyncio
import base64
import struct
import zipfile
import zlib
from io import BytesIO
from typing import Any

import httpx

from app.config import Settings


def arun(coro: Any) -> Any:
    """同步用例里跑协程（避免为几个单测引入 pytest-asyncio）。"""
    return asyncio.run(coro)


def settings(**overrides: Any) -> Settings:
    """测试用配置：显式给值、**不读 .env 也不受环境变量影响**。

    `_env_file=None` 只关掉 .env 文件；环境变量仍会生效 ⇒ 显式把关键字段钉死，
    避免本机/CI 的 TEXTIN_* 环境变量把用例带偏（wuli 轮踩过：整个套件 401）。
    """
    base: dict[str, Any] = {"API_KEYS": "", "TOKEN": "", "ROTATE_XFF": False,
                            "ALLOW_UNVERIFIED": False, "QUOTA_COOLDOWN": 0.0}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------


def _png(w: int = 24, h: int = 16, rgb: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    """手写 PNG（zlib+struct，无第三方依赖）：`image_size` 能解析出真实宽高。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (struct.pack(">I", len(data)) + payload
                + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG_SMALL = _png()
#: 24x16 真 JPEG（636B，PIL 生成一次后固化 —— 测试不依赖 PIL）
JPEG_SMALL = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZG"
    "NywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09P"
    "T09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAQABgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAA"
    "AAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAk"
    "M2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKT"
    "lJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QA"
    "HwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdh"
    "cRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hp"
    "anN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDEooornPrwooooA//Z"
)
PDF_SMALL = (b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
             b"trailer<</Root 1 0 R>>\n%%EOF\n")
CSV_SMALL = "姓名,金额\n张三,1200.00\n".encode()


def _zip_container(entries: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


DOCX_SMALL = _zip_container({
    "[Content_Types].xml": b"<Types/>",
    "word/document.xml": b"<w:document/>",
})
XLSX_SMALL = _zip_container({
    "[Content_Types].xml": b"<Types/>",
    "xl/workbook.xml": b"<workbook/>",
})
PPTX_SMALL = _zip_container({
    "[Content_Types].xml": b"<Types/>",
    "ppt/presentation.xml": b"<p:presentation/>",
})
OFD_SMALL = _zip_container({
    "OFD.xml": b"<OFD/>",
    "Doc_0/Pages/Page_0/Content.xml": b"<Page/>",
})
#: OLE2 容器（.doc/.xls 的共用外壳；只对 magic，不做真文档）
OLE2_SMALL = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128


def data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------------------
# 上游回包构造
# ---------------------------------------------------------------------------


def envelope(result: Any, *, code: int = 200, msg: str = "success",
             request_id: str = "req-test") -> dict[str, Any]:
    return {"code": code, "msg": msg, "x_request_id": request_id,
            "data": {"result": result, "file_type": "", "file_data": ""}}


def error_envelope(code: int, msg: str, *, request_id: str = "req-err") -> dict[str, Any]:
    return {"code": code, "msg": msg, "x_request_id": request_id, "data": {}}


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)
