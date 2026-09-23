"""出站请求形状与错误映射用例。

断言的是「**真正发出去的东西**」（URL 参数、Content-Type、body 字节、token 头），
而不是「内层函数被调用了」—— 后者会让整条路径从未被真实执行：
biz-api 曾把 httpx 的 `content=` 传给 `requests`，单测全绿、线上必 500。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.errors import (
    ApiError,
    UpstreamDailyQuotaError,
    UpstreamParamError,
    UpstreamQuotaError,
    UpstreamTimeout,
    UpstreamUnavailableError,
)
from app.models import lookup
from app.upstream.textin.client import TextinClient
from tests.helpers import (
    JPEG_SMALL,
    PNG_SMALL,
    arun,
    b64,
    envelope,
    error_envelope,
    json_response,
    settings,
)


def _client(handler, **overrides):
    conf = settings(**overrides)
    return conf, TextinClient(conf, transport=httpx.MockTransport(handler))


def _capture(seen: dict):
    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["url"] = str(req.url)
        seen["headers"] = dict(req.headers)
        seen["content"] = req.content
        return json_response(envelope({"image": b64(JPEG_SMALL)}))
    return handler


def test_raw_call_posts_bytes_with_real_mime_and_empty_token_header():
    seen: dict = {}
    _, client = _client(_capture(seen))
    out = arun(client.call(lookup("textin:watermark-remove"), PNG_SMALL, "image/png"))
    arun(client.aclose())

    assert seen["method"] == "POST"
    assert seen["url"] == ("https://api.textin.com/home/user_trial_ocr"
                           "?service=watermark-remove")
    assert seen["headers"]["content-type"] == "image/png"
    # 匿名时**仍然发空 token 头**：与实测 CLI（tx_tools.py）逐字一致
    assert seen["headers"]["token"] == ""
    # 默认不做 XFF 轮换（对抗性规避）
    assert "x-forwarded-for" not in seen["headers"]
    assert seen["content"] == PNG_SMALL
    assert out["code"] == 200


def test_table_excel_query_is_two_params_in_order():
    seen: dict = {}
    _, client = _client(_capture(seen))
    arun(client.call(lookup("textin:table-excel"), PNG_SMALL, "image/png"))
    arun(client.aclose())
    assert seen["url"].endswith("?service=table&excel=1")


def test_image_to_pdf_posts_json_channel():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(headers=dict(req.headers), content=req.content)
        return json_response(envelope(b64(b"%PDF-1.4\n%%EOF\n")))

    _, client = _client(handler)
    arun(client.call(lookup("textin:image-to-pdf"), PNG_SMALL, "image/png"))
    arun(client.aclose())
    assert seen["headers"]["content-type"] == "application/json"
    body = json.loads(seen["content"])
    assert list(body) == ["files"]


def test_token_and_xff_interaction():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(req.headers)
        return json_response(envelope({"image": b64(JPEG_SMALL)}))

    # 配了 token：发 token，不发 XFF（不轮换出口）
    _, c1 = _client(handler, TOKEN="tk-123", ROTATE_XFF=True)
    arun(c1.call(lookup("textin:demoire"), PNG_SMALL, "image/png"))
    arun(c1.aclose())
    assert seen["headers"]["token"] == "tk-123"
    assert "x-forwarded-for" not in seen["headers"]

    # 没配 token 且**显式**开轮换：才有 XFF（合法 IPv4 形状）
    _, c2 = _client(handler, TOKEN="", ROTATE_XFF=True)
    arun(c2.call(lookup("textin:demoire"), PNG_SMALL, "image/png"))
    arun(c2.aclose())
    xff = seen["headers"]["x-forwarded-for"]
    assert len(xff.split(".")) == 4 and all(part.isdigit() for part in xff.split("."))


def test_dry_run_never_touches_upstream():
    called = []

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        called.append(req)
        return json_response(envelope(None))

    _, client = _client(handler, TOKEN="tk-secret")
    preview = arun(client.call(lookup("textin:pdf-to-word"), b"%PDF-1.4\n%%EOF\n",
                               "application/pdf", dry_run=True))
    arun(client.aclose())

    assert called == [], "dry_run 必须一个字节都不发"
    assert preview["dry_run"] is True
    assert preview["method"] == "POST"
    assert preview["url"].startswith("https://api.textin.com/home/user_trial_ocr?service=pdf-to-word")
    assert preview["content_type"] == "application/pdf"
    assert preview["content_bytes"] == len(b"%PDF-1.4\n%%EOF\n")
    assert preview["headers"]["token"] == "***", "预览里的凭据必须打码"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (451, UpstreamQuotaError),
        (431, UpstreamDailyQuotaError),
        (400, UpstreamParamError),
        (40004, UpstreamParamError),
        (40303, UpstreamParamError),
        (59999, UpstreamUnavailableError),
    ],
)
def test_error_code_mapping(code, expected):
    def handler(req: httpx.Request) -> httpx.Response:
        return json_response(error_envelope(code, "上游拒绝"))

    _, client = _client(handler)
    with pytest.raises(expected) as ei:
        arun(client.call(lookup("textin:watermark-remove"), PNG_SMALL, "image/png"))
    arun(client.aclose())
    assert ei.value.code == code
    if isinstance(ei.value, (UpstreamQuotaError, UpstreamDailyQuotaError)):
        assert "service=watermark-remove" in ei.value.message, "额度类错误必须带 service（配额按 service 计）"


def test_non_json_and_missing_code_are_upstream_errors():
    def not_json(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>challenge</html>")

    _, c1 = _client(not_json)
    with pytest.raises(UpstreamUnavailableError):
        arun(c1.call(lookup("textin:watermark-remove"), PNG_SMALL, "image/png"))
    arun(c1.aclose())

    _, c2 = _client(lambda req: json_response({"msg": "no code"}))
    with pytest.raises(UpstreamUnavailableError):
        arun(c2.call(lookup("textin:watermark-remove"), PNG_SMALL, "image/png"))
    arun(c2.aclose())


def test_timeout_maps_to_upstream_timeout():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    _, client = _client(handler)
    with pytest.raises(UpstreamTimeout):
        arun(client.call(lookup("textin:watermark-remove"), PNG_SMALL, "image/png"))
    arun(client.aclose())


def test_convert_family_uses_convert_timeout():
    """文档族用更长的超时（word-to-image 实测 7s，图像族 60s 不够稳）。"""
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["timeout"] = req.extensions.get("timeout")
        return json_response(envelope(b64(b"%PDF-1.4\n%%EOF\n")))

    _, client = _client(handler, TIMEOUT=11.0, CONVERT_TIMEOUT=77.0)
    arun(client.call(lookup("textin:word-to-pdf"), PNG_SMALL, "image/png"))
    arun(client.aclose())
    assert set(seen["timeout"].values()) == {77.0}  # httpx 把单值摊成四个方向


# --------------------------------------------------------------------- 外链输入


def test_fetch_url_ok():
    _, client = _client(lambda req: httpx.Response(200, content=PNG_SMALL,
                                                   headers={"content-type": "image/png"}))
    data, ct = arun(client.fetch_url("https://example.invalid/x.png", 1 << 20))
    arun(client.aclose())
    assert data == PNG_SMALL and ct == "image/png"


def test_fetch_url_rejects_by_content_length_and_by_stream():
    _, c1 = _client(lambda req: httpx.Response(200, content=b"x" * 10,
                                               headers={"content-length": str(1 << 21)}))
    with pytest.raises(ApiError) as ei:
        arun(c1.fetch_url("https://example.invalid/big.bin", 1 << 20))
    arun(c1.aclose())
    assert ei.value.code == "input_too_large"

    # 没有 content-length ⇒ 读流封顶兜住
    _, c2 = _client(lambda req: httpx.Response(200, content=b"x" * (1 << 20 + 5)))
    with pytest.raises(ApiError):
        arun(c2.fetch_url("https://example.invalid/big2.bin", 1 << 20))
    arun(c2.aclose())


def test_fetch_url_http_error():
    _, client = _client(lambda req: httpx.Response(404, content=b"nope"))
    with pytest.raises(UpstreamUnavailableError):
        arun(client.fetch_url("https://example.invalid/miss.png", 1 << 20))
    arun(client.aclose())


# --------------------------------------------------------------------- 代理池


def test_proxy_pool_rotates_a_fresh_client_per_request(monkeypatch):
    """配了池 ⇒ **每个请求新建 client 并轮换代理**（复用连接会拿回同一出口）。

    离线做法：替换 `httpx.AsyncClient` 构造器 —— 记录 kwarg（证明"代理真的传下去了"），
    同时仍用 MockTransport 承接请求（一个字节都不出网）。
    🔴 回归点：有代理时**不得**再传 `transport`（httpx 会让 proxy 胜出、把假传输绕开，
    2026-09-24 实测），所以断言 kwargs 里只有 proxy。
    """
    real_cls = httpx.AsyncClient  # 先抓住真类：monkeypatch 是**全局**替换，不抓会自递归
    seen: list[dict] = []

    def fake_ctor(**kwargs):
        seen.append(dict(kwargs))
        clean = {k: v for k, v in kwargs.items() if k not in ("proxy", "transport")}
        return real_cls(transport=httpx.MockTransport(_ok_image_handler), **clean)

    monkeypatch.setattr("app.upstream.textin.client.httpx.AsyncClient", fake_ctor)
    conf = settings(PROXY_POOL="http://p-one:1,http://p-two:2")
    client = TextinClient(conf)
    cap = lookup("textin:watermark-remove")
    for _ in range(3):
        arun(client.call(cap, PNG_SMALL, "image/png"))
    arun(client.aclose())

    assert len(seen) == 4, "1 个长命 client + 3 个按请求新建的 client"
    assert "proxy" not in seen[0], "无池语义下不传代理"
    assert [c.get("proxy") for c in seen[1:]] == ["http://p-one:1", "http://p-two:2",
                                                  "http://p-one:1"], "轮询顺序"
    assert all("transport" not in c for c in seen[1:]), "有代理时不能再传 transport"
    # 凭据脱敏视图
    masked = TextinClient(settings(PROXY_POOL="http://user:pass@p1.cn:2086,socks5://p2.cn:1080"))
    assert masked.masked_proxies() == ["http://p1.cn:2086", "socks5://p2.cn:1080"]
    assert masked.pool_size == 2
    arun(masked.aclose())


def test_no_pool_keeps_single_long_lived_client(monkeypatch):
    real_cls = httpx.AsyncClient
    seen: list[dict] = []

    def fake_ctor(**kwargs):
        seen.append(dict(kwargs))
        return real_cls(**kwargs)

    monkeypatch.setattr("app.upstream.textin.client.httpx.AsyncClient", fake_ctor)
    conf = settings(PROXY_POOL="")
    client = TextinClient(conf, transport=httpx.MockTransport(_ok_image_handler))
    for _ in range(3):
        arun(client.call(lookup("textin:demoire"), PNG_SMALL, "image/png"))
    arun(client.aclose())

    assert len(seen) == 1, "无池：全程一个 client"
    assert seen[0].get("proxy") is None and "transport" in seen[0]


def test_dry_run_reports_whether_proxy_is_in_use():
    _, client_no_pool = _client(_ok_image_handler)
    preview = arun(client_no_pool.call(lookup("textin:watermark-remove"), PNG_SMALL,
                                       "image/png", dry_run=True))
    arun(client_no_pool.aclose())
    assert preview["via_proxy"] is False

    conf = settings(PROXY_POOL="http://p-one:1")
    client_pool = TextinClient(conf, transport=httpx.MockTransport(_ok_image_handler))
    preview2 = arun(client_pool.call(lookup("textin:watermark-remove"), PNG_SMALL,
                                     "image/png", dry_run=True))
    arun(client_pool.aclose())
    assert preview2["via_proxy"] is True


def _ok_image_handler(req: httpx.Request) -> httpx.Response:
    return json_response(envelope({"image": b64(JPEG_SMALL)}))
