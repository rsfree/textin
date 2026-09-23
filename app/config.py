"""配置层。

纪律（沿用 jimeng / pavo）：**每个旋钮都必须有人读** —— 没人读的旋钮会让人以为已调优。
这条由 `tests/test_wiring.py::test_config_knobs_are_all_read` 兜底（静态扫描 + 断言）。

与兄弟服务的一个结构差别：本服务是**同步翻译层**（无任务库、无协调器、无并发闸门），
所以这里刻意**没有** DB / QUEUE / WORKERS 一类旋钮 —— 登记了也没人读，那就是假配置。
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行期配置。全部字段都有默认值 ⇒ 缺 .env 也能起来（上游匿名可用）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="TEXTIN_",
    )

    # ---- 上游（api.textin.com）----
    BASE_URL: str = "https://api.textin.com"
    OCR_PATH: str = "/home/user_trial_ocr"
    # 可选登录 token（浏览器 localStorage._textin_token）。留空 = 匿名。
    # 匿名时**仍然发送 `token:` 空头** —— 与实测 CLI（tx_tools.py）逐字一致，
    # 空串是它跑通全 18 条链路时的形态。
    TOKEN: str = ""
    TIMEOUT: float = 60.0           # 图像 / 识别族
    CONVERT_TIMEOUT: float = 120.0  # 文档转换族（word-to-image 实测 ~7s，留裕量）
    # 🔴 默认 **关**：上游信任 X-Forwarded-For，换 IP 即重置匿名配额（实测），
    # 但那是「绕按 IP 试用配额」= 对抗性规避，与本项目立场冲突（见 docs/UPSTREAM.md §4）。
    ROTATE_XFF: bool = False
    # 外链输入的硬上限（调用方传 URL 时由本服务代取）。
    MAX_DOWNLOAD_MB: int = 50

    # ---- 本服务 ----
    API_KEYS: str = ""              # 逗号分隔静态白名单；空 = 鉴权整体关闭（仅限内网）
    PORT: int = 8600
    LOG_LEVEL: str = "INFO"
    MEDIA_DIR: str = "var/media"    # response_format=url 时的落盘目录
    # 431（按天额度打满）后的静默窗（秒）。451 不进静默窗（多节点软限，偶发重试可能成功）。
    QUOTA_COOLDOWN: float = 1800.0
    # 未取证能力闸门：text_auto_removal / ofd-to-image 默认不可用（见 app/models.py）。
    ALLOW_UNVERIFIED: bool = False

    # ---- 可观测性（可选，可静默降级）----
    LOGFIRE_TOKEN: str = ""
    OTEL_SERVICE_NAME: str = "textin-service"
    LOGFIRE_ENVIRONMENT: str = ""
    OTEL_CAPTURE_UPSTREAM: bool = True
    OTEL_SCRUBBING: bool = False


_settings: Settings | None = None


def get_settings() -> Settings:
    """取全局 Settings（进程内单例）。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """丢弃缓存实例（测试用）。"""
    global _settings
    _settings = None
