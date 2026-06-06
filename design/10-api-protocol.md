---
title: HTTP 协议与命令行参考
created: 2026-06-06
status: reference
---

# HTTP 协议与命令行参考

服务是 REST/JSON over HTTP，默认 `127.0.0.1:8765`。命令行 `vortex-backtest` 既能起服务（`serve`），也能作为协议客户端操作服务。

## 异步作业生命周期（ADR-3，重要）

`POST /backtests` **不再同步返回结果**，而是入队：

```
POST /backtests            -> 202 { job_id, status: "queued" }
   后台 worker:  queued -> running -> completed | failed
   (服务重启时残留 running 自动重排回 queued = interrupted 恢复)
GET  /backtests/{job_id}   -> 轮询 status / progress 直到终态
   终态 ∈ { completed, failed, cancelled, interrupted }
GET  /backtests/{job_id}/summary | /daily | /trades | /rejections   -> 取报告
```

不支持的参数（非 `1min` / 非 `qfq`）在 `POST` 时**同步** `400 {"error": ...}`，不入队。

## 端点一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/accounts` | 建账户 `{account_id, initial_cash, name?}` → 201 |
| GET | `/accounts` · `/accounts/{id}` | 列/查账户 |
| POST | `/accounts/{id}/orders` | 下单（幂等键 account+batch+request_id）→ 201 |
| GET | `/accounts/{id}/orders` | `?order_batch_id=&start_date=&end_date=` |
| POST | `/backtests` | 提交回测 → **202 {job_id, status:queued}** |
| GET | `/backtests` · `/backtests/{job_id}` | 列作业 / 查状态+进度 |
| GET | `/backtests/{job_id}/summary` | 账户汇总 + 各策略 + 日级 daily |
| GET | `/backtests/{job_id}/daily` · `/daily/{date}` | 日净值/持仓/成交/拒单 |
| GET | `/backtests/{job_id}/trades` · `/rejections` | `?trade_date=` 过滤 |
| GET | `/accounts/{id}/summary` · `/positions` | 账户最近一次完成回测 |
| GET | `/symbols/{symbol}` | 代码/板块规则与手数 |

订单字段：`order_batch_id?`（默认 `default`）、`request_id`、`trade_date`(YYYY-MM-DD)、`symbol`(如 `000001.SZ`)、`side`(1=买/2=卖)、`quantity`、`price_type?`、`limit_price?`(真实价)。

**报告口径 = 日级**：每日净值 + 当日成交/拒单/持仓 + 摘要指标（收益、最大回撤等）。已移除分钟级输出（无 `/minutes`、无 `minute_equity.csv`）。

拒单原因枚举：`suspended` / `zero_volume` / `invalid_price_tick` / `invalid_lot_size` / `limit_up_buy_blocked` / `limit_down_sell_blocked` / `insufficient_cash` / `insufficient_position` / `t_plus_1_not_sellable` / `volume_cap_below_lot`。

## 写接口鉴权（P6）

写接口（建账户 / 下单 / 提交回测）：
- 配了 `VORTEX_BACKTEST_TOKEN` → 请求需带 `Authorization: Bearer <token>` 或 `X-Auth-Token: <token>`，否则 `401`。
- 未配 token → 仅本机回环放行；绑到非回环 host（如 `0.0.0.0`）时写接口直接 `403`（fail-closed，避免裸暴露）。
- 作业失败只回安全错误码；未知异常脱敏为 `internal_error`（完整堆栈只在服务端日志）。

## 可配置撮合参数（`execution`，P6）

`POST /backtests` 可带 `execution`（缺省等于原硬编码值，省略则行为不变）：
`{commission_rate, min_commission, stamp_tax_rate, transfer_fee_rate, max_volume_participation, slippage_bps}`。

## 命令行 `vortex-backtest`

全局 `--base-url`（默认 `$VORTEX_BACKTEST_BASE_URL` 或 `http://127.0.0.1:8765`）。

- `serve [--host --port --reload]` —— 起 HTTP 服务。
- `account create --id --cash [--name]` · `account list` · `account get --id`
- `order add --account --request-id --date --symbol --side --qty [--batch --limit-price]`，或批量 `order add --account --file orders.json`
- `backtest run --account --start --end [--batch --strategies-file] [--wait]` —— 提交；`--wait` 轮询到终态
- `backtest status <job_id>`
- `report <job_id> --what summary|daily|trades|rejections`
- `symbol <symbol>`

### 典型流程

```bash
vortex-backtest serve &                       # 起服务（或用 docker compose up -d）
vortex-backtest account create --id demo --cash 100000
vortex-backtest order add --account demo --request-id buy-1 \
    --date 2026-01-02 --symbol 000001.SZ --side buy --qty 100 --batch b1
vortex-backtest backtest run --account demo --start 2026-01-02 --end 2026-01-05 \
    --batch b1 --wait                          # 提交并轮询到完成
vortex-backtest report <job_id> --what daily   # 取日级报告
```

多策略：把策略列表写进 JSON 文件用 `--strategies-file`，每个策略形如
`{"strategy_id","strategy_type":"order_replay","initial_cash","symbols":["000001.SZ"],"params":{"order_batch_id":"b1"}}`。
