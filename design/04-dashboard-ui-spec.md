---
title: vortex_backtest 展示界面规格
created: 2026-06-06
status: draft
depends_on: design/03-productization-plan.md
---

# vortex_backtest 展示界面规格

## Product Decision

MVP 需要一个 HTML 界面，但它**只回答量化用户一个问题**：

> 我提交的这次回测，跑成什么样了——净值/回撤怎样，成交了哪些，哪些被拒、为什么被拒，持仓和分钟轨迹如何？

它**不是**策略 IDE、不是数据采集控制台、不是下单台。它是**只读的回测结果查看器**：

- 不在界面里写策略（策略是客户端脚本，调用本服务）。
- 不在界面里做复杂设置（用户明确：界面"大概率不去做设置"；设置=跑某个策略脚本，由脚本调服务）。
- 唯一允许的写操作是**取消正在跑的作业**（且对外暴露时需 token）。

为部署简单，MVP 采用 Python 服务直接托管的静态 HTML/CSS/JS + 原生 JS 轮询，**不引入 React/Vite 构建链**（与 `vortex_data` 看板一致）。图表用 Chart.js（CDN）。从第一天就**设计 token 化 + 内置暗色**（吸取 vortex_data 看板"只做浅色、后期补暗色"的教训）。

视觉方向：安静、密集、分析风。盈亏用色克制有语义——**绿=盈利 / 红=亏损 / 琥珀=拒单原因 / 蓝=进行中 / 灰=空闲**。不要营销式落地页，第一屏就是回测历史。

## First Screen

第一屏是**回测列表（运行历史）**，不是欢迎页。带参数打开某个 `job_id` 时直接进该次回测的概览。

```text
┌──────────────────────────────────────────────────────────────────┐
│ vortex_backtest    account:[all▾]  status:[all▾]   🔄auto  ◐theme │
├──────────────────────────────────────────────────────────────────┤
│ Backtests                                                          │
│ job_id   account  window           status     return  maxDD  trades│
│ 3bd7…    demo     0102–0131 1min   ● running 62%                   │
│ 57a2…    demo     0102–0105 1min   ✓ done     +3.2%  -1.1%   14     │
│ dc02…    star     0102–0110 1min   ✗ failed  minute_data_missing   │
└──────────────────────────────────────────────────────────────────┘
        点一行 → 单次回测概览
```

## Views

### A. Backtests（列表）
**目的**：看所有回测作业及其状态/关键结果。
**列**：job_id、account、window（start–end + freq）、status、total_return、max_drawdown、#trades、#rejections、created。
**筛选**：account、status（queued/running/completed/failed/cancelled）。
**行为**：对 `running` 行显示进度（已处理 bar/总 bar、当前交易日）并轮询；终态停止轮询。`failed` 行直接显示失败原因（`*_data_missing` / `unsupported_*` 等）。

### B. Backtest Overview（单次回测概览）— 核心页
**目的**：一屏看懂这次回测好不好。
**模块**：
- 头部：account、window、qfq 口径、引擎、状态；多策略时显示策略数。
- KPI 卡：总收益、年化、最大回撤、Sharpe/Sortino/Calmar、成交数、胜率、换手、期末权益（指标来自 `empyrical`，见 `design/02` ADR-1）。
- **净值曲线**：日级为主、可切分钟级（大区间自动降采样）；下方叠**水下回撤图**（underwater）。
- 多策略：各子账户净值缩略叠加 + "进入对比"入口（见 F）。
- 失败/进行中：失败显示原因与下一步；进行中显示进度条。

### C. Trades（成交）
**目的**：核对成交明细。
**列**：time、strategy、symbol、side、qty、price、amount、commission、stamp_tax、transfer_fee、cash_after。
**筛选**：strategy / symbol / 日期；支持导出（v1.1）。

### D. Rejections（拒单）— 量化用户最关心的调试面
**目的**：回答"我的单为什么没成交"。
**模块**：
- **拒单原因分布图**（按 `reason` 计数的条形图）：`suspended / zero_volume / invalid_price_tick / invalid_lot_size / limit_up_buy_blocked / limit_down_sell_blocked / insufficient_cash / insufficient_position / t_plus_1_not_sellable / volume_cap_below_lot`。
- 明细表：request_id、time、symbol、side、qty、reason；按 reason 筛选。
> 这是把"撮合规则透明化"的关键页——拒单原因枚举本就是本服务的优势，要在界面放大。

### E. Positions（持仓）
**目的**：看期末与每日持仓。
**列**：strategy、symbol、qty、available_qty、cost_basis、last_price、market_value、unrealized_pnl、unrealized_pnl_ratio。
**行为**：日期切换看每日快照。

### F. Minute Drilldown（分钟钻取）
**目的**：看某交易日的盘中轨迹。
**行为**：选某日 → 分钟净值曲线 + 当日盘中成交/拒单标注；**分页/降采样**取数（配合 `design/03` 阶段 4 的分钟产物落文件与分页 API），不一次性拉全量。

### G. Strategy Comparison（多策略对比）
**目的**：横向比较各子账户。
**模块**：净值曲线叠加；指标对比表（收益/回撤/Sharpe/成交/拒单数）。

## State Semantics

作业状态：`queued`（已入队）｜`running`（执行中，显示进度）｜`completed`｜`failed`（显示原因）｜`cancelled`｜`interrupted`（worker 崩溃后重排）。

进行中必须显示具体进度（当前交易日 / 已处理 bar）；失败必须显示**确切原因与下一步**：

- `minute_data_missing` / `adjustment_data_missing` / `market_rules_data_missing` → 提示补哪个数据集（指向 vortex_data）。
- `unsupported_frequency/price_adjustment` → 提示当前仅支持 `1min`/`qfq`。
- 引擎内拒单不是作业失败——在 Rejections 页呈现。

## Charts

仅用 Chart.js（CDN 白名单）：

- 净值曲线（日/分钟，line）。
- 水下回撤（area，负向填充）。
- 拒单原因分布（bar）。
- 多策略净值叠加（multi-line）。

大区间分钟序列在前端**降采样**后再画（或由 `/equity` 端点服务端降采样）。

## API Needed by Page

读：

- `GET /api/backtests?account=&status=`（列表 + 进度）
- `GET /api/backtests/{job_id}`（状态 + 进度 + 元信息）
- `GET /api/backtests/{job_id}/summary`（KPI + empyrical 绩效）
- `GET /api/backtests/{job_id}/equity?freq=daily|minute&strategy_id=&downsample=`（曲线）
- `GET /api/backtests/{job_id}/trades?strategy_id=&symbol=&date=&page=`
- `GET /api/backtests/{job_id}/rejections?reason=&page=`（含按 reason 聚合计数）
- `GET /api/backtests/{job_id}/positions?date=&strategy_id=`
- `GET /api/backtests/{job_id}/minutes?strategy_id=&date=&page=`

写（需 token）：

- `POST /api/backtests/{job_id}/cancel`

> 端点对应 `design/03` §6；分页/降采样对应 `design/03` 阶段 4。**界面不暴露任意 SQL、不暴露下单/建账户、不做复杂设置。**

## Implementation Notes

- 静态页由 Python API 托管；`web/static/app.css|app.js` + `web/templates/index.html`，从一开始就拆文件（别学 vortex_data 把 1700 行塞进 Python 字符串）。
- 设计 token 化 + `[data-theme=dark]`，顶栏主题切换（跟随系统/浅/深），偏好存 `localStorage`。
- 轮询：仅对 `running` 作业轮询（2–3s），终态即停；后台标签页（`document.hidden`）暂停轮询。
- 局部更新而非整页 `innerHTML` 重渲染，保住滚动/焦点（避免 vortex_data F2 的坑）。
- 分钟/大序列分页或服务端降采样，避免大 JSON。
- 任何写操作（取消）走 token；读接口本地可用、对外暴露需 token。

## Acceptance Criteria

- `docker compose up -d` 后页面可加载，第一屏是回测列表。
- 进行中作业显示进度并自动刷新；终态停止轮询。
- 概览页正确显示净值/回撤曲线与 KPI（与 `summary` 数值一致）。
- 拒单页能按 reason 看分布与明细——量化用户能据此定位"为什么没成交"。
- 多策略可叠加对比净值与指标。
- 失败作业显示确切原因与下一步（数据缺失指向 vortex_data）。
- 浅/深主题切换即时生效且刷新后保持；后台标签停轮询。
- 界面不出现下单、建账户、策略编辑、数据抓取或任意 SQL 入口。
