from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi.testclient import TestClient

from vortex_backtest.app import create_app
from vortex_backtest.backtrader_adapter import BacktraderMinuteReplayEngine
from vortex_backtest.data_adapter import TushareMinuteDataLoader
from vortex_backtest.market_rules import AShareRuleEngine
from vortex_backtest.models import Side
from vortex_backtest.store import DataStore
from vortex_backtest.worker import drain_jobs


def write_workspace(
    workspace: Path,
    *,
    symbols: tuple[str, ...] = ("000001.SZ",),
    include_minutes: bool = True,
    include_adj_factor: bool = True,
    include_limits: bool = True,
    limit_overrides: dict[tuple[str, int], tuple[float, float]] | None = None,
    adj_overrides: dict[tuple[str, int], float] | None = None,
) -> None:
    dates = (20260102, 20260105)
    if include_minutes:
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            for trade_date in dates:
                day_text = f"{str(trade_date)[:4]}-{str(trade_date)[4:6]}-{str(trade_date)[6:]}"
                rows.extend(
                    [
                        {
                            "symbol": symbol,
                            "date": trade_date,
                            "trade_time": f"{day_text} 09:31:00",
                            "minute": "09:31:00",
                            "freq": "1min",
                            "open": 10.0,
                            "high": 10.1,
                            "low": 9.9,
                            "close": 10.0,
                            "volume": 1000,
                            "amount": 10000.0,
                        },
                        {
                            "symbol": symbol,
                            "date": trade_date,
                            "trade_time": f"{day_text} 14:57:00",
                            "minute": "14:57:00",
                            "freq": "1min",
                            "open": 10.2,
                            "high": 10.3,
                            "low": 10.1,
                            "close": 10.2,
                            "volume": 1000,
                            "amount": 10200.0,
                        },
                    ]
                )
        write_parquet(workspace, "stk_mins", rows)

    if include_adj_factor:
        rows = []
        for symbol in symbols:
            for trade_date in dates:
                factor = 1.0
                if adj_overrides and (symbol, trade_date) in adj_overrides:
                    factor = adj_overrides[(symbol, trade_date)]
                rows.append({"symbol": symbol, "date": trade_date, "adj_factor": factor})
        write_parquet(workspace, "adj_factor", rows)

    if include_limits:
        rows = []
        for symbol in symbols:
            for trade_date in dates:
                up_limit, down_limit = (11.0, 9.0)
                if limit_overrides and (symbol, trade_date) in limit_overrides:
                    up_limit, down_limit = limit_overrides[(symbol, trade_date)]
                rows.append(
                    {
                        "symbol": symbol,
                        "date": trade_date,
                        "up_limit": up_limit,
                        "down_limit": down_limit,
                    }
                )
        write_parquet(workspace, "stk_limit", rows)

    write_parquet(
        workspace,
        "calendar",
        [{"cal_date": str(trade_date)} for trade_date in dates],
    )
    write_parquet(
        workspace,
        "instruments",
        [
            {
                "symbol": symbol,
                "name": "sample",
                "list_date": "20000101",
                "delist_date": None,
                "industry": "bank",
                "market_cap": 100.0,
            }
            for symbol in symbols
        ],
    )
    write_parquet(workspace, "stock_st", [])
    write_parquet(workspace, "suspend_d", [])


def write_parquet(workspace: Path, dataset: str, rows: list[dict[str, Any]]) -> None:
    root = workspace / "data" / dataset
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(root / "data.parquet", index=False)


def test_account_defaults_to_backtrader_engine(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    response = client.post(
        "/accounts",
        json={"account_id": "acct-alpha", "name": "alpha", "initial_cash": 10000},
    )

    assert response.status_code == 201
    assert response.json()["engine"] == "backtrader"


def test_historical_rqalpha_and_ashare_accounts_migrate_to_backtrader(tmp_path: Path) -> None:
    store = DataStore(tmp_path)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO accounts(account_id, name, initial_cash, engine, created_at)
            VALUES ('old-rqalpha', NULL, 10000, 'rqalpha', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO accounts(account_id, name, initial_cash, engine, created_at)
            VALUES ('old-ashare', NULL, 10000, 'ashare_replay', '2026-01-01T00:00:00+00:00')
            """
        )

    migrated = DataStore(tmp_path)

    assert migrated.get_account("old-rqalpha")["engine"] == "backtrader"
    assert migrated.get_account("old-ashare")["engine"] == "backtrader"


def test_backtest_requires_minute_frequency_and_qfq(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    write_workspace(workspace)
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(workspace))
    client = TestClient(create_app(tmp_path / "state"))
    client.post("/accounts", json={"account_id": "acct", "initial_cash": 10000})

    raw_response = client.post(
        "/backtests",
        json={
            "account_id": "acct",
            "price_adjustment": "raw",
            "start_date": "2026-01-02",
            "end_date": "2026-01-05",
        },
    )
    daily_response = client.post(
        "/backtests",
        json={
            "account_id": "acct",
            "frequency": "1d",
            "start_date": "2026-01-02",
            "end_date": "2026-01-05",
        },
    )

    assert raw_response.status_code == 400
    assert raw_response.json()["detail"]["error"] == "unsupported_price_adjustment"
    assert daily_response.status_code == 400
    assert daily_response.json()["detail"]["error"] == "unsupported_frequency"


def test_data_loader_builds_qfq_minutes_and_requires_adj_factor(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    write_workspace(workspace)
    loader = TushareMinuteDataLoader(workspace)

    dataset = loader.load(
        symbols={"000001.SZ"},
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 5),
    )

    assert list(dataset.minutes["symbol"].unique()) == ["000001.SZ"]
    assert {"open_qfq", "high_qfq", "low_qfq", "close_qfq", "limit_up_qfq"}.issubset(
        dataset.minutes.columns
    )
    assert dataset.minutes.iloc[0]["open_qfq"] == 10.0
    assert dataset.minutes.iloc[0]["limit_up_qfq"] == 11.0

    missing_workspace = tmp_path / "missing-adj"
    write_workspace(missing_workspace, include_adj_factor=False)
    missing_loader = TushareMinuteDataLoader(missing_workspace)

    try:
        missing_loader.load(
            symbols={"000001.SZ"},
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 5),
        )
    except ValueError as exc:
        assert str(exc) == "adjustment_data_missing"
    else:
        raise AssertionError("expected adjustment_data_missing")


def test_rule_engine_rejects_a_share_order_violations() -> None:
    rules = AShareRuleEngine()
    base_bar = {
        "symbol": "000001.SZ",
        "date": 20260102,
        "trade_time": pd.Timestamp("2026-01-02 14:57:00"),
        "close_qfq": 10.0,
        "limit_up_qfq": 11.0,
        "limit_down_qfq": 9.0,
        "volume": 1000,
        "suspended": False,
        "board": "main",
        "is_st": False,
    }

    assert rules.validate_order(
        order={"request_id": "odd-buy", "side": int(Side.BUY), "quantity": 150},
        bar=base_bar,
        cash=10000,
        position_quantity=0,
        sellable_quantity=0,
        fill_price=10.0,
    ) == "invalid_lot_size"
    assert rules.validate_order(
        order={"request_id": "no-cash", "side": int(Side.BUY), "quantity": 1000},
        bar=base_bar,
        cash=100,
        position_quantity=0,
        sellable_quantity=0,
        fill_price=10.0,
    ) == "insufficient_cash"
    assert rules.validate_order(
        order={"request_id": "t1", "side": int(Side.SELL), "quantity": 100},
        bar=base_bar,
        cash=10000,
        position_quantity=100,
        sellable_quantity=0,
        fill_price=10.0,
    ) == "t_plus_1_not_sellable"
    assert rules.validate_order(
        order={"request_id": "limit-up-buy", "side": int(Side.BUY), "quantity": 100},
        bar={**base_bar, "close_qfq": 11.0},
        cash=10000,
        position_quantity=0,
        sellable_quantity=0,
        fill_price=11.0,
    ) == "limit_up_buy_blocked"
    assert rules.validate_order(
        order={"request_id": "star-buy", "side": int(Side.BUY), "quantity": 100},
        bar={**base_bar, "symbol": "688809.SH", "board": "star"},
        cash=10000,
        position_quantity=0,
        sellable_quantity=0,
        fill_price=10.0,
    ) == "invalid_lot_size"


def test_qfq_anchors_to_global_latest_factor_not_window(tmp_path: Path) -> None:
    # adj_factor 20260102=1.0, 20260105=2.0 → 该标的全历史最新=2.0
    workspace = tmp_path / "workspace"
    write_workspace(
        workspace,
        adj_overrides={("000001.SZ", 20260102): 1.0, ("000001.SZ", 20260105): 2.0},
    )
    loader = TushareMinuteDataLoader(workspace)
    # 只查更早的一天；qfq 基准必须仍取全历史最新(2.0)，不随窗口漂移（修复 C3）
    dataset = loader.load(
        symbols={"000001.SZ"},
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
    )
    row = dataset.minutes.iloc[0]
    assert float(row["qfq_multiplier"]) == 0.5     # 1.0 / 2.0(全历史最新)
    assert float(row["open_qfq"]) == 5.0           # raw 10.0 × 0.5；窗口最新(1.0)的旧 bug 会得 10.0


def test_tick_check_uses_raw_price_not_qfq() -> None:
    rules = AShareRuleEngine()
    bar = {
        "symbol": "000001.SZ", "date": 20260102, "board": "main",
        "close": 10.0, "close_qfq": 7.1017,        # qfq 非 0.01 对齐
        "up_limit": 11.0, "down_limit": 9.0,
        "limit_up_qfq": 7.8119, "limit_down_qfq": 6.3915,
        "volume": 1000, "suspended": False, "is_st": False,
    }
    # qfq 价 7.1017 不在 0.01 网格；但 raw 价 10.0 合法 → 不应再误判 invalid_price_tick（修复 C1）
    assert rules.validate_order(
        order={"request_id": "buy", "side": int(Side.BUY), "quantity": 100},
        bar=bar, cash=100000, position_quantity=0, sellable_quantity=0,
        fill_price=7.1017, raw_fill_price=10.0,
    ) is None


def test_limit_price_compared_in_raw_space() -> None:
    rules = AShareRuleEngine()
    bar = {
        "symbol": "000001.SZ", "date": 20260102, "board": "main",
        "close": 10.0, "close_qfq": 7.1017,
        "up_limit": 11.0, "down_limit": 9.0,
        "volume": 1000, "suspended": False, "is_st": False,
    }
    # 用户限价是【真实价】：10.50 ≥ raw 成交价 10.0 → 可成交
    assert rules.validate_order(
        order={"request_id": "lp1", "side": int(Side.BUY), "quantity": 100, "limit_price": 10.50},
        bar=bar, cash=100000, position_quantity=0, sellable_quantity=0,
        fill_price=7.1017, raw_fill_price=10.0,
    ) is None
    # 9.99 < raw 成交价 10.0 → 不可成交
    assert rules.validate_order(
        order={"request_id": "lp2", "side": int(Side.BUY), "quantity": 100, "limit_price": 9.99},
        bar=bar, cash=100000, position_quantity=0, sellable_quantity=0,
        fill_price=7.1017, raw_fill_price=10.0,
    ) == "limit_price_not_marketable"


def test_http_backtest_runs_independent_minute_strategies(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    write_workspace(workspace, symbols=("000001.SZ", "688809.SH"))
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(workspace))
    client = TestClient(create_app(tmp_path / "state", run_worker=False))

    assert client.post(
        "/accounts",
        json={"account_id": "acct", "initial_cash": 20000},
    ).status_code == 201
    orders = [
        {
            "order_batch_id": "batch-main",
            "request_id": "main-buy",
            "trade_date": "2026-01-02",
            "symbol": "000001.SZ",
            "side": 1,
            "quantity": 100,
            "limit_price": 10.5,
        },
        {
            "order_batch_id": "batch-main",
            "request_id": "main-same-day-sell",
            "trade_date": "2026-01-02",
            "symbol": "000001.SZ",
            "side": 2,
            "quantity": 100,
        },
        {
            "order_batch_id": "batch-main",
            "request_id": "main-next-day-sell",
            "trade_date": "2026-01-05",
            "symbol": "000001.SZ",
            "side": 2,
            "quantity": 100,
        },
        {
            "order_batch_id": "batch-star",
            "request_id": "star-buy-200",
            "trade_date": "2026-01-02",
            "symbol": "688809.SH",
            "side": 1,
            "quantity": 200,
        },
    ]
    for order in orders:
        assert client.post("/accounts/acct/orders", json=order).status_code == 201

    response = client.post(
        "/backtests",
        json={
            "account_id": "acct",
            "frequency": "1min",
            "price_adjustment": "qfq",
            "start_date": "2026-01-02",
            "end_date": "2026-01-05",
            "strategies": [
                {
                    "strategy_id": "main-replay",
                    "strategy_type": "order_replay",
                    "initial_cash": 10000,
                    "symbols": ["000001.SZ"],
                    "params": {"order_batch_id": "batch-main"},
                },
                {
                    "strategy_id": "star-replay",
                    "strategy_type": "order_replay",
                    "initial_cash": 10000,
                    "symbols": ["688809.SH"],
                    "params": {"order_batch_id": "batch-star"},
                },
            ],
        },
    )

    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"

    drain_jobs(client.app.state.store)
    job = client.get(f"/backtests/{job['job_id']}").json()
    assert job["status"] == "completed"

    summary = client.get(f"/backtests/{job['job_id']}/summary").json()
    assert [item["strategy_id"] for item in summary["strategies"]] == [
        "main-replay",
        "star-replay",
    ]
    assert len(summary["trades"]) == 3
    assert {trade["strategy_id"] for trade in summary["trades"]} == {"main-replay", "star-replay"}
    assert summary["rejections"][0]["request_id"] == "main-same-day-sell"
    assert summary["rejections"][0]["reason"] == "t_plus_1_not_sellable"
    assert summary["price_adjustment"] == "qfq"
    assert summary["frequency"] == "1min"
    assert Path(summary["artifacts"]["account_summary"]).exists()
    assert Path(summary["artifacts"]["daily_equity"]).exists()

    daily = client.get(f"/backtests/{job['job_id']}/daily").json()
    assert len(daily) >= 1
    assert daily[0]["total_value"] > 0


def test_http_backtest_reports_missing_minute_data(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    write_workspace(workspace, include_minutes=False)
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(workspace))
    client = TestClient(create_app(tmp_path / "state", run_worker=False))
    client.post("/accounts", json={"account_id": "acct", "initial_cash": 10000})

    response = client.post(
        "/backtests",
        json={
            "account_id": "acct",
            "start_date": "2026-01-02",
            "end_date": "2026-01-05",
            "strategies": [
                {
                    "strategy_id": "main-replay",
                    "strategy_type": "order_replay",
                    "symbols": ["000001.SZ"],
                }
            ],
        },
    )

    assert response.status_code == 202
    drain_jobs(client.app.state.store)
    job = client.get(f"/backtests/{response.json()['job_id']}").json()
    assert job["status"] == "failed"
    assert job["summary"]["error"] == "minute_data_missing"


def test_symbol_crosswalk_is_tushare_first_without_rqalpha_surface(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    response = client.get("/symbols/688809.SH")

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "688809.SH"
    assert body["tushare"] == "688809.SH"
    assert body["board"] == "star"
    assert body["buy_min_shares"] == 200
    assert "rqalpha" not in body

