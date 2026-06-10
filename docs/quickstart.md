# vortex_backtest 快速上手：怎么用这个回测功能

面向"我手上有账户 + 一批订单，想回放看看成交/拒单/持仓/净值"的使用者。完整接口参考见
[usage-and-api.md](usage-and-api.md)；口径与设计见 [design/15](../design/15-trader-completion-plan.md)。

---

## 0. 前置

- **Python 3.12 或 3.13** 的虚拟环境（仓库 `.venv` 已就绪）。`pip install -e '.[dev]'` 装运行+测试依赖。
- **行情数据**：来自 `vortex_data`。用环境变量指向其 workspace（loader 自动接 `/data`）：

  ```bash
  export VORTEX_WORKSPACE=$WS          # vortex_data 导出的 workspace 根
  export VORTEX_INDEX_DATA_DIR="$VORTEX_WORKSPACE/data/index_daily"   # 基准曲线用，可选
  ```

- **当前可用数据窗口**：`2026-05-06 ~ 2026-06-05`（约 23 个交易日）。订单交易日须落在窗口内、且当日该标的有行情，否则记 `no_market_data` 拒单。

---

## 1. 三十秒上手（自包含样例，无需起服务）

```bash
./.venv/bin/python examples/quickstart.py
```

它在进程内跑完整链路（建账户 → 提交一买一卖 → 回测 → 打印成交/拒单/持仓/日净值/汇总），
并把报告落到一个临时目录。改 `QS_SYMBOL / QS_BUY_DATE / QS_SELL_DATE / QS_START / QS_END` 环境变量即可换标的与区间。

---

## 2. 生产形态：起 HTTP 服务 + 走 HTTP

服务是独立 HTTP 服务，回测**异步**（提交即返回 `job_id`，轮询到 `completed`）。所有操作走 HTTP；命令行只用来起服务。

```bash
# (1) 起服务（默认 127.0.0.1:8766；命令行只剩 serve）
./.venv/bin/vortex-backtest serve --port 8766
#   等价：./.venv/bin/python -m vortex_backtest.cli serve --port 8766
```

另开一个终端，用**开闭环脚本**一条命令跑完「建账户 → 买卖 → 结束 → 关闭 → 报告」（仅依赖 curl + python3）：

```bash
scripts/backtest_roundtrip.sh --symbol 600000.SH --start 2026-05-06 --end 2026-06-05
#   远端 + 鉴权：scripts/backtest_roundtrip.sh --base-url http://10.0.0.5:8766 --token "$TOK"
#   全部选项：  scripts/backtest_roundtrip.sh --help
```

或自己拼 HTTP（等价端点，详见 [usage-and-api.md](usage-and-api.md) §3–4）：
`POST /accounts` · `POST /accounts/{id}/orders` · `POST /backtests`（→202+job_id）·
`GET /backtests/{job_id}`（轮询到 `completed`）· `GET /backtests/{job_id}/summary|daily|minutes|trades|rejections`。

---

## 3. 输入格式

**账户**：`{"account_id": "demo", "initial_cash": 1000000}`（`engine` 默认 `replay`，旧值
`backtrader/qlib/...` 会自动归一）。

**订单**（外部委托，按 `account_id + order_batch_id + request_id` 幂等去重）：

```json
{
  "request_id": "buy-1",
  "order_batch_id": "default",
  "trade_date": "2026-05-06",
  "symbol": "600000.SH",
  "side": 1,                 // 1=买 2=卖
  "quantity": 1000,
  "price_type": "close",     // 日级：open=当日首分钟 / close=当日末分钟（默认 close）
  "exec_time": "10:30",       // 分钟级：盘中分钟 HH:MM[:SS]，在 at-or-after 该分钟成交（填了则优先于 price_type）
  "limit_price": 10.50        // 可选；限价，挂单合法性对 raw 价判定
}
```

**多策略**（可选，`strategies.json`，每个策略是**独立子账户**，各自初始资金/持仓/净值，最后聚合）：

```json
[
  {"strategy_id": "main", "strategy_type": "order_replay", "initial_cash": 500000,
   "params": {"order_batch_id": "batch-main"}, "symbols": ["600000.SH"]},
  {"strategy_id": "star", "strategy_type": "order_replay", "initial_cash": 500000,
   "params": {"order_batch_id": "batch-star"}, "symbols": ["688981.SH"]}
]
```

把上面的数组放进 `POST /backtests` 请求体的 `strategies` 字段即可。

---

## 4. 输出与字段

回测产出（API 返回 + 落盘 artifacts：`account_summary.json / trades.csv / rejections.csv /
positions.csv / daily_equity.csv / minute_equity.csv`）：

- **成交 trades**：`price/amount/commission/stamp_tax/transfer_fee/cash_after`，外加
  `realized_pnl`（卖出已实现盈亏）、`requested_quantity`（原始下单量，与 `quantity` 实际成交量不等即**部分成交**）。
- **拒单 rejections**：`reason`（英文码，见 §6）。
- **持仓 positions**：`quantity/available_quantity（可卖，T+1）/cost_basis/last_price/market_value/unrealized_pnl`。
- **日净值 daily**：每个交易日 `cash/market_value/total_value/daily_pnl/total_return/drawdown`（按交易日历补齐，停牌日 forward-fill）。
- **逐分钟净值**：`GET /backtests/{id}/minutes`（分页 limit/offset，总数在 `X-Total-Count`）+ 产物 `minute_equity.csv`（不进 summary/SQLite，规避膨胀）。
- **汇总 summary**：`cash/market_value/total_value/total_return/max_drawdown/realized_pnl` + 各策略明细。

---

## 5. 与券商对账单对照（验真实场景）

回测走 **qfq 前复权、不建模现金分红**，与券商真实账本按**容差**对照即可：

```bash
python scripts/reconcile_statement.py \
    --summary account_summary.json --statement 对账单.csv \
    --events-dir "$VORTEX_WORKSPACE/data/events" --tolerance 0.005
```

按 `(date, symbol, side)` 聚合比较数量/成交额/费用；窗口内**除权**的标的会被标注为**预期 qfq 分红差**（与真 bug 区分）。对账单 CSV 列：`date,symbol,side,quantity,price`（可含 `amount/commission/stamp_tax/transfer_fee/request_id`）。

---

## 6. A 股口径（撮合规则）

- **统一分钟数据、前复权 qfq**；订单可**日级**（`price_type` open/close → 当日首/末分钟）或**分钟级**（`exec_time` 指定盘中分钟 → at-or-after 该分钟成交）。仍**非**日内逐 tick 挂单等待（限价在成交分钟判定）。
- **T+1**：当日买入次日才可卖。
- **涨跌停**：以数据为准，涨停不可买 / 跌停不可卖。
- **手数**：主板 100、科创板 ≥200、北交所规则；不足整手向下取整。
- **量能上限**：成交量受当日量限制 → 部分成交。
- **费用**：佣金 0.03%（最低 5 元）、印花税 0.05%（仅卖出）、过户费 0.001%；可在 `POST /backtests` 的 `execution` 里按回测覆盖（含 `slippage_bps`）。

---

## 7. 排错

| 现象 | 原因 |
|---|---|
| 作业 `failed: minute_data_missing` | 没设 `VORTEX_WORKSPACE`，或订单日期/区间落在数据窗口外 |
| 拒单 `no_market_data` | 该标的当日无行情（停牌/非交易日/越界） |
| 拒单 `t_plus_1_not_sellable` | 当日买入当日卖 |
| 拒单 `limit_up_buy_blocked` / `limit_down_sell_blocked` | 涨停买 / 跌停卖 |
| 拒单 `invalid_lot_size` | 手数不合规（如科创板 < 200） |
| 拒单 `insufficient_cash` / `insufficient_position` | 现金不足（已含滑点）/ 持仓不足 |
| 拒单 `volume_cap_below_lot` | 当日量能不足一手 |
| 写接口 403 | 绑了非回环 host 但没配 `VORTEX_BACKTEST_TOKEN` |

更多环境变量与端点见 [usage-and-api.md](usage-and-api.md)。
