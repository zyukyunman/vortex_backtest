"""分钟级撮合引擎综合测试（合成 fixture，确定性强断言）。

通过 HTTP 层（TestClient + drain_jobs 同步排空作业）端到端跑回测，覆盖：
T+1、限价(真实价空间)、涨跌停拦截、手数(主板/科创/创业)、成交量上限部分成交、
费用(佣金下限/卖出印花税/过户费)、现金不足、停牌、零量、多日净值/回撤/收益、
open vs close 成交点、持仓成本均价与浮动盈亏、qfq 口径。

这些用例既是“更多分钟回测样例”，也把**预期口径**钉死，便于回归。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vortex_backtest.app import create_app
from vortex_backtest.worker import drain_jobs


# ----------------------------- 辅助 -----------------------------

def _client(tmp_path: Path, monkeypatch, workspace: Path) -> TestClient:
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(workspace))
    return TestClient(create_app(tmp_path / "state", run_worker=False))


def _run(
    client: TestClient,
    *,
    account_id: str = "acct",
    initial_cash: float = 100_000.0,
    orders: list[dict[str, Any]],
    start: str,
    end: str,
    default_price_type: str = "close",
    strategies: list[dict[str, Any]] | None = None,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assert client.post(
        "/accounts", json={"account_id": account_id, "initial_cash": initial_cash}
    ).status_code == 201
    for order in orders:
        resp = client.post(f"/accounts/{account_id}/orders", json=order)
        assert resp.status_code == 201, resp.text
    payload: dict[str, Any] = {
        "account_id": account_id,
        "frequency": "1min",
        "price_adjustment": "qfq",
        "default_price_type": default_price_type,
        "start_date": start,
        "end_date": end,
    }
    if strategies is not None:
        payload["strategies"] = strategies
    if execution is not None:
        payload["execution"] = execution
    resp = client.post("/backtests", json=payload)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    drain_jobs(client.app.state.store)
    job = client.get(f"/backtests/{job_id}").json()
    assert job["status"] == "completed", job
    return client.get(f"/backtests/{job_id}/summary").json()


def _trade(summary: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    return next((t for t in summary["trades"] if t["request_id"] == request_id), None)


def _rejection(summary: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    return next((r for r in summary["rejections"] if r["request_id"] == request_id), None)


# ----------------------------- T+1 -----------------------------

def test_t_plus_1_blocks_same_day_sell_but_allows_next_day(tmp_path, monkeypatch, workspace_builder):
    ws = (
        workspace_builder
        .day("2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000)
        .day("2026-01-05", "000001.SZ", open=10.0, close=10.0, volume=1_000_000)
        .build()
    )
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[
            {"request_id": "buy", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000},
            {"request_id": "sell-same-day", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 2, "quantity": 1000},
            {"request_id": "sell-next-day", "trade_date": "2026-01-05", "symbol": "000001.SZ", "side": 2, "quantity": 1000},
        ],
        start="2026-01-02",
        end="2026-01-05",
    )
    assert _trade(summary, "buy") is not None
    assert _rejection(summary, "sell-same-day")["reason"] == "t_plus_1_not_sellable"
    assert _trade(summary, "sell-next-day") is not None
    # 一买一卖后清仓
    assert summary["positions"] == []


# --------------------------- 限价(真实价空间) ---------------------------

def test_limit_price_is_compared_in_raw_space(tmp_path, monkeypatch, workspace_builder):
    # 成交点 = 收盘 10.20；限价买 10.50 应成交，10.00 应拒。
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.2, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[
            {"request_id": "lp-ok", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100, "limit_price": 10.50},
            {"request_id": "lp-low", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100, "limit_price": 10.00},
        ],
        start="2026-01-02",
        end="2026-01-02",
    )
    assert _trade(summary, "lp-ok")["price"] == 10.2
    assert _rejection(summary, "lp-low")["reason"] == "limit_price_not_marketable"


# --------------------------- 涨跌停拦截 ---------------------------

def test_limit_up_blocks_buy(tmp_path, monkeypatch, workspace_builder):
    # 成交价 == 涨停价 → 买入拦截。
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=11.0, close=11.0, volume=1_000_000, up_limit=11.0, down_limit=9.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[
            {"request_id": "buy-at-up", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000},
        ],
        start="2026-01-02",
        end="2026-01-02",
    )
    assert _rejection(summary, "buy-at-up")["reason"] == "limit_up_buy_blocked"


def test_limit_down_blocks_sell_when_holding(tmp_path, monkeypatch, workspace_builder):
    # 第一天正常买入持仓；第二天成交价落在跌停 → 卖出被拦截。
    ws = (
        workspace_builder
        .day("2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=11.0, down_limit=9.0)
        .day("2026-01-05", "000001.SZ", open=9.0, close=9.0, volume=1_000_000, up_limit=11.0, down_limit=9.0)
        .build()
    )
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[
            {"request_id": "buy", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000},
            {"request_id": "sell-down", "trade_date": "2026-01-05", "symbol": "000001.SZ", "side": 2, "quantity": 1000},
        ],
        start="2026-01-02",
        end="2026-01-05",
    )
    assert _trade(summary, "buy") is not None
    assert _rejection(summary, "sell-down")["reason"] == "limit_down_sell_blocked"


# --------------------------- 手数规则 ---------------------------

def test_lot_size_rules_main_board(tmp_path, monkeypatch, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[
            {"request_id": "odd-150", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 150},
            {"request_id": "round-200", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 200},
        ],
        start="2026-01-02",
        end="2026-01-02",
    )
    assert _rejection(summary, "odd-150")["reason"] == "invalid_lot_size"
    assert _trade(summary, "round-200")["quantity"] == 200


def test_lot_size_rules_star_board_min_200(tmp_path, monkeypatch, workspace_builder):
    # 科创板 688xxx：买入最小 200 股，且允许非整百（如 250）。
    ws = workspace_builder.day(
        "2026-01-02", "688981.SH", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        initial_cash=100_000,
        orders=[
            {"request_id": "star-100", "trade_date": "2026-01-02", "symbol": "688981.SH", "side": 1, "quantity": 100},
            {"request_id": "star-250", "trade_date": "2026-01-02", "symbol": "688981.SH", "side": 1, "quantity": 250},
        ],
        start="2026-01-02",
        end="2026-01-02",
        strategies=[{"strategy_id": "s", "strategy_type": "order_replay", "initial_cash": 100_000, "symbols": ["688981.SH"]}],
    )
    assert _rejection(summary, "star-100")["reason"] == "invalid_lot_size"
    assert _trade(summary, "star-250")["quantity"] == 250


# --------------------------- 成交量上限 / 部分成交 ---------------------------

def test_volume_participation_caps_fill_to_lot(tmp_path, monkeypatch, workspace_builder):
    # 当日总量 1500，参与率 50% → 上限 750 → 向下取整到 700 股成交（剩余静默不补，见 findings）。
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1500, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[
            {"request_id": "big", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000},
        ],
        start="2026-01-02",
        end="2026-01-02",
        execution={"max_volume_participation": 0.5},
    )
    filled = _trade(summary, "big")
    assert filled is not None and filled["quantity"] == 700
    # 当前实现：剩余 300 股既不在成交也不在拒单里（观察性缺口，findings #3）
    assert _rejection(summary, "big") is None


def test_volume_below_one_lot_rejects(tmp_path, monkeypatch, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=50, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[{"request_id": "tiny", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100}],
        start="2026-01-02",
        end="2026-01-02",
    )
    assert _rejection(summary, "tiny")["reason"] == "volume_cap_below_lot"


# --------------------------- 费用 ---------------------------

def test_fee_breakdown_buy_and_sell(tmp_path, monkeypatch, workspace_builder):
    # 买 1000@10 → amount 10000：佣金 max(3,5)=5、印花税 0(买)、过户费 0.1。
    # 次日卖 1000@10 → 佣金 5、印花税 10000*0.0005=5、过户费 0.1。
    ws = (
        workspace_builder
        .day("2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .day("2026-01-05", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .build()
    )
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[
            {"request_id": "buy", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000},
            {"request_id": "sell", "trade_date": "2026-01-05", "symbol": "000001.SZ", "side": 2, "quantity": 1000},
        ],
        start="2026-01-02",
        end="2026-01-05",
    )
    buy, sell = _trade(summary, "buy"), _trade(summary, "sell")
    assert (buy["commission"], buy["stamp_tax"], buy["transfer_fee"]) == (5.0, 0.0, 0.1)
    assert (sell["commission"], sell["stamp_tax"], sell["transfer_fee"]) == (5.0, 5.0, 0.1)


def test_custom_execution_fees_apply(tmp_path, monkeypatch, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[{"request_id": "buy", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000}],
        start="2026-01-02",
        end="2026-01-02",
        execution={"commission_rate": 0.001, "min_commission": 0.0},  # 10000*0.001=10
    )
    assert _trade(summary, "buy")["commission"] == 10.0


# --------------------------- 现金不足 / 停牌 / 零量 ---------------------------

def test_insufficient_cash_rejected(tmp_path, monkeypatch, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        initial_cash=5_000,  # 买 1000@10 需 ~10005 > 5000
        orders=[{"request_id": "rich", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000}],
        start="2026-01-02",
        end="2026-01-02",
        strategies=[{"strategy_id": "s", "strategy_type": "order_replay", "initial_cash": 5_000, "symbols": ["000001.SZ"]}],
    )
    assert _rejection(summary, "rich")["reason"] == "insufficient_cash"


def test_suspended_day_rejected(tmp_path, monkeypatch, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, suspended=True, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[{"request_id": "halt", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100}],
        start="2026-01-02",
        end="2026-01-02",
    )
    assert _rejection(summary, "halt")["reason"] == "suspended"


def test_zero_volume_rejected(tmp_path, monkeypatch, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=0, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[{"request_id": "z", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100}],
        start="2026-01-02",
        end="2026-01-02",
    )
    assert _rejection(summary, "z")["reason"] == "zero_volume"


# --------------------------- open vs close 成交点 ---------------------------

def test_price_type_open_vs_close_fill(tmp_path, monkeypatch, workspace_builder):
    # 开盘 10.00、收盘 12.00：price_type=open 应成交在 10.00，close 应成交在 12.00。
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=12.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[
            {"request_id": "at-open", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100, "price_type": "open"},
            {"request_id": "at-close", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100, "price_type": "close"},
        ],
        start="2026-01-02",
        end="2026-01-02",
    )
    assert _trade(summary, "at-open")["price"] == 10.0
    assert _trade(summary, "at-close")["price"] == 12.0


# --------------------------- 持仓成本均价 / 浮盈 ---------------------------

def test_position_cost_basis_averaging_and_unrealized(tmp_path, monkeypatch, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=12.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[
            {"request_id": "b1", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100, "price_type": "open"},   # 10.0
            {"request_id": "b2", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 100, "price_type": "close"},  # 12.0
        ],
        start="2026-01-02",
        end="2026-01-02",
    )
    pos = summary["positions"][0]
    assert pos["quantity"] == 200
    assert pos["cost_basis"] == 11.0          # (100*10 + 100*12)/200
    assert pos["last_price"] == 12.0          # 当日收盘
    assert pos["unrealized_pnl"] == 200.0     # (12-11)*200


# --------------------------- 多日净值 / 回撤 / 收益 ---------------------------

def test_multi_day_nav_pnl_and_drawdown(tmp_path, monkeypatch, workspace_builder):
    # 10 万买 1000 股(open 10.0)，次日涨到收盘 11.0。
    ws = (
        workspace_builder
        .day("2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .day("2026-01-05", "000001.SZ", open=10.0, close=11.0, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .build()
    )
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        initial_cash=100_000,
        orders=[{"request_id": "buy", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000, "price_type": "open"}],
        start="2026-01-02",
        end="2026-01-05",
    )
    daily = {d["trade_date"]: d for d in summary["daily"]}
    assert set(daily) == {"2026-01-02", "2026-01-05"}
    # 买入花费 10000 + 费用5.1；day1 收盘市值 1000*10=10000 → 总值 99994.9
    assert daily["2026-01-02"]["total_value"] == 99994.9
    # day2 市值 1000*11=11000 → 总值 89994.9 + 11000 = 100994.9
    assert daily["2026-01-05"]["total_value"] == 100994.9
    assert daily["2026-01-05"]["daily_pnl"] == 1000.0
    assert summary["total_value"] == 100994.9
    # 最大回撤取自 day1 的微跌（费用导致）
    assert summary["max_drawdown"] == pytest.approx(99994.9 / 100000 - 1, abs=1e-9)


# --------------------------- qfq 口径 ---------------------------

def test_qfq_fill_price_uses_adjusted_value(tmp_path, monkeypatch, workspace_builder):
    # adj: 01-02=1.0, 01-05=2.0 → 全历史最新=2.0 → 01-02 的 qfq 乘子=0.5。
    # raw close 10.0 → qfq 成交价 5.0（撮合/估值用 qfq；tick/limit 用 raw）。
    ws = (
        workspace_builder
        .day("2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, adj_factor=1.0, up_limit=99.0, down_limit=1.0)
        .day("2026-01-05", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, adj_factor=2.0, up_limit=99.0, down_limit=1.0)
        .build()
    )
    client = _client(tmp_path, monkeypatch, ws)
    summary = _run(
        client,
        orders=[{"request_id": "buy", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 1000, "price_type": "close"}],
        start="2026-01-02",
        end="2026-01-02",
    )
    assert _trade(summary, "buy")["price"] == 5.0
