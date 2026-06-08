"""Adversarial determinism / byte-for-byte reproducibility tests (attack surface: Determinism).

Property under attack (design/18 leans on this everywhere — idempotent advance, crash
recovery, golden-oracle equivalence): *same inputs → same outputs, byte for byte*.

We attack three layers:
  1. END-TO-END: run the SAME multi-day session twice through the FastAPI TestClient with a
     fresh tmp state dir each time, and assert trades.jsonl / rejections.jsonl / snapshots.jsonl /
     summary.json are sha256-identical between the two runs (true reproducibility regression).
  2. ENGINE-LEVEL ORDER SENSITIVITY: feed `advance()` the SAME logical inputs but with the
     physical row order permuted (within a single trade_time bar; dividend list order). The
     gateway sorts minute bars by [trade_time, symbol] (gateway_adapter.py:161) but the engine
     itself does NOT re-key by symbol, and the local-fallback loader / direct callers are not
     guaranteed to. Any field whose *serialized bytes* depend on that physical order is a
     reproducibility hazard.
  3. SERIALIZATION FIXED POINT: hydrate → dump → hydrate must be idempotent (no float reformat,
     no key reorder, no field drift).

Bugs exposed are marked xfail(strict=False) with the BUG id so the suite stays green; the exact
failing assertion is the repro. Passing tests are kept as regression coverage.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from vortex_backtest.app import create_app
from vortex_backtest.market_rules import AShareRuleEngine
from vortex_backtest.replay_engine import Position
from vortex_backtest.session_engine import (
    SessionRuntime,
    advance,
    apply_corporate_actions,
)


# --------------------------------------------------------------------------- helpers


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rt(positions=None, *, cash=1_000_000.0, sim_time=None, universe=None, **kw) -> SessionRuntime:
    base = dict(
        session_id="s",
        strategy_id="s",
        sim_time=sim_time,
        cash=cash,
        initial_cash=1_000_000.0,
        positions=positions if positions is not None else {},
        last_prices={},
        open_orders=[],
        trade_counter=0,
        current_date_key=None,
        universe=universe if universe is not None else [],
        fill_timing="next_bar",
        default_price_type="close",
        slippage_bps=0.0,
    )
    base.update(kw)
    return SessionRuntime(**base)


def _bar(symbol: str, ts: str, date_key: int, open_: float, close: float, *, volume: int = 1_000_000):
    """A fully-populated RAW rich bar (open_qfq==open since adj=1)."""
    return {
        "symbol": symbol,
        "trade_time": ts,
        "date": date_key,
        "open": open_,
        "high": max(open_, close),
        "low": min(open_, close),
        "close": close,
        "open_qfq": open_,
        "close_qfq": close,
        "volume": volume,
        "board": "主板",
        "suspended": False,
        "is_st": False,
        "up_limit": round(open_ * 1.1, 2),
        "down_limit": round(open_ * 0.9, 2),
    }


def _div(symbol, ex_date, *, cash_div_tax=0.0, stk_div=0.0, stk_bo_rate=0.0, stk_co_rate=0.0):
    return {
        "symbol": symbol,
        "ex_date": ex_date,
        "cash_div_tax": cash_div_tax,
        "stk_div": stk_div,
        "stk_bo_rate": stk_bo_rate,
        "stk_co_rate": stk_co_rate,
    }


# =========================================================================== 1. END-TO-END
# Two independent runs of the SAME multi-day session through the HTTP layer must produce
# byte-identical artifact files. This is the headline reproducibility guarantee.


def _build_workspace(builder):
    """Two clean tradeable symbols across two trading days (gap-free, real movement)."""
    (
        builder
        .day("2026-01-05", "000001.SZ", open=10.0, close=10.2, volume=1_000_000)
        .day("2026-01-05", "600519.SH", open=100.0, close=101.0, volume=1_000_000)
        .day("2026-01-06", "000001.SZ", open=10.2, close=10.5, volume=1_000_000)
        .day("2026-01-06", "600519.SH", open=101.0, close=102.0, volume=1_000_000)
    )
    return builder.build()


def _drive_session(client: TestClient, *, universe, orders_day1):
    client.post("/accounts", json={"account_id": "acc", "name": "n", "initial_cash": 1_000_000.0, "engine": "replay"})
    r = client.post(
        "/sessions",
        json={
            "account_id": "acc",
            "level": "1min",
            "start_date": "2026-01-05",
            "end_date": "2026-01-06",
            "universe": universe,
            "strategy_id": "det",
            "fill_timing": "next_bar",
        },
    )
    sid = r.json()["session_id"]
    # advance day 1 (submit orders), then day 2, then close
    client.post(f"/sessions/{sid}/advance", json={"to": "2026-01-05", "orders": orders_day1})
    client.post(f"/sessions/{sid}/advance", json={"to": "2026-01-06"})
    client.post(f"/sessions/{sid}/close")
    return sid


def _run_full(tmp_path: Path, workspace_root: Path, run_name: str, *, universe, orders) -> dict[str, str | None]:
    import os
    from contextlib import contextmanager

    @contextmanager
    def _env(**kv):
        """Set env vars hermetically and restore them — never leak global state to other tests."""
        old = {k: os.environ.get(k) for k in kv}
        try:
            for k, v in kv.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            yield
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    state = tmp_path / run_name
    # local-fallback loader (no VORTEX_DATA_URL): reads the synthetic parquet workspace
    with _env(VORTEX_WORKSPACE=str(workspace_root), VORTEX_DATA_URL=None):
        app = create_app(state_dir=state)
        client = TestClient(app)
        sid = _drive_session(client, universe=universe, orders_day1=orders)
    sdir = state / "reports" / "sessions" / sid
    return {
        "trades": _sha256(sdir / "trades.jsonl"),
        "rejections": _sha256(sdir / "rejections.jsonl"),
        "snapshots": _sha256(sdir / "snapshots.jsonl"),
        "summary": _sha256(sdir / "summary.json"),
        "_sid": sid,
        "_dir": str(sdir),
    }


def test_e2e_same_session_twice_is_byte_identical(tmp_path, workspace_builder):
    """REGRESSION: identical inputs, two fresh runs → identical artifact bytes (sha256)."""
    ws = _build_workspace(workspace_builder)
    orders = [
        {"symbol": "000001.SZ", "side": 1, "quantity": 1000, "request_id": "b1", "exec_time": "09:31:00"},
        {"symbol": "600519.SH", "side": 1, "quantity": 100, "request_id": "b2", "exec_time": "09:31:00"},
    ]
    uni = ["000001.SZ", "600519.SH"]
    a = _run_full(tmp_path, ws, "runA", universe=uni, orders=orders)
    b = _run_full(tmp_path, ws, "runB", universe=uni, orders=orders)
    # session_id is a random uuid; everything else must be byte-identical
    for key in ("trades", "rejections", "snapshots", "summary"):
        assert a[key] == b[key], f"{key}.jsonl/json differs between two identical runs"


def test_e2e_unsorted_universe_normalizes_identically(tmp_path, workspace_builder):
    """Universe given in a DIFFERENT order across two runs must still yield identical artifacts.

    A strategy may pass its universe in arbitrary order (or from a set()). The engine stores
    universe verbatim, but the produced trades/snapshots/summary must not depend on it.
    """
    ws = _build_workspace(workspace_builder)
    orders = [
        {"symbol": "000001.SZ", "side": 1, "quantity": 1000, "request_id": "b1", "exec_time": "09:31:00"},
        {"symbol": "600519.SH", "side": 1, "quantity": 100, "request_id": "b2", "exec_time": "09:31:00"},
    ]
    a = _run_full(tmp_path, ws, "uniA", universe=["000001.SZ", "600519.SH"], orders=orders)
    b = _run_full(tmp_path, ws, "uniB", universe=["600519.SH", "000001.SZ"], orders=orders)
    for key in ("trades", "rejections", "snapshots", "summary"):
        assert a[key] == b[key], f"{key} differs when only the universe order differs"


# =========================================================================== 2. ORDER SENSITIVITY
# Permute the *physical* row order of inputs while keeping the *logical* content identical.


def test_last_prices_serialization_independent_of_bar_row_order():
    """BUG-DET-1 repro: ``last_prices`` key order (hence ``config_json`` bytes) depends on the
    physical within-bar DataFrame row order.

    advance() updates ``rt.last_prices`` by iterating ``row_by_symbol.items()``
    (session_engine.py:408), whose key order == the input frame's row order within a single
    ``trade_time`` group (groupby(sort=True) preserves intra-group order). Two runs with the
    SAME bars but a different physical row order therefore persist byte-different ``config_json``
    → the saved session state is not byte-reproducible. dump() should sort last_prices keys.
    """
    rules = AShareRuleEngine()
    rows = [
        _bar("000001.SZ", "2026-01-05 09:31:00", 20260105, 10.0, 10.0),
        _bar("600519.SH", "2026-01-05 09:31:00", 20260105, 100.0, 100.0),
    ]
    rt_sorted = _rt(universe=["000001.SZ", "600519.SH"])
    advance(rt_sorted, pd.DataFrame(rows), rules=rules, to="2026-01-05 09:31:00")

    rt_rev = _rt(universe=["000001.SZ", "600519.SH"])
    advance(rt_rev, pd.DataFrame([rows[1], rows[0]]), rules=rules, to="2026-01-05 09:31:00")

    # cash/positions agree (logic is order-independent) ...
    assert rt_sorted.cash == rt_rev.cash
    # ... but the serialized state must also be byte-identical for reproducibility:
    assert rt_sorted.dump()["config_json"] == rt_rev.dump()["config_json"], (
        "config_json (last_prices) byte-diverges with input bar row order"
    )


def test_corporate_actions_list_order_independent_of_dividend_input_order():
    """BUG-DET-2 repro: the ``corporate_actions`` result list order (hence
    corporate_actions.jsonl bytes) depends on the physical order of the input ``dividends`` list
    for same-ex_date entries.

    apply_corporate_actions sorts only by ex_ts (session_engine.py:308). Python's sort is stable,
    so ties keep input order. The dividend list comes from load_dividends(), whose order ==
    gateway response row order (dedup via a dict, gateway_adapter.py:203-206) — not guaranteed
    sorted by symbol. Two runs with the same dividends in different row order emit a
    byte-different corporate_actions.jsonl. The sort key should be (ex_ts, symbol).
    """
    pos_a = {"000001.SZ": Position(100, 100, 10.0), "600519.SH": Position(100, 100, 10.0)}
    rt_a = _rt(pos_a, cash=0.0)
    applied_a = apply_corporate_actions(
        rt_a,
        [_div("000001.SZ", 20260608, cash_div_tax=1.0), _div("600519.SH", 20260608, cash_div_tax=2.0)],
        lower=None,
        upper=pd.Timestamp("2026-06-08"),
    )
    pos_b = {"000001.SZ": Position(100, 100, 10.0), "600519.SH": Position(100, 100, 10.0)}
    rt_b = _rt(pos_b, cash=0.0)
    applied_b = apply_corporate_actions(
        rt_b,
        [_div("600519.SH", 20260608, cash_div_tax=2.0), _div("000001.SZ", 20260608, cash_div_tax=1.0)],
        lower=None,
        upper=pd.Timestamp("2026-06-08"),
    )
    # cash total is order-independent (positions are disjoint) ...
    assert rt_a.cash == rt_b.cash
    # ... but the *emitted list* (what lands in corporate_actions.jsonl) must be byte-identical:
    assert json.dumps(applied_a, default=str) == json.dumps(applied_b, default=str), (
        "corporate_actions list order diverges with dividend input order on same ex_date"
    )


def test_fill_order_within_bar_independent_of_order_submission_is_documented():
    """REGRESSION: multi-symbol same-bar fills get trade_number in open_orders (submission) order,
    which is stable regardless of the *bar* row order. Cash deduction order is therefore stable.

    This documents the one thing that IS deterministic: trade numbering follows the order in
    which orders were submitted/parked, not the order the bar rows happen to arrive in.
    """
    rules = AShareRuleEngine()
    rows = [
        _bar("000001.SZ", "2026-01-05 09:31:00", 20260105, 10.0, 10.0),
        _bar("600519.SH", "2026-01-05 09:31:00", 20260105, 100.0, 100.0),
    ]
    orders = [
        {"symbol": "000001.SZ", "side": 1, "quantity": 100, "request_id": "first", "exec_time": "09:31:00"},
        {"symbol": "600519.SH", "side": 1, "quantity": 100, "request_id": "second", "exec_time": "09:31:00"},
    ]

    def run(frame_rows):
        rt = _rt(universe=["000001.SZ", "600519.SH"])
        advance(rt, pd.DataFrame(frame_rows), rules=rules, orders=[dict(o) for o in orders], to="2026-01-05 09:31:00")
        return [(t["request_id"], t["trade_id"]) for t in rt.trades]

    assert run(rows) == run([rows[1], rows[0]]), "trade_number assignment must be submission-ordered, bar-order-independent"


# =========================================================================== 3. SERIALIZATION FIXED POINT


def test_hydrate_dump_hydrate_is_a_fixed_point():
    """REGRESSION: dump() → hydrate() → dump() must be byte-identical (idempotent serialization).

    No float reformat, no key reorder, no field drift across a save/load cycle. This is what
    crash-recovery (app.py: update_session then re-hydrate on next advance) relies on.
    """
    rt = _rt(
        {
            "600519.SH": Position(quantity=100, sellable_quantity=100, cost_basis=101.2345),
            "000001.SZ": Position(quantity=300, sellable_quantity=0, cost_basis=10.6789),
        },
        cash=987654.3210,
        sim_time=pd.Timestamp("2026-01-06 14:57:00"),
        universe=["000001.SZ", "600519.SH"],
        current_date_key=20260106,
    )
    rt.last_prices = {"600519.SH": 102.0, "000001.SZ": 10.5}
    rt.processed_advances = ["adv-1", "adv-2"]
    rt.open_orders = [
        {
            "order": {
                "order_batch_id": "default",
                "request_id": "park1",
                "trade_date": "2026-01-06",
                "symbol": "000001.SZ",
                "side": 1,
                "quantity": 200,
                "price_type": "close",
                "limit_price": None,
                "exec_time": None,
            },
            "price_field": "close",
            "target_ts": "2026-01-06T14:57:00",
        }
    ]

    dump1 = rt.dump()
    # the persisted row carries the dump()'d mutable fields PLUS the immutable session
    # columns (initial_cash) that store.create_session set and update_session never touches.
    row = {"session_id": "s", "initial_cash": rt.initial_cash, **dump1}
    rt2 = SessionRuntime.hydrate(row)
    dump2 = rt2.dump()

    for key in dump1:
        assert dump1[key] == dump2[key], f"field {key!r} drifted across hydrate round-trip"


def test_position_cost_basis_float_stable_across_round_trip():
    """REGRESSION: a non-terminating cost_basis (e.g. from a split) survives JSON round-trip
    without reformatting that would change the serialized bytes."""
    rt = _rt({"600519.SH": Position(quantity=3, sellable_quantity=3, cost_basis=100.0 / 3.0)})
    row = {"session_id": "s", "initial_cash": rt.initial_cash, **rt.dump()}
    rt2 = SessionRuntime.hydrate(row)
    assert rt.dump()["positions_json"] == rt2.dump()["positions_json"]
