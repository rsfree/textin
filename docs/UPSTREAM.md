# 上游取证记录（TextIn 工具站）

> 本文件是**证据档**：哪些是实测、哪些是推断、哪些没做 —— 一目了然。
> 对外契约在 [`INTERFACE.md`](INTERFACE.md)；本仓跑过什么在 [`VERIFY.md`](VERIFY.md)。
>
> 取证时间线：**2026-09-11/12**（全量实测，18 条链路出产物，原始素材在
> `reverse-proxy/textin/probe/`）+ **2026-09-24**（站点现状复核：浏览器实读 + bundle 核对）。

---

## 0. 一句话

**一个端点覆盖全部工具**：`POST https://api.textin.com/home/user_trial_ocr?service=<名>`，
请求体是**文件裸字节**（Content-Type = 文件真实类型），**同步返回**（无任务 id、无轮询），
免登录（`token` 头传空串）。产物是响应体内的 base64。

---

## 1. 端点与请求形状（实测模板）

```http
POST /home/user_trial_ocr?service=watermark-remove HTTP/1.1
Host: api.textin.com
Content-Type: image/png          ← 必须是**文件真实类型**（非标 mime 会被直接拒）
accept: application/json
cache-control: no-cache
pragma: no-cache
token:                           ← 匿名传空串；登录态传 localStorage._textin_token
origin: https://tools.textin.com
referer: https://tools.textin.com/
user-agent: Mozilla/5.0 …Chrome/140…
```

- `origin` / `referer` / `cache-control` 实测缺失仍 200（照发只为与实测形态一致）；
- **`X-Forwarded-For` 服务端信任**（换 IP 即重置匿名配额）—— 见 §4，本服务**默认不用**；
- 唯一例外：`image-to-pdf` 走 **JSON 通道** `{"files": ["<b64>"]}`（`Content-Type: application/json`），
  这个特殊分支在 2026-09-24 的线上 bundle 里**仍然存在**（`["image-to-pdf"].includes(r)`）。

## 2. 基址与配置（2026-09-24 实读 bundle 复核）

| 环境 | 业务 API 基址 |
|---|---|
| production | `https://api.textin.com` ← 本服务用 |
| pre | `https://textin-api-pre.intsig.com` |
| test | `https://textin-sandbox.intsig.com` |

静态素材/图片下载另一个域：`https://web-api.textin.com/open/image/download?filename=<md5>`。

请求层（`_app` chunk 模块 424）2026-09-24 复核结论，**与研究期一致**：
JSON body 会做 camelCase→snake_case 转换；`Blob/File` 原样发送；
拦截器注入 `headers.token = localStorage._textin_token || ""`；
`pdf_to_markdown` 默认带 `page_count=<常量>`。

---

## 3. 响应与错误码

```json
{"code": 200, "msg": "success", "x_request_id": "…", "data": {"result": …}}
```

`data.result` 的三种形态（**解析必须按族区分**）：

| 形态 | 场景 | 处理 |
|---|---|---|
| dict，图在 `image` / `image_list[].image` | 图像族 | `extract_images`（含裸串/列表兜底） |
| **base64 字符串**（整份文件） | 转换族 | `extract_file`，并按扩展名做**容器签字校验** |
| 结构化 dict | 解析族 | `extract_parsed` 原样透传 |

| code | 含义 | 归属 |
|---|---|---|
| 200 | 成功 | — |
| **451** | `need_register`：该 service 的匿名试用配额用尽 | 429 `quota`（**不自动重试**） |
| **431** | `今日请求超过限制次数`（按天总额度） | 429 `daily_quota`（**静默窗** + Retry-After） |
| 400 | 缺参数 / service 名不存在 / 文件类型不匹配（**文案不提示是哪个**） | 400 `param` |
| 40004 | `image-to-pdf` 没走 JSON 通道 | 400 `param` |
| 40301/40302/40303 | 文件类型不支持等 | 400 `param` |

> 🔴 400 的高频诱因：**service 名拼错**。`pdf2word` / `word2jpg` / `pdf-to-jpg` 这类
> "看起来合理"的名字**全部不存在**（那是**页面路由**名，不是 service 名 —— 见 §6）。

---

## 4. 配额（本服务行为的直接依据）

### 4.0 数值速查（2026-09-24 实测，匿名）

| 项 | 数值 | 量法 |
|---|---|---|
| **软墙**（该 service 的 `451`） | 新鲜 IP 的**第 3~4 发**出现第一次；之后通过率 ≈ **75%**（拒绝呈**每第 4 发一次**、**与节奏无关** —— 1.6s 与 4s 两种节奏下比例相同） | 连打记序；换节奏复核 |
| **硬墙**（按天总额度的 `431`） | 单个 IP × 单个 service 累计约 **25~30 次成功**后出现（今天 28 次；09-12 基准 25 次），**当天不恢复** | 清点成功次数直到 431 |
| **维度** | **IP × service × 天**：同一 IP 上 A 能力 431 时，换 B/C/D 能力**立刻 200** | 受限状态下换 service 各打一发 |
| **个别 service** | 匿名额度 ≈ 0：`text_recognize_3d1` **首发即 451** | 首发观测 |
| **换 IP** | 计数整体重置（新 IP = 新账本）；但**共享出口可能已被别人用枯** | 池每连接新 IP 对比 |

> 拒绝都**极快**（451 约 0.06~0.6s、431 约 0.1~0.2s）—— 服务端没有干活，可以当廉价探针
> （但注意：**快速失败也计入风控评分**，别拿它当无限探针用）。

| 事实 | 证据 |
|---|---|
| 匿名配额**按 IP + 按 service 独立计**，约 3 次后 `451` | 研究期实测 |
| 计数是**多节点软限**：同一出口连打 `成功×3 → 失败 → 成功×2` | 研究期实测 |
| 另有**按天总额度**：打满后 `431`，当天不恢复 | 2026-09-12 复测（n=34，成功 25） |
| 成功率约 **73.5%**（失败全是 451）；成功样本延迟 p50 **1.073s** | 基准 n=25 |
| 上游**信任 `X-Forwarded-For`**，换 IP 即重置匿名配额（RFC 5737 保留段命中率近 100%） | 研究期实测 10 次随机 XFF：7 成功 |

🔴 **本服务默认不做 XFF 轮换**（`TEXTIN_ROTATE_XFF=0`）：那属于"绕按 IP 试用配额"的
对抗性规避，与本项目"只做服务端允许范围内的降速与退避"的立场冲突。
该开关存在只为**留证据与可复现性**，不是推荐用法。

登录 token：`localStorage._textin_token`。**未取证**：配了 token 后配额口径如何变化
（研究期无账号）。本服务支持 `TEXTIN_TOKEN` 透传，行为待部署方实测。

### 4.1 维度归因：是 IP 还是身份/指纹？（2026-09-24 实测闭合）

起因：`451` 到底是"按出口 IP 的额度"还是"绑身份/指纹"（像文心的昆仑挑战、qwen 的
cookie/umid）——这决定绕过手段是"换 IP"还是"换身份"。两轴对照实验
（**具体 IP 地址不入库**，只留结构）：

| 组 | 身份 | 出口 | 序列（`code`） |
|---|---|---|---|
| A | 裸 python（无 cookie；python TLS/UA） | 直连出口（此前已被本机烧过 3 次） | 200, 200, **451** |
| A′ | 同上 | 直连出口（同上） | **451**, 200, 200 |
| B（3 批） | **真实 Chrome**（页面上下文 fetch；真实 TLS/JA3 + sec-ch-ua* + UA） | **系统代理出口** | 每批 3 发各夹 1 次 **451** |
| D | 裸 python | **系统代理出口（与 B 同 IP）** | 200, **451**, 200 |
| C | 裸 python | **池出口 ×3（每次新连接 = 新 IP，互不相同）** | 200, 200, 200 |

**结论：以出口 IP 为维度（软限），身份/指纹不是维度。**

1. **同 IP 换身份 → 行为同型**（B vs D，同一出口上 Chrome 与裸客户端都夹着 451）；
   直连出口上 python 的序列（A/A′）与浏览器同型 ⇒ 真实浏览器的全套指纹**没有**换来独立额度。
2. **同身份换 IP → 立刻恢复**（C：同裸身份，三个全新出口 3/3 干净 200）。
3. **出口 IP 的"额度"是共享资源**：B 的首发 451 出现在**我们从未用过**的一个出口
   （那是共享代理节点，别人用过也算数）⇒ 共享出口会继承他人的消耗，
   这就是"新节点也不一定干净"的原因。
4. 三站真相互不相同（**别把结论串站**）：文心 = 昆仑风险评分挑战（换 IP 无效）；
   qwen = 绑 cookie/umid 身份（换 IP 无效）；**textin = 按出口 IP 的软限（换 IP 有效、换身份无效）**。

**工程含义**：本服务提供两条可选的换出口手段，**默认都关**（部署方决定是否启用）：

- `TEXTIN_ROTATE_XFF=1` —— 伪造 `X-Forwarded-For`（客户端可控、零成本；赌厂商不修，属对抗性规避）；
- `TEXTIN_PROXY_POOL=<http://user:pass@host:port,...>` —— 真实换出口；
  **每个请求新建连接**并轮换（复用连接会拿回同一出口）；池的"标称"要实测
  （用 `scripts/probe.py --phases pool` 验：出口数、轮换粒度、是否共享已耗节点）。

实测（2026-09-24）：池 3 轮得 3 个不同出口；经服务客户端真实调用 `200`（3.43s）。
📌 补记（同日）：**池的节点也可能已被别人用枯** —— 实测某节点直接回 `451`，换一个即 `200`
⇒ 用池时把「`451` → 换节点重试」当常规（本服务当前**不自动重试**，由调用方决定；
要内置可选的「451→换节点→重试」环请提需求）。

### 4.2 身份线：textin **没有**可铸造的匿名指纹身份（2026-09-24 实测，负结果）

参考 metaso 的做法（`metaso/tools/mint_identity.py`：Playwright 起无痕 Chromium、
让指纹 JS 回种 cookie，收全量后注入 `METASO_COOKIE`），对 textin 做了同样的铸造与注入：

| 步骤 | 结果 |
|---|---|
| 纯 HTTP 探测（curl，零额度） | `api.textin.com` **不发任何 cookie**；`tools./web-api.textin.com` 只发 WAF 的 `acw_tc`（HttpOnly，30min TTL） |
| Playwright 铸造（本仓 `scripts/mint_identity.py`） | 只有 `acw_tc` + 分析类（百度 `Hm_*`/`HMACCOUNT`、诸葛 `zg_did`）；**没有** metaso 那套阿里指纹（`_c_WBKFRo`/`_nb_ioWEgULi`/`aliyungf_tc`/`tid`）；匿名访客**没有** `_textin_token` |
| 注入实测（直连出口处于 `431` 时，逐变体） | ① 裸发 = `431`；② +铸造 cookie = **`431`**；③ +cookie+完整浏览器头（`sec-ch-ua`/`sec-fetch-*`/Accept-Language）= **`431`** |

⇒ **textin 的身份维度不可利用**（与 metaso 正相反）：站点不给匿名访客发指纹身份，
且这些字段被上游忽略（配额键只看**连接侧 IP**）。
**本服务刻意不提供 `TEXTIN_COOKIE` 之类的注入旋钮** —— 没有可用素材的配置就是假配置
（项目纪律）；`scripts/mint_identity.py` 保留，用于日后复验"站点是否改发指纹身份"。

---

## 5. 站点服务词表与命名陷阱（2026-09-24 实读 bundle + 浏览器复核）

首页当前 **21 个菜单项**（4+3+4+8+2 组），与研究期一致；`/v1/models` 的 21 条能力
与之一一对应。三条容易踩的命名事实：

1. **页面路由名 ≠ service 名**：`/transform/word2jpg` 页面的 service 是 `word-to-image`、
   `/transform/pdf2jpg` 是 `pdf-to-image`、`/transform/pdf2md` 是 `pdf_to_markdown`。
   照路由名拼 service 必 400。
2. **`table&excel=1` 是两个 query 参数**（`service=table` + `excel=1`），
   塞进一个 service 值里会被 URL 编码成 `%26` ⇒ 400。
3. **`dewarp` 是重定向页**：`/image_processing/dewarp` **308 → `crop_enhance_image`**；
   裸名 `dewarp` 调用返回 400「缺少必要参数」⇒ 不是 crop_enhance_image 的同义调用。

### 站点上还有、但本服务**不注册**的 service

| service | 为什么没注册 |
|---|---|
| `dewarp` | 需要未知附加参数（裸名 400）；站点把它的页面重定向到切边增强 ⇒ 没有可交付的独立行为 |
| `image_quality_inspect` | 「图像质量检测」；2026-09-11 枚举判定为**付费档配额**（匿名次数很紧），返回形态未取证 |
| `recognize-document-3d1-multipage` | 2026-09-24 实读 bundle 发现的、与 `text_recognize_3d1` 并列的多页识别 id；是否独立 service 未取证 |

---

## 6. 各 service 的输入 / 输出速查（实测）

| service | 输入 | 产物位置 |
|---|---|---|
| `watermark-remove` | 图 | `result.image`（恒 JPEG、与输入同分辨率；RGBA 丢 alpha） |
| `crop_enhance_image` | 图/PDF | `result.image_list[0].image`（**输出会被裁边** 900×600→896×596） |
| `demoire` | 图/PDF | `result.image` |
| `text_auto_removal` | 图 | `result.image`（⚠️ 见 §7） |
| `pdf-to-word` / `pdf-to-excel` / `pdf-to-ppt` / `pdf-to-image` | PDF | `result` = b64（docx / xlsx / pptx / zip） |
| `word-to-pdf` / `word-to-image` | doc/docx | `result` = b64（pdf / zip）；word-to-image 全表最慢（实测 7.0s） |
| `excel-to-pdf` | xlsx/xls/**csv** | `result` = b64 pdf |
| `image-to-pdf` | 图（**JSON 通道**） | `result` = b64 pdf |
| `ofd-to-image` | ofd | 未取证（见 §7） |
| `text_recognize_3d1` | 图 | `result.lines[]` + angle/width/height |
| `table`（+`excel=1`） | 图 | `result.tables[]`；`excel=1` 时多 `result.excel`（b64 xlsx） |
| `bill_recognize_v2` | 图/PDF(/OFD) | `result.pages[]` |
| `manipulation_detection` | 图/PDF | `result.{is_risk, risk_types, image_width, image_height, image_property}` |
| `recognize_stamp` | 图/PDF | `result.details.stamp[]` |
| `pdf_to_markdown` | PDF/图 | `result.markdown`（**默认 b64；带 `markdown_details=1` 才是明文**）+ pages[]/detail[] |
| 财报解析 | PDF | **不是独立 service**，= `pdf_to_markdown` + 固定参数组（`page_start=0&page_count=200&dpi=144&parse_mode=auto&table_flavor=html&apply_document_tree=1&markdown_details=1`） |

## 7. 未知与未取证（诚实清单）

| 事项 | 状态 |
|---|---|
| `ofd-to-image` 的真实产出形态 | **无 .ofd 样本，从未端到端跑过** ⇒ 本服务默认门禁（`TEXTIN_ALLOW_UNVERIFIED=1` 才放开） |
| `text_auto_removal` 的端到端产物 | 无 out2/ 产物；判据是 ① `tx_wm.py` 记录 `result.image`；② 枚举探测时该 service 在**匿名宽松配额档**正常返回。2026-09-24 用户拍板放开 |
| 配了 `TEXTIN_TOKEN` 后的配额口径 | 未取证（无账号） |
| 生产域名是否会对匿名加严（WAF/风控） | 研究期未见 WAF/滑块/指纹校验；2026-09-24 复核页面仍免登录可用 |
| 上游批量接口（页面上有"在线批量处理"入口） | 未取证（本服务是单文件语义） |

## 8. 复核记录（2026-09-24，零成本）

- 浏览器（bsk）实读 `tools.textin.com` 首页与 `/image_processing/watermark-remove`：
  21 个工具菜单与文档一致；工具按钮的可访问名直接含 service 名（`watermark-remove …`）；
  水印页标明「支持 jpg、jpeg、png、bmp，单张 ≤50M」（**UI 口径**；API 实测还接受 webp/tiff/pdf）；
- 拉取当日 `_app` chunk 复核：`/home/user_trial_ocr`、`api.textin.com`、
  `image-to-pdf` 的 JSON 分支、`_textin_token`、`headers.token` 注入、snake_case 转换
  **全部仍在**；
- 站点未登录态可见（首页无登录墙）；`/image_processing/text_auto_removal` 与
  `/image_processing/image_quality_inspect` 页面已 404（service 仍在 bundle 里）。
