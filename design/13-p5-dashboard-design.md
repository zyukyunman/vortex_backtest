---
title: P5 看板设计（开发交付规格 + 可达性审查 + 工时排期）
created: 2026-06-06
status: ready-for-impl
depends_on: design/04-dashboard-ui-spec.md, design/03-productization-plan.md
supersedes_notes: design/04 §F 分钟钻取、分钟净值端点（P4b 已去分钟产物）
---

# P5 看板设计

design/04 给了"是什么/有哪些视图"的产品规格；本文把它落成**可直接开发的交付规格**——
布局、设计令牌、组件、状态、数据契约——并附 **WCAG 2.1 AA 可达性审查**与**实现工时/分期**。
三个视角分别对应 `design:design-handoff` / `design:accessibility-review` / `operations:capacity-plan`。

定位不变（design/04）：**只读的回测结果查看器**，回答"我这次回测跑成什么样"。不写策略、不建账户、
不下单、不暴露 SQL；唯一写操作=取消运行中的作业（需 token）。技术栈：Python 服务托管的静态
HTML/CSS/JS + 原生轮询 + Chart.js（CDN），无 React/构建链；从第一天 token 化 + 内置暗色。

---

## 0. 与既有决策对齐（必读：改了 design/04 三处）

| design/04 原计划 | 现状裁决 | 看板影响 |
|---|---|---|
| 净值曲线"日级为主、可切分钟级" | **报告只到日净值**（P4b 去掉分钟产物） | 净值/回撤**只画日级**；删 `freq=minute` |
| §F 分钟钻取（盘中轨迹） | 分钟仅是**执行内部**口径，不外显 | **删除视图 F** 及 `/minutes` 端点 |
| KPI 含 Sharpe/Sortino/Calmar/年化/胜率/换手 | `summary` 现仅 `total_return`/`max_drawdown` | 后端 `empyrical` 统一算**完整绩效目录 + 基准相对**（§7.2），**纳入 MVP**；短样本指标加"样本不足"护栏 |
| 净值曲线静态 | 量化要可交互 + 对标指数 | 升级为**交互式**（十字线/缩放/rebase/图例/点选）+ **沪深300 等基准对标**（§6） |

执行口径：**统一分钟级**（引擎把分钟 bar 归约为当日会话 bar），但**对外报告与看板均为日级**。
这不矛盾：分钟决定成交价/量能口径，日级是净值与展示粒度。

---

## 1. 信息架构与导航（便利性优先）

两层、可深链、键盘优先：

```
列表(运行历史)  ──点行──▶  单次回测概览
   ▲                          │  概览内顶部标签页（不跳页、改 hash）
   └──────面包屑/Esc──────────┴─ Overview · Trades · Rejections · Positions · Compare
```

- **深链**：`#/`（列表，带 `?account=&status=` 同步到 URL）；`#/job/<job_id>/overview|trades|rejections|positions|compare`。刷新/分享保持位置。
- **便利性要点**：
  - 顶栏全局：account 筛选、status 筛选、`auto-refresh` 开关、主题切换（跟随系统/浅/深）。
  - 列表行点击进概览；`j/k` 上下选行、`Enter` 进入、`Esc` 返回（渐进增强，非必需）。
  - 概览标签用真链接（`<a href=#/...>`）+ `role=tab`，可中键新开页。
  - 单策略时隐藏 Compare 标签；多策略才出现。
  - 顶栏放**当前作业关键态**（status badge + 进度），切标签时不丢上下文。
- **轮询**：仅对 `queued/running` 作业轮询（2.5s）；终态即停；`document.hidden` 时暂停，可见时立即补一拍。

---

## 2. 布局与响应式

12 列流式栅格，最大内容宽 `1280px`，左右留白 `space-lg`。

| 断点 | 宽度 | 变化 |
|---|---|---|
| Desktop | >1024px | 列表为表格；概览 KPI 4 列；净值+水下回撤左右 2/3 + 1/3 或上下叠放 |
| Tablet | 768–1024px | KPI 2 列；图表全宽堆叠；表格保留横向滚动 |
| Mobile | <768px | 顶栏筛选折叠进抽屉；KPI 1–2 列；表格转为"卡片行"或锁首列横滑；标签页可横向滚动 |

- 表格在窄屏**锁定首列**（job_id / symbol）横向滚动，避免信息丢失。
- 概览首屏（无需滚动）必须容纳：头部信息条 + KPI 卡 + 净值曲线首屏高度。

---

## 3. 设计令牌（Design Tokens）

CSS 变量；`:root` 为浅色，`[data-theme=dark]` 覆盖。**用令牌、不写死值**。

### 3.1 颜色（语义优先；含浅/深）

| 令牌 | 浅色 | 深色 | 用途 |
|---|---|---|---|
| `--bg` | `#ffffff` | `#0f1419` | 页面底 |
| `--surface` | `#f7f8fa` | `#1a212b` | 卡片/表头 |
| `--border` | `#e3e6eb` | `#2b333f` | 描边/分隔 |
| `--text` | `#1a1f29` | `#e6e9ee` | 正文 |
| `--text-muted` | `#5b6472` | `#9aa4b2` | 次要文字 |
| `--accent` | `#1d4ed8` | `#60a5fa` | 链接/选中/聚焦环 |
| `--profit` | `#166534` | `#4ade80` | 盈利（文字档） |
| `--loss` | `#b91c1c` | `#f87171` | 亏损（文字档） |
| `--warn` | `#b45309` | `#fbbf24` | 拒单/告警（琥珀） |
| `--running` | `#1d4ed8` | `#60a5fa` | 进行中 |
| `--idle` | `#4b5563` | `#9ca3af` | 空闲/排队 |
| `--profit-fill` | `#22c55e` | `#22c55e` | 图表/标记填充（非文字，≥3:1 即可） |
| `--loss-fill` | `#ef4444` | `#ef4444` | 同上 |

> 盈亏不仅靠颜色区分（色盲友好，见 §8）：数值带 `+/−` 前缀、方向用 `▲/▼`、拒单用琥珀**且**带原因文案。

### 3.2 字体 / 间距 / 圆角 / 阴影

| 令牌 | 值 | 用途 |
|---|---|---|
| `--font-sans` | system-ui, "Segoe UI", "PingFang SC", Roboto, sans-serif | 全局 |
| `--font-mono` | ui-monospace, "SF Mono", Menlo, monospace | 数字/价格/job_id（**表格数字等宽对齐**） |
| `--fs-kpi` | 28px / 600 | KPI 主数值 |
| `--fs-h1` | 18px / 600 | 页/区标题 |
| `--fs-body` | 14px / 400 | 正文/表格 |
| `--fs-cap` | 12px / 500 | 标签/单位/次要 |
| `--space-xs/sm/md/lg` | 4 / 8 / 16 / 24px | 间距阶梯 |
| `--radius` | 8px（卡片）/ 6px（控件） | 圆角 |
| `--elev-1` | 0 1px 2px rgba(0,0,0,.06) | 卡片浮起 |
| `--focus-ring` | 0 0 0 2px var(--bg), 0 0 0 4px var(--accent) | 统一聚焦环 |

数值类一律 `--font-mono` + 右对齐 + 固定小数位（金额 2 位、比率 2 位百分比）。

---

## 4. 组件清单

| 组件 | 变体 | 关键 props / 数据 | 状态 | 备注 |
|---|---|---|---|---|
| `StatusBadge` | queued/running/completed/failed/cancelled/interrupted | `status`, `progress?` | — | 形+色+文三编码；running 内嵌进度 |
| `KpiCard` | neutral/profit/loss | `label`,`value`,`unit`,`trend?` | loading(骨架)/empty(—) | 主数值 `--font-mono`；盈亏配 ▲▼ |
| `BacktestTable` | — | rows[] | loading/empty/error；行 running 自刷新 | 首列锁定；行可聚焦可 Enter |
| `EquityChart` | absolute / rebase100 | `dates[]`,`equity[]`,`benchmark[]?`,`baseline` | loading/empty | **交互**：十字线 tooltip、缩放/平移/重置、图例开关、点选联动当日明细（§6.1） |
| `UnderwaterChart` | — | `dates[]`,`drawdown[]` | — | area 负向填充；与净值**共 x 轴、游标/缩放联动** |
| `MetricsPanel` | — | `metrics{}`（empyrical） | loading/empty/insufficient | 分组卡：绝对 / 风险调整 / 基准相对；短样本置灰标注（§7.2） |
| `BenchmarkPicker` | — | `indices[]`,`selected` | — | 顶部"对标 [沪深300▾]"，可关；切换重算相对指标（§6.2） |
| `RejectionBar` | — | `{reason:count}` | empty(无拒单=好事，给正向空态) | 横向 bar，按计数降序 |
| `DataTable` | trades/positions/rejections | `columns`,`rows`,`page` | loading/empty/error | 列可排序；分页；数字等宽右对齐 |
| `FilterBar` | — | `account`,`status`,`date?` | — | 选择即写 URL；可重置 |
| `Toast`/`InlineError` | info/error | `code`,`message`,`nextStep` | — | 失败码→人话+下一步（§5） |
| `CancelButton` | — | `jobId` | idle/confirm/pending/done | 二次确认；需 token；仅 running 可见 |
| `ThemeToggle` | system/light/dark | — | — | 偏好存 `localStorage` |

所有交互元素：≥44×44px 命中区、可见聚焦环、`:hover/:active/:disabled` 齐全。

---

## 5. 状态与交互

| 元素 | 状态 | 行为 |
|---|---|---|
| 列表行（running） | 轮询中 | 每 2.5s 局部更新该行 status/进度；**不整页重渲染**（保滚动/焦点） |
| KPI / 表格 / 图表 | loading | 骨架占位（非 spinner 闪烁），保留布局高度防跳动 |
| 任意数据区 | empty | 友好空态：列表"还没有回测"；拒单"0 拒单 ✓ 全部通过"（正向） |
| 作业 | failed | 红条 + **确切原因码→人话→下一步**（见下表） |
| 作业 | running | 进度条（当前交易日 / 已处理 bar，取自 `job.progress`）+ Cancel |
| CancelButton | confirm→pending | 二次确认→调 `POST /cancel`（带 token）→乐观置 cancelling，轮询确认 |
| 主题切换 | — | 即时换 `data-theme`，刷新保持；不触发布局跳动 |

失败码映射（来自 worker `SAFE_ERROR_CODES`）：

| code | 人话 | 下一步 |
|---|---|---|
| `minute_data_missing` | 该区间/标的缺分钟行情 | 去 vortex_data 导出对应分钟数据 |
| `adjustment_data_missing` | 缺复权因子 | 同上（adj_factor） |
| `market_rules_data_missing` | 缺涨跌停/停牌数据 | 同上（stk_limit/suspend） |
| `unsupported_frequency` | 仅支持分钟级 | 用 `frequency=1min` |
| `unsupported_price_adjustment` | 仅支持 qfq | 用 `price_adjustment=qfq` |
| `no_symbols` / `start_end_required` | 入参不全 | 检查策略 symbols / 回测区间 |
| `internal_error` | 服务端异常（已脱敏） | 看服务端日志 / 重试 |

> 引擎内**拒单 ≠ 作业失败**：拒单进 Rejections 页（§6 图 + 明细），不在此红条。

动效（克制）：

| 元素 | 触发 | 动画 | 时长 | 缓动 |
|---|---|---|---|---|
| 标签切换 | 点击 | 内容淡入 | 120ms | ease-out |
| KPI/行更新 | 轮询到新值 | 数值底色一闪（profit/loss 微染） | 400ms | ease-out |
| 骨架 | loading | 轻微 shimmer | 1.2s loop | linear |
| Toast | 失败/成功 | 滑入顶部 | 160ms | ease-out |

`@media (prefers-reduced-motion: reduce)` 时全部降级为无动效（仅状态切换）。

---

## 6. 图表与交互（Chart.js + zoom 插件，CDN 白名单）

净值曲线是核心、必须**可交互**，并支持**基准对标**（默认沪深300）。

### 6.1 交互式净值曲线
- 库：Chart.js + `chartjs-plugin-zoom`（CDN 白名单）。canvas `role=img` + `aria-label` 概述 + 可展开数据表（1.1.1）。
- **悬浮十字线 + tooltip**：跟随光标显示 `日期 / 策略净值 / 基准净值 / 当日收益% / 回撤%`；多策略时逐条列出。
- **缩放 / 平移**：滚轮·双指缩放、拖拽平移、双击或"重置"按钮还原（x=时间轴，y=净值）。
- **区间刷选**：拖选一段 → 局部放大，并联动算该区间"区间收益 / 区间回撤"小指标。
- **rebase 切换**：绝对权益 ¥ ↔ **归一到 100**；叠加基准时默认 rebase，使策略与指数同起点可比。
- **图例可点**：开关 策略 / 基准 / 各子策略 曲线。
- **点选数据点**：跳到该交易日的成交 / 持仓（与 Trades/Positions 标签联动）。
- 水下回撤图与净值**共 x 轴**、游标 / 缩放 / tooltip 联动（同一时间光标）。
- 大区间（>250 日）先 LTTB 降采样再画；本期数据量小可等距。

### 6.2 基准对标（沪深300 等）
- 数据源：vortex_data `index_daily`（日级，含 `close / pct_chg`，已确认覆盖）。可选指数：**沪深300 `000300.SH`（默认）**、中证500 `000905.SH`、上证50 `000016.SH`、创业板指 `399006.SZ`。
- 对齐：按回测交易日历对齐基准日收益；rebase 到策略起点同 100。
- **基准相对指标**（§7.2）：超额收益、年化超额、`alpha / beta`（对基准回归）、信息比率、跟踪误差、上行/下行捕获。
- 选择器：概览顶部"对标 [沪深300▾]"，可关；切换即重算相对指标并叠加曲线。
- 落地可二选一：①后端读 `index_daily` 直接服务基准序列（推荐，解耦 qlib 选股 provider）；②把指数也导出成 qlib instrument（`SH000300`…），供引擎内对冲/相对回测复用（更重，留待将来）。

### 6.3 其他图
| 图 | 类型 | 数据来源（真实字段） | 可达性 |
|---|---|---|---|
| 拒单原因分布 | 横向 bar | `summary.rejections[]` 按 `reason` 聚合计数 | 每条带数值标签，非纯色编码 |
| 多策略净值叠加 | multi-line | 各 `strategies[].daily[].total_value`（可叠基准） | 线型/标记区分，不只靠颜色 |

每张图**配可展开 `<table>` 数据表**；canvas 可聚焦，方向键移动游标并 `aria-live` 播报当前点。

---

## 7. 数据契约（映射真实 models + 端点 + 后端缺口）

看板消费的**读模型**直接来自 `models.py`（已存在字段）：

- `BacktestJobOut`：`job_id, account_id, order_batch_id, market_data_set_id, frequency, price_adjustment, status, start_date, end_date, created_at, completed_at, summary, progress`
- `summary`(`AccountSummaryOut`)：`cash, market_value, total_value, total_return, max_drawdown, positions[], trades[], rejections[], daily[], strategies[], artifacts{}`
- `DailySnapshotOut`：`trade_date, cash, market_value, total_value, daily_pnl, total_return, drawdown, positions[], trades[], rejections[]`
- `TradeOut`：`trade_id, request_id, trade_date, symbol, side_name, quantity, price, amount, commission, stamp_tax, transfer_fee, cash_after`
- `RejectionOut`：`request_id, trade_date, symbol, side_name, quantity, reason`
- `PositionOut`：`symbol, quantity, available_quantity, cost_basis, last_price, market_value, unrealized_pnl, unrealized_pnl_ratio`

### 7.1 端点（读 / 写）

| 端点 | 现状 | 看板用途 |
|---|---|---|
| `GET /api/backtests?account=&status=` | **待加** | 列表 + 轮询 |
| `GET /api/backtests/{job_id}` | 已有（job 详情/进度） | 概览头部、进度 |
| `GET /api/backtests/{job_id}/summary` | **待加**（现读 summary JSON/artifacts） | 概览基础块 |
| `GET …/metrics?benchmark=000300.SH` | **待加**（empyrical 计算） | 绩效面板（绝对 + 风险调整 + 基准相对）|
| `GET …/equity?strategy_id=&benchmark=000300.SH&rebase=` | **待加**（含基准序列，**仅日级**） | 交互曲线 + 对标 |
| `GET …/trades?strategy_id=&symbol=&date=&page=` | **待加**（分页/筛选） | 成交表 |
| `GET …/rejections?reason=&page=`（含聚合计数） | **待加** | 拒单图+表 |
| `GET …/positions?date=&strategy_id=` | **待加** | 持仓表 |
| `GET /api/benchmarks` | **待加**（读 `index_basic/index_daily`） | 可选对标指数列表 |
| `POST /api/backtests/{job_id}/cancel`（token） | 视 worker 取消能力 | 取消运行中作业 |

> **后端缺口**（进 §9 估算）：①只读 REST（薄包 `store` + summary JSON，分页/筛选在服务端）；②**`empyrical` 绩效服务**（绝对/风险调整/基准相对，含短样本护栏）；③**基准序列服务**（读 `index_daily`、按交易日历对齐 + rebase）；④FastAPI 托管静态 SPA。

### 7.2 绩效指标目录（empyrical 计算，日级收益序列）

后端从 `summary.daily[].total_value` 求日收益序列，用 `empyrical` 统一计算；基准相对项需对标指数日收益（§6.2）。年化按 A 股 **244** 交易日；无风险利率默认 0（可接 `shibor`）。

**收益 / 风险（绝对）**

| 指标 | 来源 | 备注 |
|---|---|---|
| 累计收益 | `cum_returns_final` / 现有 `total_return` | 现成 |
| 年化收益 | `annual_return` | |
| 年化波动 | `annual_volatility` | |
| 最大回撤 | `max_drawdown` / 现有 | 现成 |
| 最长回撤 / 修复天数 | 自算（最深谷 + 恢复） | |

**风险调整**

| 指标 | 来源 |
|---|---|
| Sharpe | `sharpe_ratio` |
| Sortino | `sortino_ratio` |
| Calmar | `calmar_ratio` |
| Omega / 尾比 | `omega_ratio` / `tail_ratio` |
| 日 VaR(95%) | `value_at_risk` |

**基准相对（对标沪深300 等，§6.2）**

| 指标 | 来源 |
|---|---|
| 超额收益 / 年化超额 | 策略 − 基准 |
| Alpha / Beta | `alpha_beta`（对基准回归） |
| 信息比率 | `excess_sharpe` |
| 跟踪误差 | 超额收益年化波动 |
| 上行 / 下行捕获 | `up_capture` / `down_capture` |

**成交层（本服务自算，非 empyrical）**：成交数、胜率、盈亏比、平均盈/亏、换手率、持仓暴露、费用合计。

> **短样本护栏（重要）**：风险调整与年化指标在样本过短（经验阈值 **<60 交易日**；当前 smoke 仅 5 日）时**无统计意义**。`MetricsPanel` 对这些卡置灰并标注"样本不足，仅供参考"，避免把噪声当信号；累计收益 / 回撤 / 成交统计不受此限。`/metrics` 返回里带 `sample_days` 与 `low_confidence:true` 标记供前端判定。

---

## 8. 可达性审查（WCAG 2.1 AA）

**标准**：WCAG 2.1 AA ｜ **范围**：本设计令牌 + 交互模式（实现后再用 axe/Lighthouse 复测）。

### 8.1 摘要
预防性设计，已规避常见问题；下表为**设计期需保证项**与验收门槛。

#### Perceivable
| # | 关注点 | 准则 | 级别 | 保证措施 |
|---|---|---|---|---|
| 1 | 盈亏不能只靠红/绿（色盲） | 1.4.1 | 🔴 | 数值带 `+/−`、`▲/▼` 图标、文案；红绿仅作增强 |
| 2 | 正文/数值对比 ≥4.5:1 | 1.4.3 | 🔴 | 见 §8.2 对比表（令牌已选达标值） |
| 3 | 图表/描边/状态点 ≥3:1 | 1.4.11 | 🟡 | `--border`、状态点、图表线均校验 |
| 4 | 图表非文字内容有替代 | 1.1.1 | 🟡 | 每图配可展开数据表 + `aria-label` 概述 |

#### Operable
| # | 关注点 | 准则 | 级别 | 保证措施 |
|---|---|---|---|---|
| 5 | 全功能可键盘达成 | 2.1.1 | 🔴 | 标签/行/筛选/取消均原生可聚焦可操作 |
| 6 | 可见聚焦指示 | 2.4.7 | 🔴 | 统一 `--focus-ring`，禁止 `outline:none` 无替代 |
| 7 | 命中区 ≥44×44 | 2.5.5 | 🟡 | 按钮/标签/行控件最小尺寸 |
| 8 | 逻辑焦点顺序 | 2.4.3 | 🟡 | DOM 序=阅读序；标签页 `tab`/`tablist` 模式 |

#### Understandable / Robust
| # | 关注点 | 准则 | 级别 | 保证措施 |
|---|---|---|---|---|
| 9 | 轮询自动更新不打断焦点/朗读 | 3.2.x / 4.1.3 | 🟡 | 局部更新；状态变化用 `aria-live=polite` 播报 |
| 10 | 失败有明确说明 | 3.3.1 | 🟡 | 原因码→人话→下一步（§5） |
| 11 | 组件有 name/role/value | 4.1.2 | 🟡 | badge=`status`、tab=`role=tab`、表用 `<th scope>` |
| 12 | 200% 缩放不破版 | 1.4.4/1.4.10 | 🟡 | 流式栅格 + 表格横滚，无固定像素裁切 |

### 8.2 对比度核对（选定令牌，近似值，实测以工具为准）

| 元素 | 前景 | 背景 | 比率≈ | 要求 | 通过 |
|---|---|---|---|---|---|
| 正文（浅） | `#1a1f29` | `#ffffff` | 15.8:1 | 4.5 | ✅ |
| 次要文字（浅） | `#5b6472` | `#ffffff` | 5.6:1 | 4.5 | ✅ |
| 盈利文字（浅） | `#166534` | `#ffffff` | 6.4:1 | 4.5 | ✅ |
| 亏损文字（浅） | `#b91c1c` | `#ffffff` | 5.9:1 | 4.5 | ✅ |
| 拒单琥珀（浅） | `#b45309` | `#ffffff` | 5.0:1 | 4.5 | ✅ |
| 链接/选中（浅） | `#1d4ed8` | `#ffffff` | 6.5:1 | 4.5 | ✅ |
| 正文（深） | `#e6e9ee` | `#0f1419` | 14:1 | 4.5 | ✅ |
| 盈利文字（深） | `#4ade80` | `#0f1419` | 9.5:1 | 4.5 | ✅ |
| 亏损文字（深） | `#f87171` | `#0f1419` | 6.5:1 | 4.5 | ✅ |
| 边框/分隔（深） | `#2b333f` | `#0f1419` | 3.1:1 | 3.0 | ✅（非文字） |

### 8.3 键盘 / 屏幕阅读器
| 元素 | Tab | Enter/Space | Esc | 方向键 |
|---|---|---|---|---|
| 列表行 | 进入序列 | 打开概览 | — | `j/k` 移动（增强） |
| 概览标签 | 单 tab 停靠 | 激活标签 | — | ←/→ 切标签（`tablist`） |
| 取消按钮 | 可聚焦 | 触发二次确认 | 关确认 | — |
| 筛选下拉 | 可聚焦 | 展开 | 关闭 | 上下选项 |

屏幕阅读器：作业完成/失败经 `aria-live=polite` 播报（如"作业 57a2 完成，收益 +3.2%"）；图表 `role=img`+`aria-label`，并提供数据表；表格用 `<caption>` 与 `scope`。

### 8.4 优先级
1. 🔴 对比度令牌 + 盈亏非颜色编码 + 键盘可达 + 聚焦环（阻断性，先做）。
2. 🟡 `aria-live` 轮询播报、图表数据表替代、命中区尺寸。
3. 🟢 `j/k` 快捷、动效降级打磨。

---

## 9. 实现工时与分期（capacity-plan 视角）

假设：**1 名全栈/前端为主**的开发投入（与本项目人手一致），按工程师·天（eng-day）估算，目标利用率 ~80%（含联调/自测缓冲）。如多人可并行处「后端只读 API」与「前端壳」。

### 9.1 工作分解与估时

| # | 工作包 | 角色 | 估时(eng-day) | 依赖 |
|---|---|---|---|---|
| B1 | 后端只读 REST（list/summary/equity/trades/rejections/positions，分页+筛选，薄包 store/summary JSON） | BE | 3.0 | 现有 store/report |
| B2 | FastAPI 托管静态 SPA + token 透传（写操作） | BE | 0.5 | B1 |
| B4 | **绩效指标服务**：empyrical 算绝对/风险调整/基准相对 → `/metrics`（含短样本护栏 `sample_days/low_confidence`） | BE | 2.0 | B1 |
| B5 | **基准序列服务**：读 `index_daily`、按交易日历对齐 + rebase → `/benchmarks` 及 `equity?benchmark=` | BE | 1.0 | B1 |
| F0 | 前端壳：令牌/主题/路由(hash)/布局/轮询框架 | FE | 2.0 | — |
| F1 | 列表页（筛选+轮询+状态徽章+失败码映射） | FE | 1.5 | B1,F0 |
| F2 | 概览页：**指标面板**(三组) + **交互式**净值/水下回撤(十字线/缩放/平移/rebase/图例/点选联动) + **基准对标**叠加 | FE | 4.0 | B1,B4,B5,F0 |
| F3 | 成交 / 拒单(含分布图) / 持仓 三表 + 分页筛选 | FE | 2.5 | B1,F0 |
| F4 | 多策略对比（叠加+指标表，可叠基准） | FE | 1.0 | F2 |
| A1 | 可达性落地（聚焦环/aria-live/**交互图键盘游标**/图表数据表）+ axe/Lighthouse 过 AA | FE | 2.0 | F1–F4 |
| Q1 | 联调/暗色校验/200%缩放/空错态/验收 | FE | 1.0 | 全部 |
| — | **MVP 合计** | | **≈20–21 eng-day（约 4 周/单人 @80%）** | |
| B3 | Cancel 端点接 worker 取消（worker 未支持先占位 501） | BE | 0.5–1.5 | worker |
| E2 | 导出（trades/rejections/指标 CSV）、列排序记忆、收益归因增强 | FE | 1.5 | F3 |

### 9.2 分期（关键路径 = B1 →(B4/B5 并行)→ F2 → A1 → Q1）

- **P5.0 MVP**：B1,B2,B4,B5,F0,F1,F2,F3,F4,A1,Q1 → 列表 + 概览(完整指标 + 交互曲线 + 沪深300 对标) + 三表 + 多策略对比 + 可达性 AA。**不含取消**（worker 未就绪则 Cancel 灰显 + 提示）。
- **P5.1 增强**：B3 取消、E2 导出/排序/归因。

### 9.3 瓶颈与建议
- **关键依赖=B1 只读 API**：前端全卡它，**先排 B1**；F0 前端壳可用假数据并行起步。
- **B4/B5 可与前端并行**：指标 / 基准是纯后端计算，定好 `/metrics`、`/benchmarks` 契约后前后端并行推进。
- **短样本护栏不可省**：当前仅 5 日数据，Sharpe/年化等是噪声；UI 必须置灰标注，否则误导用户。
- **取消(B3)风险**：依赖 worker 是否支持中断；MVP 可降级灰显，不阻塞交付。
- 缓冲：估时已按 80% 利用率含自测；勿排到 100%。

---

## 10. 验收标准（在 design/04 基础上 + 本文新增）

- `docker compose up -d` 后页面可加载，第一屏=回测列表（深链可直达某 job）。
- 运行中作业显示进度并自动刷新；终态停轮询；后台标签暂停轮询；局部更新不丢滚动/焦点。
- 概览净值/水下回撤与 KPI 同 `summary` 数值一致（**仅日级**）。
- 概览展示**完整绩效目录**（年化/波动/Sharpe/Sortino/Calmar + 基准相对 alpha/beta/IR/超额）；**短样本**指标带"样本不足"置灰标注。
- 净值曲线**可交互**：悬浮十字线 tooltip（日期/策略净值/基准/当日收益/回撤）、缩放·平移·重置、绝对↔rebase100 切换、图例开关、点选数据点联动当日成交/持仓。
- 可叠加**沪深300 等基准**并算相对指标；可切换/关闭对标。
- 交互图**键盘可达**（方向键移游标 + `aria-live` 播报）并配数据表。
- 拒单页按 `reason` 看分布+明细；0 拒单时给正向空态。
- 多策略可叠加对比净值与指标。
- 失败作业显示确切原因码→人话→下一步（数据缺失指向 vortex_data）。
- 浅/深主题即时切换且刷新保持。
- **可达性闸**：axe/Lighthouse 无 critical；对比度全过 AA；纯键盘可完成"列表→概览→看拒单→取消"；盈亏不只靠颜色。
- 界面无下单/建账户/策略编辑/数据抓取/任意 SQL 入口；**无分钟钻取**（报告日级）。
