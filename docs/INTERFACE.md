# 对外契约（textin-service）

> 本文件是**唯一的对外契约真相**。改它 = 破坏调用方，必须同步 `tests/test_api.py`
> 与 `docs/VERIFY.md`。上游侧的字段、错误码与取证结论在 `docs/UPSTREAM.md`；
> 推导过程在 `.workbuddy/memory/`。
>
> **形态裁决（2026-09-24）**：上游是**同步单请求**（无任务 id、无轮询）⇒ 本服务
> **同步直给**，不做假异步。与兄弟服务（jimeng/wuli/grok/pavo 的两段式 `/async/v1`）
> 刻意不同，因为它们的上游本来就是异步的。裁决过程见 `docs/VERIFY.md §5`。

---

## 0. 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/v1/images/generations` | **图像族**：图进图出（去水印 / 切边增强 / 去屏幕纹 / 擦除手写） |
| `POST` | `/v1/files/convert` | **转换族**：文件进文件出（pdf↔word/excel/ppt/image 等 9 项） |
| `POST` | `/v1/files/parse` | **解析族**：文件进结构化（文字/表格/票据/印章/篡改检测/文档解析/财报） |
| `GET` | `/v1/models` | 模型清单（OpenAI 兼容：`{"object":"list","data":[四键模型对象…]}`）—— **免鉴权** |
| `GET` | `/llms.txt` | **LLM/Agent 站点索引**（[llmstxt.org](https://llmstxt.org) 约定：`# 标题` + `> 摘要` + 分节链接）—— **免鉴权** |
| `GET` | `/` ｜ `/favicon.svg` ｜ `/favicon.ico` | 站点边角（人类着陆页 + 图标，**免鉴权**、`include_in_schema=False` 不进 OpenAPI）—— 不属于对外 API 契约 |
| `GET` | `/files/{name}` | 结果取件（仅 `response_format=url` 时产生） |

运维端点（**不属于对外契约**）：`GET /healthz`（容器探活）、`GET /readyz`、
`GET /stats`（要 Key）、`GET /capabilities`（要 Key；全集 + 未注册项与原因）。

鉴权：`Authorization: Bearer <key>`，与 `TEXTIN_API_KEYS`（逗号分隔）做白名单比对。
🔴 **为空 ⇒ 拒绝启动**（fail-closed；2026-09-24 事故后收紧——当时空值只打 WARNING，
服务在公网上无鉴权跑了数小时）。确需无鉴权（受信内网 / 干跑）必须**显式**设
`TEXTIN_ALLOW_NO_AUTH=1`，此时 `/readyz` 自报 `api_keys_enabled=false` + `allow_no_auth=true`。
`/v1/models`、`/llms.txt`、`/healthz`、`/readyz`、`/docs`、`/openapi.json`、`/files/*` 刻意免鉴权。

### 0.1 `models` 端点

**逐字四键**（多一个键就是契约变更）：

```json
{"id": "textin:demoire", "object": "model", "created": 1790208000, "owned_by": "textin"}
```

- `created` = 本服务**能力表的版本时间**（不是上游模型创建时间）；
- **只列本部署可调用的模型**（默认 20 条；`TEXTIN_ALLOW_UNVERIFIED=1` 时 21 条）——
  列一个调不通的 id 等于把 503 埋给调用方；全集/原因在 `GET /capabilities`；
- **刻意不提供** `GET /v1/models/{model}`（单取）：2026-09-24 曾实现并随后按要求收敛掉
  （网关只需要列表）；它现在返回 404，且有用例钉住这一点（`test_models_single_retrieve_is_not_exposed`）。

### 0.2 `GET /llms.txt`（LLM/Agent 站点索引）

给 AI Agent 的**一页纸索引**（llmstxt.org 约定，`text/markdown`）：

- **内容由注册表生成**，不手写清单 ⇒ 不会与 `/v1/models` 漂移
  （用例钉住：模型条数 == `/v1/models` 条数，且每条用注册表的 `label`）；
- 只列**本部署可调用**的；未取证项不列条目，但在末尾用一行 `🔒 …` **点名**
  （说清"它存在、被门禁挡住、去哪看原因"）；
- 链接基址 = **调用方看到的基址**（回环访问得回环链接，经域名访问得域名链接）；
- 免鉴权（与 `/v1/models` 同属发现面）。
- **实现**：渲染抽在 `app/llms.py` 的**纯函数** `render(base, *, allow_unverified=…)`；
  `main.py` 只做「还原调用方看到的基址」（含 `X-Forwarded-Proto`）。
- **对账门禁**（`tests/test_llms.py`）：① 条目 ↔ 注册表**双向**对账，且说明文字**逐字**来自
  `Capability.label`；② 「端点」小节 ↔ FastAPI **真实路由表**双向对账（未进索引的路由必须在
  `EXCLUDED_ROUTES` 里**带理由**豁免，豁免项本身也得是真路由）。改索引/改路由而不改另一侧 ⇒ 用例红。
- 索引里也列了 `docs` 与 `openapi.json`（交互式文档：免鉴权，正文即公开契约）。

---

## 1. 三族各读什么字段

| 族 | 输入字段 | 结果字段 |
|---|---|---|
| `image` | `image` | `data[].b64_json` + `mime` + `size` |
| `convert` | `file`（可带 `filename` 提示） | `data[].b64_json` + `mime` + `filename` |
| `parse` | `file`（可带 `filename`） | `data[].result`（上游结构化原样）+ `text`（markdown 族）+ `attachments[]` |

公共字段：`model`（**必填**）、`response_format`（`b64_json` 默认 | `url`）、`dry_run`（布尔）。

**输入三种形态**（`image` / `file` 字符串）：

```jsonc
"data:image/png;base64,<b64>"     // data URI（mime 只是声明，真实类型以字节为准）
"https://example.com/a.pdf"       // 外链（本服务代取，上限 TEXTIN_MAX_DOWNLOAD_MB=50）
"<裸 base64>"                     // 纯 base64（≥32 字符且不含逗号等）
```

🔴 **类型一律按真实字节嗅探**（PNG/JPEG/WEBP/BMP/TIFF/GIF/PDF/ZIP/DOCX/XLSX/PPTX/OFD/CSV），
扩展名与 Content-Type 不作数 —— 上游按 Content-Type 校验，写错直接拒。
类型不在该能力 `accepts` 内 ⇒ `400 input_kind_not_accepted`，报文里带**嗅探到的类型**。

---

## 2. 请求与响应（示例）

```http
POST /v1/images/generations
Authorization: Bearer <key>
Content-Type: application/json

{"model": "textin:watermark-remove", "image": "data:image/png;base64,…"}
```

**响应 `200`**：

```jsonc
{
  "created": 1789948800,
  "model": "textin:watermark-remove",
  "dry_run": false,
  "data": [{"b64_json": "…", "mime": "image/jpeg", "size": "900x600"}],
  "usage": {"generated_images": 1},
  "requested": {"model": "…", "response_format": "b64_json", "image": {"form": "data-uri", "length": 109}}
  ,
  "effective": {"service": "watermark-remove", "params": {"service": "watermark-remove"},
                "input": {"kind": "png", "mime": "image/png", "bytes": 81959, "source": "data-uri"}},
  "warnings": [],          // 降级/忽略必须在这里留痕（例如"已由本服务代取外链"）
  "unsupported": [],       // 认识但上游没有的字段（prompt / n / size / quality / …）
  "upstream": {"service": "watermark-remove", "code": 200, "request_id": "…"}
}
```

`requested` / `effective` / `warnings` / `unsupported` / `upstream` 是**加性扩展**，
不改变标准字段语义（第三方 SDK 可以只读 `created` / `model` / `data`）。

`response_format=url` 时 `data[]` 里给 `url: "/files/<sha256前20位>.<ext>"`（相对路径），
文件落在 `TEXTIN_MEDIA_DIR`（默认 `var/media`，**不设 TTL，由部署方清理**）。

### 转换族 / 解析族的响应

```jsonc
// POST /v1/files/convert  {"model":"textin:pdf-to-word","file":"…","filename":"合同.pdf"}
"data": [{"b64_json": "…", "mime": "application/vnd…wordprocessingml.document",
          "filename": "合同.docx"}]                    // 文件名由输入文件名派生
// POST /v1/files/parse    {"model":"textin:table-excel","file":"…"}
"data": [{"result": {"tables": [ … ]},                 // 上游 result 原样
          "attachments": [{"b64_json": "…", "mime": "…sheet",
                           "filename": "output-excel.xlsx"}]}]
```

---

## 3. 能力清单（`GET /v1/models` 的 id）

`models` 只列**本部署可调用**的（默认 20 条；`TEXTIN_ALLOW_UNVERIFIED=1` 时 21 条）。
全集、未取证项的原因与开启方式在 `GET /capabilities`。

| 族 | id | 输入 → 输出 |
|---|---|---|
| image | `textin:watermark-remove` | 图 → 图（去水印；结果恒 JPEG、与输入同分辨率） |
| image | `textin:crop-enhance` | 图/PDF → 图（切边增强矫正；**输出会被裁边**） |
| image | `textin:demoire` | 图/PDF → 图（去屏幕纹） |
| image | `textin:text-auto-removal` | 图 → 图（擦除手写；⚠️ 证据强度见 `docs/UPSTREAM.md §7`） |
| convert | `textin:pdf-to-word` / `pdf-to-excel` / `pdf-to-ppt` / `pdf-to-image` | PDF → docx / xlsx / pptx / zip |
| convert | `textin:word-to-pdf` / `word-to-image` | doc/docx → pdf / zip |
| convert | `textin:excel-to-pdf` | xlsx/xls/csv → pdf |
| convert | `textin:image-to-pdf` | 图 → pdf（**唯一走 JSON 通道**的能力） |
| convert | `textin:ofd-to-image` | ofd → zip（🔒 默认门禁，未取证） |
| parse | `textin:text-recognize` | 图 → `result.lines[]` |
| parse | `textin:table` / `textin:table-excel` | 图 → `result.tables[]`（后者多 `excel` 附件） |
| parse | `textin:bill-recognize` | 图/PDF → `result.pages[]` |
| parse | `textin:manipulation-detection` | 图/PDF → `result.{is_risk, risk_types, …}` |
| parse | `textin:recognize-stamp` | 图/PDF → `result.details.stamp[]` |
| parse | `textin:doc-parse` | PDF/图 → `result.markdown`（明文）+ `text` |
| parse | `textin:finance-report` | PDF → 同上（固定参数预设） |

---

## 4. `dry_run`：翻译层验收与花钱解耦

请求头 `X-Avm-Dry-Run: 1` 或 body `dry_run: true` ⇒ **一个字节都不发上游**，
返回 `preview`（将要发出的 method / url / params / content_type / content_bytes / headers），
`data` 为空、`dry_run: true`。预览里的 `token` 头一律打码为 `***`。

`dry_run` **穿透未取证闸门**（否则"未开启"就等于"永远看不到翻译结果"）。
⚠️ 它仍会解析输入：data URI / base64 本地完成；**URL 输入会真的代取**（取回才能知道真实类型）。

---

## 5. 错误信封

```jsonc
// 本服务自己的错误
{"error": {"code": "unknown_field", "message": "不认识的字段：typo_field。…"}}
// 上游错误的透传（带 kind 与 request_id，便于对账）
{"error": {"code": 451, "message": "need_register；该 service 的匿名试用配额用尽…",
           "kind": "quota", "upstream": true, "request_id": "…"}}
```

上游 `kind` → 对外状态码（**唯一映射处** `app/errors.py::UPSTREAM_KIND_STATUS`）：

| kind | 状态码 | 含义 | 重试有救吗 |
|---|---|---|---|
| `param` | 400 | 上游拒绝请求（service 名/文件类型问题） | 否（改请求） |
| `quota` | 429 | `451`：该 service 的匿名试用配额用尽 | 偶发可能（**按出口 IP** 的软限）；**本服务自动换出口重试一次**（`warnings[]` 留痕；`TEXTIN_SOFT_LIMIT_RETRY=0` 可关） |
| `daily_quota` | 429 | `431`：按天总额度已满 | 否（当天不恢复）⇒ 进**静默窗**并带 `Retry-After` |
| `timeout` | 504 | 上游超时 | 由调用方决定 |
| `upstream` | 502 | 其它上游错误（含"`code=200` 但没产物"的静默失败） | — |

本服务自己的错误码：`unauthorized` / `invalid_json` / `invalid_body` / `missing_model` /
`unknown_model` / `model_wrong_endpoint` / `unknown_field` / `invalid_response_format` /
`missing_input` / `input_kind_not_accepted` / `invalid_input_format` / `invalid_base64` /
`input_too_large` / `url_input_unsupported` / `capability_not_verified`（503）/
`daily_quota_cooldown`（429）/ `media_write_failed`（503）/ `internal`（500）。

### 校验顺序（刻意的）

**先模型、后键集**：把 convert 族的模型打到 `images` 端点时，返回
`model_wrong_endpoint` 并直接告诉你该打哪个端点（比报"多了 file 字段"有用）。

### 静默窗语义

收到 `431` 后本服务进入静默窗（`TEXTIN_QUOTA_COOLDOWN`，默认 1800s）：
窗内所有需要触网的能力**直接 429 `daily_quota_cooldown`**，不再打上游。
`451` **不进静默窗**：服务侧**自动换出口重试一次**（每次重试都新建连接 ⇒ 池里下一个出口；
成功 ⇒ `warnings[]` 留痕、span 记 `soft_limit_retried`；仍失败 ⇒ 错误原文后缀"已自动重试 N 次"）。
调用方仍可自行再试；`TEXTIN_SOFT_LIMIT_RETRY` 调重试次数（0=关）。
🔴 静默窗是**进程内状态**：多 worker 会变 N 份（见 `gunicorn_conf.py`）。

---

## 6. 幂等与副作用

- 全部能力都是**只读翻译**（图片/文档处理），**没有计费、没有积分、不创建持久资源**；
- 同一次调用重复执行 = 重复消耗该 service 的匿名试用配额（这是唯一"代价"）；
- 上游没有 task id / 没有回调 / 没有取消 —— 本服务也不发明它们。
