# 验证记录（textin-service）

> 本文件回答一个问题：**这台机器上到底跑过什么、结果如何、怎么复现。**
> 上游事实在 [`UPSTREAM.md`](UPSTREAM.md)；契约在 [`INTERFACE.md`](INTERFACE.md)。

复现环境：受管 venv `/Users/betterme/.workbuddy/binaries/python/envs/textin`
（下文记作 `$V`）。

---

## 1. 单元/接口用例：**86 项全绿、零出网**（2026-09-24）

```bash
cd ~/PycharmProjects/AI/reverse/textin
$V/python -m pytest -q --basetemp=/tmp/textin_pytest        # 86 passed in ~1.2s
$V/ruff check .                                             # All checks passed!
```

「零出网」不是自觉，是**门禁**：`tests/conftest.py::_no_real_network` 把
`socket.connect` 钉死；`tests/test_wiring.py::test_network_guard_is_alive`
证明这道门禁**真的拦得住**（否则它是死代码）。

覆盖分布：翻译层（4 种 result 形态 / 容器签字 / JSON 通道）、媒体层（嗅探 / 四种输入形态 /
落盘 / 像素尺寸）、客户端（出站形状 / 错误映射 / dry_run 零请求 / 外链上限）、
接口层（三族 / 鉴权 / 门禁 503 / 静默窗 / 校验顺序 / 取件）、注册表冻结、接线门禁。

## 2. 零成本自检：`scripts/probe.py`（21 条能力全过）

```bash
$V/python scripts/probe.py            # shapes + loop，**零真实上游请求**
```

- **shapes（21/21）**：逐条打印出站请求形状（service 名 / 附加 query / 通道 / Content-Type /
  字节数）。这一步抓的是"注册表与翻译层是否自洽"——例如 `table&excel=1` 必须是两个 query 参数、
  `image-to-pdf` 必须走 JSON 通道。
- **loop（20/20，1 条门禁跳过）**：起本地假上游，把每条能力从 HTTP 端点打到"上游"再回装配，
  断言 200 + `data[]` 形状。**全程不发真实上游请求。**

## 3. 服务级冒烟：`scripts/smoke.sh`（14 项全过）

```bash
PY=$V/python zsh scripts/smoke.sh 8699     # 默认 8699（服务）/ 8700（假上游）
```

真起 uvicorn + 真 HTTP + **假上游**（`scripts/mock_upstream.py`）。断言清单：

| # | 断言 | 结果 |
|---|---|---|
| 1 | `images` 200 + `b64_json` + `mime` | ✅ |
| 2 | `response_format=url` 落盘 + `/files/{name}` 取件 | ✅ |
| 3 | `convert` 200 且文件名由输入派生（`样本.pdf → 样本.docx`） | ✅ |
| 4 | `parse` 200 + xlsx 附件 | ✅ |
| 5 | `dry_run` 有 preview 且**零上游请求** | ✅ |
| 6 | 上游收到 `service=watermark-remove` | ✅ |
| 7 | 上游收到 `Content-Type=image/png`（**真实类型**而非扩展名） | ✅ |
| 8 | 上游收到 PNG magic 的**裸字节** body | ✅ |
| 9 | 匿名发**空 `token` 头**（与实测 CLI 逐字一致） | ✅ |
| 10 | 默认**不做** XFF 轮换 | ✅ |
| 11 | 未取证项 `503 capability_not_verified`（含开启方式） | ✅ |
| 12 | 模型打错端点 `400 model_wrong_endpoint`（附指路） | ✅ |
| 13 | 上游 `431` → `429 daily_quota` + `Retry-After: 600` | ✅ |
| 14 | 静默窗内第二次**直接 429 且不再打上游**（上游计数 +1） | ✅ |

## 4. 真实端到端：**1 次**（用户 2026-09-24 批准，零费用）

```bash
$V/python scripts/probe.py --phases "" --live --cap textin:watermark-remove
```

| 项 | 值 |
|---|---|
| 能力 | `textin:watermark-remove`（anonymous） |
| 输入 | `scripts/samples/wm_sample.png`（900×600，81959 B） |
| 耗时 | **1.21 s**（与研究基准 p50 1.073s 同档） |
| 上游 `x_request_id` | `3ed8dd8387f2fc5f1f90163eb75ff1c9` |
| 产物 | `var/live/20260924-021159-textin_watermark-remove-0.jpg`（900×600 JPEG，**41405 B**） |
| 报告 | `var/live/20260924-021159-textin_watermark-remove.json` |

产物核验（PIL，独立于本服务）：
- 尺寸与输入一致（900×600）、模式 RGB；**41405 B 与研究期 21 次基准中的多数样本逐字节同长**
  （`docs/UPSTREAM.md §4`）——说明拿到的是同一处理管线的同一形态产物；
- 与输入逐像素比对：差异区域覆盖全图 bbox，**显著变化像素占 3.5%**（水印区域被处理）。

⚠️ 该次调用消耗 `watermark-remove` 的匿名试用配额 **1 次**（不涉及任何计费）。
除此之外，本仓**没有**对真实上游发过任何其他请求。

## 5. 频控维度归因实验（2026-09-24：IP vs 身份/指纹）

两轴对照（完整矩阵与结论见 `docs/UPSTREAM.md §4.1`；**具体 IP 地址不入库**）：

| 组 | 身份 | 出口 | 序列 |
|---|---|---|---|
| A / A′ | 裸 python | 直连出口（此前已烧过 3 次） | 200,200,**451** ／ **451**,200,200 |
| B（×3 批） | **真实 Chrome**（bsk 页面上下文） | **系统代理出口** | 均夹 1 次 **451** |
| D | 裸 python | **系统代理出口（与 B 同 IP）** | 200,**451**,200 |
| C | 裸 python | **池出口 ×3（新连接 = 新 IP）** | 200,200,200 |

**结论：按出口 IP 的软限；身份/指纹不是维度**（同 IP 换身份行为同型；同身份换 IP 立刻恢复）。
方法与坑：

- **出口 IP 必须逐请求钉住**：用"同一条 keep-alive 连接上紧跟一次 echo"确认
  （qwen 轮教训：单独探到的 IP ≠ 实际用的 IP）；
- **浏览器的出口要单独验**：本机系统代理让 Chrome 从**另一个出口**出去而不是直连出口 ——
  页面内 fetch echo 被 CSP 挡，改用**导航到 echo 页读文本**
  （`bsk navigate https://ifconfig.me/ip` + evaluate）；
- **共享出口会继承他人的消耗**：某从未被我们用过的节点首发即 451。

同日另交付：**代理池出口**（`TEXTIN_PROXY_POOL`，默认空）——
离线单测 3 条（轮换/单例/脱敏）+ `scripts/probe.py --phases pool` 实测
（3 轮 3 个不同出口）+ 经服务客户端真实调用 `200`（3.43s）。全套用例 89 项全绿。

## 6. 关键裁决与理由（2026-09-24）

| 裁决 | 结论 | 理由 |
|---|---|---|
| 对外契约形态 | **同步直给**（不做假异步） | 上游同步单请求；另两个方案（两段式 / 两者都做）在 Jev 咨询中分别为 0.81 / 0.01，但同步最贴合上游特性、实现与失败面最小 —— **用户拍板取同步** |
| 结果交付 | 默认 `b64_json`，`url` 可选（落盘 + `/files/`） | Jev 分布 0.62；b64 自包含零配置，url 便于转发 |
| 未取证能力 | `text_auto_removal` **放开**；`ofd-to-image` **门禁** | 前者有 `result.image` 形态记录 + 匿名宽松配额证据（用户拍板）；后者连样本都没有 |
| 配额策略 | 都不自动重试；431 进静默窗、451 不进 | Jev 0.84；451 是多节点软限（偶发重试可能成功，交调用方决定） |
| 适配范围 | 全量三族（21 条） | Jev 0.94 |

Jev 咨询原文（`jev-1.13.0`，五项判定与把握度）见
`.workbuddy/memory/2026-09-24.md`；上游站点现状复核见 `UPSTREAM.md §8`。

## 6. 本机产物（不入库）

- `var/live/*`（§4 的真实产物与报告）、`var/media/*`（`response_format=url` 的落盘）
- `.env` 不存在（本服务匿名可用；要配 `TEXTIN_TOKEN` 时自行创建，已在 `.gitignore`）
