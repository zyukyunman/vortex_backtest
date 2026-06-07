"""HTTP 接口全量调用测试。

逐个端点验证状态码、校验、过滤、错误分支与作业生命周期。所有用例用 TestClient，
回测作业用 drain_jobs 同步排空以保证确定性。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from vortex_backtest.app import create_app
from vortex_backtest.worker import drain_jobs


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "state", run_worker=False))


def _workspace(workspace_builder) -> Path:
    return (
        workspace_builder
        .day("2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .day("2026-01-05", "000001.SZ", open=10.0, close=10.5, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .build()
    )


# ----------------------------- health / 账户 -----------------------------

def test_health(tmp_path):
    assert _client(tmp_path).get("/health").json() == {"status": "ok"}


def test_account_crud_and_errors(tmp_path):
    client = _client(tmp_path)
    created = client.post("/accounts", json={"account_id": "a1", "initial_cash": 100000, "name": "n"})
    assert created.status_code == 201
    assert created.json()["engine"] == "replay"

    assert client.get("/accounts/a1").status_code == 200
    assert client.get("/accounts/missing").status_code == 404
    assert {a["account_id"] for a in client.get("/accounts").json()} == {"a1"}

    # 重复 account_id → 409
    assert client.post("/accounts", json={"account_id": "a1", "initial_cash": 1}).status_code == 409
    # initial_cash 必须 > 0 → 422
    assert client.post("/accounts", json={"account_id": "a2", "initial_cash": 0}).status_code == 422


# ----------------------------- 订单 -----------------------------

def test_order_create_dup_and_validation(tmp_path):
    client = _client(tmp_path)
    client.post("/accounts", json={"account_id": "a", "initial_cash": 100000})
    order = {"request_id": "r1", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100}

    assert client.post("/accounts/a/orders", json=order).status_code == 201
    # 同 account+batch+request_id 幂等键重复 → 409
    assert client.post("/accounts/a/orders", json=order).status_code == 409
    # 账户不存在 → 404
    assert client.post("/accounts/none/orders", json=order).status_code == 404
    # side 必须是数值枚举：布尔、字符串、越界都 422
    for bad in (True, "1", 3, 0):
        body = {**order, "request_id": f"x{bad}", "side": bad}
        assert client.post("/accounts/a/orders", json=body).status_code == 422
    # quantity 必须 > 0
    assert client.post("/accounts/a/orders", json={**order, "request_id": "q", "quantity": 0}).status_code == 422


def test_order_listing_with_filters(tmp_path):
    client = _client(tmp_path)
    client.post("/accounts", json={"account_id": "a", "initial_cash": 100000})
    for i, (batch, day) in enumerate([("b1", "2026-01-02"), ("b1", "2026-01-05"), ("b2", "2026-01-02")]):
        client.post("/accounts/a/orders", json={
            "order_batch_id": batch, "request_id": f"r{i}", "trade_date": day,
            "symbol": "000001.SZ", "side": 1, "quantity": 100,
        })
    assert len(client.get("/accounts/a/orders").json()) == 3
    assert len(client.get("/accounts/a/orders", params={"order_batch_id": "b1"}).json()) == 2
    assert len(client.get("/accounts/a/orders", params={"start_date": "2026-01-05"}).json()) == 1
    assert client.get("/accounts/missing/orders").status_code == 404


# ----------------------------- symbols -----------------------------

def test_symbol_crosswalk(tmp_path):
    client = _client(tmp_path)
    body = client.get("/symbols/688981.SH").json()
    assert body["board"] == "star" and body["buy_min_shares"] == 200
    assert client.get("/symbols/000001.SZ").json()["board"] == "main"
    assert client.get("/symbols/300750.SZ").json()["board"] == "chinext"
    # 非法代码 → 422
    assert client.get("/symbols/NOTACODE").status_code == 422


# ----------------------------- 回测提交校验 -----------------------------

def test_backtest_submit_validation(tmp_path, monkeypatch, workspace_builder):
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(_workspace(workspace_builder)))
    client = _client(tmp_path)
    client.post("/accounts", json={"account_id": "a", "initial_cash": 100000})

    # 不支持的 frequency / price_adjustment / order_price_adjustment → 同步 400（不入队）
    assert client.post("/backtests", json={"account_id": "a", "frequency": "1d"}).json()["detail"]["error"] == "unsupported_frequency"
    assert client.post("/backtests", json={"account_id": "a", "price_adjustment": "raw"}).json()["detail"]["error"] == "unsupported_price_adjustment"
    assert client.post("/backtests", json={"account_id": "a", "order_price_adjustment": "hfq"}).json()["detail"]["error"] == "unsupported_order_price_adjustment"
    # 账户不存在 → 404（且先于参数校验）
    assert client.post("/backtests", json={"account_id": "ghost", "frequency": "1d"}).status_code == 404


# ----------------------------- 作业生命周期 + 报告端点 -----------------------------

def test_job_lifecycle_and_report_endpoints(tmp_path, monkeypatch, workspace_builder):
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(_workspace(workspace_builder)))
    client = _client(tmp_path)
    client.post("/accounts", json={"account_id": "a", "initial_cash": 100000})
    for o in [
        {"request_id": "buy", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000},
        {"request_id": "sell", "trade_date": "2026-01-05", "symbol": "000001.SZ", "side": 2, "quantity": 1000},
    ]:
        client.post("/accounts/a/orders", json=o)

    submit = client.post("/backtests", json={
        "account_id": "a", "frequency": "1min", "price_adjustment": "qfq",
        "start_date": "2026-01-02", "end_date": "2026-01-05",
    })
    assert submit.status_code == 202
    job_id = submit.json()["job_id"]
    assert submit.json()["status"] == "queued"

    # 完成前取报告 → 404
    assert client.get(f"/backtests/{job_id}/summary").status_code == 404

    drain_jobs(client.app.state.store)
    job = client.get(f"/backtests/{job_id}").json()
    assert job["status"] == "completed"
    assert job["frequency"] == "1min" and job["price_adjustment"] == "qfq"

    summary = client.get(f"/backtests/{job_id}/summary").json()
    assert summary["account_id"] == "a"
    assert summary["frequency"] == "1min"
    assert Path(summary["artifacts"]["account_summary"]).exists()

    daily = client.get(f"/backtests/{job_id}/daily").json()
    assert {d["trade_date"] for d in daily} == {"2026-01-02", "2026-01-05"}
    # 单日快照
    assert client.get(f"/backtests/{job_id}/daily/2026-01-02").json()["trade_date"] == "2026-01-02"
    assert client.get(f"/backtests/{job_id}/daily/2026-01-09").status_code == 404

    # trades / rejections + trade_date 过滤
    assert len(client.get(f"/backtests/{job_id}/trades").json()) == 2
    d2_trades = client.get(f"/backtests/{job_id}/trades", params={"trade_date": "2026-01-05"}).json()
    assert len(d2_trades) == 1 and d2_trades[0]["request_id"] == "sell"
    assert isinstance(client.get(f"/backtests/{job_id}/rejections").json(), list)

    # 列作业 + 按账户过滤
    assert len(client.get("/backtests", params={"account_id": "a"}).json()) == 1
    # 未知 job → 404
    assert client.get("/backtests/not-a-job").status_code == 404


def test_account_latest_summary_and_positions(tmp_path, monkeypatch, workspace_builder):
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(_workspace(workspace_builder)))
    client = _client(tmp_path)
    client.post("/accounts", json={"account_id": "a", "initial_cash": 100000})

    # 还没有完成的回测 → 404
    assert client.get("/accounts/a/summary").status_code == 404
    assert client.get("/accounts/a/positions").status_code == 404

    client.post("/accounts/a/orders", json={"request_id": "buy", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000})
    submit = client.post("/backtests", json={"account_id": "a", "start_date": "2026-01-02", "end_date": "2026-01-05"})
    drain_jobs(client.app.state.store)
    assert submit.json()["status"] == "queued"

    assert client.get("/accounts/a/summary").json()["account_id"] == "a"
    positions = client.get("/accounts/a/positions").json()
    assert positions[0]["symbol"] == "000001.SZ" and positions[0]["quantity"] == 1000


def test_failed_job_reports_safe_error_code(tmp_path, monkeypatch, workspace_builder):
    # 没有分钟数据 → 作业 failed，summary.error == minute_data_missing（安全错误码，不脱敏）
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000
    ).build()
    # 删掉 stk_mins 模拟缺数据
    import shutil
    shutil.rmtree(ws / "data" / "stk_mins")
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(ws))
    client = _client(tmp_path)
    client.post("/accounts", json={"account_id": "a", "initial_cash": 100000})
    client.post("/accounts/a/orders", json={"request_id": "b", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100})
    submit = client.post("/backtests", json={"account_id": "a", "start_date": "2026-01-02", "end_date": "2026-01-02"})
    drain_jobs(client.app.state.store)
    job = client.get(f"/backtests/{submit.json()['job_id']}").json()
    assert job["status"] == "failed"
    assert job["summary"]["error"] == "minute_data_missing"


# ----------------------------- 写接口鉴权 -----------------------------

def test_write_auth_token_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("VORTEX_BACKTEST_TOKEN", "secret")
    client = _client(tmp_path)
    # 无 token → 401
    assert client.post("/accounts", json={"account_id": "a", "initial_cash": 1000}).status_code == 401
    # Bearer / X-Auth-Token 正确 → 201
    assert client.post("/accounts", json={"account_id": "a", "initial_cash": 1000}, headers={"Authorization": "Bearer secret"}).status_code == 201
    assert client.post("/accounts", json={"account_id": "b", "initial_cash": 1000}, headers={"X-Auth-Token": "secret"}).status_code == 201
    # 错 token → 401
    assert client.post("/accounts", json={"account_id": "c", "initial_cash": 1000}, headers={"X-Auth-Token": "nope"}).status_code == 401
    # 读接口不需要 token
    assert client.get("/accounts").status_code == 200
    assert client.get("/health").status_code == 200


# ----------------------------- execution 配置 -----------------------------

def test_execution_config_round_trips_through_backtest(tmp_path, monkeypatch, workspace_builder):
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(_workspace(workspace_builder)))
    client = _client(tmp_path)
    client.post("/accounts", json={"account_id": "a", "initial_cash": 100000})
    client.post("/accounts/a/orders", json={"request_id": "buy", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000})
    submit = client.post("/backtests", json={
        "account_id": "a", "start_date": "2026-01-02", "end_date": "2026-01-02",
        "execution": {"commission_rate": 0.001, "min_commission": 0.0, "slippage_bps": 10},
    })
    assert submit.status_code == 202
    drain_jobs(client.app.state.store)
    summary = client.get(f"/backtests/{submit.json()['job_id']}/summary").json()
    buy = next(t for t in summary["trades"] if t["request_id"] == "buy")
    # 自定义佣金率 0.001、min 0：1000 股 @ qfq close 10.0 × (1+10bps) → amount≈10010，佣金≈10.01
    assert buy["commission"] > 9.9
