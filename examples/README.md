# 会话式回测 · 示例

`session_scenarios.py` 演示 5 个**不同流程**的场景，对着真实 HTTP 接口走一遍，供使用者参考/调试。

## 前置

1. **起 data 网关**（vortex_data）：默认 `127.0.0.1:8765`。建议设 `VORTEX_DATA_DASHBOARD_TOKEN`。
2. **起 backtest 服务**（vortex_backtest）：`vortex-backtest serve`（默认 `127.0.0.1:8767`），
   并设 `VORTEX_DATA_URL=http://127.0.0.1:8765` 让它走网关（不设则回退本地直读 parquet）。
   若 data 网关配了 token，backtest 侧也设同名 `VORTEX_DATA_DASHBOARD_TOKEN`。
3. 运行：

```bash
python examples/session_scenarios.py daily        # 日频选股：收盘决策→次日开盘成交
python examples/session_scenarios.py minute       # 分钟择时：this_bar + exec_time 精确分钟
python examples/session_scenarios.py scan         # 全市场扫描选股：/data op=topN 下推
python examples/session_scenarios.py progressive  # 循序渐进取数：取窗口→缩股池→推进
python examples/session_scenarios.py replay       # A 特例：订单全预提交、一次跑到底
python examples/session_scenarios.py all
```

## 五个场景 = 五种流程

| 场景 | level | fill_timing | 关键流程 |
|---|---|---|---|
| daily | daily | next_bar | 收盘出信号 → `to=next_day` 次日开盘成交 |
| minute | 1min | this_bar | `exec_time` 精确分钟、当根成交 |
| scan | 1min | next_bar | `/data` `op=topN` 全市场扫描选股 → `set_universe` |
| progressive | 1min | next_bar | `/data` 取窗口 → 缩股池（粘住）→ advance |
| replay | 1min | this_bar | 订单带 `trade_date+exec_time` 全预提交，一次 advance 到 end（= 旧 A 的等价形态）|

## 接口速查

- `POST /accounts` `{account_id, initial_cash}`
- `POST /sessions` `{account_id, level, start_date, end_date, universe?, fill_timing?}` → `{session_id}`
- `POST /sessions/{id}/data` `{datasets:[{dataset,symbols,fields,level?,window?,op?}]}` → 全部 `≤ sim_time`
- `POST /sessions/{id}/advance` `{orders?, set_universe?, to}` → `{sim_time, cash, positions, nav, filled, rejected}`
- `POST /sessions/{id}/close` → `{summary}`
- `GET /sessions/{id}/{summary|daily|trades|rejections|minutes}`

> 防未来函数：`as_of` 由会话用 `sim_time` 自动填给网关，**不信客户端时间**；复权用**前复权 + PIT 锚点**。
