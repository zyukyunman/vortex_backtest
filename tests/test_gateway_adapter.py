"""GatewayDataAdapter 前复权 oracle 测试（design/18 B4）：前复权 PIT 锚点 + 富 bar 装配，mock 网关响应。

N8 起默认口径为不复权 RAW（见 test_gateway_adapter_raw.py）；前复权(``price_mode="qfq"``)保留为金标 oracle，
本文件显式测该 oracle 路径。"""
from __future__ import annotations

from datetime import date

import pytest

from vortex_backtest.gateway_adapter import GatewayDataAdapter


def _gateway_response():
    return {"as_of": "2026-05-06T15:00:00", "results": {
        "stk_mins": {"rows": [
            {"symbol": "600519.SH", "trade_time": "2026-05-06 09:30:00", "date": 20260506, "freq": "1min",
             "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100000},
            {"symbol": "600519.SH", "trade_time": "2026-05-06 09:31:00", "date": 20260506, "freq": "1min",
             "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100000},
        ]},
        "adj_factor": {"rows": [{"symbol": "600519.SH", "date": 20260506, "adj_factor": 2.0}]},
        "stk_limit": {"rows": [{"symbol": "600519.SH", "date": 20260506, "up_limit": 11.0, "down_limit": 9.0}]},
        "suspend_d": {"rows": []},
        "stock_st": {"rows": []},
    }}


def test_qfq_anchor_realistic_magnitude(monkeypatch):
    """前复权 PIT 锚点：锚在 ≤as_of 最新因子 → 最新日 multiplier=1，量级真实（不被累计因子放大）。"""
    adapter = GatewayDataAdapter(base_url="http://x")
    monkeypatch.setattr(adapter, "_query", lambda as_of, datasets: _gateway_response())
    ds = adapter.load(symbols={"600519.SH"}, start_date=date(2026, 5, 6), end_date=date(2026, 5, 6),
                      as_of="2026-05-06T15:00:00", price_mode="qfq")
    df = ds.minutes
    assert len(df) == 2
    row = df.iloc[0]
    # 单一可见因子（adj=2）即锚 → multiplier=2/2=1 → 前复权价 = 原始价（量级真实，可现金结算）
    assert row["close_qfq"] == 10.0
    assert row["open_qfq"] == 10.0
    assert row["limit_up_qfq"] == 11.0
    assert row["limit_down_qfq"] == 9.0
    # 原始价仍在（validate 用 raw 判 tick/涨跌停）
    assert row["close"] == 10.0
    assert row["up_limit"] == 11.0
    assert row["suspended"] == False
    assert row["is_st"] == False
    assert row["board"]  # market_board 派生
    assert ds.calendar == [20260506]


def test_qfq_anchor_scales_history(monkeypatch):
    """跨除权：前复权把历史价缩放到 ≤as_of 最新口径（PIT 安全，不含未来除权）。"""
    adapter = GatewayDataAdapter(base_url="http://x")

    def resp(as_of, datasets):
        return {"results": {
            "stk_mins": {"rows": [
                {"symbol": "000001.SZ", "trade_time": "2026-05-05 09:30:00", "date": 20260505, "freq": "1min",
                 "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1000},
                {"symbol": "000001.SZ", "trade_time": "2026-05-06 09:30:00", "date": 20260506, "freq": "1min",
                 "open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0, "volume": 1000},
            ]},
            # 除权：0505 因子 2，0506 因子 4（≤as_of 最新=4 → 锚）
            "adj_factor": {"rows": [
                {"symbol": "000001.SZ", "date": 20260505, "adj_factor": 2.0},
                {"symbol": "000001.SZ", "date": 20260506, "adj_factor": 4.0}]},
            "stk_limit": {"rows": [
                {"symbol": "000001.SZ", "date": 20260505, "up_limit": 11.0, "down_limit": 9.0},
                {"symbol": "000001.SZ", "date": 20260506, "up_limit": 5.5, "down_limit": 4.5}]},
            "suspend_d": {"rows": []}, "stock_st": {"rows": []},
        }}

    monkeypatch.setattr(adapter, "_query", resp)
    df = adapter.load(symbols={"000001.SZ"}, start_date=date(2026, 5, 5), end_date=date(2026, 5, 6),
                      as_of="2026-05-06T15:00:00", price_mode="qfq").minutes.sort_values("date")
    # 0505: mult=2/4=0.5 → 前复权 10×0.5=5（与 0506 的 5 连续）；0506: mult=1 → 5
    assert list(df["close_qfq"]) == [5.0, 5.0]


def test_missing_minutes_raises(monkeypatch):
    adapter = GatewayDataAdapter(base_url="http://x")
    monkeypatch.setattr(adapter, "_query", lambda as_of, datasets: {"results": {"stk_mins": {"rows": []}}})
    with pytest.raises(ValueError):
        adapter.load(symbols={"000001.SZ"}, start_date=date(2026, 5, 6), end_date=date(2026, 5, 6), as_of="2026-05-06T15:00:00")


def test_no_url_errors():
    from vortex_backtest.gateway_adapter import GatewayDataError
    adapter = GatewayDataAdapter(base_url="")
    with pytest.raises(GatewayDataError):
        adapter._query("2026-05-06T15:00:00", [])


def _exdate_resp(as_of, datasets):
    """mock 网关：0505 raw=10/adj=2，0506 raw=5/adj=4（除权拆股）。按 as_of 日期裁剪。"""
    asd = int(as_of[:10].replace("-", ""))
    rows = {
        "stk_mins": [
            {"symbol": "600519.SH", "trade_time": "2026-05-05 09:30:00", "date": 20260505, "freq": "1min", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1000},
            {"symbol": "600519.SH", "trade_time": "2026-05-06 09:30:00", "date": 20260506, "freq": "1min", "open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0, "volume": 1000},
        ],
        "adj_factor": [
            {"symbol": "600519.SH", "date": 20260505, "adj_factor": 2.0},
            {"symbol": "600519.SH", "date": 20260506, "adj_factor": 4.0}],
        "stk_limit": [
            {"symbol": "600519.SH", "date": 20260505, "up_limit": 11.0, "down_limit": 9.0},
            {"symbol": "600519.SH", "date": 20260506, "up_limit": 5.5, "down_limit": 4.5}],
        "suspend_d": [], "stock_st": [],
    }
    out = {}
    for k, rs in rows.items():
        out[k] = {"rows": [r for r in rs if int(r["date"]) <= asd]}
    return {"results": out}


def _qfq_0505(adapter, as_of, anchor):
    from datetime import date
    df = adapter.load(symbols={"600519.SH"}, start_date=date(2026, 5, 5), end_date=date(2026, 5, 6),
                      as_of=as_of, anchor_date=anchor, price_mode="qfq")
    d = df.minutes
    return float(d[d["date"] == 20260505]["close_qfq"].iloc[0])


def test_fixed_anchor_consistent_across_exdate(monkeypatch):
    """固定锚：0505 价在 as_of=0505 与 as_of=0506（跨除权）下一致 → 跨除权 return 正确。"""
    from datetime import date
    ad = GatewayDataAdapter(base_url="http://x")
    monkeypatch.setattr(ad, "_query", _exdate_resp)
    anchor = date(2026, 5, 5)
    p_before = _qfq_0505(ad, "2026-05-05T15:00:00", anchor)  # 只见 0505，锚=2 → 10×2/2=10
    p_after = _qfq_0505(ad, "2026-05-06T15:00:00", anchor)   # 见到 0506，锚仍=2(固定) → 10
    assert p_before == p_after == 10.0


def test_per_as_of_anchor_drifts(monkeypatch):
    """对照：per-as_of(无 anchor_date) 跨除权会漂移（证明固定锚的必要性）。"""
    ad = GatewayDataAdapter(base_url="http://x")
    monkeypatch.setattr(ad, "_query", _exdate_resp)
    p_before = _qfq_0505(ad, "2026-05-05T15:00:00", None)  # 锚=2 → 10
    p_after = _qfq_0505(ad, "2026-05-06T15:00:00", None)   # 锚=4(最新) → 10×2/4=5
    assert p_before == 10.0 and p_after == 5.0 and p_before != p_after
