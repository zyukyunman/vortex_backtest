"""GatewayDataAdapter RAW 口径 + 分红取数（design/18 N8 真实账户口径）。

N8 把撮合/估值基准从 N5 前复权改为 **不复权 RAW 价**（close_qfq==close、multiplier=1），
分红送转改由除权日显式入账（替代前复权把分红吸进价）。前复权路保留为金标 oracle（price_mode="qfq"）。
"""
from __future__ import annotations

from datetime import date

import pytest

from vortex_backtest.gateway_adapter import GatewayDataAdapter


def _raw_resp(as_of, datasets):
    """单日行情，给一个会让 qfq≠raw 的 adj=2.0；raw 模式应忽略 adj。"""
    return {"results": {
        "stk_mins": {"rows": [
            {"symbol": "600519.SH", "trade_time": "2026-05-06 09:30:00", "date": 20260506, "freq": "1min",
             "open": 1800.0, "high": 1810.0, "low": 1790.0, "close": 1805.0, "volume": 1000},
        ]},
        "adj_factor": {"rows": [{"symbol": "600519.SH", "date": 20260506, "adj_factor": 2.0}]},
        "stk_limit": {"rows": [{"symbol": "600519.SH", "date": 20260506, "up_limit": 1980.0, "down_limit": 1620.0}]},
        "suspend_d": {"rows": []}, "stock_st": {"rows": []},
    }}


def _exdate_resp(as_of, datasets):
    """0505 raw=10/adj=2，0506 raw=5/adj=4（除权拆股）。"""
    return {"results": {
        "stk_mins": {"rows": [
            {"symbol": "600519.SH", "trade_time": "2026-05-05 09:30:00", "date": 20260505, "freq": "1min", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1000},
            {"symbol": "600519.SH", "trade_time": "2026-05-06 09:30:00", "date": 20260506, "freq": "1min", "open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0, "volume": 1000},
        ]},
        "adj_factor": {"rows": [
            {"symbol": "600519.SH", "date": 20260505, "adj_factor": 2.0},
            {"symbol": "600519.SH", "date": 20260506, "adj_factor": 4.0}]},
        "stk_limit": {"rows": [
            {"symbol": "600519.SH", "date": 20260505, "up_limit": 11.0, "down_limit": 9.0},
            {"symbol": "600519.SH", "date": 20260506, "up_limit": 5.5, "down_limit": 4.5}]},
        "suspend_d": {"rows": []}, "stock_st": {"rows": []},
    }}


def test_raw_mode_uses_unadjusted_prices(monkeypatch):
    adapter = GatewayDataAdapter(base_url="http://x")

    def resp(as_of, datasets):
        names = [d["dataset"] for d in datasets]
        assert "adj_factor" not in names  # raw 模式不取复权因子
        return _raw_resp(as_of, datasets)

    monkeypatch.setattr(adapter, "_query", resp)
    ds = adapter.load(symbols={"600519.SH"}, start_date=date(2026, 5, 6), end_date=date(2026, 5, 6),
                      as_of="2026-05-06T15:00:00", price_mode="raw")
    row = ds.minutes.iloc[0]
    assert row["close_qfq"] == 1805.0 == row["close"]   # 撮合/估值用不复权 RAW
    assert row["open_qfq"] == 1800.0 == row["open"]
    assert row["limit_up_qfq"] == 1980.0 == row["up_limit"]
    assert row["limit_down_qfq"] == 1620.0 == row["down_limit"]


def test_default_price_mode_is_raw(monkeypatch):
    """默认即 RAW（N8 口径替代 N5 前复权）。adj=2/4 若是 qfq 会缩放历史，raw 不缩放。"""
    adapter = GatewayDataAdapter(base_url="http://x")
    monkeypatch.setattr(adapter, "_query", _exdate_resp)
    ds = adapter.load(symbols={"600519.SH"}, start_date=date(2026, 5, 5), end_date=date(2026, 5, 6),
                      as_of="2026-05-06T15:00:00")  # 不传 price_mode → 默认 raw
    df = ds.minutes.sort_values("date")
    assert list(df["close_qfq"]) == [10.0, 5.0]   # 原始价（qfq 固定锚会是 [10,10]/[5,5]）


def test_qfq_mode_still_available_as_oracle(monkeypatch):
    """前复权路保留为金标 oracle：固定锚 → 跨除权历史价缩放到锚口径。"""
    adapter = GatewayDataAdapter(base_url="http://x")
    monkeypatch.setattr(adapter, "_query", _exdate_resp)
    ds = adapter.load(symbols={"600519.SH"}, start_date=date(2026, 5, 5), end_date=date(2026, 5, 6),
                      as_of="2026-05-06T15:00:00", price_mode="qfq")  # per-as_of 锚=最新4
    df = ds.minutes.sort_values("date")
    assert list(df["close_qfq"]) == [5.0, 5.0]   # 0505: 10×2/4=5；0506: 5×4/4=5（连续）


def test_load_dividends_filters_to_implemented(monkeypatch):
    """只取实施(有 ex_date)的分红；预案(ex_date 空)被滤除（未除权不入账）。"""
    adapter = GatewayDataAdapter(base_url="http://x")

    def resp(as_of, datasets):
        assert datasets[0]["dataset"] == "dividend"
        return {"results": {"dividend": {"rows": [
            {"symbol": "600519.SH", "ex_date": 20260420, "cash_div_tax": 25.0, "stk_div": 0.0,
             "stk_bo_rate": 0.0, "stk_co_rate": 0.0, "div_proc": "实施"},
            {"symbol": "600519.SH", "ex_date": None, "cash_div_tax": 28.0, "stk_div": 0.0,
             "stk_bo_rate": None, "stk_co_rate": None, "div_proc": "预案"},
        ]}}}

    monkeypatch.setattr(adapter, "_query", resp)
    out = adapter.load_dividends(symbols={"600519.SH"}, as_of="2026-05-06T15:00:00")
    assert len(out) == 1
    d = out[0]
    assert d["symbol"] == "600519.SH" and int(d["ex_date"]) == 20260420
    assert d["cash_div_tax"] == 25.0 and d["stk_div"] == 0.0


def test_load_dividends_empty(monkeypatch):
    adapter = GatewayDataAdapter(base_url="http://x")
    monkeypatch.setattr(adapter, "_query", lambda as_of, datasets: {"results": {"dividend": {"rows": []}}})
    assert adapter.load_dividends(symbols={"600519.SH"}, as_of="2026-05-06T15:00:00") == []
