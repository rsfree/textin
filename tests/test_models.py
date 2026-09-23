"""注册表门禁：**冻结** service 词表与族划分。

注册表是"上游契约"在本仓的唯一投影 —— 这里断言的不是代码风格，
而是「我们对外承诺的能力集合没有悄悄漂移」。改注册表 = 改契约，
必须同步 `docs/INTERFACE.md` 与这些断言。
"""

from __future__ import annotations

from app import models
from app.upstream.textin.capabilities import build_call

#: 冻结表：(对外模型名 → (族, service 名, 附加 query))。**逐字**来自实测取证。
FROZEN: dict[str, tuple[str, str, dict[str, str]]] = {
    # 图像族
    "textin:watermark-remove": ("image", "watermark-remove", {}),
    "textin:crop-enhance": ("image", "crop_enhance_image", {}),
    "textin:demoire": ("image", "demoire", {}),
    "textin:text-auto-removal": ("image", "text_auto_removal", {}),
    # 转换族
    "textin:pdf-to-word": ("convert", "pdf-to-word", {}),
    "textin:pdf-to-excel": ("convert", "pdf-to-excel", {}),
    "textin:pdf-to-ppt": ("convert", "pdf-to-ppt", {}),
    "textin:pdf-to-image": ("convert", "pdf-to-image", {}),
    "textin:word-to-pdf": ("convert", "word-to-pdf", {}),
    "textin:word-to-image": ("convert", "word-to-image", {}),
    "textin:excel-to-pdf": ("convert", "excel-to-pdf", {}),
    "textin:image-to-pdf": ("convert", "image-to-pdf", {}),
    "textin:ofd-to-image": ("convert", "ofd-to-image", {}),
    # 解析族
    "textin:text-recognize": ("parse", "text_recognize_3d1", {}),
    "textin:table": ("parse", "table", {"excel": "0"}),
    "textin:table-excel": ("parse", "table", {"excel": "1"}),
    "textin:bill-recognize": ("parse", "bill_recognize_v2", {}),
    "textin:manipulation-detection": ("parse", "manipulation_detection", {}),
    "textin:recognize-stamp": ("parse", "recognize_stamp", {}),
    "textin:doc-parse": ("parse", "pdf_to_markdown", {"markdown_details": "1"}),
    "textin:finance-report": ("parse", "pdf_to_markdown", {
        "page_start": "0", "page_count": "200", "dpi": "144", "parse_mode": "auto",
        "table_flavor": "html", "apply_document_tree": "1", "markdown_details": "1",
    }),
}


def test_registry_matches_frozen_table() -> None:
    assert set(models.CAPABILITIES) == set(FROZEN)
    for name, (family, service, params) in FROZEN.items():
        cap = models.CAPABILITIES[name]
        assert cap.family == family, name
        assert cap.service == service, name
        assert dict(cap.service_params) == params, name
        assert cap.params()["service"] == service, name


def test_no_service_value_contains_ampersand() -> None:
    """`table&excel=1` 必须是**两个** query 参数 —— 塞进 service 值会被转义成 %26（上游 400）。"""
    for cap in models.CAPABILITIES.values():
        assert "&" not in cap.service
        assert "&" not in str(cap.service_params)


def test_family_counts() -> None:
    assert len(models.by_family("image")) == 4
    assert len(models.by_family("convert")) == 9
    assert len(models.by_family("parse")) == 8


def test_only_ofd_is_gated() -> None:
    """未取证闸门当前**只**关 ofd-to-image（text_auto_removal 已按用户决定放开）。"""
    gated = {n for n, c in models.CAPABILITIES.items() if not c.verified}
    assert gated == {"textin:ofd-to-image"}
    assert len(models.available(allow_unverified=False)) == 20
    assert len(models.available(allow_unverified=True)) == 21


def test_gated_reason_mentions_how_to_open() -> None:
    cap = models.CAPABILITIES["textin:ofd-to-image"]
    ok, reason = models.availability(cap, allow_unverified=False)
    assert not ok and "TEXTIN_ALLOW_UNVERIFIED" in reason
    ok2, reason2 = models.availability(cap, allow_unverified=True)
    assert ok2 and reason2 == ""


def test_every_capability_has_evidence() -> None:
    """每条能力都必须写清取证出处 —— 没有证据的能力不允许出现在表里。"""
    for name, cap in models.CAPABILITIES.items():
        assert cap.evidence.strip(), name
        assert cap.accepts, name


def test_accepts_use_known_input_kinds() -> None:
    known = set(models.InputKind.__args__)  # type: ignore[attr-defined]
    for name, cap in models.CAPABILITIES.items():
        unknown = set(cap.accepts) - known
        assert not unknown, f"{name}: {unknown}"


def test_convert_family_declares_output_container() -> None:
    for name, cap in models.by_family("convert").items():
        assert cap.output == "file", name
        assert cap.output_ext.startswith(".") and cap.output_mime, name


def test_image_to_pdf_is_the_only_json_channel() -> None:
    """全表唯一走 JSON 通道的能力 —— 由翻译层断言（这里只钉住"唯一"这个事实）。"""
    for name, cap in models.CAPABILITIES.items():
        call = build_call(cap, b"%PDF-1.4\n%%EOF\n" if cap.accepts == ("pdf",) else b"x",
                          "application/pdf" if "pdf" in cap.accepts else "image/png")
        if name == "textin:image-to-pdf":
            assert call.channel == "json"
        else:
            assert call.channel == "raw", name


def test_not_registered_has_reasons() -> None:
    assert {"dewarp", "image_quality_inspect"} <= set(models.NOT_REGISTERED)
    for key, reason in models.NOT_REGISTERED.items():
        assert "差" in reason, key  # 每条都要写清「差什么才注册」
