"""会话端点端到端测试（design/18 B3）：create → advance → close。

bar 源 monkeypatch 成合成富 bar，不依赖真实落盘数据。
"""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from vortex_backtest import app as app_mod
from vortex_backtest.app import create_app


def _bar(symbol, ts, date_key, px=10.0, vol=100000):
    return {
        "symbol": symbol, "trade_time": pd.Timestamp(ts), "date": date_key,
        "open": px, "close": px, "open_qfq": px, "close_qfq": px,
        "volume": vol, "board": "主板", "suspended": False,
        "up_limit": px * 1.1, "down_limit": px * 0.9,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    bars = pd.DataFrame([
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506),
        _bar("600519.SH", "2026-05-06 09:31:00", 20260506),
        _bar("600519.SH", "2026-05-07 09:30:00", 20260507, px=11.0),
        _bar("600519.SH", "2026-05-07 09:31:00", 20260507, px=11.0),
    ])

    def fake_load(symbols, start_d, end_d, as_of=None, anchor_d=None):
        return bars.copy(), [20260506, 20260507]

    monkeypatch.setattr(app_mod, "_load_session_bars", fake_load)
    app = create_app(tmp_path, run_worker=False)
    return TestClient(app)


def test_session_lifecycle(client):
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    r = client.post("/sessions", json={
        "account_id": "a", "level": "1min",
        "start_date": "2026-05-06", "end_date": "2026-05-07",
        "universe": ["600519.SH"], "fill_timing": "this_bar",
    })
    assert r.status_code == 201
    sid = r.json()["session_id"]
    assert r.json()["nav"] == 1_000_000.0

    # day1 买入
    r1 = client.post(f"/sessions/{sid}/advance", json={
        "orders": [{"request_id": "o1", "symbol": "600519.SH", "side": 1, "quantity": 100, "exec_time": "09:30"}],
        "to": "2026-05-06T09:31:00",
    })
    assert r1.status_code == 200
    body = r1.json()
    assert len(body["filled"]) == 1
    assert body["positions"][0]["quantity"] == 100
    assert body["positions"][0]["available_quantity"] == 0  # T+1

    # day2 卖出（跨日解锁）
    r2 = client.post(f"/sessions/{sid}/advance", json={
        "orders": [{"request_id": "o2", "symbol": "600519.SH", "side": 2, "quantity": 100, "exec_time": "09:30"}],
        "to": "2026-05-07T09:31:00",
    })
    assert r2.status_code == 200
    assert len(r2.json()["filled"]) == 1  # 卖单成交
    assert r2.json()["positions"] == []   # 清仓

    # 报告端点（会话进行中即可读累积产物）
    assert len(client.get(f"/sessions/{sid}/trades").json()) == 2
    assert len(client.get(f"/sessions/{sid}/minutes").json()) >= 2
    live = client.get(f"/sessions/{sid}/summary").json()
    assert live["realized_pnl"] > 0

    # close
    rc = client.post(f"/sessions/{sid}/close")
    assert rc.status_code == 200
    summary = rc.json()["summary"]
    assert summary["realized_pnl"] > 0   # 11 卖 / 10 买
    assert rc.json()["status"] == "closed"
    # close 后 summary/daily 仍可读
    assert client.get(f"/sessions/{sid}/summary").json()["realized_pnl"] > 0
    assert isinstance(client.get(f"/sessions/{sid}/daily").json(), list)

    # 已关闭会话不能再 advance
    r3 = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-07T09:31:00"})
    assert r3.status_code == 409


def test_advance_books_dividend_on_ex_date(tmp_path, monkeypatch):
    """N8 端到端：持仓跨入除权日 → 现金分红入账，公司行动出现在返回与日志。"""
    bars = pd.DataFrame([
        _bar("600519.SH", "2026-05-06 09:30:00", 20260506, px=10.0),
        _bar("600519.SH", "2026-05-07 09:30:00", 20260507, px=9.0),  # 除权后 RAW 跌
    ])
    monkeypatch.setattr(app_mod, "_load_session_bars",
                        lambda symbols, start_d, end_d, as_of=None, anchor_d=None: (bars.copy(), [20260506, 20260507]))
    monkeypatch.setattr(app_mod, "_load_session_dividends",
                        lambda symbols, as_of: [{"symbol": "600519.SH", "ex_date": 20260507,
                                                 "cash_div_tax": 1.0, "stk_div": 0.0,
                                                 "stk_bo_rate": 0.0, "stk_co_rate": 0.0}])
    client = TestClient(create_app(tmp_path, run_worker=False))
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "level": "1min", "start_date": "2026-05-06", "end_date": "2026-05-07",
        "universe": ["600519.SH"], "fill_timing": "this_bar"}).json()["session_id"]

    # D1 买 100，本步窗口不含除权日 → 不入账
    r1 = client.post(f"/sessions/{sid}/advance", json={
        "orders": [{"request_id": "o1", "symbol": "600519.SH", "side": 1, "quantity": 100, "exec_time": "09:30"}],
        "to": "2026-05-06T09:30:00"}).json()
    assert r1["positions"][0]["quantity"] == 100
    assert not r1.get("corporate_actions")
    cash_after_buy = r1["cash"]

    # 推进到 D2（除权日）→ 现金分红 100×1 入账
    r2 = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-07T09:30:00"}).json()
    assert r2["corporate_actions"] and r2["corporate_actions"][0]["symbol"] == "600519.SH"
    assert abs(r2["cash"] - (cash_after_buy + 100.0)) < 0.01
    assert r2["positions"][0]["quantity"] == 100  # 现金分红不改股数


def test_monotonic_clock_rejected(client):
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "start_date": "2026-05-06", "end_date": "2026-05-07", "universe": ["600519.SH"],
    }).json()["session_id"]
    client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-07T09:30:00"})
    # 回到过去
    r = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-06T09:30:00"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "non_monotonic_clock"


def test_session_data_endpoint(client, monkeypatch):
    """/sessions/{id}/data 透传网关：服务端注入 as_of=sim_time，universe 展开成显式列表。"""
    from vortex_backtest import gateway_adapter
    captured = {}

    def fake_query(self, as_of, datasets):
        captured["as_of"] = as_of
        captured["datasets"] = datasets
        return {"as_of": as_of, "results": {"stk_mins": {"rows": [{"symbol": "600519.SH", "close": 10}]}}}

    monkeypatch.setattr(gateway_adapter.GatewayDataAdapter, "_query", fake_query)
    monkeypatch.setenv("VORTEX_DATA_URL", "http://x")
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "start_date": "2026-05-06", "end_date": "2026-05-07",
        "universe": ["600519.SH"]}).json()["session_id"]
    out = client.post(f"/sessions/{sid}/data", json={
        "datasets": [{"dataset": "stk_mins", "symbols": "universe", "window": {"count": 5}}]}).json()
    # 未 advance → as_of = start@09:30；universe 展开
    assert captured["as_of"].startswith("2026-05-06")
    assert captured["datasets"][0]["symbols"] == ["600519.SH"]
    assert out["results"]["stk_mins"]["rows"]


def test_session_data_without_gateway_503(client, monkeypatch):
    monkeypatch.delenv("VORTEX_DATA_URL", raising=False)
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={"account_id": "a", "start_date": "2026-05-06", "end_date": "2026-05-07", "universe": ["X.SH"]}).json()["session_id"]
    r = client.post(f"/sessions/{sid}/data", json={"datasets": []})
    assert r.status_code == 503


def test_advance_idempotent(client):
    """幂等：同 request_id 重发 advance → no-op，不双成交/双推进。"""
    client.post("/accounts", json={"account_id": "a", "initial_cash": 1_000_000})
    sid = client.post("/sessions", json={
        "account_id": "a", "level": "1min", "start_date": "2026-05-06", "end_date": "2026-05-07",
        "universe": ["600519.SH"], "fill_timing": "this_bar"}).json()["session_id"]
    body = {"orders": [{"request_id": "o1", "symbol": "600519.SH", "side": 1, "quantity": 100, "exec_time": "09:30"}],
            "to": "2026-05-06T09:31:00", "request_id": "adv1"}
    r1 = client.post(f"/sessions/{sid}/advance", json=body).json()
    assert len(r1["filled"]) == 1
    assert r1["positions"][0]["quantity"] == 100
    # 重发同 request_id → 去重 no-op
    r2 = client.post(f"/sessions/{sid}/advance", json=body).json()
    assert r2.get("duplicate") is True
    assert len(r2["filled"]) == 0
    assert r2["positions"][0]["quantity"] == 100  # 没翻倍
    assert r2["sim_time"] == r1["sim_time"]        # 时钟没重复推进
