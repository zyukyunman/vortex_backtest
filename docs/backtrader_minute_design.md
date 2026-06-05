# Backtrader + Tushare 分钟级 A 股回测框架设计

## 目标

`vortex_backtest` 固定为独立 HTTP 回测服务：HTTP 层负责账户、订单批次、策略配置和报告查询；回测层使用 Backtrader 作为分钟事件驱动基础，并由服务内 A 股规则层完成账户账本、成交、拒单、净值和 artifact 归一化。

第一阶段只支持 A 股现金账户、`1min` 分钟频率、前复权 `qfq` 单一价格口径、多策略独立账户回测。RQAlpha 和旧 `ashare_replay` 不再作为正式路径，也不做 fallback。

## 数据口径

服务从 `VORTEX_DATA_WORKSPACE` 指向的本地 Tushare workspace 读取 Parquet 数据；未设置时默认 `/Users/zyukyunman/Documents/vortex_workspace`。

核心数据集：

| 数据集 | 用途 | 缺失处理 |
| --- | --- | --- |
| `stk_mins` | 主行情，字段为 `symbol/date/trade_time/minute/freq/open/high/low/close/volume/amount` | job 失败：`minute_data_missing` |
| `adj_factor` | 生成分钟 qfq OHLC 和 qfq 涨跌停价格 | job 失败：`adjustment_data_missing` |
| `stk_limit` | 每日涨跌停价，以该表为准，不硬编码比例 | job 失败：`market_rules_data_missing` |
| `suspend_d` | 停复牌信息；有 `S` 记录时对应日期拒单 | 缺表按无停牌处理，但 runbook 会提示补齐 |
| `stock_st` | 历史 ST 标识 | 缺表按非 ST 处理 |
| `instruments` | 证券主数据、板块辅助识别 | 缺表时退回代码规则 |
| `calendar` | 交易日排序和 T+1 可卖日期 | 缺表时使用行情日期排序 |

qfq 价格生成规则：同一 symbol 在回测区间内取最后一个 `adj_factor` 作为基准，分钟 raw OHLC 与 `stk_limit.up_limit/down_limit` 统一乘以 `adj_factor / latest_adj_factor`。指标、买卖、成交、限价判断、持仓估值和 NAV 均使用 qfq；第一阶段不做现金分红入账，因此 qfq 账本不是券商真实流水账。

## A 股规则层

订单执行前统一经过规则层：

- T+1：当日买入的数量当日不可卖，下一交易日才进入 sellable。
- 买入手数：普通 A 股 100 股整数倍；科创板最低 200 股，超过 200 后允许 1 股递增；北交所独立函数处理。
- 卖出手数：允许不足 100 股余额一次性卖出；禁止卖空、持仓不足、T+1 不可卖。
- tick：价格必须按 0.01 对齐。
- 涨跌停：以 `stk_limit` 的 qfq 后价格为准；涨停买入、跌停卖出拒单。
- 停牌、无分钟 bar、`volume=0`、现金不足、限价不可成交、手数非法均进入 `rejections`。
- 默认 `max_volume_participation=1.0`，按分钟成交量限制可成交量；不足合法最小成交单位则拒单。

## HTTP 接口

`POST /accounts` 默认 `engine=backtrader`。历史 sqlite 中的 `rqalpha` 和 `ashare_replay` 账户在初始化时迁移为 `backtrader`。

`POST /backtests` 第一阶段只接受：

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
    }
  ]
}
```

如果 `strategies` 为空，服务会用顶层 `order_batch_id` 构造一个 `default` 策略，兼容旧的外部订单回放调用方式。多策略第一阶段采用独立账户模型：每个 strategy 独立 cash、position、trades、rejections、minute equity，最终 summary 再聚合展示。

报告查询接口：

- `GET /backtests/{job_id}/summary`
- `GET /backtests/{job_id}/daily`
- `GET /backtests/{job_id}/minutes`
- `GET /backtests/{job_id}/trades`
- `GET /backtests/{job_id}/rejections`
- `GET /accounts/{account_id}/summary`
- `GET /accounts/{account_id}/positions`

## 部署与验收

安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

启动：

```bash
export VORTEX_DATA_WORKSPACE=/Users/zyukyunman/Documents/vortex_workspace
export VORTEX_BACKTEST_STATE_DIR=/tmp/vortex-backtest-state
export VORTEX_BACKTEST_HOST=127.0.0.1
export VORTEX_BACKTEST_PORT=8765
.venv/bin/vortex-backtest
```

健康检查：

```bash
curl http://127.0.0.1:8765/health
```

如果当前 workspace 没有 `data/stk_mins`，分钟回测会明确失败为 `minute_data_missing`。这不是服务异常，而是数据预检阻止伪分钟回测通过。

