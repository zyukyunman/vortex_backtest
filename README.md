# vortex_backtest

独立 HTTP 回测/账户回放服务。当前路线固定为：

```text
HTTP 协议层 + 异步作业 + A 股分钟撮合/规则层 + Tushare 本地 Parquet 数据
```

服务不再维护 RQAlpha adapter，也不再维护旧 `ashare_replay` fallback。第一阶段只支持 A 股现金账户、`1min` 分钟回测、前复权 `qfq` 单一口径、多策略独立账户回放。

架构评审 / 引擎选型 / 协议 / 路线图见 `design/01`–`design/10`；部署见 [docs/operations.md](docs/operations.md)；**HTTP 接口协议**（端点 / 生命周期 / schema / 鉴权）见 [design/10](design/10-api-protocol.md)。

## 当前能力

- `POST /accounts` 创建账户，默认 `engine=replay`
- `POST /accounts/{account_id}/orders` 提交外部订单
- `POST /backtests` 提交 qfq 回测（**异步**：返回 `202 + job_id`，轮询 `GET /backtests/{job_id}` 到 `completed`）
- `GET /backtests/{job_id}` 查询作业状态/进度
- `GET /backtests/{job_id}/summary` 查询账户汇总
- `GET /backtests/{job_id}/daily` 查询日级净值、持仓、成交、拒单
- `GET /backtests/{job_id}/trades` 查询成交
- `GET /backtests/{job_id}/rejections` 查询拒单
- `GET /accounts/{account_id}/summary` 查询账户最近一次完成回测
- `GET /accounts/{account_id}/positions` 查询账户最近持仓
- `GET /symbols/{symbol}` 查询 Tushare/MiniQMT/Vortex 统一代码和板块规则

订单唯一性由 `account_id + order_batch_id + request_id` 保证。同一账户可以保留多批订单；多策略回测时每个 strategy 可通过 `params.order_batch_id` 选择自己的订单批次。

## 数据要求

启动前设置（`$WS` = vortex_data 导出的 workspace 根目录）：

```bash
export VORTEX_WORKSPACE=$WS
```

服务读取以下本地数据集：

| 数据集 | 用途 | 缺失错误 |
| --- | --- | --- |
| `data/stk_mins` | 1min 主行情 | `minute_data_missing` |
| `data/adj_factor` | 生成 qfq 分钟价格 | `adjustment_data_missing` |
| `data/stk_limit` | 涨跌停价 | `market_rules_data_missing` |
| `data/suspend_d` | 停复牌 | 缺表按无停牌处理 |
| `data/stock_st` | 历史 ST | 缺表按非 ST 处理 |
| `data/instruments` | 标的主数据 | 缺表退回代码规则 |
| `data/calendar` | 交易日排序 | 缺表使用行情日期 |

当前实现强制全链路 `qfq`：指标、买卖、成交、限价、持仓估值和 NAV 都用前复权价格。第一阶段不做现金分红入账；如果需要券商真实流水账，需要先把 `events/dividend` 落盘并进入第二阶段。

## 安装和启动

建议 Python 3.12 或 3.13（需 ≥3.11）：

```bash
cd $REPO            # 本仓根目录
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

启动服务（命令行 `serve` 子命令）：

```bash
export VORTEX_WORKSPACE=$WS                  # vortex_data 导出的 workspace 根
export VORTEX_STATE=$REPO/state              # 账户/作业/报告状态目录
export VORTEX_BACKTEST_HOST=127.0.0.1
export VORTEX_BACKTEST_PORT=8766
.venv/bin/vortex-backtest serve
```

容器部署用 `vortex run up backtest`（端口 8766，宿主机挂载默认 `~/vortex/{workspace,state}`，可用 `VORTEX_*_HOST_ROOT` 覆盖）；全栈用 `vortex run deploy`。端口规范以 vortex_common 的 `config/registry.yml` + ADR-003 为准。

健康检查：

```bash
curl http://127.0.0.1:8766/health
```

如果本地没有 `data/stk_mins`，服务会启动成功，但分钟回测会失败为 `minute_data_missing`。这是预期的数据预检行为，不会伪装成日线回测成功。

## 基本调用

创建账户：

```bash
curl -X POST http://127.0.0.1:8766/accounts \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","initial_cash":100000}'
```

提交订单：

```bash
curl -X POST http://127.0.0.1:8766/accounts/demo/orders \
  -H 'Content-Type: application/json' \
  -d '{
    "order_batch_id":"batch-main",
    "request_id":"buy-001",
    "trade_date":"2026-01-02",
    "symbol":"000001.SZ",
    "side":1,
    "quantity":100,
    "limit_price":10.50
  }'
```

运行单策略回测：

```bash
curl -X POST http://127.0.0.1:8766/backtests \
  -H 'Content-Type: application/json' \
  -d '{
    "account_id":"demo",
    "frequency":"1min",
    "price_adjustment":"qfq",
    "start_date":"2026-01-02",
    "end_date":"2026-01-05",
    "strategies":[
      {
        "strategy_id":"main-replay",
        "strategy_type":"order_replay",
        "initial_cash":100000,
        "symbols":["000001.SZ"],
        "params":{"order_batch_id":"batch-main"}
      }
    ]
  }'
```

回测是**异步**的：`POST /backtests` 返回 `202 + job_id`，先轮询作业到完成，再取**日级**报告（已无分钟级端点）：

```bash
curl http://127.0.0.1:8766/backtests/<job_id>            # 轮询 status 到 completed
curl http://127.0.0.1:8766/backtests/<job_id>/summary
curl http://127.0.0.1:8766/backtests/<job_id>/daily
curl http://127.0.0.1:8766/backtests/<job_id>/trades
curl http://127.0.0.1:8766/backtests/<job_id>/rejections
```

或用开闭环脚本一条命令跑完「建账户 → 买卖 → 结束 → 关闭 → 报告」（仅依赖 curl + python3）：

```bash
scripts/backtest_roundtrip.sh --symbol 000001.SZ --start 2026-01-02 --end 2026-01-05
```

HTTP 接口协议完整参考见 [design/10-api-protocol.md](design/10-api-protocol.md)。

## 多策略回测

第一阶段多策略是独立账户模型：每个 strategy 独立初始资金、持仓、成交、拒单和净值，最终 summary 做聚合展示。

```json
{
  "account_id": "demo",
  "frequency": "1min",
  "price_adjustment": "qfq",
  "start_date": "2026-01-02",
  "end_date": "2026-01-05",
  "strategies": [
    {
      "strategy_id": "main-replay",
      "strategy_type": "order_replay",
      "initial_cash": 100000,
      "symbols": ["000001.SZ"],
      "params": {"order_batch_id": "batch-main"}
    },
    {
      "strategy_id": "star-replay",
      "strategy_type": "order_replay",
      "initial_cash": 100000,
      "symbols": ["688809.SH"],
      "params": {"order_batch_id": "batch-star"}
    }
  ]
}
```

## 本地样例

样例脚本不导入手工行情，只检查 workspace 分钟数据并调用 HTTP API：

```bash
.venv/bin/python examples/run_30_day_http_sample.py \
  --base-url http://127.0.0.1:8766 \
  --workspace "$VORTEX_WORKSPACE" \
  --symbols 000001.SZ,688809.SH
```

如果 workspace 尚未落盘 `stk_mins`，样例会提前退出并提示先补分钟数据。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q vortex_backtest tests examples
```

