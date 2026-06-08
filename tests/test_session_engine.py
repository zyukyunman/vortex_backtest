"""会话步进引擎测试（design/18 B2）：撮合 / T+1 / 单调时钟 / next_bar 停泊。"""
from __future__ import annotations

import pandas as pd
import pytest

from vortex_backtest.market_rules import AShareRuleEngine
from vortex_backtest.replay_engine import Position
from vortex_backtest.session_engine import SessionRuntime, advance, finalize


def _bar(symbol, ts, date_key, px=10.0, vol=100000):
    return {
        "symbol": symbol, "trade_time": ts, "date": date_key,
        "open": px, "close": px, "open_qfq": px, "close_qfq": px,
        "volume": vol, "board": "主板", "suspended": False,
        "up_limit": px * 1.1, "down_limit": px * 0.9,
    }


def _frame(rows):
    return pd.DataFrame(rows)


def _runtime(**kw):
    base = dict(
        session_id="s", strategy_id="s", sim_time=None, cash=1_000_000.0, initial_cash=1_000_000.0,
        positions={}, last_prices={}, open_orders=[], trade_counter=0, current_date_key=None,
        universe=["600519.SH"], fill_timing="this_bar", default_price_type="close", slippage_bps=0.0,
    )
    base.update(kw)
    return SessionRuntime(**base)


def test_buy_fills_and_t_plus_1(tmp_path):
    rules = AShareRuleEngine()
    rt = _runtime()
    # day1：09:30, 09:31, 09:32
    day1 = _frame([
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506),
        _bar("600519.SH", "2026-05-06 09:31:00", 20260506),
        _bar("600519.SH", "2026-05-06 09:32:00", 20260506),
    ])
    # 在 09:30 决策买入（exec_time=09:30），推进到 09:31
    ctx = advance(rt, day1, rules=rules,
                  orders=[{"request_id": "o1", "symbol": "600519.SH", "side": 1, "quantity": 100, "exec_time": "09:30"}],
                  to="2026-05-06 09:31:00")
    assert rt.positions["600519.SH"].quantity == 100
    assert rt.positions["600519.SH"].sellable_quantity == 0   # T+1：当日不可卖
    assert ctx["cash"] < 1_000_000.0
    # 同日继续推进到 09:32 卖出 → t_plus_1_not_sellable
    advance(rt, day1, rules=rules,
            orders=[{"request_id": "o2", "symbol": "600519.SH", "side": 2, "quantity": 100, "exec_time": "09:32"}],
            to="2026-05-06 09:32:00")
    assert any(r["reason"] == "t_plus_1_not_sellable" for r in rt.rejections)
    assert rt.positions["600519.SH"].quantity == 100  # 没卖出去

    # day2：跨日 unlock，可卖
    day2 = _frame([_bar("600519.SH", "2026-05-07 09:30:00", 20260507, px=11.0)])
    advance(rt, day2, rules=rules,
            orders=[{"request_id": "o3", "symbol": "600519.SH", "side": 2, "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-07 09:30:00")
    assert "600519.SH" not in rt.positions  # 全部卖出，持仓清空
    # 11 卖 / 10 买，有正收益
    fin = finalize(rt, calendar=[20260506, 20260507])
    assert fin["realized_pnl"] > 0


def test_monotonic_clock(tmp_path):
    rules = AShareRuleEngine()
    rt = _runtime(sim_time=pd.Timestamp("2026-05-06 10:00:00"))
    with pytest.raises(ValueError):
        advance(rt, _frame([]), rules=rules, to="2026-05-06 09:00:00")  # 回到过去


def test_next_bar_parks_then_fills(tmp_path):
    rules = AShareRuleEngine()
    rt = _runtime(fill_timing="next_bar", sim_time=pd.Timestamp("2026-05-06 09:30:00"), current_date_key=20260506)
    frame = _frame([
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506),
        _bar("600519.SH", "2026-05-06 09:31:00", 20260506),
    ])
    # 在 09:30 决策（next_bar）→ 应停泊到 09:31 成交，09:30 不成交
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o1", "symbol": "600519.SH", "side": 1, "quantity": 100}],
            to="2026-05-06 09:31:00")
    assert rt.positions.get("600519.SH", Position()).quantity == 100
    # 成交发生在 09:31（next bar），不是 09:30
    assert rt.trades and rt.trades[0]["trade_id"]


def test_account_context_shape(tmp_path):
    rules = AShareRuleEngine()
    rt = _runtime()
    ctx = advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
                  rules=rules, to="2026-05-06 09:30:00")
    assert set(ctx) >= {"sim_time", "cash", "market_value", "nav", "positions", "open_orders"}
    assert ctx["nav"] == 1_000_000.0  # 没下单，净值=现金


def test_cancel_pending_order():
    """撤单：停泊的 next_bar 单在成交前撤掉 → 推进越过目标 bar 也不成交。"""
    rules = AShareRuleEngine()
    rt = _runtime(fill_timing="next_bar", sim_time=pd.Timestamp("2026-05-06 09:30:00"), current_date_key=20260506)
    frame = _frame([
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506),
        _bar("600519.SH", "2026-05-06 09:31:00", 20260506),
    ])
    # 09:30 下 next_bar 单 → 停泊到 09:31；to=当前(09:30) 不推进 → 仍挂着
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o1", "symbol": "600519.SH", "side": 1, "quantity": 100}],
            to="2026-05-06 09:30:00")
    assert len(rt.open_orders) == 1
    # 撤单（撤单-only：to=当前 sim_time）
    advance(rt, frame, rules=rules, cancel=["o1"], to="2026-05-06 09:30:00")
    assert rt.last_cancelled == ["o1"]
    assert len(rt.open_orders) == 0
    # 推进越过 09:31 → 不成交（已撤）
    advance(rt, frame, rules=rules, to="2026-05-06 09:31:00")
    assert len(rt.trades) == 0
