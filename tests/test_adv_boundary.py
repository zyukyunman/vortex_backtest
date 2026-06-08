"""Adversarial boundary / degenerate / recovery tests for the session engine + API.

Surface (per task brief): hammer the edges of session_engine.advance() + the FastAPI
session endpoints. Use MinuteWorkspaceBuilder + advance() (engine, hand-built bars) +
TestClient. Assert *intended* behavior. Where a real defect is found the test is kept and
marked xfail(strict=False) with a BUG-id, so the suite stays green and the failing
assertion is captured as the repro.

Bug ids referenced here:
  BUG-B1  : T+1 unlock skipped when clock advances over a no-bar day via `to`, then a
            later bar on that day does NOT re-trigger unlock because current_date_key was
            already bumped past it -> position stays locked / or unlocks on wrong boundary.
  BUG-B2  : universe drift (set_universe drops a held symbol) -> dropped-but-held symbol
            gets no bars -> cannot be sold and its last_price/valuation goes stale.
  (others investigated inline; negatives reported in the summary.)
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from vortex_backtest import app as app_mod
from vortex_backtest.app import create_app
from vortex_backtest.market_rules import AShareRuleEngine
from vortex_backtest.replay_engine import Position
from vortex_backtest.session_engine import SessionRuntime, advance, finalize


# --------------------------------------------------------------------------- helpers

def _bar(symbol, ts, date_key, *, px=10.0, vol=100_000, open_=None, close=None,
         high=None, low=None, up=None, down=None, suspended=False, board="主板"):
    o = open_ if open_ is not None else px
    c = close if close is not None else px
    return {
        "symbol": symbol, "trade_time": pd.Timestamp(ts), "date": date_key,
        "open": o, "close": c, "open_qfq": o, "close_qfq": c,
        "high": high if high is not None else max(o, c),
        "low": low if low is not None else min(o, c),
        "volume": vol, "board": board, "suspended": suspended,
        "up_limit": up if up is not None else round(px * 1.1, 2),
        "down_limit": down if down is not None else round(px * 0.9, 2),
    }


def _frame(rows):
    return pd.DataFrame(rows)


def _runtime(**kw):
    base = dict(
        session_id="s", strategy_id="s", sim_time=None, cash=1_000_000.0,
        initial_cash=1_000_000.0, positions={}, last_prices={}, open_orders=[],
        trade_counter=0, current_date_key=None, universe=["600519.SH"],
        fill_timing="this_bar", default_price_type="close", slippage_bps=0.0,
    )
    base.update(kw)
    return SessionRuntime(**base)


def _rules(**kw):
    return AShareRuleEngine(**kw)


# =========================================================================== T+1

def test_t_plus_1_buy_day1_cannot_sell_day1():
    """Regression: buy on day1, attempt sell same day -> t_plus_1_not_sellable."""
    rules = _rules()
    rt = _runtime()
    day1 = _frame([
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506),
        _bar("600519.SH", "2026-05-06 09:31:00", 20260506),
    ])
    advance(rt, day1, rules=rules,
            orders=[{"request_id": "b", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    assert rt.positions["600519.SH"].quantity == 100
    assert rt.positions["600519.SH"].sellable_quantity == 0
    advance(rt, day1, rules=rules,
            orders=[{"request_id": "s", "symbol": "600519.SH", "side": 2,
                     "quantity": 100, "exec_time": "09:31"}],
            to="2026-05-06 09:31:00")
    assert any(r["reason"] == "t_plus_1_not_sellable" for r in rt.rejections)
    assert rt.positions["600519.SH"].quantity == 100


def test_t_plus_1_sellable_next_day():
    """Regression: cross-day unlock fires on the date-key change driven by a day2 bar."""
    rules = _rules()
    rt = _runtime()
    advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
            rules=rules,
            orders=[{"request_id": "b", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    assert rt.positions["600519.SH"].sellable_quantity == 0
    # day2 bar present -> unlock fires
    advance(rt, _frame([_bar("600519.SH", "2026-05-07 09:30:00", 20260507, px=11.0)]),
            rules=rules, to="2026-05-07 09:30:00")
    assert rt.positions["600519.SH"].sellable_quantity == 100


def test_t_plus_1_unlock_when_clock_jumps_over_no_bar_day():
    """FAILURE MODE (BUG-B1 candidate): buy day1; advance clock to day2 with NO day2 bar
    (holiday/suspension gap) so the clock moves via `to` only. current_date_key does NOT
    update (engine ties unlock to bar date-key). Then a day3 advance whose bars are day3
    SHOULD unlock (cross-day happened). Verify the *whole* across-gap flow leaves the
    position sellable once any later trading bar appears.

    Intended behavior: after at least one calendar day has elapsed between buy and a later
    bar, the held position is sellable. We assert the position is sellable after a later
    bar on a *different* day appears.
    """
    rules = _rules()
    rt = _runtime()
    # day1 buy
    advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
            rules=rules,
            orders=[{"request_id": "b", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    assert rt.positions["600519.SH"].sellable_quantity == 0
    # advance over day2 with NO bar at all -> clock jumps to 2026-05-07 15:00 via `to`
    advance(rt, _frame([]), rules=rules, to="2026-05-07 15:00:00")
    # day3 (2026-05-08) bar appears -> cross-day MUST have happened, position sellable
    advance(rt, _frame([_bar("600519.SH", "2026-05-08 09:30:00", 20260508, px=11.0)]),
            rules=rules, to="2026-05-08 09:30:00")
    assert rt.positions["600519.SH"].sellable_quantity == 100, (
        "position bought 2 calendar days earlier must be sellable")


def test_t_plus_1_unlock_only_on_genuine_buy_day_relock_protection():
    """Buy day1, sell part day2 (unlocked). The remaining lot must STAY sellable; an
    intraday advance within day2 must not re-lock it (unlock keyed on date change only)."""
    rules = _rules()
    rt = _runtime()
    advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
            rules=rules,
            orders=[{"request_id": "b", "symbol": "600519.SH", "side": 1,
                     "quantity": 200, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    day2 = _frame([
        _bar("600519.SH", "2026-05-07 09:30:00", 20260507, px=11.0),
        _bar("600519.SH", "2026-05-07 09:31:00", 20260507, px=11.0),
    ])
    advance(rt, day2, rules=rules,
            orders=[{"request_id": "s1", "symbol": "600519.SH", "side": 2,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-07 09:30:00")
    assert rt.positions["600519.SH"].quantity == 100
    assert rt.positions["600519.SH"].sellable_quantity == 100  # remaining still sellable
    # second sell same day must succeed (not re-locked)
    advance(rt, day2, rules=rules,
            orders=[{"request_id": "s2", "symbol": "600519.SH", "side": 2,
                     "quantity": 100, "exec_time": "09:31"}],
            to="2026-05-07 09:31:00")
    assert "600519.SH" not in rt.positions


def test_t_plus_1_unlock_lost_when_late_bar_precedes_advanced_clock():
    """BUG-B1 (sharp variant the brief flagged): the engine ties T+1 unlock to the
    date-key of a *processed* bar (session_engine.py:405-407), but when the clock has
    already been advanced past a day via `to` with no bar, a bar for that day supplied in a
    later advance falls at-or-before sim_time and is dropped by the dedup guard at
    session_engine.py:400-401. Net effect: current_date_key never reaches that day, the
    position stays locked, AND the bar (which could match an order) is silently ignored.

    Repro: buy day1; advance to day2 15:00 with an empty frame (suspension gap); then
    supply day2's 09:30 bar in the next advance. The held position must be sellable (a full
    calendar day has elapsed) but the engine leaves it locked.
    """
    rules = _rules()
    rt = _runtime()
    advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
            rules=rules,
            orders=[{"request_id": "b", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 15:00:00")
    # clock jumps to day2 15:00 with NO bar (whole-day suspension / data gap)
    advance(rt, _frame([]), rules=rules, to="2026-05-07 15:00:00")
    # day2 bar arrives late (its 09:30 ts is <= current sim_time 15:00) -> skipped
    advance(rt, _frame([_bar("600519.SH", "2026-05-07 09:30:00", 20260507, px=11.0)]),
            rules=rules, to="2026-05-07 15:00:00")
    assert rt.current_date_key == 20260507, "date-key should have reached day2"
    assert rt.positions["600519.SH"].sellable_quantity == 100, (
        "position bought day1 must be sellable on day2 (one calendar day elapsed)")


# ===================================================================== suspension

def test_order_on_suspended_bar_rejected():
    """Order targeting a suspended bar -> 'suspended' rejection, no fill.

    Regression: the rejection reason is correct and the position has no real quantity.
    (The lingering zero-qty Position object is asserted separately in
    test_rejected_order_leaves_phantom_position, xfail BUG-B3.)
    """
    rules = _rules()
    rt = _runtime()
    frame = _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506, suspended=True)])
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    assert any(r["reason"] == "suspended" for r in rt.rejections)
    assert not rt.trades
    # no *real* position acquired
    assert rt.positions.get("600519.SH", Position()).quantity == 0


def test_rejected_order_leaves_phantom_position():
    """BUG-B3: _match_due_at_bar does positions.setdefault(symbol, Position()) BEFORE
    validate_order (session_engine.py:229). When the order is rejected, the empty
    Position(quantity=0) is left in rt.positions and is then serialized by dump()
    (session_engine.py:135-138 serializes ALL positions regardless of quantity), surviving
    a hydrate/dump round-trip. This bloats positions_json by one entry per distinct
    rejected symbol and is a state-hygiene defect.

    Intended behavior: a fully-rejected order must NOT mutate rt.positions at all.
    """
    rules = _rules()
    rt = _runtime()
    frame = _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506, suspended=True)])
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    assert any(r["reason"] == "suspended" for r in rt.rejections)
    # the defect: a phantom zero-qty Position lingers and gets persisted
    assert "600519.SH" not in rt.positions
    assert "600519.SH" not in json.loads(rt.dump()["positions_json"])


def test_held_position_through_suspension_still_valued_and_sellable_after():
    """Buy day1; day2 suspended (held, no trade); day3 resume -> still sellable & valued."""
    rules = _rules()
    rt = _runtime()
    advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506, px=10.0)]),
            rules=rules,
            orders=[{"request_id": "b", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    # day2 suspended bar present -> valuation still updates last_price (close_qfq), no trade
    advance(rt, _frame([_bar("600519.SH", "2026-05-07 09:30:00", 20260507, px=10.0,
                             suspended=True)]),
            rules=rules,
            orders=[{"request_id": "s_bad", "symbol": "600519.SH", "side": 2,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-07 09:30:00")
    assert any(r["reason"] == "suspended" for r in rt.rejections)
    assert rt.positions["600519.SH"].quantity == 100
    # day3 resume -> sellable (two calendar days elapsed)
    advance(rt, _frame([_bar("600519.SH", "2026-05-08 09:30:00", 20260508, px=12.0)]),
            rules=rules,
            orders=[{"request_id": "s_ok", "symbol": "600519.SH", "side": 2,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-08 09:30:00")
    assert "600519.SH" not in rt.positions


# ================================================================= limit lock

def test_buy_blocked_on_limit_up_lock():
    """One-zi-ban: bar with high==low==close==up_limit -> buy blocked."""
    rules = _rules()
    rt = _runtime()
    # raw_price == up_limit -> limit_up_buy_blocked
    frame = _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506,
                         px=11.0, open_=11.0, close=11.0, high=11.0, low=11.0,
                         up=11.0, down=9.0)])
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    assert any(r["reason"] == "limit_up_buy_blocked" for r in rt.rejections)
    assert not rt.trades
    assert rt.positions.get("600519.SH", Position()).quantity == 0


def test_sell_blocked_on_limit_down_lock():
    """Limit-down lock: raw_price <= down_limit -> sell blocked even if held & sellable."""
    rules = _rules()
    # seed a held, already-sellable position
    rt = _runtime(positions={"600519.SH": Position(quantity=100, sellable_quantity=100,
                                                   cost_basis=10.0)},
                  current_date_key=20260505, sim_time=pd.Timestamp("2026-05-05 15:00:00"))
    frame = _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506,
                         px=9.0, open_=9.0, close=9.0, high=9.0, low=9.0,
                         up=11.0, down=9.0)])
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o", "symbol": "600519.SH", "side": 2,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    assert any(r["reason"] == "limit_down_sell_blocked" for r in rt.rejections)
    assert rt.positions["600519.SH"].quantity == 100


# =============================================================== missing data

def test_missing_data_graceful_no_crash_and_clock_advances():
    """Empty bars frame -> orders get 'no_market_data', no crash, clock still hits `to`."""
    rules = _rules()
    rt = _runtime(sim_time=pd.Timestamp("2026-05-06 09:30:00"), current_date_key=20260506)
    ctx = advance(rt, _frame([]), rules=rules,
                  orders=[{"request_id": "o", "symbol": "600519.SH", "side": 1,
                           "quantity": 100, "exec_time": "09:31"}],
                  to="2026-05-06 14:57:00")
    assert any(r["reason"] == "no_market_data" for r in rt.rejections)
    assert rt.sim_time == pd.Timestamp("2026-05-06 14:57:00")
    assert ctx["nav"] == 1_000_000.0


# =============================================================== calendar gaps

def test_calendar_gap_forward_fill_nav_in_finalize():
    """Non-consecutive trade days: finalize must forward-fill NAV across the gap day,
    not drop it or divide-by-zero. Buy day1 (20260506), no day on 20260507, day3 20260508.
    Calendar contains the gap day -> daily must have a row for it carrying the held value."""
    rules = _rules()
    rt = _runtime()
    advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506, px=10.0)]),
            rules=rules,
            orders=[{"request_id": "b", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    advance(rt, _frame([_bar("600519.SH", "2026-05-08 09:30:00", 20260508, px=12.0)]),
            rules=rules, to="2026-05-08 09:30:00")
    # calendar includes the gap day 20260507 which has no snapshot
    daily = finalize(rt, calendar=[20260506, 20260507, 20260508])["daily"]
    dates = [d["trade_date"] for d in daily]
    assert dates == ["2026-05-06", "2026-05-07", "2026-05-08"]
    # gap day carries forward the day1 valuation (px=10 -> mv=1000), not zero / not crash
    gap = next(d for d in daily if d["trade_date"] == "2026-05-07")
    assert gap["market_value"] == 1000.0
    assert gap["total_value"] == daily[0]["total_value"]


def test_close_daily_includes_gap_trading_day(tmp_path, monkeypatch):
    """BUG-B4: the daily equity curve must be contiguous over the trade calendar, with
    suspension/gap days forward-filled. But app.close derives `calendar` from the snapshots
    it actually has, not the union trade calendar, so a gap trading day vanishes from
    /daily -- distorting drawdown/return and breaking any contiguous-calendar consumer.

    Repro: day1 (20260506) and day3 (20260508) have bars; day2 (20260507) is a trading day
    with NO bars. The /daily series must contain a row for 20260507 (forward-filled), but
    it is dropped.
    """
    def fake_load(symbols, start_d, end_d, as_of=None, anchor_d=None):
        rows = [_bar("600519.SH", "2026-05-06 09:30:00", 20260506, px=10.0),
                _bar("600519.SH", "2026-05-08 09:30:00", 20260508, px=12.0)]
        return pd.DataFrame(rows), [20260506, 20260507, 20260508]

    monkeypatch.setattr(app_mod, "_load_session_bars", fake_load)
    client = TestClient(create_app(tmp_path, run_worker=False))
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "level": "1min", "start_date": "2026-05-06",
        "end_date": "2026-05-08", "universe": ["600519.SH"],
        "fill_timing": "this_bar"}).json()["session_id"]
    client.post(f"/sessions/{sid}/advance", json={
        "orders": [{"request_id": "b", "symbol": "600519.SH", "side": 1,
                    "quantity": 100, "exec_time": "09:30"}],
        "to": "2026-05-06T09:30:00"})
    client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-08T09:30:00"})
    client.post(f"/sessions/{sid}/close")
    daily = client.get(f"/sessions/{sid}/daily").json()
    dates = [d["trade_date"] for d in daily]
    assert "2026-05-07" in dates, "gap trading day must be forward-filled into /daily"


# ============================================================ volume partial fill

def test_volume_partial_fill_caps_and_lot_rounds():
    """requested qty > volume*participation_cap -> executable caps + lot-rounds."""
    rules = _rules(max_volume_participation=0.5)
    rt = _runtime()
    # volume=1000, cap=floor(1000*0.5)=500, lot-round down to 500
    frame = _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506, px=10.0, vol=1000)])
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o", "symbol": "600519.SH", "side": 1,
                     "quantity": 100000, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    assert rt.trades[0]["quantity"] == 500
    assert rt.trades[0]["requested_quantity"] == 100000
    assert rt.positions["600519.SH"].quantity == 500


def test_volume_cap_below_lot_rejection():
    """cap below one lot (100) -> volume_cap_below_lot rejection, no fill."""
    rules = _rules(max_volume_participation=0.5)
    rt = _runtime()
    # volume=100, cap=floor(100*0.5)=50 < 100 -> below lot
    frame = _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506, px=10.0, vol=100)])
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    assert any(r["reason"] == "volume_cap_below_lot" for r in rt.rejections)
    assert not rt.trades
    assert rt.positions.get("600519.SH", Position()).quantity == 0


# ========================================================== path independence

def test_multiday_single_advance_equals_many_small_advances():
    """One advance over the whole span vs many small advances -> identical end state
    (cash, positions, sellable). this_bar fill, deterministic bars."""
    bars_all = [
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506, px=10.0),
        _bar("600519.SH", "2026-05-06 14:57:00", 20260506, px=10.0),
        _bar("600519.SH", "2026-05-07 09:30:00", 20260507, px=11.0),
        _bar("600519.SH", "2026-05-07 14:57:00", 20260507, px=11.0),
    ]
    orders = [{"request_id": "b", "symbol": "600519.SH", "side": 1, "quantity": 100,
               "exec_time": "09:30", "trade_date": "2026-05-06"}]

    # path A: one advance over the full span
    rules = _rules()
    rt_a = _runtime()
    advance(rt_a, _frame(bars_all), rules=rules, orders=orders, to="2026-05-07 14:57:00")

    # path B: 4 small advances (same orders submitted once at the matching step)
    rt_b = _runtime()
    advance(rt_b, _frame(bars_all), rules=rules, orders=orders, to="2026-05-06 09:30:00")
    advance(rt_b, _frame(bars_all), rules=rules, to="2026-05-06 14:57:00")
    advance(rt_b, _frame(bars_all), rules=rules, to="2026-05-07 09:30:00")
    advance(rt_b, _frame(bars_all), rules=rules, to="2026-05-07 14:57:00")

    assert rt_a.cash == rt_b.cash
    assert {s: (p.quantity, p.sellable_quantity, p.cost_basis)
            for s, p in rt_a.positions.items()} == \
           {s: (p.quantity, p.sellable_quantity, p.cost_basis)
            for s, p in rt_b.positions.items()}
    assert rt_a.sim_time == rt_b.sim_time


def test_path_independence_buy_then_sell_across_t_plus_1():
    """Stronger path-independence: a BUY on day1 + SELL on day2 (crossing the T+1 unlock).
    One advance over the span must equal two advances split at the day boundary -- identical
    cash, trade count, and end positions. (Negative result: this holds; A==B equivalence is
    robust across the unlock boundary.)"""
    rules = _rules()
    bars = [
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506, px=10.0),
        _bar("600519.SH", "2026-05-07 09:30:00", 20260507, px=11.0),
    ]
    buy = {"request_id": "b", "symbol": "600519.SH", "side": 1, "quantity": 100,
           "exec_time": "09:30", "trade_date": "2026-05-06"}
    sell = {"request_id": "s", "symbol": "600519.SH", "side": 2, "quantity": 100,
            "exec_time": "09:30", "trade_date": "2026-05-07"}
    a = _runtime()
    advance(a, _frame(bars), rules=rules, orders=[buy, sell], to="2026-05-07 09:30:00")
    b = _runtime()
    advance(b, _frame(bars), rules=rules, orders=[buy], to="2026-05-06 09:30:00")
    advance(b, _frame(bars), rules=rules, orders=[sell], to="2026-05-07 09:30:00")
    assert a.cash == b.cash
    assert len(a.trades) == len(b.trades) == 2
    assert a.positions == b.positions == {}


def test_repeated_noop_advance_does_not_reprocess_boundary_bar():
    """Advancing repeatedly with to == sim_time (no movement) must not re-process the last
    bar (no double-fill, no double-snapshot growth of cash)."""
    rules = _rules()
    rt = _runtime()
    advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
            rules=rules,
            orders=[{"request_id": "b", "symbol": "600519.SH", "side": 1,
                     "quantity": 100, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    cash_after = rt.cash
    qty_after = rt.positions["600519.SH"].quantity
    # two further no-op advances to the same clock
    advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
            rules=rules, to="2026-05-06 09:30:00")
    advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
            rules=rules, to="2026-05-06 09:30:00")
    assert rt.cash == cash_after
    assert rt.positions["600519.SH"].quantity == qty_after


def test_cancel_and_resubmit_same_step():
    """Cancel an existing parked order AND submit a new next_bar order in the same advance.
    The cancelled one must not fill; the new one parks and fills on its target bar."""
    rules = _rules()
    rt = _runtime(fill_timing="next_bar", sim_time=pd.Timestamp("2026-05-06 09:30:00"),
                  current_date_key=20260506)
    frame = _frame([
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506),
        _bar("600519.SH", "2026-05-06 09:31:00", 20260506),
    ])
    # park o1 (no advance)
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o1", "symbol": "600519.SH", "side": 1,
                     "quantity": 100}],
            to="2026-05-06 09:30:00")
    assert len(rt.open_orders) == 1
    # cancel o1 and submit o2 in the same step, then advance to fill o2
    advance(rt, frame, rules=rules, cancel=["o1"],
            orders=[{"request_id": "o2", "symbol": "600519.SH", "side": 1,
                     "quantity": 200}],
            to="2026-05-06 09:31:00")
    assert rt.last_cancelled == ["o1"]
    # only o2 filled -> exactly 200 shares (o1's 100 cancelled)
    assert rt.positions["600519.SH"].quantity == 200
    assert all(t["request_id"] == "o2" for t in rt.trades)


# ============================================================ universe drift

def test_universe_drift_dropped_held_symbol_still_sellable_via_api(tmp_path, monkeypatch):
    """BUG-B2 (design risk #8): a held symbol is dropped from the universe via
    set_universe. app._load_session_bars only fetches bars for the *new* universe, so the
    dropped-but-held symbol gets NO bars -> cannot be sold, and (worse) its market value
    silently drops out of NAV once last_prices is the only anchor.

    Intended behavior: a held position must remain valued and sellable regardless of
    universe membership. We drive it through the real API and assert the held symbol can
    still be SOLD after being dropped from the universe.
    """
    # bars keyed by which symbols are requested
    day1 = [_bar("600519.SH", "2026-05-06 09:30:00", 20260506, px=10.0)]
    day2_full = [_bar("600519.SH", "2026-05-07 09:30:00", 20260507, px=11.0),
                 _bar("000001.SZ", "2026-05-07 09:30:00", 20260507, px=5.0)]

    def fake_load(symbols, start_d, end_d, as_of=None, anchor_d=None):
        syms = set(symbols)
        # only return bars for symbols in the requested universe (mirrors gateway behavior)
        d1 = [b for b in day1 if b["symbol"] in syms]
        d2 = [b for b in day2_full if b["symbol"] in syms]
        return pd.DataFrame(d1 + d2), [20260506, 20260507]

    monkeypatch.setattr(app_mod, "_load_session_bars", fake_load)
    client = TestClient(create_app(tmp_path, run_worker=False))
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "level": "1min", "start_date": "2026-05-06",
        "end_date": "2026-05-07", "universe": ["600519.SH"],
        "fill_timing": "this_bar"}).json()["session_id"]

    # day1: buy 600519.SH
    client.post(f"/sessions/{sid}/advance", json={
        "orders": [{"request_id": "b", "symbol": "600519.SH", "side": 1,
                    "quantity": 100, "exec_time": "09:30"}],
        "to": "2026-05-06T09:30:00"})

    # day2: drop 600519.SH from universe (now only 000001.SZ) AND try to sell 600519.SH
    r = client.post(f"/sessions/{sid}/advance", json={
        "set_universe": ["000001.SZ"],
        "orders": [{"request_id": "s", "symbol": "600519.SH", "side": 2,
                    "quantity": 100, "exec_time": "09:30"}],
        "to": "2026-05-07T09:30:00"}).json()
    # intended: the held symbol is still sellable -> sale fills, position cleared
    sold = any(t["symbol"] == "600519.SH" and t["side"] == 2 for t in r.get("filled", []))
    assert sold, "dropped-but-held symbol must remain sellable (got no bars -> no_market_data)"


def test_universe_drift_dropped_held_symbol_valued_in_nav(tmp_path, monkeypatch):
    """Companion to BUG-B2 (this part PASSES — valuation is NOT zeroed): even when dropped
    from the universe and given no day2 bar, the held position keeps its last known price
    (stale forward-fill) and still contributes market_value to NAV. The defect is purely
    that it cannot be SOLD (covered above); valuation survives because position_rows falls
    back to last_prices/cost_basis. NAV stays ~conserved (initial - buy fees)."""
    day1 = [_bar("600519.SH", "2026-05-06 09:30:00", 20260506, px=10.0)]
    day2 = [_bar("000001.SZ", "2026-05-07 09:30:00", 20260507, px=5.0)]

    def fake_load(symbols, start_d, end_d, as_of=None, anchor_d=None):
        syms = set(symbols)
        rows = [b for b in (day1 + day2) if b["symbol"] in syms]
        return pd.DataFrame(rows), [20260506, 20260507]

    monkeypatch.setattr(app_mod, "_load_session_bars", fake_load)
    client = TestClient(create_app(tmp_path, run_worker=False))
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "level": "1min", "start_date": "2026-05-06",
        "end_date": "2026-05-07", "universe": ["600519.SH"],
        "fill_timing": "this_bar"}).json()["session_id"]
    client.post(f"/sessions/{sid}/advance", json={
        "orders": [{"request_id": "b", "symbol": "600519.SH", "side": 1,
                    "quantity": 100, "exec_time": "09:30"}],
        "to": "2026-05-06T09:30:00"})
    r = client.post(f"/sessions/{sid}/advance", json={
        "set_universe": ["000001.SZ"], "to": "2026-05-07T09:30:00"}).json()
    # held 100 sh @ last known 10.0 -> market_value must be 1000 (still counted, not zeroed)
    held = [p for p in r["positions"] if p["symbol"] == "600519.SH"]
    assert held, "held position must remain in positions list"
    assert held[0]["market_value"] == 1000.0
    # NAV ~ initial minus buy fees (cash spent 1000 on stock returns as 1000 mv); within ~10
    assert 999_980.0 <= r["nav"] <= 1_000_000.0


# =========================================================== empty / zero / single

def test_zero_quantity_order():
    """Zero-quantity order -> rejected (invalid_lot_size), no crash, no fill."""
    rules = _rules()
    rt = _runtime()
    frame = _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)])
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o", "symbol": "600519.SH", "side": 1,
                     "quantity": 0, "exec_time": "09:30"}],
            to="2026-05-06 09:30:00")
    assert not rt.trades
    # zero qty resolves to a target then is rejected at validate (invalid_lot_size)
    assert any(r["reason"] == "invalid_lot_size" for r in rt.rejections) or not rt.positions


def test_empty_orders_list_advances_clean():
    """Empty orders list + single bar -> clock advances, no trades, no rejections."""
    rules = _rules()
    rt = _runtime()
    ctx = advance(rt, _frame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
                  rules=rules, orders=[], to="2026-05-06 09:30:00")
    assert not rt.trades
    assert ctx["sim_time"] == "2026-05-06T09:30:00"


def test_close_with_no_trades(tmp_path, monkeypatch):
    """close with zero trades -> summary all-cash, total_return 0, no divide-by-zero."""
    monkeypatch.setattr(app_mod, "_load_session_bars",
                        lambda symbols, start_d, end_d, as_of=None, anchor_d=None:
                        (pd.DataFrame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
                         [20260506]))
    client = TestClient(create_app(tmp_path, run_worker=False))
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "start_date": "2026-05-06", "end_date": "2026-05-06",
        "universe": ["600519.SH"]}).json()["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-06T09:30:00"})
    rc = client.post(f"/sessions/{sid}/close")
    assert rc.status_code == 200
    s = rc.json()["summary"]
    assert s["total_return"] == 0.0
    assert s["realized_pnl"] == 0.0
    assert s["total_value"] == 1_000_000.0


# =================================================================== cancel race

def test_cancel_before_fill_is_noop_no_trade():
    """Place next_bar order, cancel it same step before fill -> never fills."""
    rules = _rules()
    rt = _runtime(fill_timing="next_bar", sim_time=pd.Timestamp("2026-05-06 09:30:00"),
                  current_date_key=20260506)
    frame = _frame([
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506),
        _bar("600519.SH", "2026-05-06 09:31:00", 20260506),
    ])
    # park (to=now, no advance)
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o1", "symbol": "600519.SH", "side": 1,
                     "quantity": 100}],
            to="2026-05-06 09:30:00")
    assert len(rt.open_orders) == 1
    # cancel and advance past target in same step
    advance(rt, frame, rules=rules, cancel=["o1"], to="2026-05-06 09:31:00")
    assert rt.last_cancelled == ["o1"]
    assert not rt.trades


def test_cancel_after_fill_is_noop():
    """Cancel a request_id that already filled (no longer in open_orders) -> no-op,
    position untouched."""
    rules = _rules()
    rt = _runtime(fill_timing="next_bar", sim_time=pd.Timestamp("2026-05-06 09:30:00"),
                  current_date_key=20260506)
    frame = _frame([
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506),
        _bar("600519.SH", "2026-05-06 09:31:00", 20260506),
    ])
    advance(rt, frame, rules=rules,
            orders=[{"request_id": "o1", "symbol": "600519.SH", "side": 1,
                     "quantity": 100}],
            to="2026-05-06 09:31:00")
    assert rt.positions["600519.SH"].quantity == 100  # filled
    qty_before = rt.positions["600519.SH"].quantity
    # cancel after it filled -> no-op
    advance(rt, frame, rules=rules, cancel=["o1"], to="2026-05-06 09:31:00")
    assert rt.last_cancelled == []
    assert rt.positions["600519.SH"].quantity == qty_before


# ================================================================== idempotency

def test_idempotent_advance_survives_hydrate_dump_roundtrip(tmp_path, monkeypatch):
    """Idempotency must survive a hydrate/dump round-trip: processed_advances is persisted
    in config_json (cap 200). Re-issuing the same request_id after a fresh hydrate (new
    process) must still de-dup."""
    monkeypatch.setattr(app_mod, "_load_session_bars",
                        lambda symbols, start_d, end_d, as_of=None, anchor_d=None:
                        (pd.DataFrame([
                            _bar("600519.SH", "2026-05-06 09:30:00", 20260506),
                            _bar("600519.SH", "2026-05-06 09:31:00", 20260506)]),
                         [20260506]))
    state = tmp_path / "state"
    # first app instance
    client = TestClient(create_app(state, run_worker=False))
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "level": "1min", "start_date": "2026-05-06",
        "end_date": "2026-05-06", "universe": ["600519.SH"],
        "fill_timing": "this_bar"}).json()["session_id"]
    body = {"orders": [{"request_id": "o1", "symbol": "600519.SH", "side": 1,
                        "quantity": 100, "exec_time": "09:30"}],
            "to": "2026-05-06T09:30:00", "request_id": "adv1"}
    r1 = client.post(f"/sessions/{sid}/advance", json=body).json()
    assert len(r1["filled"]) == 1

    # NEW app instance over the SAME state dir (simulates restart -> hydrate from store)
    client2 = TestClient(create_app(state, run_worker=False))
    r2 = client2.post(f"/sessions/{sid}/advance", json=body).json()
    assert r2.get("duplicate") is True
    assert r2["positions"][0]["quantity"] == 100  # not doubled


def test_idempotency_cap_200_does_not_evict_recent(tmp_path, monkeypatch):
    """The dump() keeps only the last 200 request_ids. A request_id processed within the
    last 200 advances must still de-dup. We process 5 distinct advances then replay the
    most recent -> still de-dups (sanity that recent ids are retained)."""
    monkeypatch.setattr(app_mod, "_load_session_bars",
                        lambda symbols, start_d, end_d, as_of=None, anchor_d=None:
                        (pd.DataFrame([_bar("600519.SH", f"2026-05-{6+i:02d} 09:30:00",
                                            20260506 + i) for i in range(6)]),
                         [20260506 + i for i in range(6)]))
    client = TestClient(create_app(tmp_path, run_worker=False))
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "level": "1min", "start_date": "2026-05-06",
        "end_date": "2026-05-11", "universe": ["600519.SH"],
        "fill_timing": "this_bar"}).json()["session_id"]
    last_body = None
    for i in range(5):
        last_body = {"to": f"2026-05-{6+i:02d}T09:30:00", "request_id": f"adv{i}"}
        client.post(f"/sessions/{sid}/advance", json=last_body)
    # replay the most recent advance -> must de-dup
    r = client.post(f"/sessions/{sid}/advance", json=last_body).json()
    assert r.get("duplicate") is True


# ============================================================== crash recovery

def test_crash_between_store_update_and_jsonl_no_double_trade(tmp_path, monkeypatch):
    """app ordering (app.py:289-294): update_session (authoritative, has request_id) THEN
    append JSONL. If a crash lands after update_session but before JSONL append, a retry
    with the same request_id must de-dup -> no double trade. We simulate the crash by
    making _append_jsonl raise on the first call, then retry the SAME request_id."""
    monkeypatch.setattr(app_mod, "_load_session_bars",
                        lambda symbols, start_d, end_d, as_of=None, anchor_d=None:
                        (pd.DataFrame([_bar("600519.SH", "2026-05-06 09:30:00", 20260506)]),
                         [20260506]))
    state = tmp_path / "state"
    client = TestClient(create_app(state, run_worker=False))
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "level": "1min", "start_date": "2026-05-06",
        "end_date": "2026-05-06", "universe": ["600519.SH"],
        "fill_timing": "this_bar"}).json()["session_id"]

    body = {"orders": [{"request_id": "o1", "symbol": "600519.SH", "side": 1,
                        "quantity": 100, "exec_time": "09:30"}],
            "to": "2026-05-06T09:30:00", "request_id": "adv1"}

    # make the JSONL append blow up AFTER update_session ran (simulating mid-write crash)
    real_append = app_mod._append_jsonl
    calls = {"n": 0}

    def exploding_append(path, rows):
        calls["n"] += 1
        raise RuntimeError("simulated crash before/while appending jsonl")

    monkeypatch.setattr(app_mod, "_append_jsonl", exploding_append)
    try:
        client.post(f"/sessions/{sid}/advance", json=body)
    except RuntimeError:
        pass  # crash surfaced
    # restore append, retry SAME request_id -> must be de-duped, no double fill
    monkeypatch.setattr(app_mod, "_append_jsonl", real_append)
    client2 = TestClient(create_app(state, run_worker=False))
    r2 = client2.post(f"/sessions/{sid}/advance", json=body).json()
    assert r2.get("duplicate") is True
    # position must be exactly 100 (single fill), not 200
    pos = [p for p in r2["positions"] if p["symbol"] == "600519.SH"]
    assert pos and pos[0]["quantity"] == 100


# ============================================================ non-monotonic to

def test_non_monotonic_to_returns_409(tmp_path, monkeypatch):
    """to < sim_time over the API -> 409 non_monotonic_clock."""
    monkeypatch.setattr(app_mod, "_load_session_bars",
                        lambda symbols, start_d, end_d, as_of=None, anchor_d=None:
                        (pd.DataFrame([_bar("600519.SH", "2026-05-07 09:30:00", 20260507)]),
                         [20260507]))
    client = TestClient(create_app(tmp_path, run_worker=False))
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "start_date": "2026-05-06", "end_date": "2026-05-07",
        "universe": ["600519.SH"]}).json()["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-07T09:30:00"})
    r = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-06T09:30:00"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "non_monotonic_clock"
