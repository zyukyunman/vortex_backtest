"""B1/B4/B5 看板只读 API 集成测试：在真实跑通的回测上验证 equity/metrics/基准/筛选。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from test_api import write_workspace  # 复用既有合成数据写入
from vortex_backtest.app import create_app
from vortex_backtest.worker import drain_jobs


def _completed_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, str]:
    workspace = tmp_path / "workspace"
    write_workspace(workspace, symbols=("000001.SZ",))
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(workspace))
    app = create_app(tmp_path / "state", run_worker=False)
    client = TestClient(app)
    assert client.post("/accounts", json={"account_id": "acct", "initial_cash": 100000}).status_code == 201
    assert client.post(
        "/accounts/acct/orders",
        json={"request_id": "buy1", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000},
    ).status_code == 201
    resp = client.post(
        "/backtests",
        json={
            "account_id": "acct",
            "frequency": "1min",
            "price_adjustment": "qfq",
            "start_date": "2026-01-02",
            "end_date": "2026-01-05",
        },
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    drain_jobs(app.state.store)
    assert client.get(f"/backtests/{job_id}").json()["status"] == "completed"
    return client, job_id


def test_ui_shell_is_served(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, run_worker=False))
    root = client.get("/")  # 跟随重定向到 /ui/
    assert root.status_code == 200
    assert "vortex_backtest" in root.text
    assert client.get("/ui/static/app.js").status_code == 200
    assert client.get("/ui/static/app.css").status_code == 200


def test_cancel_queued_job(tmp_path: Path) -> None:
    app = create_app(tmp_path, run_worker=False)
    c = TestClient(app)
    c.post("/accounts", json={"account_id": "a", "initial_cash": 100000})
    c.post("/accounts/a/orders", json={"request_id": "o1", "trade_date": "2026-06-01", "symbol": "000001.SZ", "side": 1, "quantity": 1000})
    jid = c.post("/backtests", json={"account_id": "a", "frequency": "1min", "price_adjustment": "qfq", "start_date": "2026-06-01", "end_date": "2026-06-05"}).json()["job_id"]
    assert c.get(f"/backtests/{jid}").json()["status"] == "queued"  # run_worker=False → 不执行
    r = c.post(f"/backtests/{jid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert len(c.get("/backtests", params={"status": "cancelled"}).json()) == 1
    assert c.post(f"/backtests/{jid}/cancel").status_code == 409  # 已终态不可再取消


def test_benchmarks_catalog(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, run_worker=False))
    body = client.get("/benchmarks").json()
    assert body["default"] == "000300.SH"
    symbols = [item["symbol"] for item in body["items"]]
    assert "000300.SH" in symbols and "000905.SH" in symbols


def test_list_status_filter(tmp_path: Path, monkeypatch) -> None:
    client, job_id = _completed_client(tmp_path, monkeypatch)
    completed = client.get("/backtests", params={"status": "completed"}).json()
    assert len(completed) == 1 and completed[0]["job_id"] == job_id
    assert client.get("/backtests", params={"status": "failed"}).json() == []


def test_equity_metrics_and_rejection_summary(tmp_path: Path, monkeypatch) -> None:
    client, job_id = _completed_client(tmp_path, monkeypatch)

    eq = client.get(f"/backtests/{job_id}/equity").json()
    assert eq["dates"] and len(eq["equity"]) == len(eq["dates"]) == len(eq["drawdown"])
    assert "baseline" in eq

    m = client.get(f"/backtests/{job_id}/metrics").json()
    assert m["sample_days"] == len(eq["dates"])
    assert m["low_confidence"] is True
    assert "cumulative_return" in m["absolute"] and "sharpe" in m["risk_adjusted"]

    rs = client.get(f"/backtests/{job_id}/rejections/summary").json()
    assert "counts" in rs and "total" in rs


def test_equity_and_metrics_with_benchmark(tmp_path: Path, monkeypatch) -> None:
    client, job_id = _completed_client(tmp_path, monkeypatch)
    idx_dir = tmp_path / "idx" / "index_daily"
    idx_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {"symbol": "000300.SH", "date": 20260102, "close": 4000.0},
            {"symbol": "000300.SH", "date": 20260105, "close": 4040.0},
        ]
    ).to_parquet(idx_dir / "data.parquet", index=False)
    monkeypatch.setenv("VORTEX_INDEX_DATA_DIR", str(idx_dir))

    eq = client.get(f"/backtests/{job_id}/equity", params={"benchmark": "000300.SH"}).json()
    assert eq["rebase"] is True
    assert eq["benchmark"]["available"] is True
    assert eq["benchmark"]["symbol"] == "000300.SH"
    assert len(eq["benchmark"]["values"]) == len(eq["dates"])

    m = client.get(f"/backtests/{job_id}/metrics", params={"benchmark": "000300.SH"}).json()
    assert m["benchmark"]["available"] is True
