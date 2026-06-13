# 2026-06-13 · 策略中心 / 排行榜（按 strategy_id 跨会话聚合 + 多策略横向对比）设计 spec

> 目标一句话：在已有"会话列表 + 单会话详情"之上，加一个**跨会话聚合视角**——按 `strategy_id`
> 把历次回测聚合成"策略"，出排行榜（n_runs / 最新一次 / 历史最优 / 收益排行）与多策略横向对比
> （净值叠加 + 指标并排）。这是旧 [design/13](../../../design/13-p5-dashboard-design.md)「策略中心」概念在会话模型下的重建。
> **引擎/撮合/会话语义/JSONL 产物零改动**，纯增量：analytics 纯函数 + 只读端点 + 看板加页。

## 1. 背景与动机

- 一期（spec [2026-06-12](2026-06-12-dashboard-analytics-design.md)）补齐了**单会话**视角：指标对比、净值曲线、年月统计、
  多粒度持仓、调仓记录；二期（spec [2026-06-13-distribution-charts](2026-06-13-distribution-charts-design.md)）补了分布图表六页签。
  两者都是**单会话**视角。
- 一期 §9 二期 backlog 明确记着：**策略中心/排行榜——按 strategy_id 聚合历次会话、多策略对比**，
  "待多策略真实产生后再做"。现 state 里已有 demo-container / demo_bank_rotate·pyramid·limit·frenzy 等多笔真实回测，时机已到。
- 用户需求：每个策略看 `n_runs` / 最新一次 / 历史最优 / 收益排行；多策略横向对比（净值曲线叠加 + 指标并排）。

## 2. 已确认的范围决策（用户拍板 2026-06-13）

| 决策点 | 结论 |
|---|---|
| 聚合键 | **`strategy_id`**（honor 一期 §9 语义：strategy=逻辑、account=资金池）。重播种 demo 数据使其真实可演示。 |
| 代表行 + 默认排序 | 每策略**代表行=最新一次回测**（`created_at` 最大）；默认按代表行**总收益**降序；另出列 `n_runs` 与**历史最优总收益**。 |
| 多策略叠加 x 轴 | **日历轴为主**（各曲线各自 rebase 1.0）+ **「对齐起点（第 N 个交易日）」前端切换**；后端只出原始对齐序列。 |
| 对比视图端点 | **不加新端点**：前端复用每个 run 的现有 `/sessions/{id}/equity`、`/metrics` 自行叠加。 |
| 聚合层取数 | **读时薄聚合**（遍历 sessions → 读 summary.json + 会话行）；零物化、零缓存（YAGNI，对齐 1/2 期"每次读重算"）。 |
| 引擎/撮合/会话语义/产物 | **零改动**（summary.json 已含 total_return/max_drawdown/daily，足够聚合）。 |

## 3. 数据现实（实现前必读）

- `strategy_id` 落在 **sessions 表 `config_json`**（建会话时 `cfg["strategy_id"]`，默认 `"session"`），并由
  `session_finalize` 写进 `summary.json` 顶层。
- **summary.json 顶层键**：`strategy_id, initial_cash, cash, market_value, total_value, total_return,
  max_drawdown, realized_pnl, positions, trades, rejections, daily`。**不含** `account_id / start_date / end_date`
  ——这三个在 **sessions 表行**里。故聚合层须 join 两边：行给身份/区间/时间戳，summary 给指标/日序列。
- 重播种前的脏现实：4 个银行场景 examples **未传 strategy_id** → 全落默认 `"session"`，按 strategy_id 分组会把
  4 个互不相关场景错塞一组。**本 spec 的 §8 重播种是前置条件**，否则榜单语义是坏的。

## 4. 架构（模块划分，沿用 spec 2026-06-12 §3 模式）

| 单元 | 职责 | 依赖 | 测试方式 |
|---|---|---|---|
| `vortex_backtest/analytics.py` 新增 `strategy_rollup()` 等纯函数 | 输入=已抽好的 per-run 记录列表 → 按 strategy_id 分组、选 latest/best、算 n_runs、去重 accounts、排序。**不读文件不碰网络** | 仅基本类型 | 金标单测（手工构造记录断言分组/选择/排序/边界） |
| `app.py` 新增 2 个只读 GET 端点 | `GET /strategies`（排行榜）+ `GET /strategies/{strategy_id}`（策略详情含 runs 列表）。复用 `_session_summary`/`_strategy_series`/`_daily_rows` + `analytics.perf_stats` | analytics + store | TestClient API 测试 |
| `web/index.html` + `static/app.js`（加页） | 顶部导航（会话列表 ↔ 策略中心）+ 排行榜页（`#/strategies`）+ 策略详情页（`#/strategy/<id>`）+ 对比视图。复用已 vendor 的 Chart.js、现有令牌/表格样式 | 仅新端点 + 现有 per-session 端点 | 端点契约测试兜底 + 起服务人工冒烟 |
| `examples/session_scenarios.py`（重播种） | 每场景传 `strategy_id`；新增"同 strategy_id 跨多区间"多次回测使 `n_runs>1` 真实 | 容器服务 | 重跑后断言 n_runs>1 |

## 5. 新端点契约

基址同现有服务；全部只读 GET、无鉴权要求（与现有报告端点一致）。

### 5.1 `GET /strategies`

排行榜，一策略一行：

```jsonc
[{
  "strategy_id": "bank_rotate",
  "n_runs": 3,
  "accounts": ["demo_bank_rotate"],                  // 跑过该策略的去重 account_id（升序）
  "first_run": "2026-02-02", "last_run": "2026-06-09",  // 各 run end_date 的 min/max（缺则 null）
  "latest": {                                         // 代表行 = created_at 最大的 run
    "session_id": "...", "account_id": "demo_bank_rotate",
    "start_date": "...", "end_date": "...", "status": "closed",
    "total_return": …, "annual_return": …, "sharpe": …, "max_drawdown": …,
    "volatility": …, "n_days": …, "low_confidence": true,
    "created_at": "...", "updated_at": "..."
  },
  "best": {"session_id": "...", "total_return": …}    // 历史最优（total_return 最大的 run）
}]
```

- 默认按 `latest.total_return` 降序返回；前端表头列可点击改排序（纯前端，不重取）。
- 无任何会话时返回空表 `[]`。

### 5.2 `GET /strategies/{strategy_id}`

策略详情：

```jsonc
{
  "strategy_id": "bank_rotate", "n_runs": 3,
  "accounts": ["demo_bank_rotate"],
  "runs": [{                                          // 按 created_at 升序
    "session_id", "account_id", "start_date", "end_date", "status",
    "total_return", "annual_return", "sharpe", "volatility",
    "max_drawdown", "n_days", "low_confidence", "created_at", "updated_at"
  }],
  "latest": {…同 5.1 latest}, "best": {…同 5.1 best}
}
```

- strategy_id 无任何会话 → 404 `{"error": "strategy_not_found"}`。

### 5.3 对比视图（无新端点）

前端从排行榜复选篮拿到选中策略的 `latest.session_id` 列表，对每个 `session_id` 调既有
`GET /sessions/{id}/equity`（拿对齐净值序列）和 `GET /sessions/{id}/metrics`（拿指标），前端叠加/并排。
策略详情页的"全部 runs 叠加"同理，对该策略每个 run 的 `session_id` 取 `/equity`。

## 6. 口径（集中定义，单一真值）

- **latest（最新一次）** = `created_at` 最大的 run；tie-break `session_id` 字典序。语义=最近一次发起的回测。
- **best（历史最优）** = `total_return` 最大的 run；tie-break `created_at` 较新者，再 `session_id`。
  选总收益而非夏普——避开短样本夏普噪声（一期 §5 已定 <60 日 low_confidence）。
- **per-run 指标来源**：`total_return/max_drawdown/annual_return/sharpe/volatility` **全部**由
  `analytics.perf_stats` 对该 run 的**日净值序列**算出——该序列经 `_strategy_series`（首交易日前注入
  `initial_cash` 基线锚点）构造，与 `/metrics` **完全同口径**（刻意：排行榜/策略详情某 run 的各指标
  与其单会话 `/metrics` 页严丝合缝一致）。其中 `total_return` 与 summary 数值等价；`max_drawdown` 用
  基线锚点序列（同 `/metrics`），可能比 summary 的回撤更深一档（捕捉首日相对期初本金的回撤）——
  排行榜口径对齐 `/metrics`，不对齐会话列表页的 summary 回撤，二者本就并存。`n_days` = 实际交易日数
  = `len(daily)`（不含基线锚点）。
- **low_confidence**：`len(daily) < analytics.LOW_CONFIDENCE_DAYS`（60），沿用一期约定。看板对 `low_confidence`
  的行**只置灰风险调整/年化列**（年化 `annual_return`、夏普 `sharpe`、波动率 `volatility`）+ title 提示；
  累计收益 `total_return` 与回撤 `max_drawdown` 不置灰（沿用一期/design13 §7.2：短样本护栏只约束风险调整与年化指标）。
  三视图统一：排行榜代表行用 `latest.low_confidence`、策略详情 runs 表用每 run 的 `low_confidence`、
  对比表用各代表 run `/metrics` 的顶层 `low_confidence`。
- **open 会话**：照常进榜/进 runs，按当前累积态即时归约（`_session_summary` 对 open 的现有行为）。
- **n_runs**：该 strategy_id 下会话总数（含 open）。
- **accounts**：该 strategy_id 下去重 account_id 升序列表（一个策略可能在多个 account 上跑过）。
- **first_run / last_run**：各 run `end_date` 的 min / max；run 无 end_date 则不计入，全缺则 null。
- **叠加图 x 轴**：后端 `/equity` 已出 `{dates, strategy:[1.0,…]}`；前端两种轴均本地算——
  日历轴直接用 `dates`；相对日轴用序列下标（第 N 个交易日），不另立后端口径。

## 7. 看板（加页，沿用无构建链静态 SPA + vendored Chart.js）

### 7.1 信息架构 / 路由

```
顶部导航：[会话列表 #/]   [策略中心 #/strategies]
#/strategies (排行榜) ──点策略行──▶ #/strategy/<strategy_id> (策略详情)
   │  勾选多策略 → [对比选中] 按钮 → 对比视图（叠加各 latest run + 指标并排）
#/strategy/<id> ──点 run 行──▶ #/session/<session_id> (既有单会话详情)
```

- 新增 hash 路由 `#/strategies` 与 `#/strategy/<strategy_id>`；既有 `#/` 列表、`#/session/<id>` 详情不变。
- 顶部导航在所有页可见，两入口互切。

### 7.2 排行榜页（`#/strategies`）

- 可排序表：列 = 策略 / `n_runs` / accounts / 代表行(总收益·年化·夏普·最大回撤) / 历史最优总收益 / 区间(first→last)。
- 默认按代表行总收益降序；点表头列切排序（纯前端）。`low_confidence` 行的风险调整列置灰标注（沿用一期）。
- 行点击进策略详情；行首复选框做"对比篮"，选 ≥2 后出 **[对比选中]** 按钮 → 对比视图。

### 7.3 策略详情页（`#/strategy/<id>`）

- 头部卡：`n_runs` / 最新一次（链接到其会话详情）/ 历史最优（链接）。
- runs 表：每行一次回测（区间 / 状态 / 总收益 / 年化 / 夏普 / 最大回撤 / 发起时间），点行进 `#/session/<session_id>`。
- **该策略全部 runs 净值叠加图**（Chart.js）：日历轴 default + 「对齐起点」切换；图例可开关各 run。
- runs 指标并排表（行=指标，列=各 run）。

### 7.4 对比视图（排行榜复选篮触发）

- 选中策略各取其 `latest.session_id`，对每个调 `/equity` 叠加净值（同款日历/相对日切换）+ `/metrics` 指标并排表。
- 可选叠加沪深300（复用现有 benchmark 下拉），**default 关**——避免跨区间基准对齐复杂度，作锦上添花。

### 7.5 删 mock

接口失败显式横幅报错（沿用一期约定），绝不静默播假数据。

## 8. Demo 重播种（前置条件，否则榜单语义坏）

改 `examples/session_scenarios.py`：

1. 每个银行场景建会话时传 `strategy_id`（= 其 account 名，如 `bank_rotate`/`bank_pyramid`/`bank_limit`/`bank_frenzy`）。
2. **新增一个跨区间多次回测**：同 `strategy_id`（如 `bank_rotate`）跨 2-3 个不同区间各跑一次 close，使 `n_runs>1`、
   `latest`/`best` 落到不同 run，真实可演示。
3. **重跑前先清旧 demo state**：删 `~/vortex/state/reports/sessions/*` 中遗留的 `strategy_id="session"` 脏会话
   （及其 sessions 表行），避免脏组混入榜单。重播种对容器服务（127.0.0.1:8766，带 `VORTEX_BACKTEST_TOKEN`）重放。
   清理与重放的具体机制（脚本加 `--reset` 还是手工清）在实现计划里定。

## 9. 测试策略（对齐 spec 2026-06-12 §8）

- `analytics.strategy_rollup()` 金标单测：多 run 选 latest（created_at 最大）/ best（total_return 最大）、
  created_at tie-break、单 run、空输入、含 open 会话计入、accounts 去重升序、first/last_run 缺 end_date 边界。
- 端点 API 测试：TestClient + fixture 造多 strategy_id × 多 run 会话，断言：排行榜默认排序 / 代表行字段 / n_runs /
  历史最优；策略详情 runs 列表升序 / 404；空库返回空表。复用既有 session fixture 惯例。
- 重播种后断言：至少一个 strategy 的 `n_runs>1`，且 `latest.session_id != best.session_id` 至少一例。
- 前端：不上前端测试框架；端点契约测试兜底 + 起服务人工冒烟（开发自验流程写入计划）。

## 10. 范围边界

- 引擎/撮合/会话语义/JSONL 产物格式**零改动**（只读消费）。
- 不动 vortex_data。
- 不做用户/权限/多租户；看板仍是只读视图。
- 对比视图基准叠加 default 关、为可选项，不在本期做跨区间基准对齐的完整口径。
- 不做"从看板发起回测"（一期 §9 另一条 backlog，依赖写接口鉴权联动，单独议）。
