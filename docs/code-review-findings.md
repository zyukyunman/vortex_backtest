---
title: 代码评审问题报告（测试扩充 / 缺陷排查）
created: 2026-06-06
updated: 2026-06-07
status: review
---

# 代码评审问题报告

> **2026-06-07 更新**：本报告成文于 design/15 修复前。其后 design/14（删 Qlib）+ design/15（引擎正名 `replay` + 修复）已落地，状态变更如下——
> 模块更名：`backtrader_adapter` → **`replay_engine`**；`qlib_engine` 已删除（引擎只剩自研 `replay`）。
> **已解决**：#1（滑点现金校验，commit 948205c）、#2（多策略日级聚合，commit 87cdeb5）、#3（部分成交透明度，commit 24e3e87 增 `requested_quantity`）、#7（Qlib 移除，`engine="qlib"` 归一为 `replay`，不再 import pyqlib）。#5 与 #2 同源，随 #2 修复（建议复测确认）。
> **仍待办**：#4（`serve --host` 旗标绕过鉴权）、#6（模块级 worker / 多进程重复领取）、#8（`stk_limit` 未按 symbol 裁剪）。下文保留原始分析备查，标题加注当前状态。

本轮工作：为分钟级回测补充综合测试 + HTTP 接口全量调用测试 + 真实 Tushare 集成测试，并排查代码问题。**未改动任何生产代码**；每个确认的缺陷都配了一个 `xfail(strict=True)` 回归测试钉住（修复后会自动转红提醒移除标记）。

新增测试文件：

| 文件 | 内容 |
|---|---|
| `tests/conftest.py` | 共享夹具：可链式构造任意分钟行情的 `MinuteWorkspaceBuilder` |
| `tests/test_minute_backtest.py` | 分钟撮合综合用例（T+1 / 限价 / 涨跌停 / 手数 / 量上限 / 费用 / 现金 / 停牌 / 净值 / qfq …） |
| `tests/test_api_endpoints.py` | HTTP 全端点：状态码、校验、过滤、作业生命周期、鉴权 |
| `tests/test_regression_bugs.py` | 钉住下述 #1 / #2 的 `xfail` 回归（#1/#2 修复后已转为正向断言） |
| `tests/test_real_data_integration.py` | 真实 stk_mins 端到端（`-m integration`，缺数据自动跳过） |

> 复现说明：#1、#2 已用**逐行照抄引擎逻辑**的独立脚本在隔离环境复现并量化（脚本见交付附带的 `verify_engine.py`）。

---

## 严重（High）

### #1 [已修复 · design/15] 滑点未计入买入现金校验 → 成交后现金可为负

- **位置**：`market_rules.AShareRuleEngine.validate_order`（BUY 分支）配合 `replay_engine.execute_order`。
- **根因**：现金充足性按 `fill_price`（不含滑点）判定：

  ```python
  total_cost = executable_quantity * fill_price + 费用(按 fill_price)
  if total_cost > cash + 1e-8: return "insufficient_cash"
  ```

  但实际成交用 `exec_price = fill_price * (1 + slippage_bps/1e4)` 扣款。两者口径不一致。
- **复现**：初始现金 `9010`，买 `900` 股、`fill=10.0`、`slippage_bps=50`。
  - 校验（按 fill）：`total_cost = 9005.09 ≤ 9010` → **通过**。
  - 成交（按 exec 10.05）：`cash_after = 9010 − 9045 − 5.09 = −40.09` → **现金为负**。
- **影响**：开启滑点后，临界买单会击穿现金约束，产生不存在的负现金/超额持仓，净值与收益被高估；滑点越大、下单越贴近满仓越易触发。
- **建议**：现金校验改用 `exec_price`（含滑点）计算 `total_cost`；或在 `execute_order` 后对负现金兜底拒单。
- **状态**：✅ 已修复（design/15 Phase 2，commit 948205c）——现金校验改用含滑点 `exec_price`；`test_regression_bugs.py::test_slippage_must_not_drive_cash_negative` 已由 xfail 转为正向断言（现金 ≥ 0）。

### #2 [已修复 · design/15] 多策略日级聚合在“日期缺口”处漏算缺席策略 → 组合净值/回撤失真

- **位置**：`replay_engine.aggregate_daily`。
- **根因**：`replay` 引擎只对“该策略当日有分钟 bar”的日期产出 daily 快照；某策略标的当日停牌/无数据时该日**无快照**。`aggregate_daily` 按交易日分组后，只把“当日有快照的策略”相加：

  ```python
  for trade_date, snapshots in sorted(grouped.items()):
      cash = sum(... for item in snapshots)        # 缺席策略被整体漏掉
      market_value = sum(... for item in snapshots)
  ```

  缺席策略的现金与持仓被当成“凭空蒸发”。
- **复现**：策略 A（标的两天都有 bar）、策略 B（标的仅第 2 天有 bar），各 1 万现金、都不下单。
  - 第 1 天聚合净值 = `10000`（只算到 A），**正确应为 `20000`**（B 空仓持 1 万）。
  - → 虚构 `−50%` 单日暴跌再 `+100%` 反弹，**最大回撤被夸大为 `−0.5`**（真实≈0）。
- **影响**：任何含停牌/不同上市起始日/标的交易日不齐的多策略回测，组合 `daily` 净值曲线、`daily_pnl`、`max_drawdown`、`total_return(按日)` 全部失真。
- **建议**：`aggregate_daily` 改为对“全体交易日 × 全体策略”补齐——某策略某日无快照时，用其上一有效快照结转（现金不变、持仓按最近价估值），再求和。
- **状态**：✅ 已修复（design/15 Phase 2，commit 87cdeb5）——按全交易日并集补齐结转。`test_regression_bugs.py::test_multi_strategy_daily_aggregation_handles_missing_dates` 已转正。（注：原“附带不一致：qlib_engine 按交易日并集不受影响”一条已**失效**——Qlib 引擎已于 design/14 删除，现仅余单一 `replay` 引擎。）

---

## 中（Medium）

### #3 [已落地 · design/15] 成交量上限部分成交：剩余量静默丢弃，既无成交也无拒单

- **位置**：`replay_engine._run_strategy`。
- **现象**：`max_volume_participation < 1` 或当日量不足时，`executable_quantity` 把下单量截断并向下取整到整手后**部分成交**；未成交的剩余部分既不在 `trades` 也不在 `rejections` 中。
  - 例：当日量 1500、参与率 0.5 → 上限 750 → 成交 700 股，剩余 300 股无任何记录。
- **影响**：观察性缺口。调用方无法从报告区分“全部成交”与“因量受限只成交了一部分”，对账困难。
- **建议**：对被截断的剩余量补一条信息性记录（如 `trade` 上加 `requested_quantity` 字段）。
- **状态**：✅ 已落地（design/15 Phase 3，commit 24e3e87）——成交记录新增 `requested_quantity`（原始下单量），`quantity ≠ requested_quantity` 即部分成交，调用方可识别量能受限。

### #4 [仍待办] 写接口鉴权按环境变量判回环，`serve --host 0.0.0.0` 旗标可绕过

- **位置**：`app.require_write_auth` 读取 `os.getenv("VORTEX_BACKTEST_HOST","127.0.0.1")` 判断是否回环；`cli.cmd_serve` 用 `--host` 旗标绑定。
- **现象**：`vortex-backtest serve --host 0.0.0.0`（未同时设环境变量 `VORTEX_BACKTEST_HOST`）→ 实际绑定 `0.0.0.0`，但鉴权仍读到默认 `127.0.0.1` → 判为回环 → **未配 token 也放行写接口**。
  - 注：`docker-compose` 路径通过环境变量统一设 `VORTEX_BACKTEST_HOST`，且默认 `0.0.0.0` + 空 token → fail-closed 403，**不受影响**；该缺口仅在用 CLI 旗标直接起服务时出现。
- **影响**：非回环暴露下写接口（建账户/下单/提交回测）可被未授权调用。
- **建议**：鉴权的“是否回环”应取自**实际绑定地址/请求**而非环境变量；或让 `cmd_serve` 把 `--host` 回填进环境后再判定。
- **状态**：⏳ 仍待办（2026-06-07 复核：`app.py` 仍按 `os.getenv` 判回环，`cmd_serve` 未回填 host）。

---

## 低 / 观察性（Low）

### #5 [随 #2 处理] 单策略在标的停牌/无 bar 日不产出当日 daily 点（#2 同源）

`_run_strategy` 仅遍历有分钟 bar 的时间戳，停牌日不生成 daily 快照 → 单策略自身的净值曲线缺该日点位；若仍持仓，跨停牌日的市值沿用停牌前最新价（陈旧估值）。建议与 #2 一并按交易日历补齐。
**状态**：与 #2 同源，design/15 的全交易日补齐已覆盖聚合路径；单策略明细建议复测确认。

### #6 [仍待办] 模块导入即启动后台 worker；多进程部署会重复执行作业

`app.py` 末尾 `app = create_app()`（默认 `run_worker=True`）在 import 时即起后台 worker 线程。若用 `uvicorn --workers N` 多进程部署，会有 N 个 worker，且每个进程启动时 `store.requeue_interrupted()` 会把**其他进程正在跑的** `running` 作业重排回 `queued` → 可能被重复领取执行。建议：单 worker 进程部署；或给作业加租约/心跳与“运行中超时”判定，避免误重排。
**状态**：⏳ 仍待办（2026-06-07 复核：`app.py:579 app = create_app()` 模块级启动 worker 未变）。

### #7 [已消解：Qlib 移除] 可创建 `engine="qlib"` 账户，但本机无 `pyqlib` 时回测仅得 `internal_error`

原问题：`AccountCreate.engine` 允许 `qlib`，`worker.engine_for` 惰性 import `qlib_engine` → 运行时 `import qlib`，未装 `pyqlib` 时抛 `ImportError` 被脱敏为 `internal_error`。
**状态**：✅ 已消解（design/14 删 Qlib）——`qlib_engine` 已删除；`engine="qlib"`（连同 `backtrader/rqalpha/ashare_replay`）在 `models.py` / `worker.py` 被**归一为 `replay`**，不再 import 任何 qlib，故该 `internal_error` 路径已不存在。

### #8 [仍待办] `stk_limit` 读取未做 symbol 分区裁剪（性能）

`data_adapter.load` 中 `self._read_required("stk_limit", ...)` **未传 `symbols`** → 全市场涨跌停 Parquet 全量读盘，再按 symbol/date 过滤。单标的回测也会读整个市场的涨跌停表。`stk_mins`/`adj_factor` 已做分区裁剪，唯独 `stk_limit` 漏了。建议：若 `stk_limit` 按 `symbol=` 分区，则同样传 `symbols` 裁剪；否则在文档中标注其按日期分区的读放大。
**状态**：⏳ 仍待办（2026-06-07 复核：`data_adapter.py` 读 `stk_limit` 仍未传 `symbols`）。

---

## 复测建议

```bash
# 常规（合成数据；#1/#2 修复后回归测试为正向断言，CI 应全绿）
.venv/bin/python -m pytest -q

# 真实数据集成（需 VORTEX_DATA_WORKSPACE 指向含 data/stk_mins 的 workspace）
export VORTEX_DATA_WORKSPACE=/path/to/vortex_workspace
.venv/bin/python -m pytest -m integration -q
```
