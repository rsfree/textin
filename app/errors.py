"""错误分类体系。

分工与兄弟服务一致：

- `UpstreamError` 是**上游**错误的容器（`kind` 决定对外 HTTP 状态）；
- `ApiError` 是本服务对外的错误信封（我们自己的报文，不载荷上游细节）。

上游 kind → 对外状态码的映射**只在这一处**（`UPSTREAM_KIND_STATUS`），
否则每个路由各写一遍就会出现"同一个上游错误在不同接口上是不同状态"的怪象。

textin 的错误码是**信封里的 `code`（整数）**，不是 HTTP 状态码 —— 上游几乎恒回
HTTP 200，判成败只看 `code`（见 docs/UPSTREAM.md §3）。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ApiError",
    "UpstreamError",
    "UpstreamQuotaError",
    "UpstreamDailyQuotaError",
    "UpstreamParamError",
    "UpstreamTimeout",
    "UpstreamUnavailableError",
    "classify_code",
    "UPSTREAM_KIND_STATUS",
]


class UpstreamError(RuntimeError):
    """上游错误基类。"""

    kind = "upstream"

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        request_id: str | None = None,
        http_status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.request_id = request_id
        self.http_status = http_status
        self.retry_after = retry_after

    def __str__(self) -> str:
        bits = [self.message]
        if self.code is not None:
            bits.append(f"code={self.code}")
        if self.request_id:
            bits.append(f"x_request_id={self.request_id}")
        return " ".join(bits)


class UpstreamQuotaError(UpstreamError):
    """该 service 的匿名试用配额用尽（`code=451 need_register`）。

    语义是「打一个**不会因重试而变好**的计数器」，但它的计数是**多节点软限**
    （实测同一 XFF 连打第 4 次失败后第 5/6 次又能成功）⇒ **不给静默期**，
    也不自动重试；调用方可自行决定是否偶发重试。
    """

    kind = "quota"


class UpstreamDailyQuotaError(UpstreamError):
    """按天计的总额度已满（`code=431 今日请求超过限制次数`，2026-09-12 复测新增）。

    当天不恢复 ⇒ 服务侧进入**静默窗**（`TEXTIN_QUOTA_COOLDOWN`），
    窗内直接 429 不再白打上游；对外带 `Retry-After`。
    """

    kind = "daily_quota"


class UpstreamParamError(UpstreamError):
    """上游拒绝请求（`400 / 40004 / 40301 / 40302 / 40303`）。

    高频诱因有两个，文案完全不提示是哪一个：
      1. **service 名拼错**（`pdf2word` 之流全部不存在，必须用注册表里的真名）；
      2. 文件类型与 service 不匹配（实测：`excel-to-pdf` 收到 PDF）。
    本服务的注册表 + 输入嗅探已经把两者都挡在本地，走到这里通常是上游行为变化。
    """

    kind = "param"


class UpstreamTimeout(UpstreamError):
    kind = "timeout"


class UpstreamUnavailableError(UpstreamError):
    kind = "upstream"


#: 上游 code → 异常类。**只登记已取证的码**；新码出现时会落进通用分支（502），
#: 那时应当回来补表并补一条断言，而不是让它静默过去。
_CODE_MAP: dict[int, type[UpstreamError]] = {
    451: UpstreamQuotaError,
    431: UpstreamDailyQuotaError,
    40004: UpstreamParamError,
    40301: UpstreamParamError,
    40302: UpstreamParamError,
    40303: UpstreamParamError,
}

#: 上游 `400` 是**两种情况共用**的码（service 名不存在 / 文件类型不匹配），
#: 按"参数错"归类（调用方问题）——它确实几乎总是请求侧的问题。
_PARAM_CODES = {400}


#: 额度类错误的**可行动指引**（拼在原文之后）。排障时最缺的不是错误原文，
#: 而是"我现在该做什么" —— 上游自己不会说。
_CODE_GUIDANCE: dict[int, str] = {
    451: (
        "该 service 的匿名试用配额用尽；它按 service 独立计、且是**多节点软限**"
        "（实测同一出口连打：成功×3 → 失败 → 成功×2，偶发重试可能成功），"
        "本服务**不自动重试** —— 要稳定使用请配 TEXTIN_TOKEN 或登录"
    ),
    431: (
        "今日总额度已用尽（**按天计、当天不恢复**）；服务侧已进入静默窗，"
        "窗内不再打上游。要稳定使用请配 TEXTIN_TOKEN"
    ),
}


def classify_code(code: int, msg: str, **kw: Any) -> UpstreamError:
    """把上游信封 code 映射成异常类型，并补上可行动指引。"""
    cls = _CODE_MAP.get(code)
    if cls is None and code in _PARAM_CODES:
        cls = UpstreamParamError
    if cls is None:
        return UpstreamUnavailableError(msg, code=code, **kw)
    guidance = _CODE_GUIDANCE.get(code)
    if guidance:
        msg = f"{msg}；{guidance}"
    return cls(msg, code=code, **kw)


class ApiError(RuntimeError):
    """对外错误信封（我们自己的报文，不载荷上游实现细节）。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.extra:
            body["error"].update(self.extra)
        return body


#: 上游 kind → 对外状态码。**这一张表就是「归属」的全部答案**。
#:
#: `quota` / `daily_quota` 都映射 429 —— 它们是"容量/额度"类，不是调用方把参数写错了；
#: 区别体现在错误信封的 `code` 与 `daily_quota` 附带的 `Retry-After`。
UPSTREAM_KIND_STATUS: dict[str, int] = {
    "quota": 429,
    "daily_quota": 429,
    "param": 400,
    "timeout": 504,
    "upstream": 502,
}
