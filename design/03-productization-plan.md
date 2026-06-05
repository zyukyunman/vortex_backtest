---
title: vortex_backtest 产品化范围与分阶段计划
created: 2026-06-06
status: plan
depends_on: design/01-code-review.md, design/02-architecture-decisions.md
---

# vortex_backtest 产品化范围与分阶段计划

本文件把 `design/01`（评审）与 `design/02`（架构决策）落成可执行蓝图：先定**产品范围与边界**，再给**分阶段计划**。本轮只交付计划，不改代码。工作量口径：S ≈ 半天内，M ≈ 1–2 天，L ≈ 3 天以上。问题编号（C1/A1…）对应 `design/01`，ADR-1/2/3 对应 `design/02`。

---

## 1. Context

`vortex_backtest` 要从"一个能跑的脚本式服务"变成**给量化用户用的回测服务**：

- 用户通过 HTTP 提交账户、订单批次、策略配置，服务基于本地 Tushare 分钟数据做 A 股账户回放，产出成交、拒单、持仓、分钟/日净值、汇总指标。
- **策略逻辑在客户端**：用户跑一个脚本（=一个具体策略），脚本调用本服务做回放与账户模拟。服务端只做"回放 + 撮合 + 账本 + 报告"，不内嵌信号生成（与 `vortex_data` 的"不内嵌策略"边界一致）。
- 要有**展示界面**给用户看回测结果（净值/回撤/成交/拒单），但界面**不承载复杂设置**（详见 `design/04`）。
- 数据来自 `vortex_data`，本仓不做数据抓取。

核心假设：只要回放服务能稳定、正确、可观测地接收订单并回放出可信的账户轨迹与报告，量化用户就能把策略脚本对接进来，而不必各自重写撮合与 A 股规则。

约束：

- 第一阶段：A 股现金账户、`1min`、qfq 单口径、多策略独立账户。
- 用成熟框架承载通用件，不重复造轮子（ADR-1）。
- 与 `vortex_data` 共享 workspace 数据契约，不重复数据层（ADR-2）。
- 产品化要可 Docker 部署、可观测、写接口默认安全（借鉴 `vortex_data` 评审的鉴权/绑定教训）。

## 2. Product Objects

| 对象 | 含义 |
|---|---|
| `Account` | 回测账户，含初始资金、引擎 |
| `OrderBatch` | 一批外部订单（`order_batch_id`），账户可留多批 |
| `Order` | 单条买卖意图（幂等键 account+batch+request） |
| `Strategy` | 回测中的一个独立子账户，绑定一个 order_batch + symbols |
| `BacktestJob` | 一次回测作业（异步，有状态机） |
| `Fill/Trade` | 撮合成交（价、量、费、税、成交后现金） |
| `Rejection` | 拒单（带显式原因枚举） |
| `Position` | 持仓（数量、可卖、成本、市值、浮盈） |
| `EquityCurve` | 分钟级 / 日级净值轨迹 |
| `Report/Artifact` | 落盘产物（CSV/Parquet/JSON）与摘要指标 |

作业反馈状态（ADR-3）：`queued | running | completed | failed | cancelled | interrupted`。

## 3. Feature Inventory

| Feature | 用户价值 | 工作量 | 决策 |
|---|---|---:|---|
| 账户/订单批次/多策略回放 | 高 | — | 已有，保留 |
| A 股规则层（T+1/手数/涨跌停/印花税/停牌） | 高 | — | 已有，保留为核心 |
| qfq 口径正确性修复（C1/C3） | 高 | M | MVP（P0） |
| 引擎去框架化 + 成熟库（ADR-1） | 高 | L | MVP |
| DuckDB 数据下推（ADR-2） | 高 | M | MVP |
| 异步作业模型（ADR-3） | 高 | M | MVP |
| 报告体积治理（分钟产物落文件） | 高 | M | MVP |
| 可配置费率/滑点/参与率 | 中 | M | MVP |
| 展示界面（回测结果看板） | 高 | L | MVP（见 04） |
| Docker 部署 + 写接口默认安全 | 高 | M | MVP |
| 基准/相对收益、更多绩效指标 | 中 | M | v1.1 |
| 多策略共享账户模型 | 中 | L | v1.1 |
| 更多 strategy_type（含服务端信号回测） | 低（本仓） | 高 | 暂缓（触发 ADR-1 重评） |
| 数据抓取 | — | — | 切（属 vortex_data） |
| QMT/实盘下单 | — | — | 切 |

## 4. MVP Scope

### Must Have
1. **正确性**：qfq tick 不再误拒（C1）；qfq 基准固定、用户 `limit_price` 与撮合口径一致（C3）。
2. **引擎**：删 Backtrader 死依赖；薄撮合核心正名 `replay_engine`；指标用 `empyrical-reloaded`、日历用 `exchange_calendars`/vortex_data（ADR-1）。
3. **数据**：`DataGateway` + DuckDB 分区/谓词下推，不再全量读盘（ADR-2）。
4. **作业**：`POST /backtests` 入队返回 `202+job_id`，后台执行，`GET` 轮询状态（ADR-3）。
5. **报告**：分钟级产物落 Parquet/CSV 文件，DB 只存摘要 + artifact 路径；分钟查询分页。
6. **配置**：费率/印花税/滑点/参与率可按账户或按回测覆盖。
7. **展示界面**：回测结果看板（见 `design/04`）。
8. **部署与安全**：Docker + compose，挂载 workspace；写接口默认本地绑定，对外暴露需 token（借鉴 vortex_data 教训）。

### Should Have
- 基准对比与相对收益、年化/Sharpe/Sortino/Calmar 等完整绩效页。
- 数据集字段说明与 symbol 校验诊断端点。
- 回测结果导出（CSV/Excel）。

### Cut Entirely
- 数据抓取（属 `vortex_data`）。
- 服务端策略信号生成 / 因子研究。
- QMT/雪球/实盘执行。
- 把界面做成混合策略/交易/数据的大控制台。

## 5. 与 vortex_data 的边界

- `vortex_data` 负责抓取、落盘、质量、查询；`vortex_backtest` 只**消费**其数据契约（schema/分区布局），不复制数据层（ADR-2）。
- 数据访问优先进程内 DuckDB 读共享 workspace；两服务分开部署时切 `vortex_data` HTTP 查询。
- 不在本仓重建数据看板；本仓界面只讲回测结果。

## 6. API 草案

**已有（保留/微调）**：`POST /accounts`、`POST /accounts/{id}/orders`、`GET /symbols/{symbol}`、报告查询若干。

**产品化新增/变更**：

- `POST /backtests` → `202 { job_id, status: "queued" }`（异步，ADR-3）
- `GET /backtests/{job_id}` → 含进度（已处理 bar/总 bar、当前交易日）
- `POST /backtests/{job_id}/cancel`
- `GET /backtests/{job_id}/summary` → 摘要 + 完整绩效指标（empyrical）
- `GET /backtests/{job_id}/minutes?strategy_id=&start=&end=&page=` → **分页**，不再一次性吐全量
- `GET /backtests/{job_id}/equity?freq=daily|minute` → 净值曲线（供看板画图）
- `GET /backtests/{job_id}/rejections?reason=` → 按原因筛选（看板拒单分布）
- 费率/撮合参数随 `POST /backtests` 传入或账户级配置
- 写接口（建账户/下单/提交回测）受 token 保护（对外暴露时）

MVP 不做：任意 SQL 端点、服务端策略/信号端点、绕过作业队列的同步回测。

## 7. Deployment Model

目标"部署进去就能用"：

```
git clone / image pull
  -> 提供 .env（workspace 路径、可选 token）
  -> docker compose up -d
  -> 服务启动：HTTP API + 后台 worker + 结果看板
  -> 客户端脚本 POST 订单/回测 -> 轮询 job -> 看板看结果
```

重启容错：worker 崩溃时把残留 `running` 标 `interrupted` 并重排；workspace 缺数据时回测明确失败为 `*_data_missing`（保留现有预检语义）；写接口默认 `127.0.0.1`，对外需显式 host + token。

---

## 8. 分阶段计划

### 阶段 0 · 基线与防回归（前置，S）
**为什么**：`git status` 显示当前仅 `README.md` 被跟踪，其余全未跟踪（与 vortex_data 改造前同样的状态）。改动前必须固化基线，否则无法 diff/回滚。
**任务**：整体提交基线 commit（确认不带 secrets）；建工作分支；跑通现有 `pytest` 记录绿/红基线；加最小冒烟脚本（起服务 → `/health` → 建账户 → 跑一次极短回测）。
**验收**：基线已提交；pytest 基线有记录；冒烟可一键验证。**风险**：极低。**回滚**：删分支。

### 阶段 1 · 正确性 P0（M）
对应 C1、C3。先止血，否则真实 adj≠1 数据上回测结果不可信。
**任务**：
1. tick/手数等"挂单合法性"校验改对**真实价(raw)**判断，撮合/估值用 qfq（C1）。
2. qfq 基准固定为**全局最新因子**（而非窗口内最新），消除窗口依赖（C3）。
3. 明确并统一"用户下单价口径 vs 内部 qfq 估值口径"；`limit_price` 比较口径一致。
4. **补 adj_factor≠1.0 的测试**（现有 fixture 全是 1.0，`test_api.py:67`，正好遮住这两个 bug）。
**验收**：构造 adj≠1 数据，正常订单不再被 `invalid_price_tick` 误拒；同一历史 bar 在不同窗口的 qfq 绝对价一致；限价单语义稳定。**风险**：口径调整可能改动既有回测数值——需在变更说明里写清。**回滚**：阶段独立可 revert。

### 阶段 2 · 引擎去框架化 + 成熟库（ADR-1，M–L）
**任务**：删 `backtrader` 依赖与死类；`backtrader_adapter.py`→`replay_engine.py`，抽 `Order/Fill/Position/EquityPoint` 数据类；绩效换 `empyrical-reloaded`；T+1 用 `exchange_calendars`(XSHG)/vortex_data 日历；费率/印花税/滑点/参与率配置化；修正 README/设计文档引擎表述；补规则层与撮合核心单测。
**验收**：依赖中无 backtrader；指标由库计算且与手算对拍一致；T+1 用交易日历驱动；`pytest` 全绿；安装/CI 更轻。**风险**：重构面中等，小步提交每步测。**回滚**：按子任务 revert。

### 阶段 3 · 数据访问 DuckDB（ADR-2，M）
**任务**：定义 `DataGateway`；DuckDB 实现做 symbol/date 分区裁剪 + 列裁剪 + 谓词下推；dataset/列白名单 + SQL 参数化（防注入，借鉴 vortex_data S2）；以 vortex_data schema 写对拍测试；预留 HTTP gateway 空壳。
**验收**：单次回测内存/IO 不再随全库增长（用大 workspace 计时验证）；`..%2f` 类越权 dataset 名被拒；DuckDB 结果与预期一致。**风险**：共享磁盘布局假设——用 schema 当契约并测试守护。**回滚**：保留旧 loader 为 fallback 直到 gateway 稳定。

### 阶段 4 · 异步作业 + 报告体积（ADR-3 + A3，M）
**任务**：`POST /backtests` 入队 `202+job_id`；进程内 worker（进程池）执行，启动时把残留 `running` 标 `interrupted` 重排；幂等键去重（A4）；`GET /backtests/{job_id}` 暴露进度；分钟级产物落 Parquet/CSV 文件、DB 只存摘要+artifact 路径；`/minutes` 分页。
**验收**：提交立即返回、可轮询到 `running`；并发回测不打满请求线程；长回测不再把巨型 JSON 塞 SQLite；崩溃可恢复；重复提交被去重。**风险**：作业生命周期/取消/恢复需小心。**回滚**：保留同步路径开关过渡。

### 阶段 5 · 展示界面（见 design/04，M–L）
**任务**：按 `design/04` 实现回测结果看板——回测列表、单次回测概览（净值/回撤曲线、关键指标）、成交、拒单原因分布、持仓、分钟级钻取、多策略对比；轮询作业状态；静态资源由 Python 托管（不引 React 构建链，先与 vortex_data 风格一致）。
**验收**：见 `design/04` 验收标准。**风险**：曲线数据量——配合阶段 4 的分页/降采样。**回滚**：界面独立模块，可灰度。

### 阶段 6 · 强化与功能（v1.1，L）
**任务**：基准对比/相对收益、完整绩效页；结果导出（CSV/Excel）；多策略共享账户模型（v2）；错误信息脱敏（M6）；Docker + compose + 写接口默认安全落地；金额口径统一（M7）。
**验收**：可对基准看相对收益；可导出；写接口默认安全、对外需 token；Docker 一键起。**风险**：共享账户模型是语义升级，需新测试。**回滚**：功能模块化灰度。

---

## 顺序、里程碑与依赖

```
阶段0 基线 ─▶ 阶段1 正确性(P0) ─▶ 阶段2 引擎去框架化 ─┐
                  │                                      ├─▶ 阶段4 异步作业+报告 ─▶ 阶段5 界面 ─▶ 阶段6 强化
                  └──────────────▶ 阶段3 数据访问 ───────┘
```

- 阶段 0 是一切前置；阶段 1 先止血再产品化。
- 阶段 2/3 可部分并行（数据访问不依赖引擎重构）。
- 阶段 4 受益于 2/3，界面（5）依赖 4 的作业状态与新 API。
- 建议里程碑：**M1=阶段0+1（结果可信）**；**M2=阶段2+3+4（轻、快、异步、可观测）**；**M3=阶段5+6（好看、好用、可部署）**。

## 全局原则

- 小步提交、每步跑测试（阶段 2/4 重构面大）。
- 口径/接口变更要知会（阶段 1 改 qfq 口径、阶段 4 改 `POST` 语义属可感知变更，写进迁移说明）。
- 不重复造通用件（指标/日历/数据访问用成熟库）；A 股规则核心保持自有、显式、可测。
- 写操作默认安全：本地绑定 + 对外需 token。

## 验收总清单（全部完成时）

- [ ] adj≠1 数据下正常订单不被误拒；qfq 绝对价不随窗口漂移；limit_price 口径一致。
- [ ] 依赖中无 backtrader；绩效/日历由成熟库提供且对拍通过。
- [ ] 单次回测内存/IO 不随全库增长；查询端点参数化无注入。
- [ ] `POST /backtests` 异步返回 job_id，可轮询进度；并发不打满；崩溃可恢复；重复提交去重。
- [ ] 分钟产物落文件，DB 不再存巨型 JSON；`/minutes` 分页。
- [ ] 费率/滑点/参与率可配置。
- [ ] 结果看板可用（见 design/04 验收）。
- [ ] Docker 一键起；写接口默认安全，对外需 token。
- [ ] 关键路径（规则层/撮合核心/数据 gateway/作业）有单测，`pytest` 全绿。

## Open Questions

- qfq 基准：固定全局最新因子，还是支持 raw/hfq 多口径并存？
- 多策略：何时从"独立账户"升级到"共享账户（统一现金/风控）"？
- 作业并发上限与单任务超时默认值？
- 界面鉴权：MVP 仅本地、还是从一开始就 token？
- 是否需要"基准指数"数据契约（从 vortex_data 取指数行情）？
