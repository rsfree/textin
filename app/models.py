"""能力注册表（本服务的唯一真相）。

**注册 = 本服务真的能跑通的能力。** 判断依据是 2026-09-11/12 的全量实测
（`reverse-proxy/textin/probe/`，18 条链路端到端产物在 `out2/`），不是页面菜单、
也不是名字看起来对不对。

三条纪律（与兄弟服务同源）：

1. **service 名必须查表，不能靠猜。** 上游命名无规律：`pdf-to-word`（kebab）、
   `pdf_to_markdown`（下划线）、`watermark-remove`、`text_recognize_3d1` 混用。
   `pdf2word` / `word2jpg` / `pdf-to-jpg` 这类"看起来合理"的名字**全部不存在**（400）。
2. **`&` 不许塞进 service 值。** `table&excel=1` 是**两个** query 参数
   （`service=table` + `excel=1`），塞进一个值里会被 httpx 转义成 `%26` ⇒ 上游 400。
3. **没跑通的不注册假能力**：`text_auto_removal` / `ofd-to-image` 登记但默认**不可用**
   （`verified=False` + 闸门），`/capabilities` 里给出原因与开启方式。

对外模型名 = `textin:<kebab>`，与 reverse-proxy 的 biz-api 逐字一致
（`textin:watermark-remove` 等），避免同一上游在两个服务里有两套名字。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

__all__ = [
    "Capability",
    "CAPABILITIES",
    "NOT_REGISTERED",
    "MODEL_RELEASED_AT",
    "OWNED_BY",
    "lookup",
    "by_family",
    "available",
    "all_names",
    "availability",
    "query_of",
]

Family = Literal["image", "convert", "parse"]
OutputKind = Literal["image", "file", "json"]

#: 输入家族（按**真实字节**嗅探得出，绝不信扩展名/Content-Type —— 上游按真实类型校验）
InputKind = Literal[
    "png", "jpeg", "webp", "bmp", "tiff", "gif",
    "pdf", "docx", "doc", "xlsx", "xls", "csv", "pptx", "ofd",
]

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_PDF_MIME = "application/pdf"
_ZIP_MIME = "application/zip"

#: 图像类输入的统一可接受集合（实测：PNG/JPEG/WEBP/BMP/TIFF 全部通过；GIF 未测 ⇒ 不收）
_IMAGE_KINDS: tuple[InputKind, ...] = ("png", "jpeg", "webp", "bmp", "tiff")
_IMAGE_OR_PDF: tuple[InputKind, ...] = (*_IMAGE_KINDS, "pdf")

#: `/v1/models` 的 `created` 字段：**本服务能力表的版本时间**，不是上游模型创建时间。
MODEL_RELEASED_AT = int(datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp())
OWNED_BY = "textin"


@dataclass(frozen=True)
class Capability:
    """一条能力：对外模型名 ↔ 上游 (`service` + 附加 query)。"""

    name: str                       # 对外模型名，如 textin:watermark-remove
    family: Family                  # 归属的端点族（image=图片面 / convert / parse）
    service: str                    # 上游 service 名（**逐字照抄实测，不猜**）
    #: 附加 query（顺序固定）。目前只有 `table&excel=1` 与 `pdf_to_markdown` 两组。
    service_params: tuple[tuple[str, str], ...] = ()
    #: 可接受的输入家族（resolve 时按嗅探结果校验，不在集合内 ⇒ 400 并说清期望）
    accepts: tuple[InputKind, ...] = ()
    output: OutputKind = "json"
    output_ext: str = ""            # file 族：结果文件扩展名
    output_mime: str = ""           # file 族：结果文件 MIME
    #: json 族里需要解码成文件的 result 键 → (key, ext, mime)
    b64_attachments: tuple[tuple[str, str, str], ...] = ()
    #: 是否已端到端跑通（False = 登记但默认被闸门挡住，见 availability()）
    verified: bool = True
    evidence: str = ""              # 取证出处（实测产物文件名）
    notes: str = ""

    def params(self) -> dict[str, str]:
        """发给上游的完整 query（含 `service`）。"""
        return {"service": self.service, **dict(self.service_params)}


CAPABILITIES: dict[str, Capability] = {
    # ------------------------------------------------------------- 图像族（图进图出）
    "textin:watermark-remove": Capability(
        name="textin:watermark-remove",
        family="image",
        service="watermark-remove",
        accepts=_IMAGE_KINDS,
        output="image",
        evidence="out2/wm_sample_watermark-remove.jpg（另有 n=25 的延迟基准）",
        notes="结果**恒 JPEG**、与输入同分辨率；RGBA 透明输入会丢 alpha（透明区被合成）",
    ),
    "textin:crop-enhance": Capability(
        name="textin:crop-enhance",
        family="image",
        service="crop_enhance_image",
        accepts=_IMAGE_OR_PDF,
        output="image",
        evidence="out2/wm_sample_crop_enhance_image.jpg",
        notes="输出会被**裁边**（实测 900x600 → 896x596），不是逐像素对齐",
    ),
    "textin:demoire": Capability(
        name="textin:demoire",
        family="image",
        service="demoire",
        accepts=_IMAGE_OR_PDF,
        output="image",
        evidence="out2/wm_sample_demoire.jpg",
        notes="去屏幕纹（摩尔纹）",
    ),
    "textin:text-auto-removal": Capability(
        name="textin:text-auto-removal",
        family="image",
        service="text_auto_removal",
        accepts=_IMAGE_KINDS,
        output="image",
        evidence=(
            "tx_wm.py 记录其响应形态为 result.image；枚举探测时该 service 匿名响应正常；"
            "2026-09-24 用户拍板放开"
        ),
        notes=(
            "「自动擦除手写文字」。⚠️ 证据强度**低于**其余 18 条 —— out2/ 里没有它的端到端产物。"
            "判据是 ① tx_wm.py 的记录（result.image）；② 枚举探测时该 service 在"
            "**匿名宽松配额档**正常返回。若上游结果形态有变，extract 会**显式失败**而非静默"
        ),
    ),
    # ------------------------------------------------------------- 转换族（文件进文件出）
    "textin:pdf-to-word": Capability(
        name="textin:pdf-to-word",
        family="convert", service="pdf-to-word",
        accepts=("pdf",), output="file", output_ext=".docx", output_mime=_DOCX_MIME,
        evidence="out2/test_pdf-to-word.docx（ZIP 内含 word/document.xml）",
    ),
    "textin:pdf-to-excel": Capability(
        name="textin:pdf-to-excel",
        family="convert", service="pdf-to-excel",
        accepts=("pdf",), output="file", output_ext=".xlsx", output_mime=_XLSX_MIME,
        evidence="out2/test_pdf-to-excel.xlsx",
    ),
    "textin:pdf-to-ppt": Capability(
        name="textin:pdf-to-ppt",
        family="convert", service="pdf-to-ppt",
        accepts=("pdf",), output="file", output_ext=".pptx", output_mime=_PPTX_MIME,
        evidence="out2/test_pdf-to-ppt.pptx（ZIP，17 entries）",
    ),
    "textin:pdf-to-image": Capability(
        name="textin:pdf-to-image",
        family="convert", service="pdf-to-image",
        accepts=("pdf",), output="file", output_ext=".zip", output_mime=_ZIP_MIME,
        evidence="out2/test_pdf-to-image.zip（ZIP 内含 1.jpg…）",
        notes="⚠️ 是 `pdf-to-image`，**不是** `pdf-to-jpg`（那个 400）",
    ),
    "textin:word-to-pdf": Capability(
        name="textin:word-to-pdf",
        family="convert", service="word-to-pdf",
        accepts=("docx", "doc"), output="file", output_ext=".pdf", output_mime=_PDF_MIME,
        evidence="out2/test_word-to-pdf.pdf（%PDF-1.7）",
    ),
    "textin:word-to-image": Capability(
        name="textin:word-to-image",
        family="convert", service="word-to-image",
        accepts=("docx", "doc"), output="file", output_ext=".zip", output_mime=_ZIP_MIME,
        evidence="out2/test_word-to-image.zip",
        notes="⚠️ 是 `word-to-image`，**不是** `word-to-jpg`；全表最慢（实测 7.0s）",
    ),
    "textin:excel-to-pdf": Capability(
        name="textin:excel-to-pdf",
        family="convert", service="excel-to-pdf",
        accepts=("xlsx", "xls", "csv"), output="file", output_ext=".pdf", output_mime=_PDF_MIME,
        evidence="out2/test_excel-to-pdf.pdf",
        notes="也接受 CSV（实测通过）；收到 PDF 会回 400「服务器内部错误」",
    ),
    "textin:image-to-pdf": Capability(
        name="textin:image-to-pdf",
        family="convert", service="image-to-pdf",
        accepts=_IMAGE_KINDS, output="file", output_ext=".pdf", output_mime=_PDF_MIME,
        evidence="out2/wm_sample_image-to-pdf.pdf",
        notes="❗全表唯一例外：body 是 JSON `{\"files\":[\"<b64>\"]}`，不是裸字节（抓包实证）",
    ),
    "textin:ofd-to-image": Capability(
        name="textin:ofd-to-image",
        family="convert", service="ofd-to-image",
        accepts=("ofd",), output="file", output_ext=".zip", output_mime=_ZIP_MIME,
        verified=False,
        evidence=(
            "**未取证（无产物）**：只做过错类型探测（传 PDF 回 40303「文件类型不支持」，"
            "证明路由存在）；缺 .ofd 样本，从未端到端跑过"
        ),
        notes=(
            "服务**存在**（见 evidence），但输出是否真是 ZIP、扩展名对不对都未知 ⇒ "
            "默认门禁，要放开请设 TEXTIN_ALLOW_UNVERIFIED=1（结果可能不是 zip，自担）"
        ),
    ),
    # ------------------------------------------------------------- 解析族（文件进结构化出）
    "textin:text-recognize": Capability(
        name="textin:text-recognize",
        family="parse", service="text_recognize_3d1",
        accepts=_IMAGE_KINDS,
        evidence="out2/wm_sample_text_recognize_3d1.json.json",
        notes="result.lines[]（text/pos/score）+ angle/width/height",
    ),
    "textin:table": Capability(
        name="textin:table",
        family="parse", service="table", service_params=(("excel", "0"),),
        accepts=_IMAGE_KINDS,
        evidence="out2/wm_sample_table.zip（服务端返回的表结构）",
        notes="result.tables[]（table_cells/rows/cols）",
    ),
    "textin:table-excel": Capability(
        name="textin:table",
        family="parse", service="table", service_params=(("excel", "1"),),
        accepts=_IMAGE_KINDS,
        b64_attachments=(("excel", ".xlsx", _XLSX_MIME),),
        evidence="out2/wm_sample_table.xlsx（result.excel 解出的 xlsx）",
        notes="与 textin:table 同 service，多一个 `excel=1` ⇒ result.excel 是 base64 xlsx",
    ),
    "textin:bill-recognize": Capability(
        name="textin:bill-recognize",
        family="parse", service="bill_recognize_v2",
        accepts=_IMAGE_OR_PDF,
        evidence="out2/wm_sample_bill_recognize_v2.json.json",
        notes="result.pages[]；上游文档称也接受 OFD，未取证故本服务不收",
    ),
    "textin:manipulation-detection": Capability(
        name="textin:manipulation-detection",
        family="parse", service="manipulation_detection",
        accepts=_IMAGE_OR_PDF,
        evidence="out2/wm_sample_manipulation_detection.json.json",
        notes="result.{is_risk, risk_types, image_width, image_height, image_property}（无图）",
    ),
    "textin:recognize-stamp": Capability(
        name="textin:recognize-stamp",
        family="parse", service="recognize_stamp",
        accepts=_IMAGE_OR_PDF,
        evidence="out2/wm_sample_recognize_stamp.json",
        notes="result.details.stamp[] + type/angle",
    ),
    "textin:doc-parse": Capability(
        name="textin:doc-parse",
        family="parse", service="pdf_to_markdown",
        service_params=(("markdown_details", "1"),),
        accepts=_IMAGE_OR_PDF,
        evidence="out2/test_pdf_to_markdown.md",
        notes=(
            "`result.markdown` **默认是 base64**，带 `markdown_details=1` 才是明文 —— "
            "本服务固定带该参数（抓包实证的坑，见 docs/UPSTREAM.md §3）"
        ),
    ),
    "textin:finance-report": Capability(
        name="textin:finance-report",
        family="parse", service="pdf_to_markdown",
        service_params=(
            ("page_start", "0"), ("page_count", "200"), ("dpi", "144"),
            ("parse_mode", "auto"), ("table_flavor", "html"),
            ("apply_document_tree", "1"), ("markdown_details", "1"),
        ),
        accepts=("pdf",),
        evidence="浏览器抓包实证的参数组（reverse-proxy 契约 §2.5）",
        notes="**不是独立 service**，就是文档解析的固定参数预设；结构化表格由调用方从 markdown 二次抽取",
    ),
}


def all_names() -> list[str]:
    return sorted(CAPABILITIES)


def by_family(family: Family) -> dict[str, Capability]:
    return {k: v for k, v in CAPABILITIES.items() if v.family == family}


def lookup(name: str) -> Capability:
    try:
        return CAPABILITIES[name]
    except KeyError as exc:
        known = "、".join(all_names())
        raise KeyError(f"未注册的模型 {name!r}；已知：{known}") from exc


def availability(cap: Capability, *, allow_unverified: bool) -> tuple[bool, str]:
    """该能力在本部署是否可用；不可用时给出**原因 + 开启方式**（空串 = 可用）。"""
    if cap.verified:
        return True, ""
    if allow_unverified:
        return True, ""
    return False, (
        f"{cap.name} 的返回体**未端到端校验**（{cap.notes}）；"
        f"要放开请设 TEXTIN_ALLOW_UNVERIFIED=1（自担，结果可能是意外形态）"
    )


def available(*, allow_unverified: bool) -> dict[str, Capability]:
    """本部署当前**真正可调用**的能力（= /v1/models 的依据）。"""
    return {
        k: v for k, v in CAPABILITIES.items()
        if availability(v, allow_unverified=allow_unverified)[0]
    }


def query_of(cap: Capability) -> dict[str, str]:
    """上游完整 query（别名，便于调用点自解释）。"""
    return cap.params()


#: 站点上**存在但本服务刻意不注册**的 service（每一条写清「差什么才注册」）。
#: 与 `verified=False`（已登记、门禁）不同：这些连注册都没有 —— 没有可交付的行为。
NOT_REGISTERED: dict[str, str] = {
    "dewarp": (
        "站点有独立 SEO 页与 service 名，但 `/image_processing/dewarp` **308 重定向到** "
        "`crop_enhance_image`，且裸名调用返回 400「缺少必要参数」⇒ 它与 crop_enhance_image "
        "不是同一个调用（需要未知参数）。差：一次带参数的实测取证。"
    ),
    "image_quality_inspect": (
        "「图像质量检测」（完整度/光斑/模糊）。2026-09-11 枚举判定为**付费档配额**"
        "（匿名可用次数很紧），返回形态未取证（疑似纯结构化、无图）。"
        "差：一次成功的实测（拿到 result 形态）。"
    ),
    "recognize-document-3d1-multipage": (
        "2026-09-24 实读 bundle 发现：与 `text_recognize_3d1` 并列出现的多页文档识别 id。"
        "差：取证它是不是独立 service（以及调用形态）。"
    ),
}
