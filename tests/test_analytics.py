"""analytics 金标：手工构造已知序列，断言指标到 1e-9。"""
import math

import pytest

from vortex_backtest import analytics


def s(pairs):  # 简写：[(date, value)] -> series dict
    return {d: v for d, v in pairs}


def test_perf_stats_constant_growth():
    # 恒定日收益 0.1%：TR=(1.001)^4-1；std=0 → sharpe None、vol=0；无回撤
    series = s([("2026-01-05", 100.0), ("2026-01-06", 100.1), ("2026-01-07", 100.2001),
                ("2026-01-08", 100.3003001), ("2026-01-09", 100.40060040)])
    st = analytics.perf_stats(series)
    assert st["total_return"] == pytest.approx(1.001 ** 4 - 1, rel=1e-9)
    assert st["annual_return"] == pytest.approx((1.001 ** 4) ** (252 / 4) - 1, rel=1e-9)
    assert st["sharpe"] is None and st["volatility"] == pytest.approx(0.0)
    assert st["max_drawdown"] == pytest.approx(0.0)
    assert st["win_days_ratio"] == pytest.approx(1.0)
    assert st["n_days"] == 5


def test_perf_stats_known_drawdown():
    series = s([("2026-01-05", 100.0), ("2026-01-06", 110.0), ("2026-01-07", 99.0), ("2026-01-08", 121.0)])
    st = analytics.perf_stats(series)
    assert st["max_drawdown"] == pytest.approx(99.0 / 110.0 - 1, rel=1e-12)  # -0.1
    assert st["total_return"] == pytest.approx(0.21, rel=1e-12)


def test_perf_stats_degenerate():
    assert analytics.perf_stats({})["total_return"] == 0.0
    one = analytics.perf_stats({"2026-01-05": 100.0})
    assert one["annual_return"] is None and one["n_days"] == 1


def test_equity_curve_rebased_and_drawdown():
    series = s([("2026-01-05", 100.0), ("2026-01-06", 110.0), ("2026-01-07", 99.0)])
    bench = s([("2026-01-05", 4000.0), ("2026-01-07", 4400.0)])  # 06 缺数 → carry
    eq = analytics.equity_curve(series, bench)
    assert eq["dates"] == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert eq["strategy"] == [1.0, 1.1, 0.99]
    assert eq["benchmark"] == [1.0, 1.0, 1.1]  # 缺数日 forward-fill
    assert eq["drawdown"][2] == pytest.approx(0.99 / 1.1 - 1)
    assert analytics.equity_curve(series, None)["benchmark"] is None


def test_relative_stats_beta2_alpha0():
    # 策略日收益恒为基准 2 倍 → beta=2、alpha=0、TE=std(rb)*sqrt(252)
    bench_vals, strat_vals = [100.0], [100.0]
    rb_seq = [0.01, -0.02, 0.015, 0.005]
    for r in rb_seq:
        bench_vals.append(bench_vals[-1] * (1 + r))
        strat_vals.append(strat_vals[-1] * (1 + 2 * r))
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    strat = dict(zip(dates, strat_vals))
    bench = dict(zip(dates, bench_vals))
    rel = analytics.relative_stats(strat, bench)
    assert rel["beta"] == pytest.approx(2.0, rel=1e-9)
    assert rel["alpha"] == pytest.approx(0.0, abs=1e-9)
    rs = [2 * r for r in rb_seq]
    diff = [a - b for a, b in zip(rs, rb_seq)]
    m = sum(diff) / len(diff)
    te = math.sqrt(sum((x - m) ** 2 for x in diff) / (len(diff) - 1)) * math.sqrt(252)
    assert rel["tracking_error"] == pytest.approx(te, rel=1e-9)
    assert rel["excess_return"] == pytest.approx(
        (strat_vals[-1] / strat_vals[0] - 1) - (bench_vals[-1] / bench_vals[0] - 1), rel=1e-9)


def test_relative_stats_insufficient_overlap():
    rel = analytics.relative_stats({"2026-01-05": 1.0}, {"2026-01-06": 1.0})
    assert rel["beta"] is None and rel["information_ratio"] is None


def test_period_stats_monthly_split():
    # 1 月两天 +1%/日，2 月一天 -2%：月收益分别 ≈2.01% 与 -2%
    strat = s([("2026-01-29", 100.0), ("2026-01-30", 101.0), ("2026-02-02", 102.01), ("2026-02-03", 99.9698)])
    rows = analytics.period_stats(strat, None, freq="M")
    assert [r["period"] for r in rows] == ["2026-01", "2026-02"]
    assert rows[0]["strategy_return"] == pytest.approx(0.01, rel=1e-9)        # 01-30 一笔日收益
    # 02 月：r(02-02)=1%、r(02-03)=-2% → (1.01*0.98)-1
    assert rows[1]["strategy_return"] == pytest.approx(1.01 * 0.98 - 1, rel=1e-9)
    assert rows[1]["benchmark_return"] is None and rows[1]["max_drawdown"] <= 0


def _mk_pos(symbol, qty, mv):
    return {"strategy_id": "t", "symbol": symbol, "quantity": qty, "available_quantity": qty,
            "cost_basis": 10.0, "last_price": mv / qty if qty else 0, "market_value": mv,
            "unrealized_pnl": 0.0, "unrealized_pnl_ratio": 0.0}


def _mk_daily(trade_date, cash, mv, positions):
    return {"strategy_id": "t", "trade_date": trade_date, "cash": cash, "market_value": mv,
            "total_value": cash + mv, "daily_pnl": 0, "total_return": 0, "drawdown": 0,
            "positions": positions, "trades": [], "rejections": []}


def _mk_snap(ts, cash, mv, positions):
    return {"strategy_id": "t", "timestamp": ts, "frequency": "1min", "cash": cash,
            "market_value": mv, "total_value": cash + mv, "positions": positions,
            "trades": [], "rejections": []}


def test_daily_views_weight():
    rows = [_mk_daily("2026-02-03", 900.0, 100.0, [_mk_pos("000001.SZ", 10, 100.0)])]
    views = analytics.daily_views(rows)
    assert views[0]["timestamp"] == "2026-02-03"
    assert views[0]["positions"][0]["weight"] == pytest.approx(0.1)


def test_weekly_views_last_trading_day_of_week():
    # 02-03(周二)/02-06(周五) 同属 2026-W06；02-09(周一) 属 W07 → 每周留最后一行
    rows = [_mk_daily("2026-02-03", 1000, 0, []), _mk_daily("2026-02-06", 1100, 0, []),
            _mk_daily("2026-02-09", 1200, 0, [])]
    views = analytics.weekly_views(rows)
    assert [(v["week"], v["timestamp"]) for v in views] == [("2026-W06", "2026-02-06"), ("2026-W07", "2026-02-09")]


def test_hourly_views_buckets():
    snaps = [_mk_snap("2026-02-03T09:31:00", 1, 0, []), _mk_snap("2026-02-03T10:30:00", 2, 0, []),
             _mk_snap("2026-02-03T11:29:00", 3, 0, []), _mk_snap("2026-02-03T14:55:00", 4, 0, []),
             _mk_snap("2026-02-03T15:00:00", 5, 0, [])]
    views = analytics.hourly_views(snaps)
    # (≤10:30]=cash2、(10:30,11:30]=cash3、(14:00,15:00] 桶内最后一根=15:00 的 cash5
    assert [(v["timestamp"], v["cash"]) for v in views] == [
        ("2026-02-03T10:30:00", 2), ("2026-02-03T11:30:00", 3), ("2026-02-03T15:00:00", 5)]


def test_minute_views_passthrough():
    snaps = [_mk_snap("2026-02-03T09:31:00", 990.0, 10.0, [_mk_pos("000001.SZ", 1, 10.0)])]
    v = analytics.minute_views(snaps)
    assert v[0]["timestamp"] == "2026-02-03T09:31:00" and v[0]["positions"][0]["weight"] == pytest.approx(0.01)


def test_rebalance_events_diff_and_aggregation():
    daily = [
        _mk_daily("2026-02-02", 1000.0, 0.0, []),
        _mk_daily("2026-02-03", 890.0, 110.0, [_mk_pos("000001.SZ", 10, 110.0)]),
        _mk_daily("2026-02-04", 1002.0, 0.0, []),
    ]
    trades = [
        {"trade_date": "2026-02-03", "symbol": "000001.SZ", "side": 1, "quantity": 6,
         "amount": 60.0, "commission": 5.0, "stamp_tax": 0.0, "transfer_fee": 0.01, "realized_pnl": 0.0},
        {"trade_date": "2026-02-03", "symbol": "000001.SZ", "side": 1, "quantity": 4,
         "amount": 44.0, "commission": 5.0, "stamp_tax": 0.0, "transfer_fee": 0.01, "realized_pnl": 0.0},
        {"trade_date": "2026-02-04", "symbol": "000001.SZ", "side": 2, "quantity": 10,
         "amount": 112.0, "commission": 5.0, "stamp_tax": 0.056, "transfer_fee": 0.01, "realized_pnl": 2.0},
    ]
    events = analytics.rebalance_events(trades, daily)
    assert [e["trade_date"] for e in events] == ["2026-02-03", "2026-02-04"]
    buy_day = events[0]
    assert buy_day["n_trades"] == 2 and buy_day["sells"] == []
    assert buy_day["buys"] == [{"symbol": "000001.SZ", "quantity": 10, "amount": 104.0,
                                "avg_price": pytest.approx(10.4)}]
    assert buy_day["fees_total"] == pytest.approx(10.02)
    assert buy_day["position_diff"] == [{"symbol": "000001.SZ", "qty_before": 0, "qty_after": 10,
                                         "weight_before": 0.0, "weight_after": pytest.approx(0.11)}]
    assert buy_day["cash_after"] == 890.0 and buy_day["total_value_after"] == 1000.0
    sell_day = events[1]
    assert sell_day["position_diff"][0]["qty_after"] == 0
    assert sell_day["realized_pnl_total"] == pytest.approx(2.0)
