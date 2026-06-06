---
title: vortex_backtest 代码评审报告
created: 2026-06-06
status: review
reviewer: Claude (Cowork)
scope: 全仓后端（vortex_backtest/ + tests/ + docs/ + examples/）
baseline: 工作树当前状态（git 仅跟踪 README.md，其余未跟踪）
---

# vortex_backtest 代码评审报告

## 0. 阅读范围与方法

本次通读了 `vortex_backtest/` 全部 Python 模块（`app.py` / `models.py` / `backtrader_adapter.py` / `data_adapter.py` / `market_rules.py` / `store.py` / `symbols.py`，约 2,200 行）、`docs/backtrader_minute_design.md`、`README.md`、`pyproject.toml`、`tests/test_api.py`、`examples/run_30_day_http_sample.py`，以及作为数据上游的姊妹仓 `vortex_data` 的 `design/04–07`。所有结论带 `file:line` 引用；两处正确性结论已用与源码同款的纯逻辑脚本复算确认（见 §3.1 C1/C3）。本轮只交付评审，不改代码。

评分口径：🔴 P0 必须先修（正确性/上线即坏）｜🟠 P1 尽快（产品化/稳定性）｜🟡 P2 应改（可维护性）｜⚪ P3 可选打磨。

---

## 1. 总体评价

这是一套**分层清晰、读起来很舒服**的账户回放服务：HTTP 层（FastAPI）、A 股规则层、数据层、存储层职责分明，Pydantic 模型规整，拒单原因有明确枚举，多策略"独立账户"模型是干净的第一阶段选择。订单幂等（`account_id + order_batch_id + request_id`）和 SQLite 迁移都做得稳妥。

但有一个**贯穿性的结构问题**，正好撞在项目"多用成熟框架、不要重复造轮子"的红线上：

> **服务名义上以 Backtrader 为引擎，实际上 Backtrader 是个完全没用上的死依赖——撮合、持仓、T+1、成交、净值全是手写的分钟循环。** `pyproject.toml:8` 声明 `backtrader>=1.9.78`，但全仓唯一的 `bt.` 用法是 `backtrader_adapter.py:28` 定义了一个 `TushareMinutePandasData(bt.feeds.PandasData)` feed 类——而这个类**从未被实例化或引用过**，没有 `Cerebro`、没有 `Broker`、没有 `adddata`、没有 `run()`。真正干活的是 `_run_strategy`（`backtrader_adapter.py:155-295`）里手写的 `groupby("trade_time")` 循环。

也就是说：README 与设计文档反复声称的"Backtrader 分钟事件驱动基础"是不成立的；自研引擎重新实现了 broker 账本、撮合、绩效统计这些成熟框架早已提供的东西，同时还背着一个用不上的重依赖。这是本报告的第一主题，详细取舍见 `design/02-architecture-decisions.md` 的 ADR-1。

紧随其后的三块是：**(2) 全链路同步阻塞**（回测在 HTTP 请求线程里跑完，没有作业队列）、**(3) 数据层全量读盘**（每次回测把整个 `stk_mins` 读进内存再过滤）、以及 **(4) qfq 价格口径的两个正确性 bug**（tick 校验打在 qfq 价上会把真实数据几乎全拒单；qfq 绝对价位依赖回测窗口）。这四块是重点。

亮点（重构时别破坏）：

- 四层划分（`app` / `market_rules` / `data_adapter` / `store`）边界干净，规则层可独立测试。
- 拒单原因是显式枚举（`suspended/zero_volume/invalid_price_tick/invalid_lot_size/limit_*_blocked/insufficient_*/t_plus_1_not_sellable/volume_cap_below_lot`），对量化用户调试极友好。
- 订单唯一性约束正确（`store.py:35`），多批订单 + 多策略选批次的模型清晰。
- SQLite 迁移防御性好（`store.py:103-181`，历史 `rqalpha/ashare_replay` 账户自动迁移为 `backtrader`）。
- 产出物（`account_summary.json` + 5 个 CSV）确定性强，便于下游消费。
- 数据缺失是**显式预检失败**（`minute_data_missing` 等），不会伪装成"成功的日线回测"——这点设计取向很对。

---

## 2. 架构总览

### 2.1 分层与一次回测的数据流

| 层 | 载体 | 职责 |
|---|---|---|
| HTTP 协议层 | FastAPI（`app.py`），单进程 uvicorn | 账户/订单/作业/报告查询；**回测在请求线程内同步执行** |
| 规则层 | `market_rules.py`（`AShareRuleEngine` + `FeeModel`） | T+1、手数、tick、涨跌停、停牌、量能上限、费用 |
| 引擎层 | `backtrader_adapter.py`（`BacktraderMinuteReplayEngine`） | **手写**分钟循环撮合 + 账本 + 净值 + 报告归一化 |
| 数据层 | `data_adapter.py`（`TushareMinuteDataLoader`） | 读本地 Parquet → 合成 qfq 分钟 OHLC/涨跌停 |
| 存储层 | `store.py`（SQLite + 文件报告） | accounts / orders / jobs 三表；报告 CSV+JSON |

```
POST /backtests  (app.py:118)
  └─ 同步：engine.run()  (app.py:154-175，在请求线程里跑完才返回)
       ├─ resolve_strategies / resolve_dates           (backtrader_adapter.py:298-354)
       ├─ data_loader.load(symbols, start, end)         (data_adapter.py:27)
       │     └─ _read_optional() rglob+concat 整个 dataset，然后才按 symbol/date 过滤
       ├─ 逐 strategy: _run_strategy()                  (backtrader_adapter.py:155)
       │     └─ for ts, rows in minutes.groupby("trade_time"):  # 手写撮合循环
       │           rules.validate_order → executable_quantity → execute_order
       ├─ aggregate_summaries()                          (backtrader_adapter.py:557)
       └─ write_reports() + write_json()                 (backtrader_adapter.py:657)
  └─ complete_job(summary)  把整个 summary（含全部分钟快照）塞进 SQLite summary_json 一个字段
```

### 2.2 进程与并发模型

单进程、纯同步（`grep` 全仓无 `async/await/threading/asyncio/Background/multiprocessing`）。`jobs` 表（`store.py:38-55`）和 `job_id/status` 字段看起来像异步作业模型，但 `run_backtest`（`app.py:118-175`）建 job 后**立刻同步 `engine.run()`**，状态在同一个 HTTP 调用里从 `running` 走到 `completed`。客户端永远拿不到"运行中"的中间态——这是产品化的主要缺口。

---

## 3. 问题清单

### 3.1 正确性（P0 优先）

**C1 🔴 tick 校验打在 qfq 价上 → 真实数据会被几乎全量拒单。**
`validate_order` 用 `is_tick_aligned(fill_price)` 判断 0.01 对齐（`market_rules.py:60`、`is_tick_aligned` 定义在 `market_rules.py:155`），而 `fill_price` 是 qfq 后的成交价（`backtrader_adapter.py:208`，取 `row["{price_type}_qfq"]`）。qfq 价 = `round(raw × adj_factor / latest_adj_factor, 4)`（`data_adapter.py:90,102`），对**任何 adj_factor 与窗口内最新因子不同的 bar**（即历史上有过分红/送转、且不是窗口内最后一段），几乎不可能恰好落在 0.01 网格上，于是统一返回 `invalid_price_tick` 拒单。

同款逻辑复算确认：

```
raw=10.00 adj=0.8767 latest=1.2345 → qfq=7.1017 → tick_aligned=False → REJECTED
raw=10.00 adj=1.0    latest=1.0    → qfq=10.00  → tick_aligned=True  → OK
```

之所以现有测试全绿，是因为**所有 fixture 都用 `adj_factor=1.0`**（`test_api.py:67`），qfq≡raw 永远对齐，把这个 bug 完全遮住了。这是上线即炸的正确性问题。
*方向*：tick/手数这类"挂单合法性"应对**真实价（raw）**校验，撮合/估值再用 qfq；或明确区分"用户下单价口径"与"内部 qfq 估值口径"。

**C2 🔴 数据层每次回测全量读盘。**
`_read_optional`（`data_adapter.py:155-163`）对 dataset 目录 `rglob("*.parquet")` 后 `pd.concat` **所有**文件，`load()` 才在内存里按 symbol/date 过滤（`data_adapter.py:43-47`）。`stk_mins` 是 `year=/universe=/symbol=` 分区、动辄成千上万个文件，等于**每跑一次回测就把全市场全历史分钟数据读进内存**再丢掉绝大部分。这与上游 `vortex_data` 明确记录的经验（`design/04`：DuckDB 支持分区裁剪/过滤下推，应作查询引擎）完全相反——上游已经把这件事做对了，下游又用最慢的方式重做了一遍。
*方向*：见 `design/02` ADR-2，消费 `vortex_data` 的查询服务或在进程内用 DuckDB 做 symbol/date 下推。

**C3 🟠 qfq 绝对价位依赖回测窗口；用户 limit_price 与 qfq 价口径不一致。**
`latest_adj_factor` 取自**被窗口过滤后**的 `adj_factor` 表（`data_adapter.py:58-60` 先按 `[start,end]` 过滤，`81-86` 再 `groupby.tail(1)` 取"最新"）。后果：同一根历史 bar 在 1 月窗口和 2 月窗口里算出不同的绝对 qfq 价。复算：

```
同一 bar raw=10, adj=0.90：窗口末因子=0.95 → qfq=9.4737；窗口末因子=1.10 → qfq=8.1818
```

收益率/比率对该常数不敏感，但**绝对价位敏感**：现金充足性检查、tick 对齐（叠加 C1）、以及尤其是——用户提交的 `limit_price`（真实挂单价）被直接拿去和 qfq 成交价比较（`market_rules.py:67-73`）。用户按真实价挂的限价单，会被一个随窗口漂移的 qfq 价判定可否成交，语义不稳。
*方向*：固定 qfq 基准（用全局最新因子，而非窗口内最新），并统一"用户价口径 vs 内部估值口径"。

### 3.2 产品化 / 稳定性

**A1 🔴 全链路同步阻塞，没有真正的作业模型。**
`run_backtest` 在 HTTP 请求线程里跑完整个回测才返回（`app.py:154-175`）。分钟级、多日、多标的回测耗时可观，会**长时间占住 uvicorn worker**；并发请求要么排队、要么打满 worker。`jobs` 表已具备 `status/created_at/completed_at`，但从不经历 `running` 中间态。要"做成给量化用户的服务"，这是第一道坎。
*方向*：见 `design/02` ADR-3，引入后台作业队列 + `GET /backtests/{job_id}` 轮询状态。

**A2 🟠 Backtrader 死依赖。** 见 §1。要么按 ADR-1 真正接入它的引擎，要么直接从依赖里删掉——当前状态既误导读者，又让安装/CI 背着一个无用重依赖。

**A3 🟠 summary 把全量分钟快照内嵌并整坨写进 SQLite。**
`_run_strategy` 为**每一分钟**生成一个含嵌套 positions/trades/rejections 的快照（`backtrader_adapter.py:259-270`、`450-482`），`aggregate_summaries` 把它们都收进 `summary["minutes"]`（`576,596`），`complete_job` 再把整个 summary `json.dumps` 进 `jobs.summary_json` 一个字段（`store.py:338-352`）。单标的 30 天 1min ≈ 240×30 ≈ 7,200 个分钟快照，多标的/多策略再翻倍。后果：SQLite 行爆胀、查询变慢、`GET /minutes`（`app.py:206-212`）一次性吐出超大 JSON。
> 旁证：现有样例报告只有 2 天且 `minutes:0`（分钟路径在样例里根本没被填充），说明**长回测 + 分钟产物这条路径几乎没被真实跑过**——体积风险尚未暴露。
*方向*：分钟级产物落 Parquet/CSV 文件，DB 只存摘要指标 + artifact 路径；分钟查询走分页/文件流。

**A4 🟠 `/backtests` 无幂等/无并发保护。** 每次调用新建 `job_id`（`app.py:124`）并重跑，相同入参不会复用；叠加 A1 的阻塞，容易被重复重活打爆。

### 3.3 可维护性 / 口径

**M1 🟡 命名与文档误导。** 模块名 `backtrader_adapter`、README/设计文档的"Backtrader 分钟事件驱动基础"均与实现不符（见 §1）。文档是契约，先于代码要对齐。

**M2 🟡 crosswalk 名不副实。** `crosswalk` 返回 `tushare==miniqmt==vortex==归一化值`（`symbols.py:54-66`），声称多格式互通但没有真实映射；而 `normalize_symbol` 只接受 `\d{6}.(SZ|SH|BJ)`（`symbols.py:16-24`），会直接拒掉 crosswalk 宣称支持的其它格式（如 MiniQMT 的 `SH600000` 风格）。要么做真映射，要么收敛文案。

**M3 🟡 strategy_type 近乎空操作。** 仅允许 `order_replay`（`backtrader_adapter.py:319-321`），"策略"实质只是"选订单批次"。这个边界本身是对的（与 `vortex_data`"不内嵌策略"一致——策略脚本在客户端、回放在服务端），但产品话术里的"策略创建"应诚实表述为"外部订单回放 / 账户回放"。

**M4 🟡 费率与撮合参数硬编码、不可配。** `FeeModel` 费率写死（`market_rules.py:12-16`），`max_volume_participation` 默认 1.0 也不暴露给 API（`market_rules.py:35-38`）。真实券商费率、不同回测的撮合假设需要可按账户/按回测覆盖。

**M5 🟡 T+1 解锁依赖"日期变化"启发式。** 解锁逻辑在分钟循环里按 `row_date_key` 变化触发（`backtrader_adapter.py:198-200`），依赖分钟行全局按 `trade_time` 排序（`data_adapter.py:145`）且各标的共享同一交易日历。`calendar` 虽被算出（`data_adapter.py:146`）却几乎没被引擎使用。当前能跑，但跨停牌/半日市/多标的日界处较脆，建议显式用交易日历驱动 T+1。

**M6 ⚪ 错误原文透传客户端。** 失败时 `detail` 直接回 `str(exc)` / `summary`（`app.py:172-174`），可能带出内部路径或栈信息。

**M7 ⚪ 金额 4 位小数与 A 股 2 位/整股口径混用。** `round_money` 取 4 位（`backtrader_adapter.py:721`），费用、现金累加可能积累亚分漂移；建议明确现金 2 位、股数整数的口径。

### 3.4 测试覆盖

已覆盖（8 个用例，`test_api.py`）：引擎默认值与历史账户迁移、`frequency/qfq` 守卫、数据层 qfq 合成与缺 adj 失败、规则层各类违规、HTTP 多策略 happy-path、缺分钟数据失败、crosswalk。规则层与 happy-path 覆盖不错。

关键未覆盖 / 被遮蔽：

- **adj_factor ≠ 1.0 的 qfq 路径零测试**——所有 fixture 用 `1.0`（`test_api.py:67`），直接遮住 C1（tick 拒单）与 C3（窗口依赖）。**这是性价比最高的待补测试。**
- **长回测 / 分钟产物体积**无测试（A3 风险未暴露）。
- **并发 / 阻塞行为**无测试（A1）。
- **数据层分区裁剪**无测试（因为目前根本没有裁剪，C2）。
- **limit_price 在 qfq 口径下的可成交语义**无测试（C3）。
- 费率数值、`max_volume_participation` 截断后不足一手拒单（`backtrader_adapter.py:235-243`）等边界无专测。

---

## 4. Top 优先级汇总

| # | 级别 | 类别 | 问题 | 位置 | 工作量 |
|---|---|---|---|---|---|
| 1 | 🔴 | 架构 | Backtrader 是死依赖，撮合/账本/T+1 全手写（违背"不重复造轮子"） | `pyproject.toml:8`、`backtrader_adapter.py:28,155-295` | 见 ADR-1 |
| 2 | 🔴 | 正确性 | tick 校验打在 qfq 价上，真实数据几乎全拒单 | `market_rules.py:60,155`、`backtrader_adapter.py:208` | S–M |
| 3 | 🔴 | 性能 | 数据层每次回测全量 rglob+concat 读盘 | `data_adapter.py:155-163,43-47` | M（随 ADR-2） |
| 4 | 🔴 | 产品化 | 回测同步阻塞请求线程，无真正作业模型 | `app.py:118-175` | M（随 ADR-3） |
| 5 | 🟠 | 正确性 | qfq 绝对价位依赖窗口 + 用户 limit_price 与 qfq 口径不一致 | `data_adapter.py:58-90`、`market_rules.py:67-73` | S–M |
| 6 | 🟠 | 稳定性 | summary 内嵌全量分钟快照并整坨写入 SQLite | `backtrader_adapter.py:259-270`、`store.py:338-352` | M |
| 7 | 🟠 | 稳定性 | `/backtests` 无幂等/并发保护 | `app.py:124` | S |
| 8 | 🟡 | 维护 | 命名/文档误导、crosswalk 名不副实、费率硬编码、T+1 启发式 | §3.3 | M |
| 9 | 🟡 | 测试 | adj≠1 的 qfq 路径、长回测、并发零测试 | `tests/` | M |

> 关键架构取舍见 `design/02-architecture-decisions.md`；分阶段落地见 `design/03-productization-plan.md`；展示界面见 `design/04-dashboard-ui-spec.md`。
