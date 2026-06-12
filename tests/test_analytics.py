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
