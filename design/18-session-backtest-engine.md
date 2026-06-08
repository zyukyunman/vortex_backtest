# 18 · 会话式回测引擎（sessions/data/advance/close）+ 跨服务契约

> 视角：**vortex_backtest 服务开发者**。
> 本文把回测从"订单回放(A)"迁到"会话步进(B)"：策略**醒来 → 取数 → 决策 → 指定下次醒来**，
> 服务端控模拟时钟、强制 `as_of` 防未来函数。
> 配套 data 侧实现：[vortex_data design/18 · 取数网关 + PIT 落盘布局](../../vortex_data/design/18-backtest-data-gateway.md)。
> 取数语义需求源：[17 · 回测对 vortex_data 的数据需求](17-vortex_data-data-requirements.md)。
> 关联：[16 分钟级升级](16-minute-level-upgrade.md) · [10 API 协议](10-api-protocol.md) ·
> 引擎 [`replay_engine.py`](../vortex_backtest/replay_engine.py) · 内核 [`market_rules.py`](../vortex_backtest/market_rules.py) ·
> 数据 [`data_adapter.py`](../vortex_backtest/data_adapter.py)。

---

> **实现状态（2026-06-08）**：B1–B6 已落地并测试（15 用例绿，含金标 A==B）。
> 会话存储 [`store.py`](../vortex_backtest/store.py) sessions 表；引擎 [`session_engine.py`](../vortex_backtest/session_engine.py)
> （从 replay_engine 抽 step，内核复用，单调时钟 + next_bar 停泊 + T+1）；端点
> [`app.py`](../vortex_backtest/app.py) `POST /sessions|/advance|/close` + 报告 GET；网关消费
> [`gateway_adapter.py`](../vortex_backtest/gateway_adapter.py)（`POST /api/v1/data`，**前复权PIT锚点**，缺字段降级），
> `VORTEX_DATA_URL` 配则走网关、否则回退本地直读。金标 [`tests/test_golden_a_equals_b.py`](../tests/test_golden_a_equals_b.py)
> 证明 A==B（逐笔成交/NAV/T+1拒单一致）。
> 待办：删除旧 A 的 HTTP 面（/backtests/worker/jobs/A-only models）——已派发独立清理任务（replay_engine 作金标 oracle 保留）。

## 0. 一句话

把"一次性把全量订单交给引擎跑完"换成"建会话 → 按 `sim_time` 逐步推进；每步策略向带时间闸门的 data 网关取
`≤ sim_time` 的数据、下单、撮合一步、拿回账户上下文 → close 出报告"。**撮合/T+1/费用/滑点/NAV 内核 100% 复用**，
变的只是最外层"客户端怎么和服务交互"。

---

## 1. 现状 A → 目标 B

### 1.1 现状：订单回放（A），批处理

今天是 FastAPI 异步作业服务（`app.py` 650 行）：
`POST /accounts` 建账户 → `POST /accounts/{id}/orders` 挂单（带 `trade_date` + 可选 `exec_time`）→
`POST /backtests` 入 SQLite 队列、202 + `job_id` → 轮询 `GET /backtests/{job_id}` 到终态 → 拉
`/summary /daily /trades /rejections /equity /minutes /metrics` 报告。

引擎 `MinuteReplayEngine`（`replay_engine.py:41-144`）：把整窗口分钟数据**一次性预读**（`data_adapter.load`，`:93`），
把订单**预先解析、停泊**到目标分钟（`:171-183`），然后**唯一的时钟**就是
`for timestamp, rows in strategy_minutes.groupby("trade_time")`（`:193`）把所有分钟 bar 走到底。
成交永远是**当根（this-bar）**（`:204-256`）。没有外部时钟、没有逐步 advance、没有策略回调、没有 PIT 取数闸门。

### 1.2 目标：会话步进（B）

| 维度 | A 订单回放（现状） | B 会话步进（目标） |
|---|---|---|
| 时钟 | 引擎内部 groupby 隐式走完 | **会话持有 `sim_time`，advance() 显式单调推进** |
| 订单 | 预先批量入库、整批喂引擎 | **每步增量提交，即时撮合该步** |
| 数据 | 一次性预读全窗口（自持未来数据）| **每步按 `as_of=sim_time` 向网关取切片，服务端强制闸门** |
| 成交时点 | 当根（this-bar） | **默认下一根（next-bar）防未来；this-bar 可选** |
| 生命周期 | 提交即冻结、跑完即终态 | **长驻可推进会话，主动 close** |
| 报告 | 读已完成 job 的 summary | **读会话到当前 `sim_time` 的累积产物** |

A 不是被删除，而是**成为 B 的一个特例**（决策全已知：开会话→把全部订单连 `exec_time/price_type` 一次提交→
一路 advance 到 end，从不调用 `/data`）。

---

## 2. 会话生命周期与状态机

```
POST /sessions ──创建──► [OPEN] ◄──┐
                          │ advance │  (循环：data? → advance)
                   POST /sessions/{id}/data   (只读，可省略)
                          │         │
                   POST /sessions/{id}/advance ┘  (撮合本步→推进时钟→结算→回账户上下文)
                          │
                   POST /sessions/{id}/close ──► [CLOSED]  (跑 reducer，出最终报告)
                          │
                   GET /sessions/{id}/report|summary|daily|trades|... (会话期间即可读"当前态")
```

### 2.1 会话状态（服务端持有，跨 advance 存活）

> 这些在 A 里是 `_run_strategy` 的栈内局部变量（`replay_engine.py:185-191`），B 里必须升格为持久会话对象。

| 状态 | 类型 | 说明 / A 对应 |
|---|---|---|
| `sim_time` | `pd.Timestamp` | 单调模拟时钟；替代 `:193` 的 groupby 迭代器。advance 拒绝 `to ≤ sim_time` |
| `current_date_key` | `int` | 日界标记；跨日时显式调 `unlock_positions()`（替代 `:198-200` 的隐式触发）|
| `level` / `start` / `end` | enum / date | `daily｜1min`；驱动步进节奏与网关 `level` |
| `universe` | `set[str]` | 当前股池；open 设定、advance 可 `set_universe` 替换；喂网关 `symbols="universe"` |
| `account.cash` | float | 现金 |
| `account.positions` | `dict[str, Position]` | `Position{quantity, cost_basis, sellable_quantity}` **原样复用**（`:23-27`）|
| `account.last_prices` | `dict[str, float]` | 最新价（估值用）|
| `trade_counter` | int | 单调成交号（execute_order 用）|
| `open_orders` | 停泊队列 | next-bar 成交队列：按 `(symbol, 下一可成交 bar)` 停泊，时钟到达即撮合；A 的 `orders_at`(`:175-183`) 的会话版（增量填充）|
| `outputs` | trades[]/rejections[]/snapshots[] | 累积产物；`MinuteSnapshotOut`(`models.py:230-240`，**现已定义但无端点用**) 正好做每步全快照载体 |
| `config` | 快照 | `AShareRuleEngine`+`FeeModel`、`slippage_bps`、`default_price_type`、`fill_timing`(默认 next_bar)、`market_data_set_id`——open 时建一次，每步复用 |

### 2.2 新增存储（`store.py`）

`store.py` 现仅 `accounts/orders/jobs/strategy_meta` 表（`:13-68`）。新增 **`sessions` 表**：
`session_id / status / clock / cash / positions_json / universe_json / config_json / created_at / updated_at`。
持仓序列化进 session 行，避免每步重新派生。逐步快照仍走 CSV 产物（沿用 `:296-305` 规避 SQLite 膨胀），
会话累积 NAV 重新定位存储。

---

## 3. 接口定义

> 鉴权全部沿用 `require_write_auth`（`app.py:43-69`，`VORTEX_BACKTEST_TOKEN` → `Bearer`/`X-Auth-Token`，
> 未配则仅回环放行）。账户/订单数据模型（`models.py:56-124`）与报告输出模型（`TradeOut/RejectionOut/
> PositionOut/DailySnapshotOut/MinuteSnapshotOut`，`:174-280`）基本沿用。

### 3.1 `POST /sessions` — 建会话

```jsonc
{
  "account_id": "acc-1",            // 复用 AccountCreate；或内联 initial_cash
  "level": "1min",                  // daily | 1min
  "start_date": "2026-05-06",
  "end_date":   "2026-05-29",
  "universe":  ["600519.SH", "..."],// 可选初始股池；缺省空，后续 set_universe
  "fill_timing": "next_bar",        // next_bar(默认) | this_bar
  "default_price_type": "close",
  "execution": { "slippage_bps": 5, "fees": {...}, "participation_cap": 0.1 },
  "market_data_set_id": "default-qfq"
}
```
→ `201 { session_id, sim_time, status:"open", account:{cash,positions:[],nav} }`。
**此处不加载任何数据**（网关按需 lazy 取）。

### 3.2 `POST /sessions/{id}/data` — 取数（只读，无 A 对应）

策略"醒来"后问数据。**服务用自己的 `sim_time` 当 `as_of`，绝不信客户端传的时间**——这就是防未来函数闸门。
body 直接转发 data 网关（见 §跨服务契约）：

```jsonc
{
  "datasets": [
    { "dataset":"stk_mins", "symbols":"universe", "fields":["close","volume"],
      "level":"1min", "window":{"count":20} },
    { "dataset":"stk_factor_pro", "symbols":"all", "op":{"kind":"topn","by":"pct_chg","n":50,"order":"DESC"} }
  ]
}
```
→ `200 { as_of, results:{ "<dataset>": {columns, rows} } }`，全部 `≤ sim_time`。
"循序渐进"：第一次给小股池，后续 `set_universe` 粘住，需要时才 `symbols:"all"` 看全市场。

### 3.3 `POST /sessions/{id}/advance` — 推进一步（核心）

```jsonc
{
  "orders": [                       // 本步委托，可多笔；复用 OrderCreate 字段
    { "symbol":"600519.SH", "side":1, "quantity":100, "exec_time":"10:31", "limit_price":null }
  ],
  "set_universe": ["..."],          // 可选：改下一步股池
  "to": "next_bar",                 // next_bar | next_day | "2026-05-06T13:00:00"
  "request_id": "uuid"              // 幂等：重试不重复成交/重复推进
}
```
→ `200 { sim_time, account:{cash, positions:[{symbol,quantity,sellable,cost_basis,last_price}], nav}, filled:[...], rejected:[...], open_orders:[...] }`。

`to` 必须 `≥ sim_time`（单调，自动挡掉"回到过去下单"）。**账户上下文随每步返回**，策略无需额外取数往返。

### 3.4 `POST /sessions/{id}/close` — 收尾

跑 reducer（`daily_from_minutes` + 聚合 + `write_reports`），落最终 summary，会话置 `CLOSED`。
→ `200 { session_id, status:"closed", summary:{total_return, max_drawdown, realized_pnl, ...} }`。

### 3.5 报告端点（沿用，语义放宽）

`GET /sessions/{id}/{summary|daily|trades|rejections|equity|minutes|metrics}`：几乎原样复用
`app.py:224-404`，但读**会话累积产物到当前 `sim_time`**，而非已完成 job 的 `summary_json`——
`_completed_summary_or_404`(`:604`) 从"job.status==completed"放宽为"会话当前快照"，**会话期间即可读**。

---

## 4. 一次 advance 内部发生什么

> 就是把 A 的**单根 bar 循环体**（`replay_engine.py:202-271`）抽出来，每步跑一次（而非 groupby 里跑 N 次）。

1. **目标解析**：对每笔提交订单跑 `resolve_order_target`(`:397-424`)，按 `fill_timing`——
   `next_bar` 把订单停泊到**下一可成交 bar**、本步**不成交**；`this_bar`/带 `exec_time` 在当前 bar 即时成交（同今天 `:204-256`）。
2. **跨日解锁**：若 `to` 跨日界，显式调 `unlock_positions(positions)`(`:427-429`)（替代 `:198-200` 隐式触发）。
3. **撮合到期单**：对目标 bar 上到期的停泊单——算滑点 `exec_price=fill_price*(1±slip)`(`:207-214`) →
   `validate_order`(`market_rules.py:40-122`) → `executable_quantity` 量能裁剪部分成交(`:124-173`) →
   `execute_order`(`:432-484`) 记账（买入加权成本、卖出实现盈亏 + T+1 扣减、费用） → 追加 trade/rejection、清空持仓。
4. **推进时钟**：`sim_time → to`（`next_bar｜next_day｜具体时刻`），拉该 bar 收盘刷新 `last_prices`。
5. **快照**：在新时钟点取 `minute_snapshot`/日快照(`:505-537`)。
6. **回上下文**：返回新 `sim_time` + 账户（cash/positions/T+1 可卖/挂单/NAV）。`set_universe` 改下一步 `/data` 的股池。

---

## 5. 复用内核映射（签名不变，直接调）

| 内核函数 | 位置 | advance 里怎么用 |
|---|---|---|
| `resolve_order_target` | `replay_engine.py:397-424` | 每步把订单定位到 bar/价格字段（`axis` 改为当前步的 as-of 分钟轴）|
| `AShareRuleEngine.validate_order` | `market_rules.py:40-122` | 纯逐 bar 合法性（停牌/tick/手数/涨跌停/资金持仓不足/T+1 不可卖）|
| `executable_quantity` | `market_rules.py:124-173` | 量能参与率裁剪 + 分板手数 → 部分成交 |
| 滑点 `exec_price` | `replay_engine.py:207-214` | 买上卖下；抽成 fill-price helper，逻辑不变 |
| `execute_order` | `replay_engine.py:432-484` | 原子撮合/记账，完全 loop-agnostic |
| `FeeModel.costs` | `market_rules.py:11-27` | 佣金 min5 / 印花仅卖 / 过户 |
| `unlock_positions` | `replay_engine.py:427-429` | advance 跨日界显式调 |
| `minute_snapshot`/`position_rows` | `replay_engine.py:505-570` | 每步 NAV 快照、最终持仓估值（节奏跟时钟）|
| `daily_from_minutes`/`max_drawdown`/聚合/`write_reports` | `replay_engine.py:573-773` | **close 时**跑，对累积产物归约，不变 |

**留在引擎本地、不下放给 data**（data 无状态、不感知会话）：模拟时钟与单调推进、整套撮合/结算/NAV 内核、
账户/持仓/现金状态与 T+1、订单定位、滑点、fill-timing 策略、报告聚合与写盘。**data 只交付 PIT 数据切片/算子结果。**

---

## 6. 成交时点（防未来函数的另一半）

- 用 `t` 的数据决策、又用 `t` 当根价成交 = 偷看了"决策当刻其实还没成交的价"。
- **默认 `next_bar`**：bar `t` 决策 → 下一可成交 bar 成交（盘末出信号 → 次日开盘成交；分钟同理 → 下一分钟价）。
- **`this_bar` 可选**：分钟择时"就要这一分钟进"用，且**必须 bit-for-bit 复现 A 的路径**，否则"A 作为 B 特例"会回归。
- ⚠ "下一可成交 bar"的解析要处理停牌/一字板/无量日，否则订单悄悄永不成交或迟成交 → 需**金标测试**：
  A 跑 vs B(`this_bar` + 订单全预提交) 必须逐行一致。

---

## 7. 网关耦合（替换直读）

- **替换**：`TushareMinuteDataLoader.load` + `_read_optional` 的 `rglob+read_parquet` 接缝（`data_adapter.py:28-148,158-173`）。
  引擎不再直读 parquet。每个 `/data` 与 advance 的取 bar 都调网关，`as_of=sim_time` 由服务设定。
- **新增 `GatewayDataAdapter`**：实现内核期望的同一"富 bar"接口
  （`close_qfq/open_qfq/limit_up_qfq/limit_down_qfq/volume/board/suspended/is_st`），做迁移垫片，**内核函数签名不动**。
- **复权口径：前复权 + PIT 锚点（实测定稿）**：`price_qfq = close × adj_factor ÷ (≤sim_time 最新 adj_factor)`。
  - **为何不是后复权**：`close × adj_factor` 量级被累计因子放大数倍（如茅台 adj≈8.4 → 后复权价 ~11000），
    100 股"花费"超百万把现金校验打成 `insufficient_cash`——后复权适合算收益率，不适合现金结算。（自测实测踩到。）
  - **为何不是旧前复权**：现状 `data_adapter.py:60-67` 锚"全历史最新"，未来除权会改 latest → 轻微未来函数。
  - **PIT 锚点**：锚在"可见戳 ≤ sim_time 的最新 adj_factor"。网关已按 as_of 闸门裁剪 adj_factor，每 symbol 最新一行即锚 →
    最新可见日 multiplier≈1（量级真实），历史价按比例缩放保持连续，且不含未来除权（PIT 安全）。
    `gateway_adapter.py` 已如此实现。
- **日历**：今天从已加载分钟的 distinct date 派生（`:147`）；B 改为从网关的 `calendar` 集取（data 侧 A 类），
  否则 `daily_from_minutes` 无法对缺失日正确 forward-fill。
- **算子下推是关键接缝**：`/data` 带 `op=rank/topN` 只回结果集，策略不必让引擎物化全市场分钟帧——
  直接化解 data 侧类 D 的 by-date 扫描成本。

---

## 8. 风险与对策

| 风险 | 说明 | 对策 |
|---|---|---|
| fill 时点回归 | next_bar 的停泊队列 + "下一可成交 bar"是全新行为；this_bar 须复现 A | 金标测试 A vs B(this_bar)；仔细处理停牌/一字板/无量 |
| 时钟单调 | groupby 自带有序，显式时钟没有；日界 off-by-one → T+1 双重/漏解锁 | advance 拒绝 `to ≤ sim_time`；幂等 `request_id` 去重 |
| 复权口径 | 后复权量级失真→insufficient_cash；前复权锚最新→未来函数 | **前复权锚 ≤sim_time 最新因子**（量级真实+PIT安全）|
| 每步 HTTP 开销 | 分钟级一天数百 advance | 每步回完整账户上下文（省 1 往返）、算子下推、不需盘中时 `to=next_day` 粗推、持仓序列化进 session 行 |
| 状态持久 + 崩溃恢复 | 长驻会话的 cash/positions/clock 须跨重启存活、并发 advance | 幂等 advance（`request_id` 去重，仿 `app.py:126-142`）|
| 报告语义 | 端点原要求 `completed`；读会话当前态是"进行中" | 消费方（UI/排行榜）容忍移动而非终态数据；未到 end 的 forward-fill 注意误导 |
| 股池漂移 vs 持仓 | `set_universe` 可能踢掉仍持有的股 | 内核须仍能估值/卖出"持有但不在股池"的标的（`positions ⊄ universe`），否则 NAV 静默掉持仓 |
| 网关数据完整性 | data_adapter 的 `ValueError`（缺复权/限价）现在整跑早失败 | 每步网关缺字段须**优雅降级**（跳过/拒该 symbol），不能中断活会话 |

---

## 9. 实现工作拆解（文件级 + 排期）

| 优先级 | 事项 | 落点 |
|---|---|---|
| **P0** | `sessions` 表 + 会话对象（clock/account/universe/open_orders/config 持久化）| `store.py:13-68`、新 `session.py` |
| **P0** | 从 `_run_strategy` 抽出 `step()`（单 bar 撮合体）+ `advance()` | `replay_engine.py:202-271` → 新 `session_engine.py` |
| **P0** | `POST /sessions`、`/advance`、`/close` 三端点（幂等、单调时钟、账户上下文回传）| `app.py` |
| **P0** | `GatewayDataAdapter`（消费 data 网关，富 bar 接口；qfq as_of 锚点）| 新 `gateway_adapter.py`，替 `data_adapter.py` |
| **P1** | `POST /sessions/{id}/data` + 算子下推透传；`calendar` 取自网关 | `app.py`、`gateway_adapter.py` |
| **P1** | next_bar 停泊队列 + "下一可成交 bar"解析（停牌/一字板/无量）| `session_engine.py` |
| **P1** | 报告端点读会话累积产物（放宽 `_completed_summary_or_404`）| `app.py:224-404,604` |
| **P1** | 金标测试：A vs B(this_bar, 全预提交) 逐行一致 | `tests/` |
| **P2** | A 路径作为 B 特例的兼容封装（旧 `/backtests` 转成一个会话）| `worker.py`、`app.py` |
| **P2** | 会话崩溃恢复（仿 `requeue_interrupted`）；并发 advance 锁 | `store.py`、`session_engine.py` |

---

## 跨服务契约（两侧权威对照）

> 本节是 vortex_backtest ↔ vortex_data 的**唯一权威契约**；data 侧 [design/18 §3](../../vortex_data/design/18-backtest-data-gateway.md#3-取数网关-api) 与此对齐。

### C1. 取数：backtest → data

- **端点**：`POST {VORTEX_DATA_URL}/api/v1/data`，鉴权 `X-API-Token`/`Bearer`（**共享** `VORTEX_DATA_DASHBOARD_TOKEN`）。
- **请求**：`{ as_of(必填), datasets:[{ dataset, symbols(列表|"universe"|"all"), fields[], level(daily|1min), window({count}|{range}), op? }] }`。
- **`as_of` 由 backtest 用会话 `sim_time` 填**；data **不信任、只强制** `可见时间戳 ≤ as_of`（缺 `as_of` → 400）。
- **响应**：`{ as_of, results:{ "<dataset>":{columns, rows, visibility_field} } }`，全部 `≤ as_of`；大批量可 `Accept: application/vnd.apache.arrow.stream`。

### C2. 防未来函数职责划分

| 关注点 | 谁负责 |
|---|---|
| `可见时间戳 ≤ as_of` 行级闸门（财报 ann_date、分钟 trade_time、成分生效区间…）| **data**（服务端强制、fail-closed）|
| `as_of` 取自单调 `sim_time`、绝不前瞻 | **backtest**（会话时钟）|
| 成交时点 next_bar（决策 t、成交 t+1）| **backtest**（fill_timing）|
| 前复权 `price = close × adj_factor ÷ (≤as_of 最新因子)`（量级真实+PIT安全）| **data 交付 adj_factor** + **backtest 锚算** |

### C3. 字段口径约定

- 时间：统一 `MARKET_TZ` 本地时刻字符串（`YYYY-MM-DDTHH:MM:SS`）；日级 `as_of` 给到日期即可。
- `symbols`：A 股 `600519.SH`/`000001.SZ` 统一代码；`"universe"` 由 backtest 在请求里展开成显式列表传给 data
  （data 无会话状态，不知道股池）。
- 成交时点：盘末价出信号后，backtest 提交的下一笔交易时间只能是**次日开盘及之后**（分钟同理下一分钟）——
  由 backtest 单调时钟保证，data 不参与。

### C4. 决议记录（2026-06-08 对齐）

1. ✅ **成分回溯**：`ths_member` 有 `in_date/out_date`（range 闸门）；`dc_member` 按 `trade_date` 取快照
   → data 侧把 dc_member 抓取改为按 date 落历史快照（data design/18 §8 P0）。
2. ✅ **集合竞价 `visible_at`**：开盘 `date@09:25`、收盘 `date@15:00`。确认。
3. ⏳ `op` 算子白名单第一版（rank/topN/filter/agg）——倾向够用，待实现时再收口。
4. ✅ **复权口径**：**前复权 + PIT 锚点**（`close × adj_factor ÷ ≤as_of最新因子`）。自测实测后复权量级失真，改定此。
   最新可见日 multiplier≈1（量级真实可现金结算），锚 ≤as_of 不含未来除权（PIT 安全）。
5. ⏳ **性能预判（先判断何时扛不住，扛不住再上）**：
   - 日频回测：1 步/日，几年也就几百~一千多次往返 → **毫无压力**。
   - 分钟级 + 事件/择时（每天动手几~几十次）：决策驱动推进（不需要的分钟 `to=next_day` 跳过）
     → 往返 = 决策次数，**没问题**。
   - 分钟级 + **每分钟全市场扫描**（一天 240 步 × 全市场算子）：单策略单年 ~6 万步，每步还要 data 算全市场，
     **这是会扛不住的临界点**（小时级/年/策略；多策略多年线性放大）。
   - 届时按序加码（都已在设计里留好接口，不改契约）：①每步回完整账户上下文（已设计，省一半往返）
     ②算子下推（全市场只回结果行）③Arrow 列式传输 ④WebSocket 长连接省握手 ⑤最后才考虑策略进程内嵌(C)。
