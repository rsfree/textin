"""对外接口用例：三族端点、dry_run、鉴权、门禁、错误信封、配额静默窗。

注入方式与 pavo 一致：`with TestClient(app) as c:` **起完生命周期后**把上游客户端
换成走 MockTransport 的实例 —— 这样连"服务自己 new 的真 client"这条假绿路径也被堵住。
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import CAPABILITIES
from app.observability import spans
from app.upstream.textin import TextinClient
from tests.helpers import (
    DOCX_SMALL,
    JPEG_SMALL,
    OFD_SMALL,
    PDF_SMALL,
    PNG_SMALL,
    XLSX_SMALL,
    b64,
    data_uri,
    envelope,
    error_envelope,
    json_response,
    settings,
)


def _app(tmp_path, **overrides: Any):
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    conf = settings(MEDIA_DIR=str(media), **overrides)
    return create_app(conf), media


def _inject(app, handler, **overrides: Any) -> None:
    conf = app.state.settings.model_copy(update=overrides) if overrides else app.state.settings
    app.state.client = TextinClient(conf, transport=httpx.MockTransport(handler))


def _ok_image(_req: httpx.Request) -> httpx.Response:
    return json_response(envelope({"image": b64(JPEG_SMALL)}))


# --------------------------------------------------------------------- 运维面


def test_healthz(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/healthz").json()
    assert body["status"] == "ok" and body["version"]


def test_readyz_reports_anonymous_and_capability_counts(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/readyz").json()
    assert body["status"] == "ok"
    checks = body["checks"]
    assert checks["upstream_auth"] == "anonymous"
    assert checks["xff_rotation"] is False
    assert checks["egress_proxies"] == []          # 默认直连（池为空）
    assert checks["capabilities"] == {"total": 21, "available": 20}
    assert checks["quota_window"]["cooling"] is False


# --------------------------------------------------------------------- 鉴权


def test_auth_enforced_but_models_is_public(tmp_path):
    app, _ = _app(tmp_path, API_KEYS="sk-a,sk-b")
    with TestClient(app) as c:
        # 发现端点免鉴权（与 jimeng 一致）
        assert c.get("/v1/models").status_code == 200
        # 执行端点要 Key
        r1 = c.post("/v1/images/generations", json={"model": "textin:watermark-remove",
                                                    "image": data_uri(PNG_SMALL)})
        assert r1.status_code == 401 and r1.json()["error"]["code"] == "unauthorized"
        r2 = c.post("/v1/images/generations",
                    headers={"authorization": "Bearer wrong"},
                    json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
        assert r2.status_code == 401
        # 运维面也要 Key
        assert c.get("/capabilities").status_code == 401
        assert c.get("/capabilities", headers={"authorization": "Bearer sk-b"}).status_code == 200
        # 正确 Key 才放行（下游直接打上游，这里换成 mock）
        _inject(app, _ok_image)
        r3 = c.post("/v1/images/generations",
                    headers={"authorization": "Bearer sk-b"},
                    json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
        assert r3.status_code == 200


# --------------------------------------------------------------------- 发现面


def test_models_lists_only_available(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    assert "textin:ofd-to-image" not in ids
    assert "textin:watermark-remove" in ids and "textin:pdf-to-word" in ids
    assert len(ids) == 20


def test_models_includes_unverified_when_gate_open(tmp_path):
    app, _ = _app(tmp_path, ALLOW_UNVERIFIED=True)
    with TestClient(app) as c:
        ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    assert "textin:ofd-to-image" in ids and len(ids) == 21


def test_models_single_retrieve_is_not_exposed(tmp_path):
    """契约上**没有** `GET /v1/models/{model}`（2026-09-24 收敛端点面后的回归钉子）。

    它必须 404 且**不能**被误路由到列表端点（`/v1/models` 是精确路径）。
    """
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/v1/models/textin:demoire")
    assert r.status_code == 404


def test_capabilities_explains_absences(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/capabilities").json()
    assert any(m["id"] == "textin:watermark-remove" for m in body["models"])
    gated = [m for m in body["not_available"] if m["id"] == "textin:ofd-to-image"]
    assert gated and "TEXTIN_ALLOW_UNVERIFIED" in gated[0]["reason"]
    assert "dewarp" in body["not_registered"]


# --------------------------------------------------------------------- 图像族


def test_images_happy_path(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, _ok_image)
        r = c.post("/v1/images/generations",
                   json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
    assert r.status_code == 200
    body = r.json()
    item = body["data"][0]
    assert base64.b64decode(item["b64_json"]) == JPEG_SMALL
    assert item["mime"] == "image/jpeg"
    assert item["size"] == "24x16"
    assert body["model"] == "textin:watermark-remove"
    assert body["upstream"]["service"] == "watermark-remove"
    assert body["effective"]["input"]["kind"] == "png"
    assert body["usage"] == {"generated_images": 1}


def test_images_rejects_wrong_input_kind(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/v1/images/generations",
                   json={"model": "textin:watermark-remove", "image": data_uri(PDF_SMALL)})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "input_kind_not_accepted" and err["sniffed_kind"] == "pdf"


def test_images_silent_upstream_failure_is_502(tmp_path):
    """code=200 但没图 ⇒ 显式失败（静默型失败最致命）。"""

    def no_image(req: httpx.Request) -> httpx.Response:
        return json_response(envelope({"lines": [{"text": "hi"}]}))

    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, no_image)
        r = c.post("/v1/images/generations",
                   json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
    assert r.status_code == 502
    assert "未返回图片" in r.json()["error"]["message"]


# --------------------------------------------------------------------- 转换族


def test_convert_happy_path_names_output_from_input(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, lambda req: json_response(envelope(b64(DOCX_SMALL))))
        r = c.post("/v1/files/convert", json={
            "model": "textin:pdf-to-word", "file": data_uri(PDF_SMALL),
            "filename": "合同.pdf",
        })
    assert r.status_code == 200
    item = r.json()["data"][0]
    assert base64.b64decode(item["b64_json"]) == DOCX_SMALL
    assert item["filename"] == "合同.docx"
    assert item["mime"].endswith("wordprocessingml.document")


def test_convert_container_mismatch_is_502(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, lambda req: json_response(envelope(b64(PNG_SMALL))))  # 期望 docx 却给了 PNG
        r = c.post("/v1/files/convert", json={"model": "textin:pdf-to-word",
                                              "file": data_uri(PDF_SMALL)})
    assert r.status_code == 502
    assert "容器不符" in r.json()["error"]["message"]


# --------------------------------------------------------------------- 解析族


def test_parse_passthrough(tmp_path):
    payload = {"is_risk": True, "risk_types": ["ps"], "image_width": 900}
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, lambda req: json_response(envelope(payload)))
        r = c.post("/v1/files/parse", json={"model": "textin:manipulation-detection",
                                            "file": data_uri(PNG_SMALL)})
    assert r.status_code == 200
    assert r.json()["data"][0]["result"] == payload


def test_parse_table_excel_attachments(tmp_path):
    def resp(req: httpx.Request) -> httpx.Response:
        return json_response(envelope({"tables": [{"rows": 2}], "excel": b64(XLSX_SMALL)}))

    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, resp)
        r = c.post("/v1/files/parse", json={"model": "textin:table-excel",
                                            "file": data_uri(PNG_SMALL),
                                            "filename": "表格.png"})
    att = r.json()["data"][0]["attachments"][0]
    assert att["filename"] == "表格-excel.xlsx"
    assert base64.b64decode(att["b64_json"]) == XLSX_SMALL


# --------------------------------------------------------------------- dry_run


def test_dry_run_touches_nothing_and_returns_preview(tmp_path):
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(req)
        return json_response(envelope(None))

    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, handler)
        r = c.post("/v1/files/convert", json={
            "model": "textin:pdf-to-word", "file": data_uri(PDF_SMALL), "dry_run": True,
        })
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True and body["data"] == []
    assert body["preview"]["url"].endswith("?service=pdf-to-word")
    assert body["preview"]["content_type"] == "application/pdf"
    assert calls == [], "dry_run 必须零上游请求"


def test_dry_run_via_header(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, _ok_image)
        r = c.post("/v1/images/generations",
                   headers={"x-avm-dry-run": "1"},
                   json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
    assert r.json()["dry_run"] is True


# --------------------------------------------------------------------- 门禁与配额


def test_unverified_gate_503_but_dry_run_passes(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, lambda req: json_response(envelope(b64(OFD_SMALL))))
        real = c.post("/v1/files/convert", json={"model": "textin:ofd-to-image",
                                                 "file": data_uri(OFD_SMALL)})
        dry = c.post("/v1/files/convert", json={"model": "textin:ofd-to-image",
                                                "file": data_uri(OFD_SMALL),
                                                "dry_run": True})
    assert real.status_code == 503
    assert real.json()["error"]["code"] == "capability_not_verified"
    assert dry.status_code == 200 and dry.json()["dry_run"] is True


def test_unverified_gate_can_be_opened(tmp_path):
    app, _ = _app(tmp_path, ALLOW_UNVERIFIED=True)
    with TestClient(app) as c:
        _inject(app, lambda req: json_response(envelope(b64(OFD_SMALL))))
        r = c.post("/v1/files/convert", json={"model": "textin:ofd-to-image",
                                              "file": data_uri(OFD_SMALL)})
    assert r.status_code == 200
    assert base64.b64decode(r.json()["data"][0]["b64_json"]) == OFD_SMALL


def test_daily_quota_trips_window_and_short_circuits(tmp_path):
    hits: list[str] = []

    def quota_exhausted(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return json_response(error_envelope(431, "今日请求超过限制次数"))

    app, _ = _app(tmp_path, QUOTA_COOLDOWN=600)
    with TestClient(app) as c:
        _inject(app, quota_exhausted)
        first = c.post("/v1/images/generations",
                       json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
        second = c.post("/v1/images/generations",
                        json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
    assert first.status_code == 429
    assert first.json()["error"]["kind"] == "daily_quota"
    assert first.headers.get("retry-after") == "600"
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "daily_quota_cooldown"
    assert len(hits) == 1, "静默窗内不许再打上游"


def test_per_service_quota_does_not_trip_window(tmp_path):
    """451 不进静默窗（按出口 IP 的软限），但**每次请求会先自动重试一次** ⇒ 每请求 2 次上游。"""
    hits: list[str] = []

    def need_register(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return json_response(error_envelope(451, "need_register"))

    app, _ = _app(tmp_path, QUOTA_COOLDOWN=600)
    with TestClient(app) as c:
        _inject(app, need_register)
        r1 = c.post("/v1/images/generations",
                    json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
        r2 = c.post("/v1/images/generations",
                    json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
    assert r1.status_code == 429 and r1.json()["error"]["kind"] == "quota"
    assert r2.status_code == 429 and len(hits) == 4      # 2 请求 × (1 首发 + 1 自动重试)
    # 重试过还是 451 ⇒ 错误原文要说清楚（否则调用方以为只打了一次）
    assert "已自动重试 1 次仍失败" in r1.json()["error"]["message"]
    assert "TEXTIN_TOKEN" in r1.json()["error"]["message"]


def test_soft_limit_retries_once_then_succeeds_with_warning(tmp_path):
    """451 软限：**自动换出口重试一次**（每请求新建连接）；成功时在 `warnings[]` 与 span 里留痕。"""
    hits: list[str] = []

    def first_451_then_ok(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        if len(hits) == 1:
            return json_response(error_envelope(451, "need_register"))
        return _ok_image(req)

    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, first_451_then_ok)
        r = c.post("/v1/images/generations",
                   json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
    body = r.json()
    assert r.status_code == 200 and len(hits) == 2                 # 恰好一次重试
    assert body["upstream"]["code"] == 200
    assert any("451" in w and "重试" in w for w in body["warnings"]), body["warnings"]
    calls = [s for s in spans() if s.get("stage") == "textin.call"]
    assert calls and calls[-1].get("soft_limit_retried") == 1      # 追踪层也留痕
    assert calls[-1].get("attempts") == 2


def test_soft_limit_retry_can_be_disabled(tmp_path):
    """`TEXTIN_SOFT_LIMIT_RETRY=0` ⇒ 关掉自动重试（上游只被打了 1 次）——口子必须真的接线。"""
    hits: list[str] = []

    def always_451(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return json_response(error_envelope(451, "need_register"))

    app, _ = _app(tmp_path, SOFT_LIMIT_RETRY=0)
    with TestClient(app) as c:
        _inject(app, always_451)
        r = c.post("/v1/images/generations",
                   json={"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL)})
    assert r.status_code == 429 and len(hits) == 1
    assert "已自动重试" not in r.json()["error"]["message"]


# --------------------------------------------------------------------- 校验


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"model": "textin:nope", "image": "x"}, "unknown_model"),
        ({"image": data_uri(PNG_SMALL)}, "missing_model"),
        ({"model": "textin:pdf-to-word", "file": data_uri(PDF_SMALL)}, "model_wrong_endpoint"),
        ({"model": "textin:watermark-remove", "image": data_uri(PNG_SMALL), "typo_field": 1},
         "unknown_field"),
    ],
)
def test_request_validation_errors(tmp_path, payload, code):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/v1/images/generations", json=payload)
    assert r.status_code == 400 and r.json()["error"]["code"] == code


def test_known_unsupported_fields_are_reported_not_swallowed(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, _ok_image)
        r = c.post("/v1/images/generations", json={
            "model": "textin:watermark-remove", "image": data_uri(PNG_SMALL),
            "prompt": "随便写", "n": 2, "size": "1024x1024",
        })
    body = r.json()
    assert r.status_code == 200
    assert set(body["unsupported"]) == {"prompt", "n", "size"}


# --------------------------------------------------------------------- 取件


def test_response_format_url_mirrors_and_serves(tmp_path):
    app, media = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, _ok_image)
        r = c.post("/v1/images/generations", json={
            "model": "textin:watermark-remove", "image": data_uri(PNG_SMALL),
            "response_format": "url",
        })
        url = r.json()["data"][0]["url"]
        served = c.get(url)
    assert url.startswith("/files/")
    assert served.status_code == 200 and served.content == JPEG_SMALL
    assert (media / url.rsplit("/", 1)[1]).read_bytes() == JPEG_SMALL


def test_llms_txt_route_wiring(tmp_path):
    """HTTP 层只钉「接线」：200 + `text/markdown` + **免鉴权**（不带任何头也能拿）。

    内容层面的对账（条目↔注册表、端点↔真实路由）在 `tests/test_llms.py` —— 那边只调纯函数，
    快且不必起 HTTP；这里刻意不重复。
    """
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/llms.txt", headers={"accept": "text/markdown"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("# textin-service")


def test_llms_txt_honours_forwarded_proto(tmp_path):
    """反代后面链接要用**对外**协议：`X-Forwarded-Proto: https` ⇒ 索引里是 https 链接。

    nginx 的 `textin-proxy.inc` 已设该头；不认它的话，https 站点会给出 http 链接
    （能靠 301 兜住，但索引里给错协议是硬伤）。
    """
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/llms.txt", headers={"x-forwarded-proto": "https"}).text
    assert "https://testserver/v1/models" in body
    assert "http://testserver/v1/models" not in body


def test_llms_txt_lists_gated_capability_when_gate_is_open(tmp_path):
    """门禁打开时：它变成**正常条目**、门禁说明消失（同一份注册表，行为随部署配置）。"""
    app, _ = _app(tmp_path, ALLOW_UNVERIFIED=True)
    with TestClient(app) as c:
        body = c.get("/llms.txt").text
        usable = [m["id"] for m in c.get("/v1/models").json()["data"]]
    assert "- `textin:ofd-to-image` — " in body
    assert "🔒" not in body
    assert len([ln for ln in body.splitlines() if ln.startswith("- `textin:")]) == len(usable) == 21


def test_registry_invariants_name_matches_key_and_label_present():
    """注册表不变量（**这条门禁是补的，也因为它的缺失放过了一个实测 bug**）。

    2026-09-24 实测：`textin:table-excel` 的 `name=` 被复制粘贴成 `textin:table`，
    而 `cap.name` 被 service 层用于响应回显/报错/门禁文案 ⇒ 请求 `table-excel` 时
    响应里的 `model` 字段回显成了 `table`（下游按 model 归因会记错）。
    这里把两条不变量钉死：`name == key`、`label` 非空（llms.txt 等索引用它）。
    """
    bad = [k for k, v in CAPABILITIES.items() if v.name != k]
    assert not bad, f"name 与注册键不一致：{bad}"
    unlabeled = [k for k, v in CAPABILITIES.items() if not v.label.strip()]
    assert not unlabeled, f"缺 label（llms.txt 等索引会退回 service 名）：{unlabeled}"


def test_table_excel_echoes_its_own_model_name(tmp_path):
    """bug 回归钉：`textin:table-excel` 的响应 `model` 必须是它自己（不是 `textin:table`）。

    两者 service 相同、只差 `excel=1/0` ⇒ 回显错也"看起来很正常"，正因如此必须显式钉住。
    """
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        got = {}
        for mid in ("textin:table", "textin:table-excel"):
            j = c.post("/v1/files/parse", headers={"authorization": "Bearer test-key"}, json={
                "model": mid, "file": data_uri(PNG_SMALL), "dry_run": True,
            }).json()
            got[mid] = (j["model"], j["effective"]["params"]["excel"])
    assert got["textin:table"] == ("textin:table", "0")
    assert got["textin:table-excel"] == ("textin:table-excel", "1")
