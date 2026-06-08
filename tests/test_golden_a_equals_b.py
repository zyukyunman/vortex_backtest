"""金标等价测试（design/18 B6）：A(_run_strategy) == B(session advance, this_bar)。

锁住"A 是 B 的特例"——同一份富 bar + 同一批带 exec_time 的订单，两条路径成交逐笔一致。
A(replay_engine batch) 在此作为 B 的等价 oracle 保留。
"""
from __future__ import annotations

import pandas as pd

from vortex_backtest.market_rules import AShareRuleEngine
from vortex_backtest.replay_engine import MinuteReplayEngine, Position
from vortex_backtest.session_engine import SessionRuntime, advance, finalize


def _bar(symbol, ts, date_key, px, vol=100000):
    return {
        "symbol": symbol, "trade_time": pd.Timestamp(ts), "date": date_key,
        "open": px, "high": px, "low": px, "close": px,
        "open_qfq": px, "close_qfq": px,
        "volume": vol, "board": "主板", "suspended": False, "is_st": False,
        "up_limit": px * 1.1, "down_limit": px * 0.9,
        "limit_up_qfq": px * 1.1, "limit_down_qfq": px * 0.9,
    }


def _minutes():
    rows = []
    # 2 天 × 3 分钟，价格变化制造盈亏
    for d, base in [(20260506, 10.0), (20260507, 11.0)]:
        for i, m in enumerate(("09:30:00", "09:31:00", "09:32:00")):
            rows.append(_bar("600519.SH", f"{d//10000}-{(d//100)%100:02d}-{d%100:02d} {m}", d, base + i * 0.1))
    return pd.DataFrame(rows).sort_values(["trade_time", "symbol"]).reset_index(drop=True)


def _orders():
    return [
        {"order_batch_id": "default", "request_id": "o1", "trade_date": "2026-05-06",
         "symbol": "600519.SH", "side": 1, "quantity": 200, "exec_time": "09:31"},
        {"order_batch_id": "default", "request_id": "o2", "trade_date": "2026-05-07",
         "symbol": "600519.SH", "side": 2, "quantity": 100, "exec_time": "09:30"},
    ]


def _trade_key(t):
    return (t["symbol"], int(t["side"]), int(t["quantity"]), t["price"], t["cash_after"], t["realized_pnl"])


def test_a_equals_b_trades_and_nav():
    minutes = _minutes()
    orders = _orders()
    rules = AShareRuleEngine()
    calendar = [20260506, 20260507]
    initial_cash = 1_000_000.0
    slippage = 5.0

    # ---- A: 批量回放 ----
    engine = MinuteReplayEngine()
    a = engine._run_strategy(
        strategy={"strategy_id": "default", "strategy_type": "order_replay",
                  "order_batch_id": "default", "symbols": ["600519.SH"], "initial_cash": initial_cash},
        all_orders=orders, minutes=minutes, calendar=calendar,
        default_price_type="close", rules=rules, slippage_bps=slippage,
    )

    # ---- B: 会话步进（this_bar，订单全预提交，一次 advance 到 end）----
    rt = SessionRuntime(
        session_id="default", strategy_id="default", sim_time=None,
        cash=initial_cash, initial_cash=initial_cash, positions={}, last_prices={},
        open_orders=[], trade_counter=0, current_date_key=None, universe=["600519.SH"],
        fill_timing="this_bar", default_price_type="close", slippage_bps=slippage,
    )
    advance(rt, minutes, rules=rules, orders=orders, to="2026-05-07T15:00:00")
    b = finalize(rt, calendar)

    # 逐笔成交一致
    assert [_trade_key(t) for t in a["trades"]] == [_trade_key(t) for t in b["trades"]]
    assert len(a["trades"]) == 2  # 买 + 卖都成交
    # 终值一致
    assert a["cash"] == b["cash"]
    assert a["total_value"] == b["total_value"]
    assert a["realized_pnl"] == b["realized_pnl"]
    assert a["realized_pnl"] > 0


def test_a_equals_b_with_rejection():
    """同日卖出（T+1 未解锁）两边都应拒。"""
    minutes = _minutes()
    rules = AShareRuleEngine()
    orders = [
        {"order_batch_id": "default", "request_id": "o1", "trade_date": "2026-05-06",
         "symbol": "600519.SH", "side": 1, "quantity": 100, "exec_time": "09:30"},
        {"order_batch_id": "default", "request_id": "o2", "trade_date": "2026-05-06",
         "symbol": "600519.SH", "side": 2, "quantity": 100, "exec_time": "09:32"},  # 同日卖 → T+1 拒
    ]
    engine = MinuteReplayEngine()
    a = engine._run_strategy(
        strategy={"strategy_id": "default", "strategy_type": "order_replay", "order_batch_id": "default",
                  "symbols": ["600519.SH"], "initial_cash": 1_000_000.0},
        all_orders=orders, minutes=minutes, calendar=[20260506, 20260507],
        default_price_type="close", rules=rules, slippage_bps=0.0,
    )
    rt = SessionRuntime(
        session_id="default", strategy_id="default", sim_time=None, cash=1_000_000.0, initial_cash=1_000_000.0,
        positions={}, last_prices={}, open_orders=[], trade_counter=0, current_date_key=None,
        universe=["600519.SH"], fill_timing="this_bar", default_price_type="close", slippage_bps=0.0,
    )
    advance(rt, minutes, rules=rules, orders=orders, to="2026-05-07T15:00:00")
    assert len(a["trades"]) == len(rt.trades) == 1
    assert [r["reason"] for r in a["rejections"]] == [r["reason"] for r in rt.rejections] == ["t_plus_1_not_sellable"]
