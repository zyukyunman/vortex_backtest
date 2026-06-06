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
| KPI 含 Sharpe/Sortino/Calmar/年化/胜率/换手 | `summary` 现仅有 `total_return`/`max_drawdown` | KPI 分两档：**现成**直接渲染，**扩展**指标待后端补（见 §7、§9 后端缺口） |

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
| `EquityChart` | daily | `dates[]`,`equity[]`,`baseline` | loading/empty | line；下方联动水下回撤 |
| `UnderwaterChart` | — | `dates[]`,`drawdown[]` | — | area 负向填充；与净值共 x 轴 |
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

## 6. 图表规格（Chart.js，CDN 白名单）

| 图 | 类型 | 数据来源（真实字段） | 可达性 |
|---|---|---|---|
| 净值曲线 | line | `summary.daily[].trade_date / total_value`（多策略叠 `strategies[].daily`） | 提供"查看数据表"切换；`aria-label` 概述趋势 |
| 水下回撤 | area（负向填充） | `summary.daily[].drawdown` | 同上；与净值共 x 轴、tooltip 联动 |
| 拒单原因分布 | bar（横向） | `summary.rejections[]` 按 `reason` 聚合计数 | 每条带数值标签，非纯色编码 |
| 多策略净值叠加 | multi-line | 各 `strategies[].daily[].total_value` | 线型/标记区分，不只靠颜色 |

- 净值基线 = `initial_cash`（画水平参考线）。
- 大区间（>250 日）前端**降采样**（LTTB 或等距抽样）后再画；本期数据量小，先等距即可。
- 每张图**配套一个隐藏/可展开的数据表**（`<table>`）承载同数据 → 满足屏幕阅读器与 1.1.1。

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
| `GET /api/backtests/{job_id}/summary` | **待加**（现读 summary JSON/artifacts） | KPI + 曲线 + 表 |
| `GET …/equity?strategy_id=`（**仅日级**） | **待加**（可由 summary.daily 切出） | 净值/回撤 |
| `GET …/trades?strategy_id=&symbol=&date=&page=` | **待加**（分页/筛选） | 成交表 |
| `GET …/rejections?reason=&page=`（含聚合计数） | **待加** | 拒单图+表 |
| `GET …/positions?date=&strategy_id=` | **待加** | 持仓表 |
| `POST /api/backtests/{job_id}/cancel`（token） | 视 worker 取消能力 | 取消运行中作业 |

> **后端缺口**（进 §9 估算）：①新增上述只读 REST（可薄包 `store` + summary JSON，分页/筛选在服务端）；②FastAPI 托管静态 SPA；③（可选增强）`empyrical` 绩效指标补进 summary 才能点亮"扩展 KPI"。

### 7.2 KPI 两档

- **现成**（直接渲染）：总收益 `total_return`、最大回撤 `max_drawdown`、期末权益 `total_value`、成交数 `len(trades)`、拒单数 `len(rejections)`、现金/持仓市值。
- **扩展**（需后端补 `empyrical`）：年化、Sharpe/Sortino/Calmar、胜率、换手。未补齐前**不显示空卡**，以免误导。

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
| B3 | Cancel 端点接 worker 取消（若 worker 未支持，先占位 501） | BE | 0.5–1.5 | worker |
| F0 | 前端壳：令牌/主题/路由(hash)/布局/轮询框架 | FE | 2.0 | — |
| F1 | 列表页（筛选+轮询+状态徽章+失败码映射） | FE | 1.5 | B1,F0 |
| F2 | 概览页（KPI 现成档 + 净值/水下回撤图） | FE | 2.0 | B1,F0 |
| F3 | 成交 / 拒单(含分布图) / 持仓 三表 + 分页筛选 | FE | 2.5 | B1,F0 |
| F4 | 多策略对比（叠加+指标表） | FE | 1.0 | F2 |
| A1 | 可达性落地（聚焦环/aria-live/图表数据表/键盘）+ axe/Lighthouse 过 AA | FE | 1.5 | F1–F4 |
| Q1 | 联调/暗色校验/200%缩放/空错态/验收 | FE | 1.0 | 全部 |
| — | **MVP 合计** | | **≈14.5–15.5 eng-day（约 3 周/单人 @80%）** | |
| E1 | 扩展 KPI：后端补 `empyrical` 指标进 summary + 前端点亮 | BE+FE | 2.0 | — |
| E2 | 导出（trades/rejections CSV）、列排序记忆、对比增强 | FE | 1.5 | F3 |

### 9.2 分期（关键路径 = B1 → F1/F2/F3 → A1 → Q1）

- **P5.0 MVP（先交付）**：B1,B2,F0,F1,F2,F3,A1,Q1 → 列表+概览+三表+可达性达 AA。**不含** Sharpe 等扩展指标、不含取消（若 worker 未就绪，Cancel 灰显+提示）。
- **P5.1 增强**：B3 取消、E1 扩展 KPI、E2 导出/排序/对比增强。

### 9.3 瓶颈与建议
- **关键依赖=B1 只读 API**：前端三表/图全卡它，**先排 B1**，可与 F0 前端壳并行起步（F0 用假数据先行）。
- **取消(B3)风险**：依赖 worker 是否支持中断；MVP 可降级为"Cancel 占位/灰显"，不阻塞交付。
- **扩展 KPI 不进 MVP**：避免为指标卡空等后端，先用现成 `total_return/max_drawdown` 等点亮概览。
- 缓冲：估时已按 80% 利用率含自测；勿排到 100%。

---

## 10. 验收标准（在 design/04 基础上 + 本文新增）

- `docker compose up -d` 后页面可加载，第一屏=回测列表（深链可直达某 job）。
- 运行中作业显示进度并自动刷新；终态停轮询；后台标签暂停轮询；局部更新不丢滚动/焦点。
- 概览净值/水下回撤与 KPI 同 `summary` 数值一致（**仅日级**）。
- 拒单页按 `reason` 看分布+明细；0 拒单时给正向空态。
- 多策略可叠加对比净值与指标。
- 失败作业显示确切原因码→人话→下一步（数据缺失指向 vortex_data）。
- 浅/深主题即时切换且刷新保持。
- **可达性闸**：axe/Lighthouse 无 critical；对比度全过 AA；纯键盘可完成"列表→概览→看拒单→取消"；盈亏不只靠颜色。
- 界面无下单/建账户/策略编辑/数据抓取/任意 SQL 入口；**无分钟钻取**（报告日级）。
