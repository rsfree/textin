#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可观测性 —— 唯一的埋点收拢点。

三条纪律（与 jimeng / grok 同源）：

1. **凭据不进属性**：`TEXTIN_TOKEN` 只活在 client 的请求头里；
   埋点的 url 只给 pathname（query 里带着 `service` 与业务参数，但不是凭据，给全也无妨，
   这里只为"别形成回显习惯"而收窄）。
2. **不做事后脱敏**：logfire 用 `scrubbing=False` —— SDK 自带 scrubber 按值子串命中
   `token` / `credential`，会把上游原始报文误伤成 `[Scrubbed due to 'token']`。
3. **可静默降级**：未装 logfire 或未配 token 时退化为纯日志，**绝不阻塞业务**；
   降级原因打一行 INFO，不是静默失败。
"""

from __future__ import annotations

import sys
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Iterator

from loguru import logger

__all__ = ["setup", "report", "span", "spans", "reset_spans"]

_CONFIGURED = False
_LOGFIRE: Any = None
_CAPTURE = True
#: 进程内 span 缓冲上限。**必须有界** —— 这里曾是无限增长的 list：每个请求 append 2~3 条，
#: 长跑会随请求数线性吃内存（2026-09-24 审计发现）；有界之后顺带把"排障窗口"从"最后 50 条"
#: 提到 200 条，`/stats` 仍按 `[-50:]` 截尾展示。
_SPANS_MAX = 200
_SPANS: deque[dict[str, Any]] = deque(maxlen=_SPANS_MAX)


def setup(
    *,
    level: str = "INFO",
    logfire_token: str = "",
    service_name: str = "textin-service",
    environment: str = "",
    scrubbing: bool = False,
    capture_upstream: bool = True,
) -> None:
    """进程级初始化（幂等）。"""
    global _CONFIGURED, _LOGFIRE, _CAPTURE
    if _CONFIGURED:
        return
    logger.remove()
    logger.add(sys.stderr, level=level, enqueue=False, backtrace=False, diagnose=False)
    _CAPTURE = bool(capture_upstream)
    if logfire_token:
        try:
            import logfire  # type: ignore  # noqa: PLC0415 - 可选依赖

            kwargs: dict[str, Any] = {"token": logfire_token, "scrubbing": scrubbing,
                                      "service_name": service_name}
            if environment:
                kwargs["environment"] = environment
            logfire.configure(**kwargs)
            _LOGFIRE = logfire
            logger.info("logfire 已接入（scrubbing={}）", scrubbing)
        except Exception as exc:  # noqa: BLE001 - 观测失败不许影响业务
            logger.warning("logfire 未接入（{}），继续用纯日志", type(exc).__name__)
    else:
        logger.info("未配 TEXTIN_LOGFIRE_TOKEN ⇒ 只出本地日志（埋点降级为本地 span 记录）")
    _CONFIGURED = True


def report(stage: str, **fields: Any) -> None:
    """瞬时事件上报（`fields` 由调用方保证不含凭据）。"""
    if _CAPTURE:
        _SPANS.append({"stage": stage, "at": time.time(), **fields})
    lf = _LOGFIRE
    if lf is not None:
        try:
            lf.info(stage, **fields)
        except Exception as exc:  # noqa: BLE001 - 同上
            logger.debug("logfire 上报失败（忽略）：{}", exc)


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    """记录耗时 span。返回的 dict 由调用方补字段（如 outcome）。"""
    started = time.monotonic()
    record: dict[str, Any] = {"stage": name, "at": started, **attrs}
    try:
        yield record
    finally:
        record["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        if _CAPTURE:
            _SPANS.append(record)
        lf = _LOGFIRE
        if lf is not None:
            try:
                lf.info(name, **{k: v for k, v in record.items() if k != "stage"})
            except Exception:  # noqa: BLE001, S110 - 上报失败绝不能带崩业务
                pass


def spans() -> list[dict[str, Any]]:
    return list(_SPANS)


def reset_spans() -> None:
    """测试注入点。"""
    _SPANS.clear()
