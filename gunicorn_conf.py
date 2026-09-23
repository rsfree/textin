"""gunicorn 配置。

🔴 目标必须是**工厂调用** `app.main:create_app()`：
写成模块级对象形态（把冒号后写成 `app`）会得到 `Failed to find attribute`，
而单测全绿（用例都直接调 `create_app()`），只有真起来才炸。
tests/test_wiring.py::test_gunicorn_target_uses_factory_call 钉着这条。

⚠️ 默认 WORKERS=1：
本服务**无进程内状态**（同步翻译层，没有闸门/队列/计数窗口之外的共享物），
多 worker 在语义上可行；但 431 静默窗与 span 记账都在进程内，
多 worker 会让它们变成 N 份（静默窗近似失效）。默认保守取 1，
要提吞吐先确认这些进程内状态是否需要迁移（见 README §5）。
"""

from __future__ import annotations

import os

_bind = f"{os.environ.get('TEXTIN_HOST', '0.0.0.0')}:{os.environ.get('TEXTIN_PORT', '8600')}"
bind = _bind
workers = int(os.environ.get("TEXTIN_WORKERS", "1"))
worker_class = "uvicorn_worker.UvicornWorker"
# 上游文档转换族最慢实测 7s；超时留足裕量（文档族单独 120s，见 config）。
timeout = int(os.environ.get("TEXTIN_GUNICORN_TIMEOUT", "180"))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("TEXTIN_LOG_LEVEL", "info").lower()

# 供 `gunicorn -c gunicorn_conf.py "app.main:create_app()"` 直接使用；
# 这里把命令也写出来，免得每次从 README 里抄。
CMD = f'gunicorn -c gunicorn_conf.py "app.main:create_app()" -b {_bind} -w {workers}'

__all__ = ["bind", "workers", "worker_class", "timeout", "CMD"]
