"""会话存储测试（design/18 B1）。"""
from __future__ import annotations

import json

import pytest

from vortex_backtest.models import AccountCreate
from vortex_backtest.store import DataStore


def _store(tmp_path) -> DataStore:
    store = DataStore(tmp_path)
    store.create_account(AccountCreate(account_id="acc1", initial_cash=1_000_000))
    return store


def test_create_and_get_session(tmp_path):
    store = _store(tmp_path)
    s = store.create_session(
        session_id="sess1", account_id="acc1", level="1min",
        start_date="2026-05-06", end_date="2026-05-29", sim_time="2026-05-06T09:30:00",
        initial_cash=1_000_000, universe=["600519.SH"],
        config={"fill_timing": "next_bar", "slippage_bps": 5},
    )
    assert s["status"] == "open"
    assert s["cash"] == 1_000_000
    assert json.loads(s["universe_json"]) == ["600519.SH"]
    assert json.loads(s["config_json"])["fill_timing"] == "next_bar"
    assert store.get_session("sess1")["session_id"] == "sess1"


def test_update_session_state(tmp_path):
    store = _store(tmp_path)
    store.create_session(
        session_id="s", account_id="acc1", level="daily", start_date=None, end_date=None,
        sim_time=None, initial_cash=1000.0, universe=[], config={},
    )
    updated = store.update_session(
        "s", sim_time="2026-05-07T09:30:00", cash=900.0,
        positions_json=json.dumps({"X": {"quantity": 100, "cost_basis": 1.0, "sellable_quantity": 0}}),
        trade_counter=1,
    )
    assert updated["cash"] == 900.0
    assert updated["sim_time"] == "2026-05-07T09:30:00"
    assert json.loads(updated["positions_json"])["X"]["quantity"] == 100
    assert updated["trade_counter"] == 1
    # 只允许白名单列
    store.update_session("s", account_id="hack")  # 忽略
    assert store.get_session("s")["account_id"] == "acc1"


def test_missing_account_and_session(tmp_path):
    store = DataStore(tmp_path)
    with pytest.raises(KeyError):
        store.create_session(
            session_id="x", account_id="nope", level="1min", start_date=None, end_date=None,
            sim_time=None, initial_cash=1.0, universe=[], config={},
        )
    with pytest.raises(KeyError):
        store.get_session("nope")


def test_list_sessions(tmp_path):
    store = _store(tmp_path)
    for sid in ("a", "b"):
        store.create_session(
            session_id=sid, account_id="acc1", level="1min", start_date=None, end_date=None,
            sim_time=None, initial_cash=1.0, universe=[], config={},
        )
    assert {s["session_id"] for s in store.list_sessions("acc1")} == {"a", "b"}
