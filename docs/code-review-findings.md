---
title: 代码评审问题报告（测试扩充 / 缺陷排查）
created: 2026-06-06
status: review
---

# 代码评审问题报告

本轮工作：为分钟级回测补充综合测试 + HTTP 接口全量调用测试 + 真实 Tushare 集成测试，并排查代码问题。**未改动任何生产代码**；每个确认的缺陷都配了一个 `xfail(strict=True)` 回归测试钉住（修复后会自动转红提醒移除标记）。

新增测试文件：

| 文件 | 内容 |
|---|---|
| `tests/conftest.py` | 共享夹具：可链式构造任意分钟行情的 `MinuteWorkspaceBuilder` |
| `tests/test_minute_backtest.py` | 分钟撮合综合用例（T+1 / 限价 / 涨跌停 / 手数 / 量上限 / 费用 / 现金 / 停牌 / 净值 / qfq …） |
| `tests/test_api_endpoints.py` | HTTP 全端点：状态码、校验、过滤、作业生命周期、鉴权 |
| `tests/test_regression_bugs.py` | 钉住下述 #1 / #2 的 `xfail` 回归 |
| `tests/test_real_data_integration.py` | 真实 stk_mins 端到端（`-m integration`，缺数据自动跳过） |

> 复现说明：#1、#2 已用**逐行照抄引擎逻辑**的独立脚本在隔离环境复现并量化（脚本见交付附带的 `verify_engine.py`）。

---

## 严重（High）

### #1 滑点未计入买入现金校验 → 成交后现金可为负

- **位置**：`market_rules.AShareRuleEngine.validate_order`（BUY 分支）配合 `backtrader_adapter.execute_order`；`qlib_engine._run_strategy` 同源。
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
- **钉住测试**：`test_regression_bugs.py::test_slippage_must_not_drive_cash_negative`（断言现金 ≥ 0）。

### #2 多策略日级聚合在“日期缺口”处漏算缺席策略 → 组合净值/回撤失真

- **位置**：`backtrader_adapter.aggregate_daily`。
- **根因**：`backtrader` 引擎只对“该策略当日有分钟 bar”的日期产出 daily 快照；某策略标的当日停牌/无数据时该日**无快照**。`aggregate_daily` 按交易日分组后，只把“当日有快照的策略”相加：

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
- **附带不一致**：`qlib_engine` 按**交易日并集**为每个策略逐日补快照，**不受此缺陷影响** → 两个引擎对同一多策略输入会给出不同的组合日级曲线。
- **建议**：`aggregate_daily` 改为对“全体交易日 × 全体策略”补齐——某策略某日无快照时，用其上一有效快照结转（现金不变、持仓按最近价估值），再求和。
- **钉住测试**：`test_regression_bugs.py::test_multi_strategy_daily_aggregation_handles_missing_dates`。

---

## 中（Medium）

### #3 成交量上限部分成交：剩余量静默丢弃，既无成交也无拒单

- **位置**：`backtrader_adapter._run_strategy`（及 `qlib_engine._run_strategy`）。
- **现象**：`max_volume_participation < 1` 或当日量不足时，`executable_quantity` 把下单量截断并向下取整到整手后**部分成交**；未成交的剩余部分既不在 `trades` 也不在 `rejections` 中。
  - 例：当日量 1500、参与率 0.5 → 上限 750 → 成交 700 股，剩余 300 股无任何记录。
- **影响**：观察性缺口。调用方无法从报告区分“全部成交”与“因量受限只成交了一部分”，对账困难。
- **建议**：对被截断的剩余量补一条信息性记录（如 `rejections` 增加 `volume_capped_remainder` 原因，或在 `trade` 上加 `requested_quantity` 字段）。
- **现状已被测试覆盖**：`test_minute_backtest.py::test_volume_participation_caps_fill_to_lot`（断言当前行为：成交 700、无剩余拒单），修复后据此调整。

### #4 写接口鉴权按环境变量判回环，`serve --host 0.0.0.0` 旗标可绕过

- **位置**：`app.require_write_auth` 读取 `os.getenv("VORTEX_BACKTEST_HOST","127.0.0.1")` 判断是否回环；`cli.cmd_serve` 用 `--host` 旗标绑定。
- **现象**：`vortex-backtest serve --host 0.0.0.0`（未同时设环境变量 `VORTEX_BACKTEST_HOST`）→ 实际绑定 `0.0.0.0`，但鉴权仍读到默认 `127.0.0.1` → 判为回环 → **未配 token 也放行写接口**。
  - 注：`docker-compose` 路径通过环境变量统一设 `VORTEX_BACKTEST_HOST`，且默认 `0.0.0.0` + 空 token → fail-closed 403，**不受影响**；该缺口仅在用 CLI 旗标直接起服务时出现。
- **影响**：非回环暴露下写接口（建账户/下单/提交回测）可被未授权调用。
- **建议**：鉴权的“是否回环”应取自**实际绑定地址/请求**而非环境变量；或让 `cmd_serve` 把 `--host` 回填进环境后再判定。

---

## 低 / 观察性（Low）

### #5 单策略在标的停牌/无 bar 日不产出当日 daily 点（#2 同源）

`_run_strategy` 仅遍历有分钟 bar 的时间戳，停牌日不生成 daily 快照 → 单策略自身的净值曲线缺该日点位；若仍持仓，跨停牌日的市值沿用停牌前最新价（陈旧估值）。建议与 #2 一并按交易日历补齐。

### #6 模块导入即启动后台 worker；多进程部署会重复执行作业

`app.py` 末尾 `app = create_app()`（默认 `run_worker=True`）在 import 时即起后台 worker 线程。若用 `uvicorn --workers N` 多进程部署，会有 N 个 worker，且每个进程启动时 `store.requeue_interrupted()` 会把**其他进程正在跑的** `running` 作业重排回 `queued` → 可能被重复领取执行。建议：单 worker 进程部署；或给作业加租约/心跳与“运行中超时”判定，避免误重排。

### #7 可创建 `engine="qlib"` 账户，但本机无 `pyqlib` 时回测仅得 `internal_error`

`AccountCreate.engine` 允许 `qlib`；`worker.engine_for` 惰性 import `qlib_engine` → `QlibReplayEngine` 运行时 `import qlib`。本机/镜像未装 `pyqlib` 时抛 `ImportError`，因不在 `SAFE_ERROR_CODES` 被脱敏为 `internal_error`，调用方无从知晓真因。建议：建账户时校验引擎可用性，或为“引擎不可用”补一个安全错误码。

### #8 `stk_limit` 读取未做 symbol 分区裁剪（性能）

`data_adapter.load` 中 `self._read_required("stk_limit", ...)` **未传 `symbols`** → 全市场涨跌停 Parquet 全量读盘，再按 symbol/date 过滤。单标的回测也会读整个市场的涨跌停表。`stk_mins`/`adj_factor` 已做分区裁剪，唯独 `stk_limit` 漏了。建议：若 `stk_limit` 按 `symbol=` 分区，则同样传 `symbols` 裁剪；否则在文档中标注其按日期分区的读放大。

---

## 复测建议

```bash
# 常规（合成数据，含 xfail 钉住缺陷，CI 应全绿/xfail）
.venv/bin/python -m pytest -q

# 真实数据集成（需 VORTEX_DATA_WORKSPACE 指向含 data/stk_mins 的 workspace）
export VORTEX_DATA_WORKSPACE=/path/to/vortex_workspace
.venv/bin/python -m pytest -m integration -q

# 修复 #1/#2 后，对应 xfail 将变为 XPASS（strict → 失败），据此移除标记并确认修复
```
