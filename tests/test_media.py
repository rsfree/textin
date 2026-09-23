"""媒体层用例：类型嗅探 / 输入归一 / 落盘 / 尺寸探测。

核心纪律：**按真实字节判定**，扩展名与 Content-Type 只作参考 ——
上游按 Content-Type 校验，写错直接拒（`.jpg` 名字 + 非标 mime + 真身 PNG 的坑）。
"""

from __future__ import annotations

import pytest

from app.errors import ApiError
from app.media import image_size, resolve_input, save_media, sniff
from tests.helpers import (
    CSV_SMALL,
    DOCX_SMALL,
    JPEG_SMALL,
    OFD_SMALL,
    OLE2_SMALL,
    PDF_SMALL,
    PNG_SMALL,
    PPTX_SMALL,
    XLSX_SMALL,
    arun,
    b64,
    data_uri,
)

IMAGE_ACCEPTS = ("png", "jpeg", "webp", "bmp", "tiff")
PDF_ACCEPTS = ("pdf",)
DOCX_ACCEPTS = ("docx", "doc")


def test_sniff_core_formats():
    assert sniff(PNG_SMALL) == "png"
    assert sniff(JPEG_SMALL) == "jpeg"
    assert sniff(PDF_SMALL) == "pdf"
    assert sniff(DOCX_SMALL) == "zip"
    assert sniff(OLE2_SMALL) == "ole2"
    assert sniff(CSV_SMALL) == "text"
    assert sniff(b"\x00\x01\x02\x03") == "unknown"


def test_zip_is_refined_by_inner_layout():
    """docx/xlsx/pptx/ofd 都是 ZIP —— 靠内层目录名区分。"""
    asr = arun(resolve_input(data_uri(DOCX_SMALL, "application/zip"), accepts=DOCX_ACCEPTS,
                             max_bytes=1 << 20))
    assert asr.kind == "docx" and asr.mime.endswith("wordprocessingml.document")
    xlsx = arun(resolve_input(data_uri(XLSX_SMALL), accepts=("xlsx", "xls", "csv"),
                              max_bytes=1 << 20))
    assert xlsx.kind == "xlsx"
    pptx = arun(resolve_input(data_uri(PPTX_SMALL), accepts=("pptx",), max_bytes=1 << 20))
    assert pptx.kind == "pptx"
    ofd = arun(resolve_input(data_uri(OFD_SMALL), accepts=("ofd",), max_bytes=1 << 20))
    assert ofd.kind == "ofd"


def test_ole2_maps_to_expected_kind():
    word = arun(resolve_input(data_uri(OLE2_SMALL), accepts=("docx", "doc"), max_bytes=1 << 20))
    assert word.kind == "doc"
    excel = arun(resolve_input(data_uri(OLE2_SMALL), accepts=("xlsx", "xls", "csv"),
                               max_bytes=1 << 20))
    assert excel.kind == "xls"
    # 两个都不接受时不得硬猜
    with pytest.raises(ApiError):
        arun(resolve_input(data_uri(OLE2_SMALL), accepts=("pdf",), max_bytes=1 << 20))


def test_csv_only_accepted_when_declared():
    ok = arun(resolve_input(b64(CSV_SMALL), accepts=("xlsx", "xls", "csv"), max_bytes=1 << 20))
    assert ok.kind == "csv" and ok.mime == "text/csv"
    # 换成不接受 csv 的能力 ⇒ 文本不可认（CSV 没有 magic，只在声明接受时才成立）
    with pytest.raises(ApiError):
        arun(resolve_input(b64(CSV_SMALL), accepts=PDF_ACCEPTS, max_bytes=1 << 20))
    # **裸文本**不是受支持的输入形态（三种形态：data URI / URL / base64）——
    # 带逗号的 CSV 明文会被明确拒绝，而不是猜着解码
    with pytest.raises(ApiError) as ei:
        arun(resolve_input(CSV_SMALL.decode("utf-8"), accepts=("xlsx", "xls", "csv"),
                           max_bytes=1 << 20))
    assert ei.value.code == "invalid_input_format"


def test_data_uri_mime_mismatch_is_warned_not_trusted():
    """data URI 的 mime 只是声明；真实类型以嗅探为准，不符要留痕。"""
    warnings: list[str] = []
    got = arun(resolve_input(
        data_uri(PNG_SMALL, "image/jpg"),  # 非标且错误
        accepts=IMAGE_ACCEPTS, max_bytes=1 << 20, warnings=warnings,
    ))
    assert got.kind == "png" and got.mime == "image/png"
    assert any("image/jpg" in w for w in warnings)


def test_bare_base64_roundtrip():
    got = arun(resolve_input(b64(PNG_SMALL), accepts=IMAGE_ACCEPTS, max_bytes=1 << 20))
    assert got.data == PNG_SMALL and got.source == "base64"


def test_url_input_uses_fetch_and_sniffs():
    calls: list[str] = []

    async def fake_fetch(url: str, limit: int) -> tuple[bytes, str | None]:
        calls.append(url)
        return PNG_SMALL, "text/html"  # 声明错，真实是 png

    warnings: list[str] = []
    got = arun(resolve_input("https://example.invalid/a.bin", accepts=IMAGE_ACCEPTS,
                             fetch=fake_fetch, max_bytes=1 << 20, warnings=warnings))
    assert calls == ["https://example.invalid/a.bin"]
    assert got.kind == "png" and got.source == "url"
    assert any("代取" in w for w in warnings)
    assert any("text/html" in w for w in warnings)


def test_url_without_fetch_is_rejected():
    with pytest.raises(ApiError) as ei:
        arun(resolve_input("https://example.invalid/a.png", accepts=IMAGE_ACCEPTS,
                           max_bytes=1 << 20))
    assert ei.value.code == "url_input_unsupported"


def test_rejections_say_what_was_sniffed():
    with pytest.raises(ApiError) as ei:
        arun(resolve_input(data_uri(PDF_SMALL, "application/pdf"), accepts=IMAGE_ACCEPTS,
                           max_bytes=1 << 20))
    assert ei.value.code == "input_kind_not_accepted"
    assert ei.value.extra["sniffed_kind"] == "pdf"
    with pytest.raises(ApiError) as ei2:
        arun(resolve_input("这不是任何合法形态的输入串", accepts=IMAGE_ACCEPTS,
                           max_bytes=1 << 20))
    assert ei2.value.code == "invalid_input_format"
    with pytest.raises(ApiError) as ei3:
        arun(resolve_input("", accepts=IMAGE_ACCEPTS, max_bytes=1 << 20))
    assert ei3.value.code == "missing_input"


def test_save_media_is_content_addressed(tmp_path):
    name1 = save_media(PNG_SMALL, tmp_path, ".png")
    name2 = save_media(PNG_SMALL, tmp_path, ".png")
    assert name1 == name2 and name1.endswith(".png")
    assert (tmp_path / name1).read_bytes() == PNG_SMALL
    assert len(list(tmp_path.iterdir())) == 1  # 同内容只落一份


def test_image_size_reads_real_pixels():
    assert image_size(PNG_SMALL) == (24, 16)
    assert image_size(JPEG_SMALL) == (24, 16)
    assert image_size(PDF_SMALL) is None
    assert image_size(b"\x00" * 40) is None
