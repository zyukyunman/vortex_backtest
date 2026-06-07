---
title: HTTP 接口协议（API Contract）
created: 2026-06-06
updated: 2026-06-07
status: reference
---

# vortex_backtest HTTP 接口协议

> 本文是 **接口定稿（契约）**：定义 vortex_backtest 对外暴露的全部 HTTP 端点、
> 请求/响应结构、回测生命周期与错误约定。实现以本文为准；偏离即视为 bug。
> 上手教程见 [docs/usage-and-api.md](../docs/usage-and-api.md)，端到端示例脚本见
> [scripts/backtest_roundtrip.sh](../scripts/backtest_roundtrip.sh)。

## 0. 设计原则

1. **纯 HTTP/JSON,无 CLI 协议客户端。** 所有业务操作——建账户、下单、提交回测、
   轮询状态、取报告——**只通过 HTTP**。命令行仅保留 `vortex-backtest serve`(把服务拉起来),
   它是容器/k8s 的启动契约(`vortexctl backtest` 与 `deploy/run.sh` 调它),不承担任何业务调用。
2. **一进程一容器**(见 vortex_common `ADR-001`)。规范端口 **8767**(避开 vortex_data 的 8765)。
3. **只读消费上游数据。** 行情来自 vortex_data 导出的 workspace(分钟 parquet),回测进程只读不写。
4. **回测异步。** `POST /backtests` 入队即返回 `202 + job_id`,后台 worker 执行;客户端轮询到终态再取报告。
5. **写接口 fail-closed。** 见 §7 鉴权。

**Base URL**:容器内 `http://0.0.0.0:8767`;本机默认 `http://127.0.0.1:8767`。
所有请求体与响应体均为 `application/json`(报告内的 CSV 产物除外,见 §6)。
在线可交互文档:`GET /docs`(Swagger UI)、`GET /openapi.json`(机器可读 schema)。

---

## 1. 资源模型

| 资源 | 说明 | 标识 | 生命周期 |
|---|---|---|---|
| **account** 账户 | 一笔初始资金 + 引擎(`replay`)。可复用,可承载多次回测 | `account_id`(客户端指定) | 持久 |
| **order** 订单 | 外部下达的买卖指令,挂在某 `order_batch_id` 批次下 | `account_id`+`order_batch_id`+`request_id`(幂等键) | 持久 |
| **backtest job** 回测作业 | 一次回测 = 对账户某批次订单的一次回放 | `job_id`(服务端 UUID) | queued→running→终态 |
| **report** 报告 | 作业完成后的日级净值/成交/拒单/持仓/汇总 | 随 `job_id` | 随作业 |
| **strategy** 策略 | 从历次作业按 `strategy_id` **派生的只读聚合**(非写模型) | `strategy_id` | 派生 |
| **symbol / benchmark** | 代码↔板块/手数/规则;指数基准目录 | `symbol` | 静态 |

> 「一次回测」的语义即 **作业(job)**:账户长期存在,每提交一次 `POST /backtests` 就开一个作业、
> 跑完即关闭并产出该次报告。无需独立的 open/close 会话端点——**提交即开始,完成即关闭**。

---

## 2. 回测开闭环(核心调用时序)

这是用户视角的「开始 → 提交买卖 → 结束 → 关闭 → 报告」全流程,全部 HTTP。
可执行版本见 `scripts/backtest_roundtrip.sh`。

```
① 建账户            POST /accounts                         → 201 {account_id, ...}
② 提交买卖(可多次)  POST /accounts/{id}/orders             → 201 {order ...}
③ 结束/提交回测      POST /backtests                        → 202 {job_id, status:"queued"}
④ 关闭(轮询到终态)  GET  /backtests/{job_id}  反复轮询      → status ∈ 终态
⑤ 输出报告          GET  /backtests/{job_id}/summary        → 账户级汇总 + 各策略 + 日级
                    GET  /backtests/{job_id}/daily|trades|rejections
```

最小 curl 串联(本机回环,无需 token):

```bash
B=http://127.0.0.1:8767

# ① 建账户
curl -s -XPOST $B/accounts -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","initial_cash":1000000,"name":"演示"}'

# ② 提交买卖(side: 1=买 2=卖)
curl -s -XPOST $B/accounts/demo/orders -H 'Content-Type: application/json' \
  -d '{"order_batch_id":"b1","request_id":"buy-1","trade_date":"2026-05-06","symbol":"600000.SH","side":1,"quantity":1000}'
curl -s -XPOST $B/accounts/demo/orders -H 'Content-Type: application/json' \
  -d '{"order_batch_id":"b1","request_id":"sell-1","trade_date":"2026-05-13","symbol":"600000.SH","side":2,"quantity":1000}'

# ③ 结束回测 = 提交作业(异步,拿 job_id)
JOB=$(curl -s -XPOST $B/backtests -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","order_batch_id":"b1","start_date":"2026-05-06","end_date":"2026-06-05"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

# ④ 关闭 = 轮询到终态
until curl -s $B/backtests/$JOB | python3 -c 'import sys,json;s=json.load(sys.stdin)["status"];print(s);exit(0 if s in {"completed","failed","cancelled","interrupted"} else 1)'; do sleep 1; done

# ⑤ 输出报告
curl -s $B/backtests/$JOB/summary
```

---

## 3. 回测作业生命周期(状态机)

```
            POST /backtests
                  │ (入队)
                  ▼
              ┌────────┐  worker 领取   ┌─────────┐  成功   ┌────────────┐
   cancel ◀── │ queued │ ─────────────▶ │ running │ ──────▶ │ completed  │ (有 summary)
   (→cancelled)└────────┘                └─────────┘         └────────────┘
                                              │ 异常 → failed(带安全错误码)
                                              │ 服务重启时残留 running
                                              ▼  自动重排回 queued = interrupted 恢复
```

| status | 含义 | 是否终态 | 可取报告 |
|---|---|---|---|
| `queued` | 已入队待执行 | 否 | 否 |
| `running` | worker 执行中 | 否 | 否 |
| `completed` | 成功完成 | **是** | **是** |
| `failed` | 失败(`error` 为安全错误码,见 §6) | **是** | 否 |
| `cancelled` | 排队中被取消 | **是** | 否 |
| `interrupted` | 服务重启时残留、已重排恢复的瞬时态 | 是 | 否 |

终态集合 = `{completed, failed, cancelled, interrupted}`。客户端轮询 `GET /backtests/{job_id}` 直到落入该集合。
取消:`POST /backtests/{job_id}/cancel` 仅能取消 `queued`(运行中无法安全中断 → 409;已终态 → 409)。

---

## 4. 端点总览

### 4.1 写接口(需鉴权,见 §7)

| 方法 | 路径 | 成功码 | 说明 |
|---|---|---|---|
| POST | `/accounts` | 201 | 建账户。`account_id` 重复 → 409 |
| POST | `/accounts/{account_id}/orders` | 201 | 下单。幂等键 `account+batch+request_id` 重复 → 409;账户不存在 → 404 |
| POST | `/backtests` | **202** | 提交回测,入队返回 `job_id`。不支持参数 → 400(不入队) |
| POST | `/backtests/{job_id}/cancel` | 200 | 取消排队中作业;运行中/已终态 → 409 |
| PUT | `/strategies/{strategy_id}/meta?account_id=` | 200 | 设收藏/置顶/标签 `{favorite?,pinned?,tags?}` |

### 4.2 只读 / 报告接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 `{"status":"ok"}` |
| GET | `/accounts` · `/accounts/{id}` | 账户列表 / 详情 |
| GET | `/accounts/{id}/orders?order_batch_id=&start_date=&end_date=` | 账户订单(可过滤) |
| GET | `/accounts/{id}/summary` · `/accounts/{id}/positions` | 账户**最近一次完成**回测的汇总 / 持仓 |
| GET | `/symbols/{symbol}` | 代码 ↔ 板块 / 手数 / 各市场代码 |
| GET | `/backtests?account_id=&status=` | 作业列表(含派生 `strategy_ids`) |
| GET | `/backtests/{job_id}` | 作业状态 + 进度 `progress` |
| GET | `/backtests/{job_id}/summary` | 账户级汇总 + 各策略 + 日级 `daily` |
| GET | `/backtests/{job_id}/daily` · `/daily/{trade_date}` | 日级净值序列 / 某日快照 |
| GET | `/backtests/{job_id}/minutes?limit=&offset=` | 逐分钟净值(组合 `timestamp/cash/market_value/total_value`;来自 `minute_equity.csv`,响应头 `X-Total-Count`) |
| GET | `/backtests/{job_id}/trades?trade_date=&symbol=&strategy_id=&limit=&offset=` | 成交(分页,响应头 `X-Total-Count`) |
| GET | `/backtests/{job_id}/rejections?trade_date=&reason=&strategy_id=&limit=&offset=` | 拒单(同上) |
| GET | `/backtests/{job_id}/rejections/summary` | 拒单按原因计数 `{counts, total}` |
| GET | `/backtests/{job_id}/equity?strategy_id=&benchmark=&rebase=` | 净值曲线(起点 1.0)+ 回撤 + 可选基准 |
| GET | `/backtests/{job_id}/metrics?strategy_id=&benchmark=` | 绩效指标(<60 交易日 `low_confidence=true`) |
| GET | `/benchmarks` | 可选基准目录(000300.SH 等) |

### 4.3 策略中心(按 `strategy_id` 派生的只读聚合)

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/strategies?account_id=&best_metric=` | 策略列表:n_runs / 最新 / 最优 / 标的 / 收藏 |
| GET | `/strategies/{id}?account_id=&benchmark=` | 策略详情:净值 + 当前持仓 + 成交 + `latest_job_id` |
| GET | `/leaderboard?account_id=&metric=&scope=&top=` | 排行榜(多指标;`metric`+`scope`=best/latest 决定排名) |
| GET | `/strategies/compare?ids=a,b&account_id=&benchmark=` | A/B 对比:净值叠加 + 指标并排 |

### 4.4 托管页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 302 重定向到 `/ui/` |
| GET | `/ui/` | 只读 SPA 看板(策略中心 / 排行榜 / 全部回测) |
| GET | `/guide` | 技术文档站(系统设计 / HTTP 接口协议 / 环境部署,精修 HTML) |
| GET | `/docs` · `/redoc` | 交互式 API(Swagger / ReDoc) |

---

## 5. 请求与响应 Schema(关键)

### 5.1 建账户 `POST /accounts`

```json
{ "account_id": "demo", "initial_cash": 1000000, "engine": "replay", "name": "演示" }
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `account_id` | string | 是 | 1–64 字符 |
| `initial_cash` | number | 是 | > 0 |
| `engine` | enum | 否 | 仅 `replay`(历史值 backtrader/qlib/rqalpha/ashare_replay 自动归一为 replay) |
| `name` | string | 否 | ≤128 |

### 5.2 下单 `POST /accounts/{id}/orders`

```json
{ "order_batch_id": "b1", "request_id": "buy-1", "trade_date": "2026-05-06",
  "symbol": "600000.SH", "side": 1, "quantity": 1000,
  "price_type": "close", "exec_time": "10:30", "limit_price": 10.50 }
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `order_batch_id` | string | 否 | 默认 `default`;同批次构成幂等键的一部分 |
| `request_id` | string | 是 | 同账户+批次内唯一(重复 → 409) |
| `trade_date` | date | 是 | `YYYY-MM-DD` |
| `symbol` | string | 是 | 如 `600000.SH` / `000001.SZ`(自动规整大小写) |
| `side` | int | 是 | **1=买,2=卖**(必须数字,布尔/字符串报 422) |
| `quantity` | int | 是 | > 0 |
| `price_type` | enum | 否 | `open` / `close`(日级:当日首/末分钟) |
| `exec_time` | string | 否 | `HH:MM[:SS]` 盘中择时(分钟级:当日 at-or-after 该分钟成交,优先于 `price_type`) |
| `limit_price` | number | 否 | > 0(真实价;不填按 `default_price_type` 取价) |
| `comment` | string | 否 | ≤512 |

### 5.3 提交回测 `POST /backtests`

```json
{
  "account_id": "demo",
  "order_batch_id": "b1",
  "start_date": "2026-05-06",
  "end_date": "2026-06-05",
  "frequency": "1min",
  "price_adjustment": "qfq",
  "default_price_type": "close",
  "strategies": [
    {"strategy_id":"main","symbols":["600000.SH"],"params":{"order_batch_id":"b1"}}
  ],
  "execution": {"commission_rate":0.0003,"min_commission":5,"stamp_tax_rate":0.0005,
                "transfer_fee_rate":0.00001,"max_volume_participation":1.0,"slippage_bps":0}
}
```

| 字段 | 类型 | 默认 | 同步校验 |
|---|---|---|---|
| `account_id` | string | — | 不存在 → 404 |
| `order_batch_id` | string | `default` | 单策略时按此批次回放 |
| `market_data_set_id` | string | `default-qfq` | — |
| `frequency` | string | `1min` | ≠`1min` → **400 unsupported_frequency** |
| `price_adjustment` | enum | `qfq` | ≠`qfq` → **400 unsupported_price_adjustment** |
| `order_price_adjustment` | enum | =`price_adjustment` | ≠`qfq` → **400 unsupported_order_price_adjustment** |
| `default_price_type` | enum | `close` | `open`/`close` |
| `start_date`/`end_date` | date | null | 缺失会导致作业 `failed: start_end_required` |
| `strategies[]` | array | `[]` | 空 = 按 `order_batch_id` 单策略回放;非空 = 每策略独立子账户 |
| `execution` | object | 缺省费率/滑点 | 见上;省略则行为不变 |

> 不支持的参数在 `POST` 时**同步 400,不入队**;其余执行期错误体现在作业 `status:failed` 的 `error`。

### 5.4 作业对象 `BacktestJobOut`(GET /backtests/{job_id})

```json
{ "job_id":"…","account_id":"demo","order_batch_id":"b1","status":"completed",
  "start_date":"2026-05-06","end_date":"2026-06-05","created_at":"…","completed_at":"…",
  "progress":{…},"summary":{…},"strategy_ids":["main"] }
```

`status` 见 §3;`summary` 仅在 `completed` 非空;`progress` 给运行进度。

### 5.5 汇总对象 `AccountSummaryOut`(GET /backtests/{job_id}/summary)

顶层:`cash / market_value / total_value / total_return / max_drawdown / realized_pnl`,
加 `positions[] / trades[] / rejections[] / daily[] / strategies[] / artifacts{}`。
- `daily[]`:每个交易日 `{trade_date, cash, market_value, total_value, daily_pnl, total_return, drawdown, positions, trades, rejections}`。
- `trades[]`:含 `realized_pnl`(已实现盈亏)、`requested_quantity`(原始下单量,识别量能上限导致的部分成交)、`commission/stamp_tax/transfer_fee/cash_after`。
- **逐分钟净值**:不进 summary JSON,经 `GET /backtests/{job_id}/minutes` 取(组合 `timestamp/cash/market_value/total_value`),落盘 `minute_equity.csv`(规避膨胀)。
- `artifacts{}`:落盘 CSV/JSON 的相对路径(`account_summary.json / trades.csv / rejections.csv / positions.csv / daily_equity.csv / minute_equity.csv`)。

---

## 6. 错误与拒单约定

**HTTP 错误码**:`400` 参数不支持 · `401` 未授权 · `403` 写接口被禁(非回环且无 token) · `404` 资源不存在 · `409` 冲突(重复/不可取消) · `422` 字段校验失败。
错误体形如 `{"error":"<code>","hint":"…"}` 或 `{"detail":"…"}`。

**作业安全错误码**(`failed` 时回传,完整堆栈只进服务端日志):
`minute_data_missing` · `adjustment_data_missing` · `market_rules_data_missing` · `no_symbols` ·
`start_end_required` · `unsupported_frequency` · `unsupported_price_adjustment` ·
`unsupported_order_price_adjustment` · `unsupported_strategy_type` · `missing_request_payload`。
其余未知异常一律脱敏为 `internal_error`。

**拒单原因枚举**(`rejections[].reason`,看板做中文化展示):
`suspended` · `zero_volume` · `invalid_price_tick` · `invalid_lot_size` · `limit_up_buy_blocked` ·
`limit_down_sell_blocked` · `insufficient_cash` · `insufficient_position` · `t_plus_1_not_sellable` · `volume_cap_below_lot`。

---

## 7. 写接口鉴权

写接口 = 建账户 / 下单 / 提交回测 / 取消 / 设 meta。规则(`require_write_auth`):

- **配了** `VORTEX_BACKTEST_TOKEN`:请求须带 `Authorization: Bearer <token>` 或 `X-Auth-Token: <token>`,否则 `401`。
- **没配** token:仅本机回环(`127.0.0.1/localhost/::1`)放行;绑到非回环 host(如 `0.0.0.0`)时写接口直接 `403`(fail-closed,避免裸暴露)。

只读/报告接口不鉴权。对外暴露务必先配 token,再把绑定地址放开。

---

## 8. 部署与启动契约(呼应 vortex_common)

- **唯一命令行入口**:`vortex-backtest serve [--host --port --reload]`(默认 `127.0.0.1:8767`)。
- **容器内**:`deploy/run.sh` 设好 `VORTEX_BACKTEST_STATE_DIR`/`VORTEX_DATA_WORKSPACE` 后 `exec vortex-backtest serve`;
  组合镜像里由 `vortexctl backtest` 调用它(缺失则回退内置默认),一进程一容器。
- **端口**:容器内 `8767`;`docker compose` 默认只绑宿主回环,对外暴露须先配 token。
- **卷**:`/workspace`(消费 vortex_data 导出,只读) + `/state`(账户/订单/作业/报告)。

环境变量:`VORTEX_BACKTEST_HOST/PORT`、`VORTEX_BACKTEST_STATE_DIR`、`VORTEX_DATA_WORKSPACE`、
`VORTEX_INDEX_DATA_DIR`(指数基准)、`VORTEX_BACKTEST_TOKEN`(写接口鉴权)。
