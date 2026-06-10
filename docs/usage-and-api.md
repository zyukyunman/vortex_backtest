# vortex_backtest 使用与接口指南

> 面向「怎么用」的上手文档：启动服务 → 建账户/下单 → 跑回测 → 看结果（开闭环脚本 / REST / 看板）。
> 本文命令均已在本机 `.venv`（Python 3.13）实测通过；示例数据用真实分钟行情 **2026-05-06 ~ 2026-06-05（23 个交易日）**。

---

## 0. 一分钟跑起来

```bash
cd $REPO            # 本仓根目录

# 1) 告诉服务去哪读数据（分钟行情 + 指数基准）
export VORTEX_WORKSPACE=$WS          # vortex_data 导出的 workspace 根
export VORTEX_INDEX_DATA_DIR=$VORTEX_WORKSPACE/data/index_daily

# 2) 起服务（默认 127.0.0.1:8766，自带后台 worker 执行排队作业）
./.venv/bin/python -m vortex_backtest.cli serve --host 127.0.0.1 --port 8766
```

起好后打开三个入口：

| 入口 | 地址 | 用途 |
|---|---|---|
| 看板 | http://127.0.0.1:8766/ | 策略中心 / 排行榜 / 全部回测（可视化） |
| 交互式 API 文档 | http://127.0.0.1:8766/docs | Swagger UI，点点就能试每个接口 |
| 健康检查 | http://127.0.0.1:8766/health | 返回 `{"status":"ok"}` |

> `VORTEX_WORKSPACE` 指向 **workspace 根目录**（服务自动在后面接 `/data`）。漏配会让回测报 `minute_data_missing`。

---

## 1. 数据范围（实测）

| 数据集 | 覆盖区间 | 说明 |
|---|---|---|
| `stk_mins`（分钟行情，回测主用） | **20260506 → 20260605**（23 个交易日） | 按 `year/universe/symbol` 分区，5525 个标的 |
| `adj_factor` / `stk_limit` / `bars` / `suspend_d` | 20260506 → 20260605 | 复权因子 / 涨跌停 / 日线 / 停牌 |
| `index_daily`（基准） | 20260504 → 20260605 | 含 000300.SH 沪深300 等 |

**回测可用窗口 = 2026-05-06 ~ 2026-06-05。** 5 月 1–5 日为假期/无分钟数据，落在窗口外的订单会被按区间过滤掉。需要更早的区间要等 vortex_data 回补分钟历史。

---

## 2. 核心概念

- **账户 account**：一笔初始资金 + 一个引擎（自研 A 股分钟撮合，枚举名 `replay`；本机直接读 `stk_mins` 原始分钟，无需 Docker）。
- **订单 order**：挂在某个 **批次 `order_batch_id`** 下，含交易日、代码、方向（买=1/卖=2）、数量、可选限价；可选 `exec_time`（盘中分钟 `HH:MM`）→ **分钟级**在该分钟（at-or-after）成交，不填则按 `price_type` 日级（open/close）成交。
- **策略 strategy**：`strategy_id` + 它对应的 **订单批次**（`params.order_batch_id`）。一次回测可含多个策略，每个策略是**独立子账户**（各自一份初始资金）。
- **作业 job**：一次回测。异步——POST 立即返回 `202 + job_id`，后台 worker 跑完置 `completed`。
- **报告**：日级净值、成交、拒单、持仓 + 汇总指标，落在 `.vortex_backtest/reports/<job_id>/`。

> 「策略中心」是从历次作业里按 `strategy_id` **派生**出来的只读视图（同一个 strategy_id 跨多次回测 = 多次 run），不是另建的写模型。收藏/置顶/标签存在 `strategy_meta`。

---

## 3. 怎么调用：开闭环脚本 + HTTP

回测的全部操作都走 **HTTP**，**不再有命令行协议客户端**（命令行只剩 `serve` 起服务）。两种调用方式：

**(A) 开闭环脚本（最省事）** —— 一条命令跑完「建账户 → 买卖 → 结束 → 关闭 → 报告」，仅依赖 `curl + python3`：

```bash
# 服务起好后（见 §0）
scripts/backtest_roundtrip.sh                        # 默认标的/区间（600000.SH，05-06~06-05）
scripts/backtest_roundtrip.sh --symbol 000001.SZ --buy-date 2026-05-06 --sell-date 2026-05-20
scripts/backtest_roundtrip.sh --base-url http://10.0.0.5:8766 --token "$TOK"   # 远端 + 鉴权
scripts/backtest_roundtrip.sh --help                 # 全部选项
```

**(B) 纯 curl（看清每一步协议）**：

```bash
B=http://127.0.0.1:8766

# 建账户
curl -s -XPOST $B/accounts -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","initial_cash":1000000,"name":"演示账户"}'

# 下单（side：1=买 2=卖；批量就对每条各调一次）
curl -s -XPOST $B/accounts/demo/orders -H 'Content-Type: application/json' \
  -d '{"order_batch_id":"batch-main","request_id":"m1","trade_date":"2026-05-06","symbol":"600000.SH","side":1,"quantity":1000}'

# 提交回测（结束）→ 拿 job_id
JOB=$(curl -s -XPOST $B/backtests -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","order_batch_id":"batch-main","start_date":"2026-05-06","end_date":"2026-06-05"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

# 轮询到终态（关闭）
until curl -s $B/backtests/$JOB | python3 -c 'import sys,json;s=json.load(sys.stdin)["status"];print(s);exit(0 if s in {"completed","failed","cancelled","interrupted"} else 1)'; do sleep 1; done

# 取报告
curl -s $B/backtests/$JOB/summary       # 还有 /daily /trades /rejections
curl -s "$B/symbols/688169.SH"           # 代码归属板块 + 手数/涨跌停规则
```

### 3.1 订单 JSON（HTTP body）

直连 REST 时 `side` 必须用数字 `1`（买）/`2`（卖）。批量下单 = 对清单里每条各 `POST /accounts/{id}/orders` 一次（幂等键 `account+batch+request_id` 保证可安全重试）。

```json
[
 {"order_batch_id":"batch-main","request_id":"m1","trade_date":"2026-05-06","symbol":"600000.SH","side":"buy","quantity":1000},
 {"order_batch_id":"batch-main","request_id":"m2","trade_date":"2026-05-06","symbol":"000001.SZ","side":"buy","quantity":1000},
 {"order_batch_id":"batch-main","request_id":"m5","trade_date":"2026-05-20","symbol":"000001.SZ","side":"sell","quantity":500},
 {"order_batch_id":"batch-star","request_id":"s1","trade_date":"2026-05-06","symbol":"688169.SH","side":"buy","quantity":200}
]
```

字段：`order_batch_id`、`request_id`（同账户内唯一）、`trade_date`（YYYY-MM-DD）、`symbol`、`side`、`quantity`(>0)、可选 `price_type`（open/close，日级）、`exec_time`（`HH:MM`，**分钟级**：在当日 at-or-after 该分钟成交；填了则优先于 price_type）、`limit_price`、`comment`。

### 3.2 策略列表（`POST /backtests` 的 `strategies` 字段）

多策略时把下面的数组放进 `POST /backtests` 请求体的 `strategies`。每个策略用 `params.order_batch_id` 绑定它回放的订单批次；`symbols` 不填则自动取该批次出现过的代码。

```json
[
 {"strategy_id":"main-replay","symbols":["600000.SH","000001.SZ"],"params":{"order_batch_id":"batch-main"}},
 {"strategy_id":"star-replay","symbols":["688169.SH"],"params":{"order_batch_id":"batch-star"}}
]
```

---

## 4. REST 接口清单

基址 `http://127.0.0.1:8766`。请求/响应均为 JSON。**最省事的学习方式：直接开 `/docs` 在线试。**

### 写接口（建账户 / 下单 / 提交回测）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/accounts` | 建账户。body：`{account_id, initial_cash, engine?, name?}` |
| POST | `/accounts/{account_id}/orders` | 下单。body 见 §3.1（`side` 用数字 1/2） |
| POST | `/backtests` | **提交回测，返回 202 + job_id**。body 见下 |
| POST | `/backtests/{job_id}/cancel` | 取消**排队中**作业（运行中/已终态返回 409） |
| PUT | `/strategies/{strategy_id}/meta?account_id=` | 设收藏/置顶/标签：`{favorite?,pinned?,tags?}` |

提交回测 body 示例：

```json
{
  "account_id": "demo",
  "start_date": "2026-05-06",
  "end_date": "2026-06-05",
  "frequency": "1min",
  "price_adjustment": "qfq",
  "strategies": [
    {"strategy_id":"main-replay","symbols":["600000.SH","000001.SZ"],"params":{"order_batch_id":"batch-main"}}
  ],
  "execution": {"commission_rate":0.0003,"min_commission":5,"stamp_tax_rate":0.0005,"slippage_bps":0}
}
```

> `execution` 可省略（用缺省费率/滑点）；不传 `strategies` 则按 `order_batch_id` 单策略回放。

### 只读 / 报告接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/accounts` · `/accounts/{id}` | 账户列表 / 详情 |
| GET | `/accounts/{id}/orders` | 账户订单 |
| GET | `/symbols/{symbol}` | 代码 ↔ 板块 / 手数 / 规则 |
| GET | `/backtests?account_id=&status=` | 作业列表（含派生 `strategy_ids`，看板「全部回测」用它显示策略名） |
| GET | `/backtests/{job_id}` | 作业状态 / 进度 |
| GET | `/backtests/{job_id}/summary` | 完整汇总（现金/市值/持仓/成交/拒单/各策略） |
| GET | `/backtests/{job_id}/daily` · `/daily/{trade_date}` | 日级净值序列 / 某日快照 |
| GET | `/backtests/{job_id}/minutes?limit=&offset=` | 逐分钟净值（组合；服务端分页，响应头 `X-Total-Count`） |
| GET | `/backtests/{job_id}/trades?strategy_id=&limit=&offset=` | 成交（服务端分页，响应头 `X-Total-Count`） |
| GET | `/backtests/{job_id}/rejections?reason=&strategy_id=&limit=&offset=` | 拒单（同上） |
| GET | `/backtests/{job_id}/rejections/summary` | 拒单按原因计数 |
| GET | `/backtests/{job_id}/equity?strategy_id=&benchmark=&rebase=1` | 净值曲线（**起点 1.0**）+ 基准对齐序列 |
| GET | `/backtests/{job_id}/metrics?benchmark=` | 绩效指标（绝对 / 风险调整 / 基准相对；<60 交易日 `low_confidence=true`） |
| GET | `/benchmarks` | 可选基准目录（000300.SH 等） |

### 策略中心（按 strategy_id 派生的聚合）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/strategies?account_id=&best_metric=` | 策略列表：每条含 n_runs / 最新 / 最优 / 标的 / 收藏 |
| GET | `/strategies/{id}?account_id=&benchmark=` | 策略详情：净值（起点 1.0）+ **当前持仓** + **成交记录** + `latest_job_id` |
| GET | `/leaderboard?account_id=&metric=&scope=&top=` | 排行榜：每行带**多指标**(收益/年化/Sharpe/Sortino/Calmar/回撤)；`metric`+`scope`(best/latest) 决定排名 |
| GET | `/strategies/compare?account_id=&ids=a,b&benchmark=` | A/B 对比：净值叠加 + 指标并排 |

curl 示例：

```bash
B=http://127.0.0.1:8766
curl -s "$B/backtests?account_id=demo"
curl -s "$B/strategies?account_id=demo"
curl -s "$B/leaderboard?account_id=demo&metric=total_return&scope=best"
curl -s "$B/strategies/main-replay?account_id=demo&benchmark=000300.SH"
curl -s "$B/backtests/<job_id>/trades?limit=25&offset=0" -D - | grep -i x-total-count
```

---

## 5. 看板用法

打开 http://127.0.0.1:8766/ ，顶部三标签：**首页（策略中心）/ 排行榜 / 全部回测**。

- **首页 = 策略中心**：顶部 KPI（策略数 / 运行中 / 近 7 天活跃 / 历史最优收益）；**排行榜**一行同时看 收益·年化·Sharpe·Calmar·回撤 + 标的，右上「排序依据」切指标与 最优/最新；**我的策略**表（收藏★、置顶、回测次数；勾选 ≥2 个点「对比」）；**运行中** + **近期活动**（状态点 + 完成/失败徽章 + 相对时间）。
- **策略详情**（点策略名进入）：最新/最优指标卡 + **净值曲线（起点 1.0，叠加沪深300，下方回撤轴）** + **历次回测**（点某次下钻到该次明细）+ **当前持仓** + **成交记录**。
- **全部回测**：主显**策略名**，job_id 作为下面一行小灰字；点行进入单次回测的 概览/成交/拒单/持仓/对比 五个标签。
- 右上「对标」选基准、「主题」切浅/深色、🔄 手动刷新。
- 数据源自动：优先连真实后端，连不上回退内置示例数据（无需后端也能预览界面）。

---

## 6. 注意事项（A股口径）

- **T+1**：当日买入当日不可卖，会被拒为 `t_plus_1_not_sellable`（示例里 m3 即演示了这条）。
- **分板手数**：主板 100 股/手，科创/创业 200 股起部分规则，北交所另算；非整手按板块规整或拒单。
- **涨跌停**：以数据中的 `stk_limit` 为准，涨停拦买、跌停拦卖。
- **费用**：佣金（含最低 5 元）、卖出印花税、过户费、可配滑点，均可在 `execution` 覆盖。
- **净值起点**：所有净值曲线**从 1.0 起**（不是 100）。
- **样本不足**：<60 个交易日时风险调整类指标 `low_confidence=true`，看板会置灰提示——当前 23 天窗口即属此列。
- **多策略资金**：每个策略默认各自一份 `initial_cash`，组合总额 = 各策略之和。

---

## 7. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 回测 `failed: minute_data_missing` | 起服务时没设 `VORTEX_WORKSPACE`，或订单日期落在 23 天窗口外 |
| 端口被占 | `lsof -ti tcp:8766 \| xargs kill -9` 后重启 |
| 写接口 403 | 绑了非回环 host 且没配 `VORTEX_BACKTEST_TOKEN`；本机回环默认放行 |
| 基准为空 | 没设 `VORTEX_INDEX_DATA_DIR`（指向 `.../workspace/data/index_daily`） |
| 看板图表不显示 | 已本地内置 Chart.js（`web/static/vendor/`），缺失会退到内联 SVG 静态预览 |
---

## 附：环境变量一览

| 变量 | 作用 | 示例 |
|---|---|---|
| `VORTEX_WORKSPACE` | 行情 workspace 根目录（自动接 `/data`） | vortex_data 导出的 workspace 根（`$WS`） |
| `VORTEX_INDEX_DATA_DIR` | 指数基准目录 | `$VORTEX_WORKSPACE/data/index_daily` |
| `VORTEX_BACKTEST_HOST` / `PORT` | 服务监听地址 | `127.0.0.1` / `8766` |
| `VORTEX_BACKTEST_TOKEN` | 写接口鉴权（非回环必配） | 任意密钥 |
| `BASE_URL` | 开闭环脚本 `backtest_roundtrip.sh` 默认连的服务地址 | `http://127.0.0.1:8766` |
| `VORTEX_STATE` | 状态库目录（账户/作业/meta） | 缺省 repo `state/` |

---

## 附：与券商对账单对照（容差）

回测走 **qfq 前复权、不建模现金分红**，与券商真实账本按**容差**对照即可（口径见 design/15）。

1. 跑回测拿到产物 `account_summary.json`（作业完成后在 `report_dir`，或 `GET /backtests/{job_id}/summary`）。
2. 备好券商对账单 CSV（必需列 `date,symbol,side,quantity,price`；可含 `amount/commission/stamp_tax/transfer_fee/request_id`，列名中英文别名自动识别）。
3. 对照：

   ```bash
   python scripts/reconcile_statement.py \
       --summary account_summary.json --statement 对账单.csv \
       --events-dir "$VORTEX_WORKSPACE/data/events" --tolerance 0.005
   ```

   按 `(date, symbol, side)` 聚合比较 数量 / 成交额 / 费用，超差或未匹配列入"需排查"（退出码 1，便于 CI 卡口）。窗口内**除权**的标的（读 `events.ex_date`）标注为**预期 qfq 分红差**，与真 bug 区分。成交记录另含 `realized_pnl`（已实现盈亏）与 `requested_quantity`（原始下单量，便于识别量能上限导致的部分成交）。