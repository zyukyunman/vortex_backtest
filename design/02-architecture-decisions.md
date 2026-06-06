---
title: vortex_backtest 架构决策记录 (ADR)
created: 2026-06-06
status: proposed
deciders: 项目负责人 / 后端
depends_on: design/01-code-review.md
---

# vortex_backtest 架构决策记录

本文件汇总三个相互关联的关键决策，回应项目两条主线诉求——**"多用成熟框架、不要重复造轮子"** 和 **"把它产品化成给量化用户的回测服务"**。每条 ADR 用统一格式：背景 → 决策 → 备选 → 取舍 → 影响 → 行动项。落地排期见 `design/03-productization-plan.md`。

一句话预览：

- **ADR-1（引擎）**：删掉没用上的 Backtrader；保留薄薄的 A 股撮合核心（这是真正的产品价值，没有框架现成提供），把**真正在重复造轮子的"通用件"**（绩效指标、交易日历、数据访问）换成成熟库。
- **ADR-2（数据）**：停止全量读 Parquet，引入 `DataGateway` 抽象，MVP 用进程内 DuckDB 做分区/谓词下推，并以 `vortex_data` 的存储 schema 为契约。
- **ADR-3（作业）**：从"请求线程里同步跑完"改为后台作业队列 + `job_id` 轮询。

---

# ADR-1: 回测引擎框架选型

**Status:** Accepted（2026-06-06；源码结论 + 真机 spike + Qlib 后端引擎跑通完整日级报告，三重印证齐备，见 `design/12`）
**Date:** 2026-06-06

> **⚠ 更新（2026-06-06，晚于本 ADR 初稿）**：经与负责人讨论并补充两项关键输入——(1) 愿意把 `vortex_data` 一并统一到 Qlib（消除数据双栈），(2) 两仓同栈以减少跨栈排查——**本 ADR 的推荐已由"方案 C（薄自研核心）"改为"Qlib + 薄规则层"，详见 `design/05-backtest-engine-requirements.md`**。下方分析仍然有效（是得出结论的过程），但最终取向以 05 为准；05 给出了完整能力清单与一个 1 天 spike 作为锁定前的验证。

## Context

`design/01` §1 已确认：`backtrader>=1.9.78` 是声明了却**完全没用上的死依赖**——全仓唯一 `bt.` 用法是 `backtrader_adapter.py:28` 定义的一个从未被实例化的 feed 类；撮合、持仓、T+1、成交、净值、绩效全是 `_run_strategy`（`backtrader_adapter.py:155-295`）里手写的分钟循环。这正撞在"不要重复造轮子"上。

但要选对方向，必须先把"轮子"分成两类：

| 类别 | 内容 | 谁该提供 |
|---|---|---|
| **通用件（真在重复造，应换库）** | 绩效/风险指标（max_drawdown、收益率、Sharpe…）、交易日历（T+1 下一交易日、半日市）、数据访问（分区裁剪、qfq）、净值/回撤数学 | 成熟库 |
| **领域核心（产品价值，应自有）** | A 股微结构规则：T+1、分板手数（主板 100 / 科创 200+1 / 北交所）、**以数据为准的涨跌停**、印花税（卖出）、停牌/ST、量能参与上限 | 自研规则层 `market_rules.py` |

关键事实：**没有任何一个开源框架能为"外部订单回放"开箱提供完整的 A 股规则核心。** 无论选哪个框架，`market_rules.py` 里那套规则都得以某种形式存在。所以问题不是"自研 vs 框架"，而是"**用什么承载订单/成交/持仓生命周期 + 绩效 + 数据馈送**，才能既不重复造通用件、又不被框架的取向拖累"。

约束：

- 服务边界是 **订单回放 / 账户回放**（策略脚本在客户端，调用本服务）——不是服务端做"信号→下单"。这一点强烈影响选型。
- 数据是本地 Tushare Parquet（经 `vortex_data`），分钟级，qfq。
- 目标是多租户式 HTTP 服务，要可测、可观测、依赖轻。
- `market_rules.py` 已是引擎无关的纯函数层——它是资产，任何方案都应保留复用。

## Decision

**采用方案 C：删除 Backtrader 依赖；保留并加固"薄撮合核心 + A 股规则层"，把通用件外包给成熟库。**

具体：

1. **删除** `backtrader` 依赖与 `TushareMinutePandasData` 死类；修正 README/设计文档中"Backtrader 事件驱动"的不实表述。
2. **保留** `market_rules.py`（领域核心）与一个**显式、可测、~150 行**的分钟撮合/账本循环；把它从"伪 backtrader adapter"正名为 `replay_engine`，并抽出清晰的 `Order → Fill → Position → Equity` 数据类。
3. **外包通用件**：
   - 绩效/风险指标 → `empyrical-reloaded`（纯计算、轻量；替换手写的 `max_drawdown`/`total_return`/回撤水位）。
   - 交易日历与 T+1 下一交易日 → `exchange_calendars`（`XSHG`）或直接复用 `vortex_data` 的 `calendar` 数据集，停用"日期变化"启发式（`design/01` M5）。
   - 数据访问 → 见 ADR-2。
4. **加固**：费率/印花税/滑点/参与率做成可配置（按账户/按回测覆盖），修掉 qfq tick 拒单与窗口依赖（`design/01` C1/C3）。

> 触发重评条件：**若未来产品要在服务端做"信号→组合"回测**（而非纯订单回放），应重开本 ADR 并把 Qlib 提为首选——它的强项正是 signal→strategy→executor。

## Options Considered

### 方案 A：正式接入 Backtrader（Cerebro/Broker/Analyzer）

| 维度 | 评估 |
|---|---|
| 复杂度 | 中–高：A 股规则要写成自定义 Broker/CommissionInfo/Sizer 子类，深入其内部 |
| 通用件复用 | 好：自带 order/trade 生命周期、佣金方案、Analyzers（回撤/Sharpe/TradeAnalyzer）、Sizer |
| A 股贴合度 | 低：T+1、以数据为准的涨跌停拒单、分板手数均非原生，仍需自写 |
| 维护风险 | **高**：原版最后一次发布 2023-04（已 2 年+），`backtrader2` PyPI 包不活跃，仅社区 GitHub fork 半活跃 |
| 与"订单回放"契合 | 中：要用一个"只重发外部订单"的 Strategy 去驱动，属偏门用法 |

**Pros:** 一次拿到成交生命周期 + 绩效分析；纯 Python 无重依赖。
**Cons:** 仍要手写全部 A 股规则；把项目绑死在一个停更框架的内部 API 上；订单回放是其设计的边角；收益（生命周期+分析）可被 `empyrical` + 一个小账本平替。

### 方案 B：改用 Qlib 的 A 股 Exchange/backtest

| 维度 | 评估 |
|---|---|
| 复杂度 | 高：Qlib 数据格式（.bin）、Exchange/Executor/Account 模型学习与改造成本 |
| 通用件复用 | 好：`Exchange` 原生 `trade_unit`、`limit_threshold`、`volume_threshold`、`deal_price`、`open_cost/close_cost/min_cost` |
| A 股贴合度 | **高**：本就为 A 股而生；`vortex_data` 已能导出 Qlib 视图 |
| 维护风险 | 低：微软在维护，社区活跃 |
| 与"订单回放"契合 | **低**：重心是 signal→strategy→executor 的组合回测；纯外部订单回放只用到它一个切片，逆其设计 |

**Pros:** 最贴近 A 股的撮合/限价/量能模型；与上游数据同源；后续若做信号回测可平滑扩展。
**Cons:** 为"订单回放"这件事引入一整套重而有主见的栈，且只用一小部分；STAR 200+1、北交所手数仍需扩展；数据要转成 Qlib 格式，与 ADR-2 的 DuckDB 路线重复。**用错场景的强框架 = 新的复杂度债。**

### 方案 C：薄撮合核心 + 成熟库承载通用件（**推荐**）

| 维度 | 评估 |
|---|---|
| 复杂度 | 低–中：撮合核心已存在（~150 行），只需正名、抽数据类、配置化 |
| 通用件复用 | 好：指标用 `empyrical-reloaded`、日历用 `exchange_calendars`、数据用 DuckDB（ADR-2） |
| A 股贴合度 | **高**：规则层就是为 A 股写的，保留即可 |
| 维护风险 | 低：依赖面最小；不绑任何停更/重栈框架 |
| 与"订单回放"契合 | **高**：撮合循环天然就是"按 bar 回放外部订单" |

**Pros:** 保住真正的产品 IP（A 股规则）且显式可测；把**真正在重复造的通用件**换成成熟库；依赖最轻；规则层引擎无关，未来要换引擎也不丢资产。
**Cons:** 仍自有撮合循环——需要纪律不让"自研指标/日历"重新长回来（用 lint/评审守住）。

### 已否决（一句话）

- **vectorbt / vectorbtpro**：向量化擅长参数扫描，但 T+1 + 逐 bar 涨跌停拒单 + 量能上限 + 部分成交在纯向量模型里很别扭；Pro 收费。
- **backtesting.py**：单标的、无多资产组合，A 股组合账户不合身。
- **zipline-reloaded**：事件驱动且可配自定义日历（XSHG），但 US 股基因重、ingest 管线重，T+1/涨跌停拒单仍自写，社区更小。

## Trade-off Analysis

核心权衡是 **"框架开箱的通用件" vs "为订单回放硬塞强框架的复杂度"**。方案 A/B 都能拿到现成的成交生命周期/绩效，但代价是：A 把你绑在停更框架的内部 API 上、B 让你为一个切片背整套 Qlib，而**两者都消不掉 A 股规则的自写量**。方案 C 承认一个事实——本服务的差异化价值就是那套薄而精确的 A 股规则核心，它不是轮子；真正该停止重复造的是指标/日历/数据访问这些通用件，而它们有现成的轻量库。因此 C 在"贴合 A 股 + 依赖轻 + 改造小 + 不丢资产"上最优，唯一代价（继续自有撮合循环）可用工程纪律控制。

## Consequences

- **变容易**：依赖变轻、安装/CI 更快；规则层与撮合核心可单测；指标/日历正确性外包给成熟库；qfq/口径修复集中在一处。
- **变难**：需要主动守住边界，避免自研指标/日历回潮（评审 checklist + 一条"禁止重造通用件"的约定）。
- **需复查**：一旦产品要服务端信号回测，重开本 ADR、把 Qlib 提为首选。

## Action Items

1. [ ] 从 `pyproject.toml:8` 移除 `backtrader`；删 `TushareMinutePandasData`（`backtrader_adapter.py:26-60`）。
2. [ ] 将 `backtrader_adapter.py` 正名为 `replay_engine.py`，抽出 `Order/Fill/Position/EquityPoint` 数据类，撮合循环逻辑不变但去框架化。
3. [ ] 引入 `empyrical-reloaded` 计算收益/回撤/Sharpe/Sortino/Calmar，替换 `max_drawdown`/`round_ratio` 手写统计。
4. [ ] 引入 `exchange_calendars`（XSHG）或 `vortex_data` 日历，T+1 改为"下一交易日"语义。
5. [ ] 费率/印花税/滑点/参与率配置化（按账户/按回测）。
6. [ ] 同步修正 README 与 `docs/backtrader_minute_design.md` 的引擎表述。

---

# ADR-2: 数据访问方式

**Status:** Proposed
**Date:** 2026-06-06

> **⚠ 更新（2026-06-06）**：负责人明确"直接读硬盘数据、不需要查询服务"。因此本 ADR 收敛为：**进程内直接读盘**（若走 Qlib 即用其 `FileStorage` provider，本就按需读取、无全量读盘问题），**取消"消费 vortex_data HTTP 查询服务"方案**。详见 `design/05` §5。下方对比仍可参考。

## Context

`design/01` C2：`data_adapter._read_optional`（`data_adapter.py:155-163`）对 dataset 目录 `rglob+concat` **所有** Parquet 后才在内存里按 symbol/date 过滤（`data_adapter.py:43-47`）。`stk_mins` 是 `year/universe/symbol` 分区、文件成千上万，等于每次回测把全市场全历史分钟读进内存。上游 `vortex_data` 的 `design/04` 已明确记录："DuckDB 支持 hive 分区裁剪/过滤下推，应作查询引擎"——下游却用最慢方式重做。项目设定也明确："`vortex_backtest` 使用的数据来自 `vortex_data`"。

## Decision

**引入 `DataGateway` 抽象；MVP 默认实现为进程内 DuckDB，对 symbol/date 做分区与谓词下推，并以 `vortex_data` 的存储 schema 为数据契约。** 同时预留 `vortex_data` HTTP 客户端实现，作为两服务分开部署时的解耦缝。

## Options Considered

### 方案 A：调用 `vortex_data` 的 HTTP 查询服务
**Pros:** 严格契约边界、单一数据代码路径、符合"数据来自 vortex_data"。
**Cons:** 多一次网络跳；强依赖该服务在线；分钟级大区间查询的传输/序列化开销；MVP 引入跨服务编排。

### 方案 B：进程内 DuckDB 下推读取共享 workspace（**推荐，MVP**）
**Pros:** 无网络跳、无新基础设施；`read_parquet(glob, hive_partitioning, union_by_name)` 直接裁剪分区/列/谓词；复用 `vortex_data/data/storage/parquet_duckdb.py` 的成熟模式；与 ADR-1"用成熟库"一致。
**Cons:** 与 `vortex_data` 共享磁盘布局假设（用其 schema 当契约可控）；需注意 `design/01` 之外的 DuckDB 注入面（dataset/路径白名单——可直接借鉴 vortex_data code-review 的 S2 教训）。

### 方案 C：保留 pandas 全量读（现状）— 否决
全量读盘，O(整库) 内存与 IO，无法产品化。

## Trade-off Analysis

B 在性能、简单度、上线速度上最优，且与 ADR-1 的"轻依赖、用成熟件"一致；A 的"硬契约/解耦"价值在两服务分开部署时才显现。用 `DataGateway` 接口把二者变成可切换实现：MVP 走 B，未来需要进程隔离/独立扩缩容时切 A，无需改引擎。

## Consequences

- **变容易**：单次回测内存/IO 从"整库"降到"目标 symbol×日期窗口"；冷启动与并发回测可行。
- **变难**：要管 DuckDB 连接复用与查询安全（dataset/列白名单、参数化），别重蹈 vortex_data S2 注入坑。
- **需复查**：两服务正式分开部署时，评估切到 A 或在 `vortex_data` 暴露更细的回测取数端点。

## Action Items

1. [ ] 定义 `DataGateway`（`load_minutes(symbols, start, end, columns) -> df`、`adj_factors(...)`、`limits(...)`、`calendar(...)`）。
2. [ ] DuckDB 实现：按 `symbol/date` 分区裁剪 + 列裁剪 + 谓词下推；dataset/列走白名单、SQL 参数化。
3. [ ] 以 `vortex_data` 存储 schema 为契约写一组对拍测试（同一查询，DuckDB 结果 == 预期）。
4. [ ] 预留 `VortexDataHttpGateway` 空壳实现与开关。

---

# ADR-3: 回测执行的作业模型

**Status:** Proposed
**Date:** 2026-06-06

## Context

`design/01` A1：`run_backtest`（`app.py:118-175`）在 HTTP 请求线程内同步 `engine.run()` 跑完才返回，`jobs` 表的 `status` 从不经历 `running` 中间态。分钟级多日多标的回测会长时间占住 uvicorn worker，并发即排队/打满。要做成服务，必须有真正的异步作业模型。

## Decision

**改为后台作业队列：`POST /backtests` 入队并立刻返回 `202 + job_id`（status=`queued`）；后台 worker 执行（`running`→`completed`/`failed`）；客户端用 `GET /backtests/{job_id}` 轮询。** 复用现有 `jobs` SQLite 表作为队列与状态源。MVP 用进程内 worker（独立线程/进程池消费队列），保留切换到外部队列的缝。

## Options Considered

### 方案 A：进程内后台 worker（**推荐，MVP**）
- 一个 worker 线程/小型进程池从 SQLite `jobs` 拉 `queued` 任务执行；CPU 密集的 pandas 撮合应放**进程**池避免阻塞事件循环。
- **Pros:** 零新增基础设施；直接复用 `jobs` 表；实现小。
- **Cons:** 单机；worker 崩溃需"启动时把 running 标记为 interrupted 并重排"（可借 vortex_data 的恢复思路）。

### 方案 B：外部队列/worker（Celery/RQ/Dramatiq + Redis）
- **Pros:** 可水平扩缩容、重试/限流成熟。
- **Cons:** 引入 Redis 与部署复杂度；MVP 过重。

### 方案 C：同步（现状）— 否决

## Trade-off Analysis

A 用最小代价把"同步阻塞"变成"可排队、可观测、可并发"，且与现有 SQLite 作业表天然契合；B 的扩缩容价值在量级上来后才需要。用一个 `JobQueue` 接口隔离，MVP 走 A，规模化再切 B。

## Consequences

- **变容易**：`POST` 立即返回；前端可做进度轮询（配合 `design/04` 看板）；并发回测不再打满请求线程。
- **变难**：需要作业生命周期管理（超时、取消、崩溃恢复、幂等键去重——对应 `design/01` A4）。
- **需复查**：并发量/单任务时长上来后评估切外部队列与多 worker。

## Action Items

1. [ ] `POST /backtests` 改为入队返回 `202 + job_id`；新增 `queued/running/completed/failed/cancelled` 状态机。
2. [ ] 进程内 worker（进程池）消费队列；启动时把残留 `running` 标为 `interrupted` 并重排。
3. [ ] 幂等键（account+strategies+window 指纹）去重重复提交。
4. [ ] `GET /backtests/{job_id}` 暴露进度（已处理 bar/总 bar、当前日期），供看板轮询。

---

## 参考 / Sources

- backtrader 维护状态（原版最后发布 2023-04；社区 fork）: [backtrader · PyPI](https://pypi.org/project/backtrader/) · [backtrader2 维护分析 (Snyk)](https://snyk.io/advisor/python/backtrader2) · [社区 fork](https://github.com/neilsmurphy/backtrader2)
- Qlib Exchange 参数（trade_unit/limit_threshold/volume_threshold/deal_price/costs）: [qlib/backtest/exchange.py](https://github.com/microsoft/qlib/blob/main/qlib/backtest/exchange.py) · [Qlib backtest 文档](https://qlib.readthedocs.io/en/latest/component/backtest.html)
- 绩效指标库: [empyrical-reloaded](https://github.com/stefan-jansen/empyrical-reloaded) · [quantstats-reloaded](https://pypi.org/project/quantstats-reloaded/)
- 交易日历: [exchange_calendars (PyPI)](https://pypi.org/project/exchange_calendars/)（含 `XSHG` 上交所）
