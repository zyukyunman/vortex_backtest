---
title: vortex_backtest 使用指南
created: 2026-06-06
status: guide
---

# vortex_backtest 使用指南

本指南面向**调用方**：如何起服务、准备数据、按协议提交订单与回测、取回日级报告。设计背景与决策见 `design/`；部署细节见 `docs/operations.md`；协议速查见 `design/10-api-protocol.md`；已知问题见 `docs/code-review-findings.md`。

## 1. 它是什么

`vortex_backtest` 是一个独立的 HTTP 服务，做**账户级 A 股订单回放回测**：你把「账户 + 一批外部订单 + 策略配置」交给它，它基于本地 Tushare 分钟数据，按 A 股规则（T+1、涨跌停、手数、停牌、费用）逐笔撮合，产出成交、拒单、持仓、日级净值与汇总报告。

当前阶段固定口径：**仅 A 股现金账户、`1min` 分钟回测、前复权 `qfq` 单一口径、`replay` 引擎**。多策略采用「独立账户」模型——每个策略独立初始资金/持仓/净值，最终汇总聚合展示。

整体调用链是异步的：

```text
建账户 → 下单(可多批) → 提交回测(返回 202 + job_id) → 轮询作业到 completed → 取日级报告
```

## 2. 快速开始（本地 5 分钟跑通）

需要 Python 3.12 / 3.13（代码用了 3.11+ 的 `enum.StrEnum`，3.9/3.10 会报 `cannot import name 'StrEnum'`）。

```bash
cd /Users/zyukyunman/Documents/vortex/vortex_backtest
/opt/homebrew/bin/python3.13 -m venv .venv          # 或 python3.12
.venv/bin/python -m pip install -e '.[dev]'

export VORTEX_DATA_WORKSPACE=/path/to/vortex_workspace   # 含 data/stk_mins 的目录
.venv/bin/vortex-backtest serve --port 8765 &            # 起服务
curl http://127.0.0.1:8765/health                        # {"status":"ok"}
```

一条龙（用命令行客户端，`--wait` 已封装“提交+轮询”）：

```bash
.venv/bin/vortex-backtest account create --id demo --cash 100000
.venv/bin/vortex-backtest order add --account demo --request-id buy-1 \
    --date 2026-01-02 --symbol 000001.SZ --side buy --qty 100 --batch b1
.venv/bin/vortex-backtest backtest run --account demo \
    --start 2026-01-02 --end 2026-01-05 --batch b1 --wait
.venv/bin/vortex-backtest report <job_id> --what daily
```

## 3. 部署与环境变量

两种跑法：本地 venv（开发/测试）与 Docker（部署）。Docker 一键：

```bash
cp .env.example .env          # 改 VORTEX_BACKTEST_WORKSPACE 指向数据目录
docker compose up -d --build
curl http://127.0.0.1:8765/health
```

关键环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `VORTEX_DATA_WORKSPACE` | 数据根目录（其下需有 `data/stk_mins` 等） | `/Users/zyukyunman/Documents/vortex_workspace` |
| `VORTEX_BACKTEST_STATE_DIR` | 账户/订单/作业/报告的状态目录（SQLite + 报告文件） | `./.vortex_backtest` |
| `VORTEX_BACKTEST_HOST` | 服务绑定地址 | `127.0.0.1` |
| `VORTEX_BACKTEST_PORT` | 服务端口 | `8765` |
| `VORTEX_BACKTEST_TOKEN` | 写接口鉴权 token（见 §5） | 空 |
| `VORTEX_BACKTEST_BASE_URL` | **命令行客户端**连接的服务地址 | `http://127.0.0.1:8765` |

默认只绑回环（仅本机可访问）。要对外暴露，务必同时配置 `VORTEX_BACKTEST_TOKEN`，并通过环境变量（而非 `serve --host` 旗标）设置 `VORTEX_BACKTEST_HOST=0.0.0.0`（原因见 `code-review-findings.md` #4）。

## 4. 数据准备

服务启动不依赖数据，但分钟回测会预检以下数据集（缺关键表时作业明确失败为 `*_data_missing`，不会伪装成功）：

| 数据集 | 用途 | 缺失 |
|---|---|---|
| `data/stk_mins` | 1min 主行情 | `minute_data_missing` |
| `data/adj_factor` | 生成 qfq 价 | `adjustment_data_missing` |
| `data/stk_limit` | 涨跌停价 | `market_rules_data_missing` |
| `data/suspend_d` | 停复牌 | 缺表按“无停牌” |
| `data/stock_st` | 历史 ST | 缺表按“非 ST” |
| `data/instruments` | 标的主数据 | 缺表退回代码规则 |
| `data/calendar` | 交易日排序 | 缺表用行情日期 |

qfq 基准锚定**该标的全历史最新**复权因子，绝对价位不随回测窗口漂移。撮合与估值用 qfq 价；而 tick 对齐、用户 `limit_price`、涨跌停判定一律对**真实价（raw）**进行。

## 5. 写接口鉴权

写接口 = 建账户 / 下单 / 提交回测。读接口不需要 token。

- 配了 `VORTEX_BACKTEST_TOKEN`：请求需带 `Authorization: Bearer <token>` 或 `X-Auth-Token: <token>`，否则 `401`。
- 未配 token：仅本机回环放行；绑到非回环 host 时写接口直接 `403`（fail-closed）。

```bash
curl -X POST http://127.0.0.1:8765/accounts \
  -H 'Authorization: Bearer s3cret' -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","initial_cash":100000}'
```

## 6. 核心概念

**账户**：现金账户，建时定 `initial_cash` 与 `engine`（默认 `replay`）。

**订单批次 `order_batch_id`**：同一账户可保留多批订单。订单幂等键是 `account_id + order_batch_id + request_id`——重复提交同键 → `409`。多策略时每个策略用 `params.order_batch_id` 选自己的批次。

**异步作业**：`POST /backtests` 不同步返回结果，而是入队返回 `202 + job_id`；后台 worker 跑 `queued → running → completed|failed`（服务重启时残留 `running` 自动重排回 `queued`）。

**策略 = 独立账户**：多策略各自独立初始资金/持仓/成交/拒单/净值，summary 里既给账户聚合也给逐策略明细。

**口径**：`1min` 频率、`qfq` 复权、T+1（当日买入次日才可卖）。不支持的参数在 `POST` 时**同步 `400`**，不入队。

## 7. 完整 API 流程（curl）

### 7.1 建账户

```bash
curl -X POST http://127.0.0.1:8765/accounts \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","initial_cash":100000}'        # → 201
```

### 7.2 下单

`side`：`1`=买、`2`=卖（必须是数值，布尔/字符串/越界 → `422`）。`limit_price` 是**真实价**，可选；`price_type` 可选 `open`/`close`（缺省用回测的 `default_price_type`，默认 `close`）。

```bash
curl -X POST http://127.0.0.1:8765/accounts/demo/orders \
  -H 'Content-Type: application/json' \
  -d '{"order_batch_id":"b1","request_id":"buy-1","trade_date":"2026-01-02",
       "symbol":"000001.SZ","side":1,"quantity":100,"limit_price":10.50}'   # → 201
```

查订单（支持 `?order_batch_id=&start_date=&end_date=` 过滤）：

```bash
curl 'http://127.0.0.1:8765/accounts/demo/orders?order_batch_id=b1'
```

### 7.3 提交回测（异步）

```bash
curl -X POST http://127.0.0.1:8765/backtests \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","frequency":"1min","price_adjustment":"qfq",
       "default_price_type":"close","start_date":"2026-01-02","end_date":"2026-01-05",
       "order_batch_id":"b1"}'                              # → 202 {job_id,status:"queued"}
```

省略 `start_date/end_date` 时按订单日期自动取范围。

### 7.4 轮询作业到完成

```bash
curl http://127.0.0.1:8765/backtests/<job_id>               # status: queued→running→completed
```

终态 ∈ `{completed, failed, cancelled, interrupted}`。`failed` 时 `summary.error` 给安全错误码（见 §11）。

### 7.5 取报告（日级）

```bash
curl http://127.0.0.1:8765/backtests/<job_id>/summary             # 账户汇总 + 各策略 + 日级
curl http://127.0.0.1:8765/backtests/<job_id>/daily               # 每日净值/持仓/成交/拒单
curl http://127.0.0.1:8765/backtests/<job_id>/daily/2026-01-02    # 指定交易日
curl 'http://127.0.0.1:8765/backtests/<job_id>/trades?trade_date=2026-01-02'
curl 'http://127.0.0.1:8765/backtests/<job_id>/rejections?trade_date=2026-01-02'
```

也可查账户最近一次完成的回测：

```bash
curl http://127.0.0.1:8765/accounts/demo/summary
curl http://127.0.0.1:8765/accounts/demo/positions
```

### 7.6 端点一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/accounts` | 建账户 → 201 |
| GET | `/accounts` · `/accounts/{id}` | 列/查账户 |
| POST | `/accounts/{id}/orders` | 下单（幂等键 account+batch+request_id）→ 201 |
| GET | `/accounts/{id}/orders` | `?order_batch_id=&start_date=&end_date=` |
| POST | `/backtests` | 提交回测 → 202 |
| GET | `/backtests` · `/backtests/{job_id}` | 列作业 / 查状态+进度 |
| GET | `/backtests/{job_id}/summary` | 账户汇总 |
| GET | `/backtests/{job_id}/daily` · `/daily/{date}` | 日级 |
| GET | `/backtests/{job_id}/trades` · `/rejections` | `?trade_date=` 过滤 |
| GET | `/accounts/{id}/summary` · `/positions` | 账户最近一次完成回测 |
| GET | `/symbols/{symbol}` | 代码/板块规则与手数 |

## 8. 命令行客户端

`vortex-backtest serve` 起服务；其余子命令是 HTTP 协议客户端（用 `--base-url` 或 `VORTEX_BACKTEST_BASE_URL` 指向服务）。

```bash
vortex-backtest account create --id demo --cash 100000
vortex-backtest order add --account demo --request-id buy-1 \
    --date 2026-01-02 --symbol 000001.SZ --side buy --qty 100 --batch b1
vortex-backtest order add --account demo --file orders.json          # 批量(JSON 数组)
vortex-backtest backtest run --account demo --start 2026-01-02 --end 2026-01-05 \
    --batch b1 --wait                                                # 提交并轮询
vortex-backtest backtest run --account demo --start 2026-01-02 --end 2026-01-05 \
    --strategies-file strategies.json --wait                         # 多策略
vortex-backtest report <job_id> --what summary|daily|trades|rejections
vortex-backtest symbol 000001.SZ
```

## 9. 多策略回测

把策略列表放进 `strategies`，每个策略独立 `initial_cash`、`symbols`、批次。

```json
{
  "account_id": "demo",
  "frequency": "1min",
  "price_adjustment": "qfq",
  "start_date": "2026-01-02",
  "end_date": "2026-01-05",
  "strategies": [
    {"strategy_id": "main", "strategy_type": "order_replay",
     "initial_cash": 100000, "symbols": ["000001.SZ"],
     "params": {"order_batch_id": "b-main"}},
    {"strategy_id": "star", "strategy_type": "order_replay",
     "initial_cash": 100000, "symbols": ["688981.SH"],
     "params": {"order_batch_id": "b-star"}}
  ]
}
```

> 注意：当某策略标的在某交易日停牌/无数据时，组合**日级**聚合曲线目前会失真（见 `code-review-findings.md` #2）；单策略明细不受影响。

## 10. 可配置撮合参数 `execution`

`POST /backtests` 可带 `execution`，缺省等于原硬编码值（省略则行为不变）：

| 字段 | 含义 | 默认 |
|---|---|---|
| `commission_rate` | 佣金费率 | `0.0003` |
| `min_commission` | 佣金下限（元） | `5.0` |
| `stamp_tax_rate` | 印花税率（仅卖出） | `0.0005` |
| `transfer_fee_rate` | 过户费率（双边） | `0.00001` |
| `max_volume_participation` | 单笔成交占当日量上限（0~1） | `1.0` |
| `slippage_bps` | 滑点（基点，买入抬价/卖出压价） | `0.0` |

> 注意：开启 `slippage_bps` 时，临界满仓买单的现金校验存在击穿风险（见 `code-review-findings.md` #1）。

## 11. 报告口径与错误码

**日级报告**：每个交易日给净值（`cash` / `market_value` / `total_value`）、`daily_pnl`、`total_return`、`drawdown`，以及当日成交/拒单/持仓。汇总另给 `max_drawdown` 与逐策略明细；产出文件落在 `report_dir`（`account_summary.json` / `daily_equity.csv` / `trades.csv` / `positions.csv` / `rejections.csv`）。已移除分钟级输出。

**拒单原因**：`suspended`（停牌）、`zero_volume`（当日零量）、`invalid_price_tick`（价格非 0.01 对齐）、`invalid_lot_size`（手数不合规）、`limit_up_buy_blocked`（涨停不可买）、`limit_down_sell_blocked`（跌停不可卖）、`insufficient_cash`（现金不足）、`insufficient_position`（持仓不足）、`t_plus_1_not_sellable`（T+1 当日不可卖）、`volume_cap_below_lot`（量上限不足一手）。

**作业失败错误码（安全可回传）**：`minute_data_missing` / `adjustment_data_missing` / `market_rules_data_missing` / `no_symbols` / `start_end_required` / `unsupported_frequency` / `unsupported_price_adjustment` / `unsupported_order_price_adjustment` / `unsupported_strategy_type` / `missing_request_payload`；其余未知异常统一脱敏为 `internal_error`（完整堆栈只进服务端日志）。

**A 股手数规则**：主板买入 ≥100 且整百；科创板（`688*`）买入 ≥200（允许非整百）；卖出整百，或一次性清掉不足整百的零股尾。

## 12. 常见问题

**为什么 `POST /backtests` 没直接返回结果？** 协议是异步的——返回 `202 + job_id`，需轮询 `GET /backtests/{job_id}` 到 `completed` 再取报告（CLI `--wait` 已封装）。

**为什么我的卖单被 `t_plus_1_not_sellable` 拒？** A 股 T+1：当日买入次日才可卖。

**限价怎么比？** `limit_price` 按**真实价**比较：买单要求成交真实价 ≤ 限价，卖单要求 ≥ 限价；不满足 → `limit_price_not_marketable`。

**成交价用哪根 bar？** 由 `price_type`/`default_price_type` 决定：`open` 用当日首分钟、`close` 用当日末分钟；成交价是该 bar 的 **qfq** 价。

**回测 `minute_data_missing`？** `VORTEX_DATA_WORKSPACE` 下缺 `data/stk_mins`，用 `vortex_data` 先补分钟数据。

## 13. 已知限制

详见 `docs/code-review-findings.md`：滑点临界买单现金校验击穿（#1）、多策略日级聚合在停牌/日期缺口处失真（#2，且与 qlib 引擎不一致）、成交量上限部分成交剩余量无记录（#3）、`serve --host` 旗标绕过鉴权（#4）等。
