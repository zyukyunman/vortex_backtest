# 19 · 对抗性测试 campaign（测试计划 + 已验证 bug 清单）

> 视角：**跨两服务的对抗性测试**。本轮目标不是跑现有单测（已绿），而是专门"捅没想到的地方"——
> 用 ultracode 多 agent 扇出（侦察 + 7 攻击面 + 逐 bug 对抗验证，共 15 agent）。
> 关联：[18 会话引擎](18-session-backtest-engine.md) · [data design/18 网关](../../vortex_data/design/18-backtest-data-gateway.md)。
> 日期：2026-06-08。bt 在 `main`，data 在 `improve/phase-1-6`（含端口迁移 WIP 14 文件，未动）。

---

## 0. 头号结论

**未来函数闸门——整个设计的命门——未被攻破。** 对每种可见性 kind（ts / daily_at / effective_from / range /
daily_snapshot / none / unsafe）× 每条网关路由（read_window / read_symbols / query / operate / 快照 count=1）×
盘中（10:00 看不到当日 close）/ 集合竞价 09:25 / 财报 ann_date / 成分生效区间 的泄露尝试，**全部被正确挡住**。
本轮找到的"未来函数类" bug 都是 **fail-loud 崩溃（HTTP 500/400）**，不是静默泄露——
即"会吵闹地坏掉"，而不是"悄悄给你越界的数据"。这是最重要的安全验证结果。

**22 个 bug 全部经独立对抗验证为真（is_real=true），无 P0。** 严重性口径：
- **P0** = 静默未来函数泄露 / 静默金额错 / 数据损坏 —— 本轮 **0 个**。
- **P1**（9 个）= 合理场景下产出错结果。
- **P2**（13 个）= 退化 / 边界 / 性能 / 健壮性债。

---

## 1. 测试计划（分层 + 优先级）

| 层 | 覆盖 | 落点 | 状态 |
|---|---|---|---|
| **L0 单元/纯函数** | 撮合/费用/手数/tick/T+1 规则 | 既有 `market_rules`/`replay_engine` 测试 | 已绿（基线）|
| **L1 组件（引擎）** | `advance()` 状态机、停泊、跨日、公司行动入账 | `test_adv_n8_corporate_actions.py`、`test_adv_boundary.py` | **新增** |
| **L2 组件（网关）** | 可见性 kind × 路由 闸门、算子下推、fail-closed | `test_adv_future_function.py` | **新增** |
| **L3 契约** | 错误码 400/401/409/422/502/503、鉴权、单调时钟、as_of 必填 | `test_adv_contract_backtest.py`、`test_adv_contract_data.py` | **新增** |
| **L4 集成（跨服务/真实数据）** | 起 data 服务 + backtest，真实除权/停牌/一字板/ST 跑会话 | `test_adv_integration_realdata.py` | **新增** |
| **L5 性质（确定性）** | 同输入两跑逐字节一致、序列化幂等 | `test_adv_determinism.py` | **新增** |
| **L6 性能/规模** | 全市场算子扫描、分钟密集每步开销、内存 | `test_adv_perf_scale.py`（`@slow`）| **新增** |

**优先级排序（先修哪个）**：P1 集中在 bt 侧的会计/可用性（N8-3/N8-4/N8-2/B2/CTR-2）+ 数据契约（RAWGAP-1）+
data 侧核心路径性能崩溃（FUT-PERF-1）。data 侧闸门类 bug 多为**休眠**（影响当前网关调用方未取的数据集）。

### 新测试文件清单（8 个，24 个 bug-repro 一律 `xfail(strict=False)` 保套件绿）

- bt：`test_adv_boundary.py`(30)、`test_adv_contract_backtest.py`(75)、`test_adv_determinism.py`(7)、
  `test_adv_integration_realdata.py`(9, `@integration/@slow`)、`test_adv_n8_corporate_actions.py`(15)。
- data：`test_adv_future_function.py`(46)、`test_adv_contract_data.py`、`test_adv_perf_scale.py`(12, `@slow`)。
- 真实数据靶子（侦察确证）：除权日 `000630.SZ@20260608`（cash_div_tax=0.05，adj 30.3727→30.6085，0605/0608 各 241 bar）；
  停牌+ST `000004.SZ@20260506`（halt 但有 241 根 volume=0 平 bar）；一字板 `000517.SZ@20260608`。

---

## 2. 已验证 bug 清单（22，repro + 修，验证者已逐条审）

### P1（9）

| ID | 位置 | 现象 | 修 |
|---|---|---|---|
| **N8-3** | `session_engine.py:411-423` | 除权日 NAV 快照用**拆股前股数 × 除权后 RAW 价** → 幻影回撤（10送10 → maxDD −27% vs 真实 −0.0167%）。**即使日级步进也发生**，腐蚀所有拆股股的回撤/Sharpe；total_return 正确。 | 把 `apply_corporate_actions` 提到除权日**首个 bar 的快照之前**（随跨日 unlock 一起，逐日触发，保 once-per-(sym,ex_date)）|
| **N8-4** | `session_engine.py:309-330,422` | 粗粒度 advance 对**除权日之后**才建的仓也补分红（幻影分红/回看泄露）| 同 N8-3 结构修一并解决（按除权日当日持仓入账）|
| **N8-2** | `gateway_adapter.py:177` → `query.py:125-129` | load_dividends 不带 window → 网关 count=1 → 每 symbol 只回最近一笔，粗窗口内早除权日静默丢 | 请求加 `window.range`（走 read_symbols，effective_from 闸门仍在）|
| **B2** | `app.py:269-271` | set_universe 踢掉持仓股 → 取 bar 只取新股池 → 该股**永远卖不掉**（no_market_data）；NAV 靠 stale 估值不归零但仓被困 | `symbols = set(...) | set(rt.positions.keys())`（仿同函数 div_symbols 先例）|
| **CTR-2** | `gateway_adapter.py:53-61` | 网关 ConnectError/Timeout 未包 → `/data` 500（应 502）、`/advance` 500（应优雅降级空帧）| `except httpx.HTTPError → GatewayDataError`（单点修，现有 502/降级 handler 自动对）|
| **RAWGAP-1** | 数据契约（`gateway_adapter.py:182`→入账休眠）| 真实 dividend **无 ex_date 列** → load_dividends→[] → N8 入账真实数据上休眠 → 持分红股跨除权日 NAV 静默少算一笔（000630.SZ 实测少 50=1000×0.05）| **data 侧 normalize 保留 ex_date**（机制 `preserve_date_fields` 已在；**存量 dividend 需重抓**）；bt 侧先 DIVFIELD-1 优雅降级 |
| **FUT-1** | `visibility.py:223-229` | RANGE 闸门 `CAST('' AS BIGINT)` → ConversionException（非 IOException，未被 catch）→ 500，活跃成分消失。当前休眠（真实 stock_basic.delist_date 全 NULL）| `CAST` 改 `TRY_CAST`（空/非数 → NULL，活跃=可见、未知起=排除）|
| **FUT-2** | `query.py:117-124` | range+无 symbols 路由硬编 `filters['date']` → 对 date 列≠'date' 的集（dc_member.trade_date 等）KeyError 400 | 解析真实 date 列；与 FUT-4 合并成 `storage.rows()` 路由 |
| **FUT-PERF-1** | `parquet_duckdb.py:664` | 全市场 read_window 在真实 stk_mins（10989 文件 glob）上**非确定崩溃**（"don't know what type: \r"）| **建 `stk_mins_by_date` 镜像（design P0）**（验证者推翻 agent 的"混合 Arrow 编码"诊断——symbol 全 large_string；CAST workaround 9/10 仍崩）|

### P2（13）

| ID | 位置 | 现象 | 修 |
|---|---|---|---|
| **DIVFIELD-1** | `parquet_duckdb.py:644`→`gateway_adapter.py:177` | 缺字段(ex_date) 硬 raise KeyError→400，未走文档承诺的优雅降级 [] | adapter 请求 omit `fields`（走无字段快照路，已证 PIT 安全）|
| **N8-1** | `session_engine.py:303-330` | `apply_corporate_actions` 无函数内去重 → 重复 (sym,ex_date) 行双计现金/股 | 函数内按 (norm_sym, ex_ts) 去重再入账 |
| **N8-5** | `session_engine.py:320-327` | 负 cash_div_tax 扣现金；负 bo/co 减股数（脏数据未夹）| `cash_div=max(.,0)`、`ratio=max(.,0)`（夹在 bo+co 回退之后）|
| **B1** | `session_engine.py:400-407,418-420` | 跨**无 bar 日**后该日 bar 迟到 → T+1 解锁丢、bar 被 dedup 静默吞。**自愈**（下个新日 bar 一到即补）| 解锁改按时钟 `to_key`（`to_key>current_date_key` 严格防同日重解锁）|
| **B3** | `session_engine.py:229,135-138` | 拒单 `setdefault(Position())` 先于 validate → 留零仓持久化（按 symbol 有界、不污染 NAV）| 拒前用 `get(...)`，仅成交前 `setdefault`（Alt-A）|
| **B4** | `app.py:309,329` | close/daily 的 calendar 来自快照时间戳非交易日历 → 丢停牌/缺口日，回撤连续性失真 | **两段**：①data 侧 `calendar` 改 `stk_limit['date']` 派生（分钟无 bar 日也含，gateway_adapter.py:162）②`_cal` 串进 session 喂 finalize。单做②无效 |
| **FUT-3** | `visibility.py:193` | ts 闸门 `CAST('20260505' AS TIMESTAMP)` 崩（YYYYMMDD 是 tushare 原生 ann_date 格式）。休眠（文本集当前缺）| `COALESCE(TRY_CAST(ts), TRY_STRPTIME(.,'%Y%m%d'))` |
| **FUT-4** | `query.py:121-123` | range 无 symbols 路由静默丢 `window.range.end` → 过宽历史窗（非泄露，as_of 仍兜）| 用 `storage.rows()` 同时认 start/end（含 FUT-2 的 date 列解析）|
| **FUT-5** | `query.py:110-124` | daily_snapshot 的"只取最新一期"在 range 路由未实施 → 回多期陈旧成分（count=1 路由对）| range 路由先 resolve 最新一期 `≤ as_of` 再过滤 |
| **DET-1** | `session_engine.py:128,408` | 持久化 config_json(last_prices) 字节序依赖输入行序（被上游 sort 掩盖）| dump 里 `dict(sorted(last_prices.items()))` |
| **DET-2** | `session_engine.py:308` | corporate_actions.jsonl 同 ex_date 行序依赖输入序（被上游 ORDER BY 掩盖）| sort key 改 `(ex_ts, norm_symbol)` |
| **CTR-1** | `app.py:268` | 畸形 `to`（不可解析日期）→ `pd.Timestamp().date()` 在 try 外抛 → 500（应 400）| 包 `pd.Timestamp(to_ts).date()` → 400 `invalid_to` |
| **FUT-PERF-2** | `parquet_duckdb.py:332+` | 每次网关调用对 10988 文件 glob 跑 ~0.63s DESCRIBE 税（分钟回测 241 步/日 = 数百秒/日固定开销）| 持久 DuckDB 连 + 缓存 read_parquet 视图（design §3.4/§8-P2 已留）；闸门谓词仍每次注入 |

---

## 3. 修复的服务边界

- **bt 侧（`main`，干净）**：N8-1/2/3/4/5、B1/B2/B3、DET-1/2、CTR-1/2、DIVFIELD-1。多为 P1 会计/可用性，价值最高。
  结构修（N8-3/N8-4 把入账提进 bar 循环）须用 golden `test_golden_a_equals_b` + `test_golden_raw_vs_qfq` 守不回归。
- **data 侧（`improve/phase-1-6`，有 WIP）**：FUT-1/2/3/4/5、FUT-PERF-1/2、B4 的①、RAWGAP-1 的 ex_date 保留。
  多为休眠或基建（by-date 镜像/持久连）；改动须与用户的端口迁移 WIP 分开。RAWGAP-1 还需**重抓 dividend** 解锁存量。

## 5. 修复实施状态（2026-06-08，已落地，未提交）

**20/22 已修并落地为硬断言回归测试**（测试套件全绿；bt 148 passed/1 xfail，data 263 passed + adv 91 passed）。

- **bt（main，全修，含结构性）**：N8-1/2/3/4/5、B1/B2/B3/B4、DET-1/2、CTR-1/2、DIVFIELD-1 = 15 个全修。
  - 结构性 N8-3/N8-4：把 `apply_corporate_actions` 提进 advance 的逐 bar 循环、在除权日首个 bar 的
    last_prices/快照**之前**按入场持仓入账（`only_date` 逐日触发 + `applied` 跨调用去重 + `exclude_dates`=有 bar
    日不被收尾 sweep 二次入账）。golden A==B / raw_vs_qfq 不回归。
  - N8-2：`load_dividends(start=)` 带 window → 走 read_symbols 取回窗口内全部除权行；app 透传 `from_d`。
  - B4：双段都落地——gateway_adapter 的 calendar 并入 `stk_limit['date']`（含停牌日）+ app 持久化
    `calendar.jsonl` 喂 finalize。
- **data（improve/phase-1-6，全修闸门类）**：FUT-1/2/3/4/5 = 5 个全修（visibility.py TRY_CAST/COALESCE-STRPTIME，
  query.py rows() 路由 + daily_snapshot 网关层收口）。
- **2 个保留为 documented xfail（非快速可修，待你决定）**：
  - **RAWGAP-1**（bt 集成）：N8 入账逻辑正确但真实 dividend 落盘缺 ex_date → 入账被饿死。**需重抓 dividend**
    （data-ops）+ data 侧 normalize 保留 ex_date。已落地 DIVFIELD-1 使其**优雅降级**（会话不崩、只是不入账），
    xfail 测试 `test_exday_raw_leaves_unbacked_nav_gap` 持续记录该 gap。
  - **FUT-PERF-1**（data 性能）：全市场 read_window 在 10989-文件 glob 上非确定崩溃，正解=建 `stk_mins_by_date`
    镜像（design §5.3 P0 基建），非快速可修；xfail 记录。
- **未触碰**：data 的 14 个端口迁移 WIP 文件。注：`test_docs_page.py::test_key_facts_present` 因 WIP 改了端口
  （8876）而**预先失败**，与本轮无关。

## 4. 验证者标记的"修复需注意"

- **B4**：agent 的单段修（仅串 `_cal`）**无效**——两侧 calendar 都来自分钟 bar，停牌日本就缺；须先 data 侧从 `stk_limit` 派生。
- **FUT-PERF-1**：agent 的 CAST workaround **9/10 仍崩**，且其"混合编码"根因诊断被验证推翻；正解是 by-date 镜像。
- **FUT-2/FUT-4** 改同一处，合并为一个 `rows()` 路由。
- **N8-3/N8-4** 同一结构修；务必保跨 advance 的 once-per-(sym,ex_date) 与首个新日 bar 仍触发跨日 unlock。
