"""翻译层（纯函数）用例：出站请求构造 + 上游结果解析。

上游把产物塞在**四种**不同位置（result 是字符串 / dict.image / dict.image_list /
结构化对象），必须逐一兜住；解析不出来必须是**显式失败**（静默型失败最致命）。
"""

from __future__ import annotations

import base64
import json

import pytest

from app.errors import UpstreamUnavailableError
from app.models import lookup
from app.upstream.textin.capabilities import (
    build_call,
    extract_file,
    extract_images,
    extract_parsed,
)
from tests.helpers import DOCX_SMALL, JPEG_SMALL, PNG_SMALL, XLSX_SMALL, b64

B64_JPEG = b64(JPEG_SMALL)
B64_PNG = b64(PNG_SMALL)


# --------------------------------------------------------------------- 出站


def test_raw_channel_posts_original_bytes_and_real_mime():
    cap = lookup("textin:watermark-remove")
    call = build_call(cap, PNG_SMALL, "image/png")
    assert call.channel == "raw"
    assert call.content == PNG_SMALL
    assert call.content_type == "image/png"
    assert call.params == {"service": "watermark-remove"}


def test_table_excel_is_two_query_params():
    call = build_call(lookup("textin:table-excel"), PNG_SMALL, "image/png")
    assert call.params == {"service": "table", "excel": "1"}


def test_image_to_pdf_uses_json_channel():
    """全表唯一例外：body 是 JSON `{"files": ["<b64>"]}`（抓包实证）。"""
    call = build_call(lookup("textin:image-to-pdf"), PNG_SMALL, "image/png")
    assert call.channel == "json"
    assert call.content_type == "application/json"
    body = json.loads(call.content)
    assert list(body) == ["files"]
    assert base64.b64decode(body["files"][0]) == PNG_SMALL


# --------------------------------------------------------------------- 图片


def test_images_from_dict_image_key():
    got = extract_images({"image": B64_JPEG})
    assert got == [(JPEG_SMALL, "image/jpeg", ".jpg")]


def test_images_from_image_list():
    got = extract_images({"image_list": [{"image": B64_PNG, "origin_width": 900}]})
    assert got == [(PNG_SMALL, "image/png", ".png")]


def test_images_from_bare_string_and_list():
    assert extract_images(B64_PNG) == [(PNG_SMALL, "image/png", ".png")]
    assert extract_images([B64_PNG]) == [(PNG_SMALL, "image/png", ".png")]


def test_images_empty_and_structured_only_are_explicitly_empty():
    """`code=200` 但没有图 ⇒ 返回空列表，由服务层当显式失败（绝不猜）。"""
    assert extract_images({"is_risk": False, "risk_types": []}) == []
    assert extract_images(None) == []
    assert extract_images({}) == []
    assert extract_images("short") == []
    # 长得像 b64 但真身是文本 ⇒ 不能当图
    assert extract_images(b64(b"hello world " * 30)) == []


# --------------------------------------------------------------------- 文件


def test_file_pdf_ok():
    cap = lookup("textin:pdf-to-word")
    raw = extract_file(b64(DOCX_SMALL), cap)
    assert raw == DOCX_SMALL


def test_file_rejects_non_base64():
    cap = lookup("textin:pdf-to-word")
    with pytest.raises(UpstreamUnavailableError):
        extract_file({"image": "x"}, cap)
    with pytest.raises(UpstreamUnavailableError):
        extract_file("!!!not-base64!!!", cap)


def test_file_rejects_container_mismatch():
    """产物签字与能力声明的容器不符 ⇒ 拒绝交付（可能是错误页/上游行为变化）。"""
    pdf_bytes = b"%PDF-1.4\n%%EOF\n"
    docx_cap = lookup("textin:pdf-to-word")  # 期望 .docx（ZIP 容器）
    with pytest.raises(UpstreamUnavailableError):
        extract_file(b64(pdf_bytes), docx_cap)
    pdf_cap = lookup("textin:word-to-pdf")  # 期望 .pdf
    with pytest.raises(UpstreamUnavailableError):
        extract_file(b64(DOCX_SMALL), pdf_cap)


# --------------------------------------------------------------------- 解析


def test_parsed_passthrough_and_markdown_text():
    cap = lookup("textin:doc-parse")
    payload = {"markdown": "# 标题\n\n正文", "pages": [{"index": 0}]}
    got = extract_parsed(payload, cap)
    assert got.result is payload
    assert got.text == "# 标题\n\n正文"
    assert got.attachments == ()


def test_parsed_decodes_base64_markdown():
    """上游忘了带 `markdown_details=1` 时会回 b64 —— 恒**尝试**解码（长且纯 b64 才动）。"""
    cap = lookup("textin:doc-parse")
    text = "# 标题\n\n" + "正文内容。" * 80
    got = extract_parsed({"markdown": base64.b64encode(text.encode()).decode()}, cap)
    assert got.text == text


def test_parsed_plain_string_markdown_is_wrapped():
    cap = lookup("textin:doc-parse")
    got = extract_parsed("# 只有一段 markdown", cap)
    assert got.result == {"markdown": "# 只有一段 markdown"}
    assert got.text == "# 只有一段 markdown"


def test_parsed_attachments_decoded_for_table_excel():
    cap = lookup("textin:table-excel")
    got = extract_parsed({"tables": [], "excel": b64(XLSX_SMALL)}, cap)
    assert len(got.attachments) == 1
    att = got.attachments[0]
    assert (att.key, att.ext, att.mime) == ("excel", ".xlsx",
                                            "application/vnd.openxmlformats-"
                                            "officedocument.spreadsheetml.sheet")
    assert att.data == XLSX_SMALL


def test_parsed_attachments_skip_bad_container():
    cap = lookup("textin:table-excel")
    got = extract_parsed({"excel": b64(b"not a zip at all")}, cap)
    assert got.attachments == ()


def test_parsed_rejects_non_dict():
    cap = lookup("textin:manipulation-detection")
    with pytest.raises(UpstreamUnavailableError):
        extract_parsed(12345, cap)
