FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY gunicorn_conf.py ./

# 非 root 运行。MEDIA_DIR 默认 var/media：目录要在镜像里就属于运行用户，
# 否则调用方要 url 时保存结果会失败（那是**部署问题**，不该等运行期才发现）。
RUN useradd --create-home --uid 10001 textin \
    && mkdir -p /srv/var/media \
    && chown -R textin:textin /srv
USER textin

EXPOSE 8600

# 🔴 目标必须是工厂调用形态（括号不能省）：
# 写成模块级对象会 `Failed to find attribute`，而单测全绿。
CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:create_app()", "-b", "0.0.0.0:8600", "-w", "1"]
