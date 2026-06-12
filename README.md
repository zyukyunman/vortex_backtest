# vortex_backtest

独立 HTTP **会话式回测/账户回放**服务（A 股分钟级）。策略与服务的交互模型是
「**建会话 → 按模拟时钟逐步 advance（提交委托+推进）→ close 出报告**」，
服务端控 `sim_time`、按 `as_of` 强制 point-in-time 取数（防未来函数）。

```text
HTTP 协议层(sessions) + A 股分钟撮合/规则内核 + data 取数网关(PIT) / 本地 Parquet 回退
```

第一阶段只支持 A 股现金账户、`1min` 分钟回测、多策略独立账户。
会话引擎与跨服务契约见 [design/18](design/18-session-backtest-engine.md)；部署见
[docs/operations.md](docs/operations.md)；交互式 API 文档见服务自带 `/docs`(Swagger)。

## 当前能力

- `POST /accounts` 建账户；`GET /accounts`、`GET /accounts/{id}` 查询
- `POST /sessions` 开会话（账户、区间、股池、撮合配置）；`GET /sessions`、`GET /sessions/{id}` 列表/当前状态
- `POST /sessions/{id}/advance` 提交本步委托 + 推进模拟时钟（`request_id` 幂等去重，重试不双成交）
- `POST /sessions/{id}/data` 策略取数（透传 data 网关，服务端用会话 `sim_time` 当 `as_of`，不信客户端时间）
- `POST /sessions/{id}/close` 关闭会话出最终报告
- `GET /sessions/{id}/summary|daily|trades|rejections|minutes` 报告（会话期间即可读当前累积态）
- `GET /sessions/{id}/metrics|equity|positions|rebalances`、`GET /benchmarks` 分析报告层：
  基准对比指标（夏普/回撤/IR/Beta/Alpha）、年度月度统计、多粒度持仓（日/周/时/分）、调仓记录
- `GET /symbols/{symbol}` Tushare/MiniQMT/Vortex 统一代码与板块规则
- `/ui` 看板、`/guide` 文档站、`/docs` Swagger

> 旧「订单回放」异步作业 HTTP 面已删除；批量订单回放 = 会话的特例
> （订单带 `trade_date+exec_time` 全预提交，一次 advance 到 end），
> 见 `examples/session_scenarios.py` 的 `replay` 场景。

## 数据：两条取数路（口径不同，须知悉）

| 路 | 触发条件 | 撮合/估值口径 | 分红处理 | 用途 |
|---|---|---|---|---|
| **data 网关**（推荐/部署） | 配 `VORTEX_DATA_URL`（+`VORTEX_DATA_DASHBOARD_TOKEN`） | RAW 不复权真实价 | 除权日显式入账现金/送转（真实账户口径） | 生产；服务端强制 PIT |
| 本地直读（回退） | 不配 `VORTEX_DATA_URL`，配 `VORTEX_WORKSPACE` | qfq 前复权 | 不入账（已吸进前复权价） | 离线开发/调试 |

两条路的总收益近似一致（纯拆股精确等价），但现金流/持仓估值数值不同，不要混用对账。

本地直读需要 workspace 下数据集（loader 层缺关键表报 `*_data_missing`；会话 advance 对缺数
标的优雅降级——表现为 `no_market_data` 拒单/空帧，不伪装成交）：

| 数据集 | 用途 | 缺失行为（loader 层） |
| --- | --- | --- |
| `data/stk_mins` | 1min 主行情 | `minute_data_missing` |
| `data/adj_factor` | qfq 前复权 | `adjustment_data_missing` |
| `data/stk_limit` | 涨跌停价 | `market_rules_data_missing` |
| `data/suspend_d` | 停复牌 | 缺表按无停牌处理 |
| `data/stock_st` | 历史 ST | 缺表按非 ST 处理 |

网关路另需 data 服务落盘 `dividend`（含 `ex_date` 列）供除权日入账。
数据由 vortex_data 抓取/导出，本服务**只读消费**；当前落盘覆盖以 vortex_data 实际为准。

## 安装和启动

建议 Python 3.12 或 3.13（需 ≥3.11）：

```bash
cd $REPO            # 本仓根目录
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

启动服务（命令行只剩 `serve` 子命令；回测操作统一走 HTTP）：

```bash
export VORTEX_WORKSPACE=$WS                            # 本地直读回退用
export VORTEX_DATA_URL=http://127.0.0.1:8765           # 网关路(推荐)
export VORTEX_DATA_DASHBOARD_TOKEN=<token>             # 网关 token(与 data 服务同名共享)
export VORTEX_STATE=$REPO/state                        # 账户/会话/报告状态目录
.venv/bin/vortex-backtest serve                        # 默认 127.0.0.1:8766
curl http://127.0.0.1:8766/health
```

容器部署用 `vortex run up backtest`（端口 8766，宿主机挂载默认 `~/vortex/{workspace,state}`，
可用 `VORTEX_*_HOST_ROOT` 覆盖）；全栈用 `vortex run deploy`。
端口/变量规范以 vortex_common 的 `config/registry.yml` + ADR-003 为准。
写接口鉴权：配 `VORTEX_BACKTEST_TOKEN` 则必须带 token；未配时仅本机回环放行（fail-closed）。

## 基本调用（会话式）

```bash
# ① 建账户
curl -X POST http://127.0.0.1:8766/accounts -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","initial_cash":1000000}'

# ② 开会话
curl -X POST http://127.0.0.1:8766/sessions -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","level":"1min","start_date":"2026-05-06","end_date":"2026-06-05","universe":["000001.SZ"]}'
# → {"session_id":"...","status":"open",...}

# ③ 买入并推进到当日收盘（to 传日期 = 推进到该日 15:00）
curl -X POST http://127.0.0.1:8766/sessions/<id>/advance -H 'Content-Type: application/json' \
  -d '{"request_id":"step1","to":"2026-05-06","orders":[{"request_id":"buy-1","symbol":"000001.SZ","side":1,"quantity":1000,"trade_date":"2026-05-06","exec_time":"09:31"}]}'

# ④ 关闭出报告
curl -X POST http://127.0.0.1:8766/sessions/<id>/close
curl http://127.0.0.1:8766/sessions/<id>/summary
```

或一条命令跑完开闭环（建账户 → 会话 → 买卖 → close → 报告，仅依赖 curl + python3）：

```bash
scripts/backtest_roundtrip.sh --symbol 000001.SZ \
  --buy-date 2026-05-06 --sell-date 2026-05-13 --start 2026-05-06 --end 2026-06-05
```

委托语义：带 `trade_date`+`exec_time` → 停泊到该日 at-or-after 该分钟首个 bar 成交；
不带 `exec_time` → 默认下一根 bar（`fill_timing=next_bar`，防未来）。
T+1、涨跌停、分板手数、费用由规则内核强制。

## 多场景示例

`examples/session_scenarios.py` 对真实 HTTP 接口演示 5 种流程（日频选股 / 分钟择时 /
全市场扫描选股 / 循序渐进取数 / 订单全预提交回放），见 [examples/README.md](examples/README.md)。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q vortex_backtest tests examples
```
