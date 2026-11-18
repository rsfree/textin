"""431 降级通道用例：体验页采集器（`app/fallback.py` + service 降级分支）。

零出网纪律：collector 的 HTTP 调用一律 monkeypatch 掉 —— conftest 的 socket 门禁
会把真出网拦死在测试里，能跑通即证明没有夹带真实请求。

覆盖四件事：
1. `fallback_available` 的判定（URL 空 = 关；只管 watermark-remove）
2. 默认关 ⇒ 431 行为与历史完全一致（429 + 静默窗）
3. 降级成功 ⇒ 200 出图，输出契约与常规路径完全同形
4. 降级失败/静默窗 ⇒ 回退 431 原语义；且静默窗内**不再打上游**
"""

from __future__ import annotations

import base64

import httpx
from fastapi.testclient import TestClient

from app.fallback import FallbackUnavailable, fallback_available
from app.main import create_app
from app.upstream.textin import TextinClient
from tests.helpers import (
    JPEG_SMALL,
    PNG_SMALL,
    b64,
    data_uri,
    error_envelope,
    json_response,
    settings,
)

_MODEL = "textin:watermark-remove"
_FALLBACK = "http://127.0.0.1:8790"


def _app(tmp_path, **overrides):
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    return create_app(settings(MEDIA_DIR=str(media), **overrides)), media


def _inject(app, handler):
    app.state.client = TextinClient(app.state.settings, transport=httpx.MockTransport(handler))


def _quota(_req: httpx.Request) -> httpx.Response:
    return json_response(error_envelope(431, "今日请求超过限制次数"))


def _body() -> dict:
    return {"model": _MODEL, "image": data_uri(PNG_SMALL)}


# ------------------------------------------------------------------ 纯函数


def test_fallback_available_requires_url_and_watermark():
    on = settings(FALLBACK_URL=_FALLBACK)
    assert fallback_available("watermark-remove", on) is True
    assert fallback_available("doc-to-markdown", on) is False, "采集器只支持去水印"
    off = settings(FALLBACK_URL="")
    assert fallback_available("watermark-remove", off) is False, "URL 空 = 关"


# ------------------------------------------------------- 默认关：行为不变


def test_431_without_fallback_keeps_429(tmp_path):
    app, _ = _app(tmp_path, QUOTA_COOLDOWN=600)
    with TestClient(app) as c:
        _inject(app, _quota)
        r = c.post("/v1/images/generations", json=_body())
    assert r.status_code == 429
    assert r.json()["error"]["kind"] == "daily_quota"
    assert r.headers.get("retry-after") == "600"


# ------------------------------------------------------- 降级成功：431 → 出图


def test_431_falls_back_to_collector(tmp_path, monkeypatch):
    seen: list[tuple[int, str]] = []

    async def fake_collect(image: bytes, mime: str, _settings) -> str:
        seen.append((len(image), mime))
        return b64(JPEG_SMALL)

    monkeypatch.setattr("app.service.collect_dewatermark", fake_collect)
    app, _ = _app(tmp_path, QUOTA_COOLDOWN=600, FALLBACK_URL=_FALLBACK)
    with TestClient(app) as c:
        _inject(app, _quota)
        r = c.post("/v1/images/generations", json=_body())

    body = r.json()
    assert r.status_code == 200
    assert base64.b64decode(body["data"][0]["b64_json"]) == JPEG_SMALL
    assert body["usage"]["generated_images"] == 1
    assert any("体验页" in w for w in body["warnings"]), "降级必须在 warnings 留痕"
    assert seen and seen[0][0] == len(PNG_SMALL), "原始图片字节必须原样交给采集器"


def test_cooldown_window_skips_upstream_when_fallback(tmp_path, monkeypatch):
    """静默窗内 + 降级可用 ⇒ 跳过上游（不再空打已满的额度）。"""
    hits: list[str] = []

    async def fake_collect(_image: bytes, _mime: str, _settings) -> str:
        return b64(JPEG_SMALL)

    def counting(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return json_response(error_envelope(431, "今日请求超过限制次数"))

    monkeypatch.setattr("app.service.collect_dewatermark", fake_collect)
    app, _ = _app(tmp_path, QUOTA_COOLDOWN=600, FALLBACK_URL=_FALLBACK)
    with TestClient(app) as c:
        _inject(app, counting)
        first = c.post("/v1/images/generations", json=_body())   # 431 → 降级 → 200，gate 已 trip
        second = c.post("/v1/images/generations", json=_body())  # gate 开 → 直接降级
    assert first.status_code == 200 and second.status_code == 200
    assert len(hits) == 1, "静默窗内不许再打上游"


# ------------------------------------------------------- 降级失败：回退原语义


def test_fallback_failure_falls_back_to_429(tmp_path, monkeypatch):
    async def boom(_image: bytes, _mime: str, _settings) -> str:
        raise FallbackUnavailable("collector down")

    monkeypatch.setattr("app.service.collect_dewatermark", boom)
    app, _ = _app(tmp_path, QUOTA_COOLDOWN=600, FALLBACK_URL=_FALLBACK)
    with TestClient(app) as c:
        _inject(app, _quota)
        r = c.post("/v1/images/generations", json=_body())
    assert r.status_code == 429
    assert r.json()["error"]["kind"] == "daily_quota", "采集器挂了不许掩盖 431 真因"


def test_fallback_ignored_for_other_capabilities(tmp_path, monkeypatch):
    """非去水印能力不降级：431 仍是 429（采集器能力面之外）。"""
    async def fake_collect(_image: bytes, _mime: str, _settings) -> str:  # pragma: no cover
        raise AssertionError("不该被调用")

    monkeypatch.setattr("app.service.collect_dewatermark", fake_collect)
    app, _ = _app(tmp_path, QUOTA_COOLDOWN=600, FALLBACK_URL=_FALLBACK)
    with TestClient(app) as c:
        _inject(app, _quota)
        r = c.post("/v1/files/convert", json={"model": "textin:pdf-to-word",
                                              "file": data_uri(b"%PDF-1.4 tiny")})
    assert r.status_code == 429


# ------------------------------------------------- 441（窗口/出口额度）也降级

def test_441_also_triggers_fallback(tmp_path, monkeypatch):
    """441 reach_limit 与 431 同源（该出口此刻不可用）⇒ 同样走降级。"""
    async def fake_collect(_image: bytes, _mime: str, _settings) -> str:
        return b64(JPEG_SMALL)

    def reach_limit(_req: httpx.Request) -> httpx.Response:
        return json_response(error_envelope(441, "reach_limit"))

    monkeypatch.setattr("app.service.collect_dewatermark", fake_collect)
    app, _ = _app(tmp_path, FALLBACK_URL=_FALLBACK)
    with TestClient(app) as c:
        _inject(app, reach_limit)
        r = c.post("/v1/images/generations", json=_body())
    body = r.json()
    assert r.status_code == 200
    assert base64.b64decode(body["data"][0]["b64_json"]) == JPEG_SMALL
    assert any("体验页" in w for w in body["warnings"])


def test_441_without_fallback_keeps_upstream_error(tmp_path):
    def reach_limit(_req: httpx.Request) -> httpx.Response:
        return json_response(error_envelope(441, "reach_limit"))

    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _inject(app, reach_limit)
        r = c.post("/v1/images/generations", json=_body())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == 441
