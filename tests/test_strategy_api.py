"""策略中心端点 API 测试：直接写 summary.json（closed 会话走缓存路径），控制 created_at/strategy_id/收益。"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from vortex_backtest.app import create_app
from vortex_backtest.models import AccountCreate
from vortex_backtest.store import DataStore


def _seed(state, store, *, session_id, strategy_id, account_id, last_value,
          created_at, start="2026-02-02", end="2026-02-06"):
    """建账户(幂等) + 会话行 + closed + 写 summary.json。
    total_return 由 perf_stats(日序列) 算：last_value / initial_cash(1000) - 1。"""
    try:
        store.create_account(AccountCreate(account_id=account_id, initial_cash=1000.0))
    except sqlite3.IntegrityError:
        pass
    store.create_session(session_id=session_id, account_id=account_id, level="daily",
                         start_date=start, end_date=end, sim_time=None, initial_cash=1000.0,
                         universe=["000001.SZ"], config={"strategy_id": strategy_id})
    with store.connect() as c:   # 显式控制 created_at 以断言 latest 口径
        c.execute("UPDATE sessions SET created_at=? WHERE session_id=?", (created_at, session_id))
    daily = [{"trade_date": start, "cash": 1000.0, "market_value": 0.0,
              "total_value": 1000.0, "positions": []},
             {"trade_date": end, "cash": last_value, "market_value": 0.0,
              "total_value": last_value, "positions": []}]
    summary = {"strategy_id": strategy_id, "initial_cash": 1000.0, "cash": last_value,
               "market_value": 0.0, "total_value": last_value,
               "total_return": last_value / 1000.0 - 1, "max_drawdown": 0.0,
               "realized_pnl": 0.0, "positions": [], "daily": daily}
    sdir = state / "reports" / "sessions" / session_id
    sdir.mkdir(parents=True)
    (sdir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    store.update_session(session_id, status="closed")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VORTEX_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("VORTEX_DATA_URL", raising=False)
    monkeypatch.setenv("VORTEX_BACKTEST_HOST", "127.0.0.1")
    state = tmp_path / "state"
    store = DataStore(state)
    # 策略 alpha 两次：a_old 收益高(0.20)但早；a_new 收益低(0.01)但最新 → latest=a_new、best=a_old
    _seed(state, store, session_id="a_old", strategy_id="alpha", account_id="acc_a",
          last_value=1200.0, created_at="2026-01-01T00:00:00+00:00")
    _seed(state, store, session_id="a_new", strategy_id="alpha", account_id="acc_a",
          last_value=1010.0, created_at="2026-02-01T00:00:00+00:00")
    # 策略 beta 一次：收益 0.05
    _seed(state, store, session_id="b1", strategy_id="beta", account_id="acc_b",
          last_value=1050.0, created_at="2026-01-15T00:00:00+00:00")
    return TestClient(create_app(state_dir=state))


def test_strategies_leaderboard(client):
    rows = client.get("/strategies").json()
    # 默认按 latest.total_return 降序：beta(latest 0.05) 在 alpha(latest a_new 0.01) 前
    assert [r["strategy_id"] for r in rows] == ["beta", "alpha"]
    alpha = next(r for r in rows if r["strategy_id"] == "alpha")
    assert alpha["n_runs"] == 2 and alpha["accounts"] == ["acc_a"]
    assert alpha["latest"]["session_id"] == "a_new"
    assert alpha["latest"]["total_return"] == pytest.approx(0.01)
    assert alpha["best"]["session_id"] == "a_old"
    assert alpha["best"]["total_return"] == pytest.approx(0.20)
    assert alpha["latest"]["low_confidence"] is True   # n_days=2 < 60


def test_strategy_detail_runs_sorted(client):
    d = client.get("/strategies/alpha").json()
    assert d["n_runs"] == 2
    assert [run["session_id"] for run in d["runs"]] == ["a_old", "a_new"]   # created_at 升序
    assert d["latest"]["session_id"] == "a_new" and d["best"]["session_id"] == "a_old"


def test_strategy_detail_404(client):
    assert client.get("/strategies/nope").status_code == 404


def test_strategies_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("VORTEX_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("VORTEX_DATA_URL", raising=False)
    monkeypatch.setenv("VORTEX_BACKTEST_HOST", "127.0.0.1")
    state = tmp_path / "state"
    DataStore(state)
    c = TestClient(create_app(state_dir=state))
    assert c.get("/strategies").json() == []
