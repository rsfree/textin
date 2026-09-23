#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""431 静默窗 —— 「按天额度打满」的进程内闸门。

与兄弟服务的上行限速闸门不同，这里只管**一件事**：上游回 `code=431`
（今日请求超过限制次数）后，在 `TEXTIN_QUOTA_COOLDOWN` 秒内**不再打上游**，
直接给调用方 429 —— 因为 431 当天不会恢复，白打没有意义。

451（per-service 试用配额）**刻意不进静默窗**：它的计数是多节点软限
（实测同一 XFF 连打：成功×3 → 失败 → 成功×2），偶发重试确实可能成功；
要不要重试由调用方决定，服务不替他决定（也不替他消耗）。

🔴 这是**进程内状态**：单 worker 下有效，多 worker 会变成 N 份（静默窗近似失效）。
生产要提 worker 数前先读 `gunicorn_conf.py` 的说明。
"""

from __future__ import annotations

import time
from typing import Any

__all__ = ["QuotaWindow"]


class QuotaWindow:
    """按天额度的静默窗。时间源可注入（`at=`），便于确定性测试。"""

    def __init__(self, cooldown: float = 0.0) -> None:
        self._cooldown = float(cooldown)
        self._until = 0.0

    @property
    def cooldown(self) -> float:
        return self._cooldown

    def trip(self, at: float | None = None) -> float:
        """收到 431 时开窗。返回窗口秒数（0 = 该配置下不开窗）。"""
        if self._cooldown <= 0:
            self._until = 0.0
            return 0.0
        now = time.monotonic() if at is None else at
        self._until = now + self._cooldown
        return self._cooldown

    def remaining(self, at: float | None = None) -> float:
        now = time.monotonic() if at is None else at
        return max(0.0, self._until - now)

    def open(self, at: float | None = None) -> bool:
        return self.remaining(at) > 0

    def clear(self) -> None:
        self._until = 0.0

    def snapshot(self, at: float | None = None) -> dict[str, Any]:
        remaining = self.remaining(at)
        return {
            "cooling": remaining > 0,
            "remaining_s": round(remaining, 1),
            "cooldown_s": self._cooldown,
        }
