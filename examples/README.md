# 会话式回测 · 示例

`session_scenarios.py` 演示 9 个场景（5 个通用流程 + 4 个银行股频繁买卖专题），对着真实 HTTP 接口走一遍，供使用者参考/调试。

## 前置

1. **起 data 网关**（vortex_data）：默认 `127.0.0.1:8765`。建议设 `VORTEX_DATA_DASHBOARD_TOKEN`。
2. **起 backtest 服务**（vortex_backtest）：`vortex-backtest serve`（默认 `127.0.0.1:8766`），
   并设 `VORTEX_DATA_URL=http://127.0.0.1:8765` 让它走网关（不设则回退本地直读 parquet）。
   若 data 网关配了 token，backtest 侧也设同名 `VORTEX_DATA_DASHBOARD_TOKEN`。
3. 运行：

```bash
python examples/session_scenarios.py daily        # 日频选股：收盘决策→次日开盘成交
python examples/session_scenarios.py minute       # 分钟择时：this_bar + exec_time 精确分钟
python examples/session_scenarios.py scan         # 全市场扫描选股：/data op=topN 下推
python examples/session_scenarios.py progressive  # 循序渐进取数：取窗口→缩股池→推进
python examples/session_scenarios.py replay       # A 特例：订单全预提交、一次跑到底
# —— 银行股频繁买卖专题（高换手，喂看板换手率/仓位/分布图）——
python examples/session_scenarios.py bank_rotate   # 日线轮动：10 只银行股间高频轮换
python examples/session_scenarios.py bank_pyramid  # 分钟分批：金字塔建仓 + 次日分批减仓
python examples/session_scenarios.py bank_limit    # 限价单 + 撤单：limit 校验 / cancel
python examples/session_scenarios.py bank_frenzy   # 满仓轮动狂点：买齐 10 只逐日轮换
python examples/session_scenarios.py all
```

> 银行股专题不依赖 `/data` 网关，本地直读模式即可跑；写接口需带 `VORTEX_BACKTEST_TOKEN`
> （容器部署必配，见仓内 `.env`）。每个场景 close 后打印看板详情页链接，可直接去看六页签图表。

## 五个通用场景 = 五种流程

| 场景 | level | fill_timing | 关键流程 |
|---|---|---|---|
| daily | daily | next_bar | 收盘出信号 → `to=next_day` 次日开盘成交 |
| minute | 1min | this_bar | `exec_time` 精确分钟、当根成交 |
| scan | 1min | next_bar | `/data` `op=topN` 全市场扫描选股 → `set_universe` |
| progressive | 1min | next_bar | `/data` 取窗口 → 缩股池（粘住）→ advance |
| replay | 1min | this_bar | 订单带 `trade_date+exec_time` 全预提交，一次 advance 到 end（= 旧 A 的等价形态）|

## 四个银行股专题（实测全部零拒单，喂二期分布图表）

| 场景 | level | 流程 | 实测（2026-02~06 窗口） |
|---|---|---|---|
| bank_rotate | daily | 10 只银行股间高频轮换，每轮动日卖 3 买 3，跨全窗口 | 99 笔成交 / 17 次调仓 / 月均换手 58.67% |
| bank_pyramid | 1min | 单只 D1 金字塔建仓（5 个 exec_time 递增量）+ D2 分批减仓 | 7 笔成交 / 月均换手 10.48% |
| bank_limit | 1min | 正常买入 → 限价过低撮合即拒（`limit_price_not_marketable`）→ 停泊单 → `cancel` 撤掉 | limit 校验 + 撤单全演示 |
| bank_frenzy | daily | 一次买齐 10 只 → 逐日卖 3 买回 3 制造极高换手 | 58 笔成交 / 9 次调仓 / 月均换手 22.98% |

> 均尊重 A 股 T+1（当日买入不可当日卖；卖单只针对已跨日解锁持仓），故零拒单。

## 接口速查

- `POST /accounts` `{account_id, initial_cash}`
- `POST /sessions` `{account_id, level, start_date, end_date, universe?, fill_timing?}` → `{session_id}`
- `POST /sessions/{id}/data` `{datasets:[{dataset,symbols,fields,level?,window?,op?}]}` → 全部 `≤ sim_time`
- `POST /sessions/{id}/advance` `{orders?, set_universe?, to}` → `{sim_time, cash, positions, nav, filled, rejected}`
- `POST /sessions/{id}/close` → `{summary}`
- `GET /sessions/{id}/{summary|daily|trades|rejections|minutes}`

> 防未来函数：`as_of` 由会话用 `sim_time` 自动填给网关，**不信客户端时间**；复权用**前复权 + PIT 锚点**。
