"""策略中心聚合(strategies.py)纯函数 + /strategies API 集成测试。"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_dashboard_api import _completed_client
from vortex_backtest import strategies as S


def _job(jid, created, status, strat_id, daily_vals):
    daily = [{"trade_date": f"2026-06-0{i + 1}", "total_value": v, "drawdown": 0.0} for i, v in enumerate(daily_vals)]
    summary = {"strategies": [{"strategy_id": strat_id, "daily": daily,
                               "positions": [{"symbol": "600000.SH"}], "trades": []}]}
    return {"job_id": jid, "status": status, "created_at": created, "start_date": "2026-06-01",
            "end_date": "2026-06-05", "summary": summary, "request": None, "progress": None}


def test_aggregate_groups_runs_latest_best() -> None:
    jobs = [
        _job("j1", "2026-06-01T00:00:00", "completed", "alpha", [100, 110]),  # +10%
        _job("j2", "2026-06-02T00:00:00", "completed", "alpha", [100, 105]),  # +5%
        _job("j3", "2026-06-02T00:00:00", "completed", "beta", [100, 90]),    # -10%
    ]
    aggs = S.aggregate(jobs)
    by = {a["strategy_id"]: a for a in aggs}
    assert by["alpha"]["n_runs"] == 2
    assert by["alpha"]["latest"]["job_id"] == "j2"   # created_at 最近
    assert by["alpha"]["best"]["job_id"] == "j1"     # 收益最优
    assert by["alpha"]["board"] == "main"
    lb = S.leaderboard(aggs, metric="total_return", scope="best")
    assert lb[0]["strategy_id"] == "alpha" and lb[0]["value"] > lb[1]["value"]


def test_running_job_appears_without_metrics() -> None:
    jobs = [{"job_id": "jr", "status": "running", "created_at": "2026-06-03T00:00:00",
             "start_date": "2026-06-01", "end_date": "2026-06-05", "summary": None,
             "request": {"strategies": [{"strategy_id": "gamma"}]}, "progress": {"pct": 0.5}}]
    aggs = S.aggregate(jobs)
    gamma = next(a for a in aggs if a["strategy_id"] == "gamma")
    assert gamma["running"] is True
    assert gamma["latest"]["status"] == "running"
    assert gamma["best"] is None


def test_strategies_api_endpoints(tmp_path: Path, monkeypatch) -> None:
    client, job_id = _completed_client(tmp_path, monkeypatch)
    lst = client.get("/strategies", params={"account_id": "acct"}).json()
    assert len(lst) >= 1
    sid = lst[0]["strategy_id"]
    assert "runs" not in lst[0]  # 列表不带全部 run

    detail = client.get(f"/strategies/{sid}", params={"account_id": "acct"}).json()
    assert detail["strategy_id"] == sid and "runs" in detail and detail["n_runs"] >= 1

    lb = client.get("/leaderboard", params={"account_id": "acct", "metric": "total_return"}).json()
    assert isinstance(lb, list)

    client.put(f"/strategies/{sid}/meta", params={"account_id": "acct"}, json={"favorite": True, "tags": ["核心"]})
    again = client.get("/strategies", params={"account_id": "acct"}).json()
    fav = next(a for a in again if a["strategy_id"] == sid)
    assert fav["favorite"] is True and "核心" in fav["tags"]

    cmp = client.get("/strategies/compare", params={"account_id": "acct", "ids": sid}).json()
    assert cmp["strategies"][0]["strategy_id"] == sid
