"""公司行动入账（design/18 N8 真实账户口径）。

RAW 不复权撮合/估值下，分红送转的现金/股数效果在**除权日**显式入账（替代前复权把分红吸进价）：
- 现金分红：cash += qty × cash_div_tax
- 送转：    qty += int(qty × stk_div)（stk_div = 送+转 总比例，实证 ≡ stk_bo_rate+stk_co_rate）
- 成本：    cost_basis = 旧总成本 / 新股数
检测窗口 = (上一步 sim_time, 本步 sim_time]，仅对当前持仓应用，按 ex_date 升序。
"""
from __future__ import annotations

import pandas as pd

from vortex_backtest.market_rules import AShareRuleEngine
from vortex_backtest.replay_engine import Position
from vortex_backtest.session_engine import SessionRuntime, advance, apply_corporate_actions


def _rt(positions, *, cash=0.0, sim_time=None, **kw) -> SessionRuntime:
    base = dict(
        session_id="s", strategy_id="s", sim_time=sim_time, cash=cash, initial_cash=1_000_000.0,
        positions=positions, last_prices={}, open_orders=[], trade_counter=0, current_date_key=None,
        universe=[], fill_timing="this_bar", default_price_type="close", slippage_bps=0.0,
    )
    base.update(kw)
    return SessionRuntime(**base)


def _div(symbol, ex_date, *, cash_div_tax=0.0, stk_div=0.0, stk_bo_rate=0.0, stk_co_rate=0.0):
    return {"symbol": symbol, "ex_date": ex_date, "cash_div_tax": cash_div_tax,
            "stk_div": stk_div, "stk_bo_rate": stk_bo_rate, "stk_co_rate": stk_co_rate}


def test_cash_dividend_books_cash():
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=100.0)
    rt = _rt({"600519.SH": pos})
    applied = apply_corporate_actions(
        rt, [_div("600519.SH", 20260506, cash_div_tax=2.0)],
        lower=pd.Timestamp("2026-05-05 15:00:00"), upper=pd.Timestamp("2026-05-06 15:00:00"))
    assert rt.cash == 2000.0          # 1000 × 2
    assert pos.quantity == 1000        # 现金分红不改股数
    assert pos.cost_basis == 100.0     # 旧总成本/新股数 = 100000/1000 不变
    assert len(applied) == 1


def test_stock_split_dilutes_cost_basis():
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=100.0)
    rt = _rt({"600519.SH": pos})
    apply_corporate_actions(
        rt, [_div("600519.SH", 20260506, stk_div=1.0)],  # 10送10
        lower=None, upper=pd.Timestamp("2026-05-06 15:00:00"))
    assert pos.quantity == 2000
    assert pos.cost_basis == 50.0      # 100000/2000
    assert rt.cash == 0.0


def test_stk_div_is_total_ratio_not_double_counted():
    """用 stk_div(总比例)，不是 stk_div+stk_bo_rate（送股会被双计）。600508 式：stk_div=0.4,bo=0.3,co=0.1。"""
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=10.0)
    rt = _rt({"600508.SH": pos})
    apply_corporate_actions(
        rt, [_div("600508.SH", 20260506, stk_div=0.4, stk_bo_rate=0.3, stk_co_rate=0.1)],
        lower=None, upper=pd.Timestamp("2026-05-06 15:00:00"))
    assert pos.quantity == 1400        # +int(1000×0.4)=400，不是 +int(1000×0.7)=700


def test_stk_div_fallback_to_bo_plus_co_when_missing():
    """stk_div 缺/为 0 时回退 stk_bo_rate+stk_co_rate（数值等价）。"""
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=10.0)
    rt = _rt({"000001.SZ": pos})
    apply_corporate_actions(
        rt, [_div("000001.SZ", 20260506, stk_div=0.0, stk_bo_rate=0.3, stk_co_rate=0.1)],
        lower=None, upper=pd.Timestamp("2026-05-06 15:00:00"))
    assert pos.quantity == 1400        # +int(1000×0.4)


def test_fractional_shares_floored():
    pos = Position(quantity=333, sellable_quantity=333, cost_basis=10.0)
    rt = _rt({"000001.SZ": pos})
    apply_corporate_actions(
        rt, [_div("000001.SZ", 20260506, stk_div=0.15)],
        lower=None, upper=pd.Timestamp("2026-05-06 15:00:00"))
    assert pos.quantity == 333 + int(333 * 0.15)   # 333 + 49 = 382


def test_ex_date_outside_window_skipped():
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=100.0)
    rt = _rt({"000002.SZ": pos})
    lo, hi = pd.Timestamp("2026-05-05 15:00:00"), pd.Timestamp("2026-05-06 15:00:00")
    apply_corporate_actions(rt, [_div("000002.SZ", 20260501, cash_div_tax=2.0)], lower=lo, upper=hi)  # 已过
    apply_corporate_actions(rt, [_div("000002.SZ", 20260510, cash_div_tax=2.0)], lower=lo, upper=hi)  # 未到
    assert rt.cash == 0.0 and pos.quantity == 1000


def test_not_held_symbol_skipped():
    rt = _rt({})
    applied = apply_corporate_actions(
        rt, [_div("000002.SZ", 20260506, cash_div_tax=2.0)],
        lower=None, upper=pd.Timestamp("2026-05-06 15:00:00"))
    assert applied == [] and rt.cash == 0.0


def test_applied_in_ex_date_order():
    """先送转(早)再现金(晚)：现金分红按 split 后的股数计。"""
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=100.0)
    rt = _rt({"000002.SZ": pos})
    apply_corporate_actions(rt, [
        _div("000002.SZ", 20260510, cash_div_tax=1.0),   # 乱序传入：晚的现金
        _div("000002.SZ", 20260506, stk_div=1.0),        # 早的送转
    ], lower=None, upper=pd.Timestamp("2026-05-11 15:00:00"))
    assert pos.quantity == 2000        # 先 split
    assert rt.cash == 2000.0           # 后现金，按 2000 股 × 1


def test_advance_books_on_crossing_ex_date():
    """集成：advance 跨入 ex-date → 入账，结果在返回上下文。"""
    rules = AShareRuleEngine()
    pos = Position(quantity=100, sellable_quantity=100, cost_basis=10.0)
    rt = _rt({"600519.SH": pos}, sim_time=pd.Timestamp("2026-05-05 14:57:00"), current_date_key=20260505)
    frame = pd.DataFrame([{
        "symbol": "600519.SH", "trade_time": "2026-05-06 09:31:00", "date": 20260506,
        "open": 9.0, "close": 9.0, "open_qfq": 9.0, "close_qfq": 9.0, "volume": 100000,
        "board": "主板", "suspended": False, "up_limit": 9.9, "down_limit": 8.1,
    }])
    ctx = advance(rt, frame, rules=rules, to="2026-05-06 09:31:00",
                  dividends=[_div("600519.SH", 20260506, cash_div_tax=1.0)])
    assert rt.cash == 100.0            # 100 sh × 1
    assert ctx.get("corporate_actions")


def test_advance_without_dividends_is_noop():
    """不传 dividends（默认）→ 行为不变（向后兼容）。"""
    rules = AShareRuleEngine()
    rt = _rt({"600519.SH": Position(quantity=100, sellable_quantity=100, cost_basis=10.0)},
             sim_time=pd.Timestamp("2026-05-05 14:57:00"), current_date_key=20260505)
    frame = pd.DataFrame([{
        "symbol": "600519.SH", "trade_time": "2026-05-06 09:31:00", "date": 20260506,
        "open": 9.0, "close": 9.0, "open_qfq": 9.0, "close_qfq": 9.0, "volume": 100000,
        "board": "主板", "suspended": False, "up_limit": 9.9, "down_limit": 8.1,
    }])
    ctx = advance(rt, frame, rules=rules, to="2026-05-06 09:31:00")
    assert rt.cash == 0.0
    assert not ctx.get("corporate_actions")
