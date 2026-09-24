# textin-service

**TextIn 工具站**（`tools.textin.com` / 服务端 `api.textin.com`）的**同步出口**：
把"一个端点 + 21 个 service 名"的图片/文档工具，包成三族标准 HTTP 接口 ——
图像处理、文档转换、识别解析。

```
POST /v1/images/generations   → 200 {data:[{b64_json,mime,size}]}    图像族（去水印 / 切边增强 / 去屏幕纹 / 擦手写）
POST /v1/files/convert        → 200 {data:[{b64_json,mime,filename}]} 转换族（pdf↔word/excel/ppt/jpg、word/excel→pdf、图片→pdf）
POST /v1/files/parse          → 200 {data:[{result,text,attachments}]} 解析族（文字/表格/票据/印章/篡改检测/文档解析/财报）
GET  /v1/models               → 模型清单（OpenAI 兼容：object=list + 四键模型对象，免鉴权）
GET  /llms.txt                → LLM/Agent 站点索引（llmstxt.org 约定，内容由注册表生成，免鉴权）
GET  /healthz · /readyz · /capabilities · /stats                      运维面
🔴 鉴权 fail-closed：`TEXTIN_API_KEYS` 为空 ⇒ **拒绝启动**（无鉴权须显式 TEXTIN_ALLOW_NO_AUTH=1）
```

- **对外契约全文**：**[`docs/INTERFACE.md`](docs/INTERFACE.md)**（冻结）
- **上游取证**（含未取证清单与命名陷阱）：**[`docs/UPSTREAM.md`](docs/UPSTREAM.md)**
- **本机跑过什么**（86 用例 / 冒烟 14 项 / 1 次真实调用）：**[`docs/VERIFY.md`](docs/VERIFY.md)**

> 🔴 **形态裁决**：上游是**同步单请求**（无任务 id、无轮询）⇒ 本服务同步直给，
> **不做假异步**。与兄弟服务（jimeng/wuli/grok/pavo 的 `/async/v1` 两段式）刻意不同，
> 因为它们的上游本来就是异步的。裁决记录见 `docs/VERIFY.md §5`。

---

## 0. 我要做什么 → 看哪个文件

| 我想… | 看这里 |
|---|---|
| 接这个服务 | `docs/INTERFACE.md` |
| 改「谁能做什么 / service 名 / 参数」 | `app/models.py`（**唯一的能力注册表**，每条都带取证出处） |
| 改上游请求构造 / 错误分类 / 出网 | `app/upstream/textin/client.py` |
| 改上游报文解析（四种 result 形态） | `app/upstream/textin/capabilities.py`（纯函数，可离线测） |
| 改输入类型嗅探 / 四种输入形态 / 落盘 | `app/media.py` |
| 改响应形状 | `app/service.py::_assemble` / `_deliver` —— **唯一出口** |
| 改配置 | `app/config.py`（每个旋钮都必须有人读，门禁守着） |
| 改 431 静默窗 | `app/gate.py` |
| 改埋点 | `app/observability.py` —— **唯一收拢点** |

---

## 1. 跑起来

```bash
cp .env.example .env            # 全部留空也能跑：上游**匿名**可用

# 方式一：直接起
python -m app.main              # 127.0.0.1:${TEXTIN_PORT:-8600}
# 方式二：gunicorn（🔴 工厂调用，括号不能省）
gunicorn -c gunicorn_conf.py "app.main:create_app()"
# 方式三：容器
docker compose up -d --build && curl -s localhost:8600/healthz
```

```bash
curl -s localhost:8600/v1/models | head -c 300      # 能力发现（免鉴权）
```

### 一次调用长这样

```bash
curl -s localhost:8600/v1/images/generations \
  -H 'content-type: application/json' \
  -d '{"model":"textin:watermark-remove","image":"data:image/png;base64,…"}'
# → {"created":…,"data":[{"b64_json":"…","mime":"image/jpeg","size":"900x600"}],…}
```

### 零成本自检（三件套，都不发真实上游请求）

```bash
python scripts/probe.py                 # ① 21 条能力的出站形状 + 假上游全链路回路
zsh scripts/smoke.sh 8699               # ② 真起服务 + 真 HTTP + 假上游（14 项断言）
python -m pytest -q                     # ③ 89 项用例（socket 级门禁保证零出网）
python -m ruff check .

# ④ 验池（配了 TEXTIN_PROXY_POOL 时）：出口 IP / 轮换粒度；加 --live 再真实调一次
python scripts/probe.py --phases pool [--live]
```

要**真实**打一次上游（消耗该 service 的匿名试用额度，零费用）：

```bash
python scripts/probe.py --phases "" --live --cap textin:watermark-remove
```

---

## 2. 本版做到哪、缺哪（诚实边界）

### ✅ 已打通（实测）

- **三族 21 条能力**全部注册，其中 **20 条默认可用**（`/v1/models` 即这 20 条）；
- 输入四种形态统一（data URI / http(s) URL 代取 / 裸 base64），**类型按真实字节嗅探**
  （docx/xlsx/pptx/ofd 靠 ZIP 内层目录区分，`.doc/.xls` 靠能力期望值定）；
- 输出 `b64_json` 默认 / `url` 落盘 + `/files/` 取件（内容寻址，天然幂等）；
- `dry_run`（头或 body）**零上游请求**返回将要发出的请求（凭据打码）；
- `431` 进静默窗（窗内直接 429，不再白打上游）；`451` 如实报错不自动重试；
- 错误信封统一 `{error:{code,message,kind?,request_id?,upstream?}}`；
- 真实验证：去水印端到端 1 次（1.21s，产物 900×600 JPEG 41405 B，与研究基准同档）。

### 🔒 刻意不做 / 未取证

| 事项 | 现状 |
|---|---|
| `textin:ofd-to-image` | 站点上存在但**零样本、从未端到端跑过** ⇒ 默认 503 门禁；`TEXTIN_ALLOW_UNVERIFIED=1` 放开 |
| `dewarp` / `image_quality_inspect` | **不注册**（前者是重定向页且需未知参数；后者付费档配额 + 形态未取证）—— 理由在 `/capabilities` 的 `not_registered` |
| `X-Forwarded-For` 轮换 / 代理池换出口 | **默认都关**（`TEXTIN_ROTATE_XFF=0` / `TEXTIN_PROXY_POOL` 空）：绕按 IP 试用配额属对抗性规避，开与不开由部署方决定。依据是 2026-09-24 的维度归因实验（`docs/UPSTREAM.md §4.1`：**按出口 IP 的软限，身份/指纹不是维度**） |
| 异步两段式 / 任务表 / 回调 | 上游没有任务概念，本服务**不发明**（见页首裁决） |
| 批量处理 | 站点页面上有"在线批量处理"入口，但上游批量接口未取证 |

### ⚠️ 配额现实（部署前必读）

匿名按 IP 计：**每 service 约 3 次**（软限、多节点计数不一致）后 `451`；
另有**按天总额度**（`431`，当天不恢复）。实测成功率 73.5%。
⇒ 想稳定用请配 `TEXTIN_TOKEN`（登录 token，配额口径变化未取证）。
详见 `docs/UPSTREAM.md §4`。

---

## 3. 目录

```
app/
  main.py              HTTP 层：路由 / 鉴权 / 错误信封 / 静态取件（契约唯一入口）
  service.py           编排与响应装配（唯一出口）；requested/effective/warnings/unsupported
  models.py            ★ 能力注册表（21 条，每条带 service 名 + 参数 + 取证出处）
  media.py             类型嗅探 / 四种输入形态 / 内容寻址落盘 / PNG·JPEG 尺寸
  errors.py            错误分类 + kind→状态码唯一映射 + 可行动指引
  gate.py              431 静默窗（进程内）
  config.py            配置（TEXTIN_ 前缀；每个旋钮都有人读）
  observability.py     日志 + 可选 logfire（可静默降级）
  upstream/textin/
    client.py          上游 HTTP（httpx、trust_env=False、dry_run、外链代取）
    capabilities.py    ★ 翻译层纯函数：请求构造 + 四种 result 形态解析
docs/                  UPSTREAM.md（取证）/ INTERFACE.md（契约）/ VERIFY.md（验证）
scripts/               probe.py（自检）/ smoke.sh（冒烟）/ mock_upstream.py（假上游）/ samples/
tests/                 86 项，全离线（socket 级门禁）
```

## 4. 部署要点

- **单 worker 默认**（`TEXTIN_WORKERS=1`）：431 静默窗与 span 记账在**进程内**，
  多 worker 会让它们变 N 份。要提吞吐先确认这些状态是否要迁移（README §5 的说明见 `gunicorn_conf.py`）。
- 上游是公网 HTTP，**不吃宿主代理**（`trust_env=False`）——部署机有透明代理时行为一致。
  **换出口只有两条显式通道**：`TEXTIN_ROTATE_XFF` 与 `TEXTIN_PROXY_POOL`（见上表 + `docs/UPSTREAM.md §4`）。
- `var/media` 要可写（`response_format=url` 用）；容器里已建好并 chown 给运行用户。
- 健康检查：`/healthz`（零依赖）；`/readyz` 会报配额静默窗状态与**脱敏后的出口池**
  （窗内 `degraded`）。
