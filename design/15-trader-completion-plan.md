---
title: Trader（自研撮合引擎）完善计划与口径决策
created: 2026-06-07
status: Accepted（2026-06-07，与负责人确认）
depends_on: design/14-engine-decision-revisit.md
deciders: 项目负责人 / 后端
---

# Trader 完善计划

承接 `design/14`（删 Qlib、回放走自研引擎 + 直读 parquet）。本文是"把自研 trader 完善到可信、可跑回测、可与券商对账单对照"的执行计划。引擎入口 = `backtrader_adapter.py` 的撮合循环（754 行，含死 bt 类 + 引擎 + 全部助手）。

## 0. 口径决策（拍板记录）

1. **引擎正名**：`backtrader` 这个名字误导（backtrader 早是没真用上的死依赖）。彻底正名为 `replay`。
2. **估值口径 = qfq 前复权，不建模现金分红/红利税**。理由：qfq 已把除权跳空抹平、隐含总收益；再叠加现金分红会**双算**。与券商对账时**按容差判定合理**，不追求逐分钱对齐。
3. **对账容差**：`reconcile` 脚本按可配置容差比较；并用 `events.ex_date`（分红数据集）把"窗口内除权的持仓"标注为**预期 qfq 分红差**，便于区分"已知口径差"与"真 bug"。

## 1. Phase 1 · 清理 + 正名

- **删死依赖**：移除 `backtrader_adapter.py` 顶部 `import backtrader as bt` 与未使用的 `TushareMinutePandasData` feed 类；从 `pyproject.toml` 去掉 `backtrader>=1.9.78`。
- **正名映射**：
  - `vortex_backtest/backtrader_adapter.py` → `vortex_backtest/replay_engine.py`
  - class `BacktraderMinuteReplayEngine` → `MinuteReplayEngine`
  - `EngineName.BACKTRADER="backtrader"` → `EngineName.REPLAY="replay"`；`AccountCreate.engine` 默认 `replay`
  - `store._migrate_account_engines`：把历史值 `backtrader/qlib/rqalpha/ashare_replay` 一律迁为 `replay`；API 入参收到旧值 `backtrader` 时**容错接受**（coerce→replay），不破坏老客户端
  - 同步改 `worker.engine_for`、`cli`、`tests`、`docs`、`README`
- **可选轻重构**：把纯函数助手（execute_order / aggregate_* / *_row / write_* 等）从引擎文件抽到 `replay_core.py`，引擎只留撮合循环 —— 提升可测性、与 design/06 设想一致。**默认做**（除非增加风险）。

## 2. Phase 2 · 修两个已知严重 bug

钉在 `tests/test_regression_bugs.py`（`xfail(strict=True)`，修好会 XPASS → 摘标记）。

- **bug#1 滑点击穿现金**：`market_rules.validate_order` 的买入现金校验改用**含滑点的成交价**（`fill_price*(1+slip)`）判定；与 `execute_order` 实际扣款口径一致。临界单应被 `insufficient_cash` 拒，而非把现金打负。
- **bug#2 多策略日级聚合缺口**：`aggregate_daily` 改为按**完整交易日历并集**对齐；某策略当日无快照（标的停牌/无数据）时 **forward-fill** 其现金+持仓市值（首个快照前用 `initial_cash`），不再把缺席策略当凭空蒸发。

## 3. Phase 3 · 完善 trader 逻辑（qfq 口径内）

- **日级净值按交易日历重建**：以 `dataset.calendar` 为日级轴，每策略 daily 缺席日 forward-fill —— 根治 bug#2，并修"整日停牌漏行"。
- **已实现盈亏**：卖出记 `realized_pnl =(成交价−成本)×量−费用`；策略层汇总 realized/unrealized（qfq 口径，对账容差内）。
- **部分成交透明化**：量能上限裁剪后，成交记录体现 requested vs filled；余量给一条 `volume_capped` 说明（现在静默丢弃）。
- **撮合模型文档化**：明确"按 `price_type` 在当日**首/末分钟**成交 + 合法性闸门（涨跌停/限价/手数/停牌/量能）"。**注：已于 design/16 升级为真·分钟级——订单可带 `exec_time` 盘中分钟、按 at-or-after 该分钟成交，并产出逐分钟净值；日级 open/close 向后兼容。**
- **边界健壮性**：空订单 / 订单日期越界 / 零量 bar / 全停牌日 / 卖超持仓 / 初始资金 0 等明确行为 + 测试。
- **现金不为负不变量**（随 bug#1 落地）。

## 4. Phase 4 · 测试 + 可跑回测 + 对账脚手架

- **金标准用例（手算期望值）**：涨停拒买、跌停拒卖、T+1 锁仓、科创 200 手、量能部分成交、费用（佣金 min5 + 印花仅卖 + 过户）、滑点现金不变量、多策略组合净值跨缺口、停牌持仓估值。
- **端到端可跑**：真实 23 天数据（2026-05-06~06-05）`serve` + CLI 跑通，产出成交 / 拒单 / 持仓 / 分钟净值 / 日净值 / 汇总。
- **对账脚手架** `scripts/reconcile_statement.py`：定义预期对账单 schema（date/symbol/side/qty/price/fee/position…），同批订单回放 vs 对账单 → 差异报告；**可配置容差** + 用 `events.ex_date` 标注"预期 qfq 分红差"。等负责人提供真实对账单即插即用。
- **文档**：更新 `usage-and-api` / `operations`；移除已修 bug 的 xfail。

## 5. 非目标（明确不做）

Qlib（任何形态）、raw 价重构、现金分红/红利税入账、日内触价限价撮合、因子/信号研究、配股。

## 6. 验收标准

- `pytest` 全绿：含新金标准用例；`test_regression_bugs` 两条转通过并摘 xfail。
- 真实数据端到端出全套报告（分钟净值 + 日净值 + 汇总）。
- `reconcile_statement.py` 就绪，待真实对账单跑一次容差对照。
- 全程在分支 `remove-qlib`（或续建分支），不直接动 main；交付 review 后由负责人合并。
