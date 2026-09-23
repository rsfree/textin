#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假上游（stdlib、零依赖）：按 `service` 返回与真实上游同形的信封。

用途有两个，都是**零真实上游请求**：

1. `scripts/smoke.sh`：真起服务 + 真 HTTP + 假上游 ⇒ 端到端冒烟；
2. `scripts/probe.py --phases loop`：对全部能力跑一遍"请求→响应装配"回路。

它还记录**服务真正发过来的东西**（service / query / content-type / body 长度与头部字节），
经 `GET /mock/log` 读回 —— 这样冒烟能把"链路两端都验证了"，而不只是"服务没报错"。

用法：`python scripts/mock_upstream.py [port]`（默认 8931）
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.parse import parse_qs, urlparse
from zipfile import ZIP_DEFLATED, ZipFile

# --------------------------------------------------------------------------- 样本


def _png(w: int = 24, h: int = 16) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (struct.pack(">I", len(data)) + payload
                + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes((10, 120, 200)) * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


PNG = _png()
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
DOCX = _zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"})
XLSX = _zip({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<workbook/>"})
PPTX = _zip({"[Content_Types].xml": b"<Types/>", "ppt/presentation.xml": b"<p:presentation/>"})
ZIP = _zip({"1.jpg": PNG, "2.jpg": PNG})


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


#: service → result（与真实上游的形态**逐类同形**，见 docs/UPSTREAM.md §3）
RESULT_BY_SERVICE: dict[str, object] = {
    # 图像族：图在 dict 里
    "watermark-remove": {"image": _b64(PNG)},
    "demoire": {"image": _b64(PNG)},
    "text_auto_removal": {"image": _b64(PNG)},
    "crop_enhance_image": {"image_list": [{"image": _b64(PNG), "origin_width": 24}]},
    # 转换族：整份文件是 base64 字符串
    "pdf-to-word": _b64(DOCX),
    "pdf-to-excel": _b64(XLSX),
    "pdf-to-ppt": _b64(PPTX),
    "pdf-to-image": _b64(ZIP),
    "word-to-pdf": _b64(PDF),
    "word-to-image": _b64(ZIP),
    "excel-to-pdf": _b64(PDF),
    "image-to-pdf": _b64(PDF),
    "ofd-to-image": _b64(ZIP),
    # 解析族：结构化
    "text_recognize_3d1": {"lines": [{"text": "hello", "pos": [[0, 0], [1, 0]], "score": 0.99}],
                           "angle": 0, "width": 24, "height": 16},
    "table": {"tables": [{"rows": 2, "cols": 2}]},
    "bill_recognize_v2": {"pages": [{"index": 0}]},
    "manipulation_detection": {"is_risk": False, "risk_types": [],
                               "image_width": 24, "image_height": 16},
    "recognize_stamp": {"details": {"stamp": [{"type": "round", "angle": 0}]}},
    "pdf_to_markdown": {"markdown": "# 标题\n\n假上游生成的 markdown。", "pages": [{"index": 0}]},
}

#: 带 `excel=1` 的表格识别多一个附件
_TABLE_WITH_EXCEL = {"tables": [{"rows": 2, "cols": 2}], "excel": _b64(XLSX)}

_LOG: list[dict] = []
_LOCK = threading.Lock()

#: 失败场景：`MOCK_SCENARIO="<service>:<code>"` ⇒ 该 service 一律回该 code。
#: 例：`MOCK_SCENARIO=crop_enhance_image:431`（冒烟用它验证配额闸门）。
_SCENARIO: tuple[str, int] | None = None
_env = os.environ.get("MOCK_SCENARIO", "").strip()
if ":" in _env:
    _svc, _, _code = _env.partition(":")
    try:
        _SCENARIO = (_svc.strip(), int(_code))
    except ValueError:
        _SCENARIO = None


def _result_for(service: str, query: dict[str, list[str]]) -> object:
    if service == "table" and (query.get("excel") or ["0"])[0] == "1":
        return _TABLE_WITH_EXCEL
    return RESULT_BY_SERVICE.get(service, {"unexpected_service": service})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:  # 静默（冒烟输出要干净）
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path.startswith("/mock/log"):
            with _LOCK:
                self._send(200, {"entries": list(_LOG)})
            return
        if self.path.startswith("/mock/health"):
            self._send(200, {"ok": True, "services": len(RESULT_BY_SERVICE)})
            return
        self._send(404, {"error": "unknown path"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        service = (query.get("service") or [""])[0]
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""

        entry = {
            "path": parsed.path,
            "service": service,
            "query": {k: v[0] for k, v in query.items()},
            "content_type": (self.headers.get("content-type") or ""),
            "token_header": self.headers.get("token"),
            "xff": self.headers.get("x-forwarded-for"),
            "content_length": len(body),
            "body_head": body[:8].hex(),
        }
        with _LOCK:
            _LOG.append(entry)

        # 失败场景开关（env `MOCK_SCENARIO=<service>:<code>`，不改默认行为）
        if _SCENARIO and service == _SCENARIO[0]:
            code = _SCENARIO[1]
            self._send(200, {"code": code, "msg": f"mock scenario {code}",
                             "x_request_id": f"mock-{code}", "data": {}})
            return
        if service not in RESULT_BY_SERVICE:
            self._send(200, {"code": 400, "msg": "缺少必要参数或参数值不正确",
                             "x_request_id": "mock-400", "data": {}})
            return
        self._send(200, {"code": 200, "msg": "success", "x_request_id": "mock-ok",
                         "data": {"result": _result_for(service, query),
                                  "file_type": "", "file_data": ""}})


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8931
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock upstream on http://127.0.0.1:{port} （{len(RESULT_BY_SERVICE)} 个 service）")
    server.serve_forever()


if __name__ == "__main__":
    main()
