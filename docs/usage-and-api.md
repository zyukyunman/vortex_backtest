# vortex_backtest 使用与接口指南

> 面向「怎么用」的上手文档：起服务 → 建账户 → 开会话 → advance 买卖 → close → 看报告。
> 交互模型是**会话步进**（design/18）：服务端控模拟时钟 `sim_time`，按 `as_of` 强制
> point-in-time 取数（防未来函数）。旧「订单批量回放 + 异步作业」HTTP 面已删除。

---

## 0. 一分钟跑起来

```bash
cd $REPO            # 本仓根目录

# 1) 数据从哪来（二选一，详见 §1）
export VORTEX_DATA_URL=http://127.0.0.1:8765           # 推荐：data 取数网关(PIT)
export VORTEX_DATA_DASHBOARD_TOKEN=<token>             # 网关 token(与 data 服务共享)
export VORTEX_WORKSPACE=$WS                            # 或/并配：本地直读回退

# 2) 起服务（默认 127.0.0.1:8766）
export VORTEX_STATE=$REPO/state
.venv/bin/vortex-backtest serve
curl http://127.0.0.1:8766/health    # {"status":"ok"}
```

起好后三个入口：`/docs`(Swagger，点点就能试) · `/ui/`(看板) · `/guide`(文档站)。

---

## 1. 数据：两条取数路（口径不同）

| 路 | 触发条件 | 撮合/估值口径 | 分红 | 用途 |
|---|---|---|---|---|
| **data 网关**（推荐） | 配 `VORTEX_DATA_URL` | RAW 不复权真实价 | 除权日显式入账（真实账户口径） | 生产；服务端强制 PIT |
| 本地直读（回退） | 不配 `VORTEX_DATA_URL` | qfq 前复权 | 不入账（已吸进价） | 离线开发/调试 |

依赖数据集：`stk_mins`(1min 主行情) · `stk_limit`(涨跌停) · `adj_factor`(qfq 用) ·
`suspend_d` / `stock_st`(可选) · `dividend`(网关路分红入账用，须含 `ex_date` 列)。
缺关键表 → loader 层明确报 `*_data_missing`；会话 advance 对缺数标的优雅降级
（`no_market_data` 拒单/空帧，不伪装成交）。数据覆盖以 vortex_data 实际落盘为准。

---

## 2. 核心概念

- **账户 account**：一笔初始资金 + `replay` 引擎（旧值 backtrader/qlib/rqalpha/ashare_replay 自动归一）。
- **会话 session**：挂在账户下的一次回测进程。状态 `open → closed`；持有 `sim_time`（单调时钟）、
  现金/持仓/T+1 可卖/挂单/股池。产物（成交/拒单/快照/公司行动/日历）追加 JSONL 落 `VORTEX_STATE`。
- **advance**：一步推进 = 提交本步委托 → 撮合到期单 → 推进 `sim_time` 到 `to` → 结算 → 回账户上下文。
  `request_id` 幂等去重：同 id 重试回当前状态，**不双成交、不双推进**。
- **委托语义**：带 `trade_date`+`exec_time`(HH:MM) → 停泊到该日 at-or-after 该分钟首个 bar；
  不带 `exec_time` → 按 `fill_timing`（默认 `next_bar`：sim_time 后严格下一根，防未来；可选 `this_bar`）。
- **close**：跑 reducer 出最终报告（summary/daily），会话置 `closed`。

---

## 3. 怎么调用

**(A) 开闭环脚本（最省事）** —— 一条命令跑完「建账户 → 会话 → 买卖 → close → 报告」：

```bash
scripts/backtest_roundtrip.sh                        # 默认 000001.SZ
scripts/backtest_roundtrip.sh --symbol 600519.SH --buy-date 2026-05-06 --sell-date 2026-05-20
scripts/backtest_roundtrip.sh --base-url http://10.0.0.5:8766 --token "$TOK"   # 远端 + 鉴权
scripts/backtest_roundtrip.sh --help                 # 全部选项
```

**(B) 纯 curl（看清每一步协议）**：

```bash
B=http://127.0.0.1:8766

# ① 建账户
curl -s -XPOST $B/accounts -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","initial_cash":1000000}'

# ② 开会话 → 拿 session_id
SID=$(curl -s -XPOST $B/sessions -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","level":"1min","start_date":"2026-05-06","end_date":"2026-06-05","universe":["000001.SZ"]}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

# ③ 买入并推进到当日收盘（to 传日期 = 推进到该日 15:00；side：1=买 2=卖）
curl -s -XPOST $B/sessions/$SID/advance -H 'Content-Type: application/json' \
  -d '{"request_id":"step1","to":"2026-05-06","orders":[{"request_id":"buy-1","symbol":"000001.SZ","side":1,"quantity":1000,"trade_date":"2026-05-06","exec_time":"09:31"}]}'

# ④ T+1 后卖出
curl -s -XPOST $B/sessions/$SID/advance -H 'Content-Type: application/json' \
  -d '{"request_id":"step2","to":"2026-05-13","orders":[{"request_id":"sell-1","symbol":"000001.SZ","side":2,"quantity":1000,"trade_date":"2026-05-13","exec_time":"09:31"}]}'

# ⑤ 推进到期末 → 关闭 → 报告
curl -s -XPOST $B/sessions/$SID/advance -H 'Content-Type: application/json' -d '{"request_id":"step3","to":"end"}'
curl -s -XPOST $B/sessions/$SID/close
curl -s $B/sessions/$SID/summary
```

**(C) 多场景示例**：`examples/session_scenarios.py` 演示日频选股 / 分钟择时 / 全市场扫描选股 /
循序渐进取数 / 订单全预提交回放（= 旧 A 形态）5 种流程，见 `examples/README.md`。

### 3.1 advance 请求体

```json
{
  "request_id": "step1",
  "to": "2026-05-06",
  "orders": [
    {"request_id":"buy-1","symbol":"000001.SZ","side":1,"quantity":1000,
     "trade_date":"2026-05-06","exec_time":"09:31","limit_price":null,"price_type":null}
  ],
  "set_universe": null,
  "cancel": null
}
```

- `to`：时间戳 / 日期(`YYYY-MM-DD`，自动接 15:00) / `"end"`(到 end_date) / `"next_day"`。必须单调（倒退 409）。
- `orders[*]`：`request_id`、`symbol`、`side`(1/2)、`quantity`、可选 `trade_date`、`exec_time`、
  `price_type`(open/close)、`limit_price`。
- `set_universe`：改股池（持仓股即便被踢出也会继续取 bar，保证可估值可卖出）。
- `cancel`：撤掉队列里未成交挂单的 `request_id` 列表（撤单-only = `to` 取当前 sim_time）。
- 响应：`{sim_time, cash, market_value, nav, positions, open_orders, filled, rejected, cancelled,
  corporate_actions}`（网关路除权入账行）；同 `request_id` 幂等重放时返回 `duplicate: true`。

### 3.2 策略取数 `POST /sessions/{id}/data`

透传到 data 网关；`as_of` 由服务端用会话 `sim_time` 填（**不信客户端时间**）。需配 `VORTEX_DATA_URL`。

```json
{"datasets":[{"dataset":"bars","symbols":"universe","fields":["close","pct_chg"],
              "window":{"count":20},"op":null}]}
```

`symbols:"universe"` 自动展开为会话股池；`op` 支持 rank/topn/filter/agg 算子下推（全市场扫描不传海量行）。

---

## 4. REST 接口清单

基址 `http://127.0.0.1:8766`。**最省事的学习方式：直接开 `/docs` 在线试。**

### 写接口（须鉴权，见 §6）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/accounts` | 建账户。body：`{account_id, initial_cash, engine?, name?}` |
| POST | `/sessions` | 开会话。body：`{account_id, level, start_date, end_date, universe?, strategy_id?, fill_timing?, default_price_type?, execution?:{slippage_bps}}` |
| POST | `/sessions/{id}/advance` | 提交委托 + 推进时钟（§3.1） |
| POST | `/sessions/{id}/data` | 策略取数（§3.2） |
| POST | `/sessions/{id}/close` | 关闭出最终报告 |

### 只读 / 报告接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/accounts` · `/accounts/{id}` | 账户列表 / 详情 |
| GET | `/symbols/{symbol}` | 代码 ↔ 板块 / 手数 / 规则 |
| GET | `/sessions?account_id=` | 会话列表 |
| GET | `/sessions/{id}` | 会话当前状态（sim_time / 现金 / 持仓 / 挂单） |
| GET | `/sessions/{id}/summary` | 汇总（已 close 读缓存；开着也能读"当前累积态"） |
| GET | `/sessions/{id}/daily` | 日级净值序列 |
| GET | `/sessions/{id}/trades?symbol=&limit=&offset=` | 成交 |
| GET | `/sessions/{id}/rejections?limit=&offset=` | 拒单 |
| GET | `/sessions/{id}/minutes?limit=&offset=` | 逐步快照（时间戳/现金/市值/总值） |
| GET | `/sessions/{id}/metrics?benchmark=&rf=` | 指标包：绝对/基准/相对(夏普/回撤/IR/Beta/Alpha) + 年度月度统计（行键 `period`） |
| GET | `/sessions/{id}/equity?benchmark=` | 起点 1.0 对齐净值曲线 + 逐日回撤（含期初本金基线点） |
| GET | `/sessions/{id}/positions?granularity=daily\|weekly\|hourly\|minute&date=&week=&limit=&offset=` | 多粒度持仓快照（含权重；minute 须带 date） |
| GET | `/sessions/{id}/rebalances?limit=&offset=` | 调仓事件（按日聚合买卖 + 前后持仓 diff + 费用） |
| GET | `/benchmarks` | 可选基准目录（常用指数 + 申万行业全量） |

---

## 5. 注意事项（A股口径）

- **T+1**：当日买入当日不可卖，拒为 `t_plus_1_not_sellable`。
- **分板手数**：主板 100 股/手，科创/创业/北交所各有规则；非整手按板块规整或拒单。
- **涨跌停**：以 `stk_limit` 为准，涨停拦买、跌停拦卖。
- **费用**：佣金（最低 5 元）、卖出印花税、过户费；滑点可在开会话 `execution.slippage_bps` 配。
- **口径**：网关路 = RAW 价 + 除权日入账分红送转；直读回退 = qfq 前复权不入分红。
  两路总收益近似一致（纯拆股精确等价），现金流/估值数值不同，不混用对账。

---

## 6. 鉴权与安全

写接口 fail-closed：配了 `VORTEX_BACKTEST_TOKEN` 须带 `Authorization: Bearer <t>` 或
`X-Auth-Token`，否则 401；未配 token 时仅本机回环放行，绑非回环 host 写接口直接 403。

---

## 7. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| advance 后 `filled=[]` 且拒单 `no_market_data` | workspace/网关缺该标的该窗口分钟数据（缺整表时会话面同样表现为此，而非 `*_data_missing`）；查 vortex_data 落盘 |
| loader 层 `minute_data_missing` / `market_rules_data_missing` | 缺 `stk_mins` / `stk_limit`（数据预检，不是 bug） |
| advance 409 `non_monotonic_clock` | `to` 早于当前 `sim_time`；时钟只能单调向前 |
| `/sessions/{id}/data` 503 `gateway_not_configured` | 没配 `VORTEX_DATA_URL` |
| `/sessions/{id}/data` 502 `gateway_error` | data 服务不可达/token 不对；查 8765 与 `VORTEX_DATA_DASHBOARD_TOKEN` |
| 写接口 401/403 | token 不对，或绑非回环没配 `VORTEX_BACKTEST_TOKEN` |
| 端口被占 | `lsof -ti tcp:8766 \| xargs kill -9` 后重启 |

---

## 附：环境变量一览

| 变量 | 作用 | 示例 |
|---|---|---|
| `VORTEX_DATA_URL` | data 取数网关地址（配了走网关路） | `http://127.0.0.1:8765` |
| `VORTEX_DATA_DASHBOARD_TOKEN` | 网关 token（与 data 服务共享同名变量） | 任意密钥 |
| `VORTEX_WORKSPACE` | 行情 workspace 根（本地直读回退；自动接 `/data`） | `$WS` |
| `VORTEX_STATE` | 状态目录（账户/会话/报告） | 缺省 `./.vortex_backtest` |
| `VORTEX_BACKTEST_HOST` / `PORT` | 服务监听地址 | `127.0.0.1` / `8766` |
| `VORTEX_BACKTEST_TOKEN` | 写接口鉴权（非回环必配） | 任意密钥 |
| `BASE_URL` / `TOKEN` | `backtest_roundtrip.sh` 的服务地址 / 鉴权 | `http://127.0.0.1:8766` |
