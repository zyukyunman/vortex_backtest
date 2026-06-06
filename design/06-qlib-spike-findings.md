---
title: Qlib 订单回放 Spike 结论（源码级验证 + 真机脚本）
created: 2026-06-06
status: findings
depends_on: design/05-backtest-engine-requirements.md
---

# Qlib 订单回放 Spike 结论

## 0. 方法与诚实边界

**计划**是在沙箱里装 Qlib 跑合成回测。**实际**：沙箱代理封了 PyPI（`pip install pyqlib` 返回 `Tunnel connection failed: 403`），装不了，也就**没法在这里执行 Qlib**。

于是改成**源码级验证**——直接抓取 Qlib 仓库 `main` 分支的真实源码逐行核对 API 与行为路径：`qlib/backtest/exchange.py`、`decision.py`、`position.py`。这恰好回答了 spike 的核心问题（"Qlib 能不能干净地承载 A 股外部订单回放、各条 A 股约束接在哪"），而且比合成跑更直接——是读真实实现，不是猜。**唯一没覆盖的是端到端数值与性能**（除权日 NAV、真实数据规模），那需要在你的环境跑——我已附 `spike/qlib_replay_spike.py` 让你一键补上。

每条结论标注状态：`✅源码确认`（能/不能在源码里是确定的）｜`⏳待真机`（需跑真实数据/性能）。

---

## 1. 结论先行

**锁定 Qlib 的判断成立，而且读完源码后更有底。** 两个关键认知更新：

1. **"订单回放是 Qlib 次要路径"的担忧大幅降低**：`Exchange.deal_order(order, position=...)` 就是一个**单订单执行原语**——构造 `Order(stock_id, amount, direction, start_time, end_time)`，调 `deal_order`，它做可交易性检查→量能裁剪→手数取整→费用→更新持仓，返回 `(trade_val, trade_cost, trade_price)`。外部订单回放正是它的低层用法（`decision.py` 的 `TradeDecisionWO(order_list)` 是上层封装）。**不是逆着框架用。**
2. **Qlib 原生把"高易错基础设施"做对了**：可交易性/涨跌停拦截、量能裁剪、费用、**基于 `$factor` 的复权**、停牌、NAV、甚至 T+0 现金延迟结算——这些正是我们手写引擎踩坑（C1/C3）或干脆没做（分红）的地方。

**留在我们这边的 A 股规则层很薄且清晰**：T+1 锁仓、科创/北交所手数、分项费用、insufficient-cash 口径、分红现金账本口径。下面逐条给证据。

---

## 2. 逐项清单核对（对照 design/05 §4）

| # | 清单项 | 结论 | 证据（源码） | 谁实现 | 状态 |
|---|---|---|---|---|---|
| 1 | **外部订单回放** | Qlib 支持 | `Order` 是普通 dataclass（`decision.py`）；`Exchange.deal_order(order, position)` 单订单成交并改持仓（`exchange.py`） | Qlib 原生 | ✅源码确认 |
| 2 | **涨停买/跌停卖拒单** | Qlib 支持 | `deal_order`→`check_order`→`is_stock_tradable`→`check_stock_limit`；命中则 `deal_amount=0` 返回 nan 价（`exchange.py`） | Qlib 可配 | ✅源码确认 |
| 2b | 以**数据**为准的分板涨跌停 | 需映射 | `_update_limit` 支持 float 阈值 / 表达式元组 / **预计算 `limit_buy`/`limit_sell` 布尔列**（`extra_quote`）。我们用 vortex_data 的 `stk_limit` 预计算 limit 标记最干净，分板统一 | 规则层预计算 + Qlib | ✅源码确认 |
| 3 | **T+1（当日买不可卖）** | Qlib **不**强制 | `Position._sell_stock` 只校验持仓量够不够；Exchange/Position **无 T+1**。有 `count_<bar>`（持有天数）可作钩子，但不拦卖 | **规则层必写**（我们现有 `sellable_quantity` 逻辑直接移植） | ✅源码确认 |
| 4 | **手数** | 部分 | `trade_unit` 是**单一全局值**（CN=100），`round_amount_by_trade_unit` 取整到 100 倍数 | 主板 100=Qlib；**科创 200+1 / 北交所=规则层**预校验 | ✅源码确认 |
| 5 | **复权口径 / 不假跳空** | Qlib 约定正确 | Qlib 用 `$close`(raw)+`$factor`+`$change`；手数取整用 `amount*factor`（真实股数）。复权由 factor 在正确环节处理，**不是预乘价格再判 tick** → 结构上消灭我们的 C1/C3 | Qlib 原生 | ✅源码确认（数值⏳待真机） |
| 6 | **费用** | 部分 | `open_cost`/`close_cost`/`min_cost`/`impact_cost`；`trade_cost=max(val*ratio, min_cost)` | 聚合费率=Qlib；**印花税分项/过户费分列=规则层后处理** | ✅源码确认 |
| 7 | **量能参与上限** | Qlib 支持 | `volume_threshold`+`_clip_amount_by_volume` 裁剪 deal_amount | Qlib 可配 | ✅源码确认 |
| 8 | **停牌** | Qlib 支持 | `check_stock_suspended`：`$close` 为 NaN 视为停牌 | Qlib 可配（映射 suspend_d） | ✅源码确认 |
| 9 | **现金不足** | 行为不同 | Qlib **裁剪为部分成交**（`_get_buy_amount_by_cash_limit`），我们现在是**拒单** | 需定口径（拒单 or 部分成交） | ✅源码确认 |
| 10 | **现金分红入账** | 两者都缺 | Position 无分红现金流；Qlib 走复权总收益口径，和我们现在一样不单记现金分红 | 要券商真账本=两者都需另写（非 Qlib 劣势，平手） | ✅源码确认 |
| 11 | **NAV/估值** | Qlib 支持 | `Position.calculate_value`=持仓市值+现金；`settle_start("cash")` 还能模拟 T+0 现金当步不可用 | Qlib 原生 | ✅源码确认（数值⏳待真机） |
| 12 | **性能（年×多标的分钟）** | 架构支持 | `NumpyQuote` 高性能取数结构 | — | ⏳待真机 |

---

## 3. Qlib 集成形态（最小回放循环）

```python
# 伪代码：把我们的订单批次回放进 Qlib
qlib.init(provider_uri=<vortex_data 落的 qlib 数据>, region=REG_CN)
ex = Exchange(freq="1min", start_time, end_time, codes=[...],
              deal_price="$close", limit_threshold=(buy_limit_expr, sell_limit_expr),
              volume_threshold=("current", "1.0*$volume"),
              open_cost=..., close_cost=..., min_cost=5, trade_unit=100)
pos = Position(cash=initial_cash)
for ts, orders in 我们的订单按分钟分组:
    for o in orders:
        # —— 我们的薄规则层在 deal_order 之前/之后接 ——
        if 违反 T+1(o, pos): record_reject("t_plus_1"); continue          # 规则层
        if not 合法手数(o): record_reject("invalid_lot"); continue         # 科创/北交所
        order = Order(stock_id=o.symbol, amount=o.qty, direction=..., start_time=ts, end_time=ts)
        trade_val, trade_cost, trade_price = ex.deal_order(order, position=pos)
        if order.deal_amount == 0: record_reject("limit/suspended/cash")   # Qlib 已判
        else: record_fill(order, trade_val, trade_cost, trade_price)
    snapshot_nav(pos)   # Position.calculate_value()
```

**数据字段映射（vortex_data → Qlib）**：`$close`←raw close、`$factor`←adj_factor、`$change`←日涨跌幅、`$volume`←vol、`limit_buy`/`limit_sell`←由 `stk_limit` 预计算、`$close=NaN`←suspend_d。这正是 `vortex_data` 已规划的 `qlib_view` 导出要落的东西。

---

## 4. 留在我们这边的"薄规则层"清单

1. **T+1 锁仓**：按 (symbol, 买入日) 冻结当日买入量，下一交易日解锁——我们现有 `Position.sellable_quantity` + `unlock_positions` 逻辑直接移植，挂在 `deal_order` 之前。
2. **科创 200 起+1 / 北交所手数**：`deal_order` 前预校验/规整（Qlib 单一 trade_unit 只覆盖主板 100）。
3. **分项费用**：要在报告里分列 commission/印花税/过户费，则在 `deal_order` 后按成交额拆解（Qlib 只给聚合 `trade_cost`），或覆写 `_calc_trade_info_by_order`。
4. **insufficient-cash 口径**：决定"拒单"还是"部分成交"——若要保持我们现在的"拒单"，在 deal 前用现金预检拦截。
5. **现金分红账本口径**：决定走 Qlib 的复权总收益口径，还是补一层真实现金分红入账（两者都要额外做，先明确产品语义）。

---

## 5. 对两个 qfq P0 bug 的处置

采用 Qlib 的 **`$close`(raw) + `$factor`** 数据约定后：

- **C1（tick 打在 qfq 价上）消失**：tick/手数判定走 raw 价与 factor，不再对预乘后的 qfq 价判 0.01 对齐。
- **C3（qfq 随窗口漂移）消失**：复权由 factor 在成交/估值环节统一应用，不存在"窗口内取最新因子当基准"的口径。

也就是说，统一到 Qlib **顺带**结构性修掉了这两个 bug——不用再单独打补丁。

---

## 6. 最终判定

**建议：锁定 Qlib 作引擎 + 薄规则层；把 `design/02` ADR-1 转为 Accepted。** 源码级证据支持"Qlib 能干净承载 A 股订单回放且原生解决高易错部分"，剩下的 A 股规则层小且我们已有可移植实现。

**锁定前的最后一步（诚实保留）**：在你的环境跑 `spike/qlib_replay_spike.py`，确认 4 项端到端 ✅：
- [ ] 涨停日买入 `deal_amount==0`（拒单）。
- [ ] 同日买入又卖出——Qlib 放行（证明 T+1 必须我们锁），加上我们的冻结逻辑后被拒。
- [ ] 非整手（如 150 股）被规整到 100。
- [ ] 除权除息日 NAV 不出现假跳空、费用与 min_cost 正确。

绿了就锁定；万一某项真机表现意外，按 `design/05 §2` 回退薄自研（规格与测试已现成）。

## 7. 待办

1. [ ] 你的环境跑 `spike/qlib_replay_spike.py`（需先 `pip install pyqlib` + 一份 qlib 数据；可先用 Qlib 自带 CN 样例数据冒烟，再用 vortex_data 的 qlib_view 真数据）。
2. [ ] 绿 → ADR-1 转 Accepted；排期 `vortex_data` 的 qlib 数据落盘 + `vortex_backtest` 接 Qlib + 薄规则层。
3. [ ] 与引擎无关、先做：`design/03` 阶段 0（基线提交）+ 阶段 1（修 C1/C3，或随 Qlib 迁移一并消除）。

## 参考 / Sources（Qlib 源码，git main）

- [exchange.py](https://github.com/microsoft/qlib/blob/main/qlib/backtest/exchange.py)（`deal_order`/`check_stock_limit`/`round_amount_by_trade_unit`/`_calc_trade_info_by_order`/costs）
- [decision.py](https://github.com/microsoft/qlib/blob/main/qlib/backtest/decision.py)（`Order`/`OrderDir`/`TradeDecisionWO`）
- [position.py](https://github.com/microsoft/qlib/blob/main/qlib/backtest/position.py)（`Position` 持仓/现金/`count` 持有天数/`settle_start` T+0 现金延迟）
