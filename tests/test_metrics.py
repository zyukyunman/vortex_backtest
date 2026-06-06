"""B4 指标纯函数单测：对拍手算值 + 基准相对的恒等性质。"""
from __future__ import annotations

import pytest

from vortex_backtest.metrics import compute_metrics, daily_returns, max_drawdown


def test_daily_returns_and_max_drawdown() -> None:
    assert daily_returns([100, 110, 121]) == pytest.approx([0.1, 0.1])
    # 峰值 102，谷 101 → 回撤 101/102-1
    assert max_drawdown([100, 102, 101, 104]) == pytest.approx(101 / 102 - 1)
    assert max_drawdown([100, 101, 102]) == pytest.approx(0.0)


def test_absolute_block_and_short_sample_guard() -> None:
    m = compute_metrics([100, 102, 101, 104])
    assert m["sample_days"] == 4
    assert m["low_confidence"] is True  # <60 交易日
    assert m["absolute"]["cumulative_return"] == pytest.approx(0.04)
    assert m["absolute"]["max_drawdown"] < 0
    assert m["risk_adjusted"]["sharpe"] is not None


def test_benchmark_relative_identity() -> None:
    """策略与基准完全相同 → beta≈1、alpha≈0、超额≈0、跟踪误差≈0、捕获≈1、IR 无定义。"""
    vals = [100, 102, 101, 104]
    m = compute_metrics(vals, benchmark_values=vals)
    br = m["benchmark_relative"]
    assert br["beta"] == pytest.approx(1.0, abs=1e-6)
    assert br["alpha"] == pytest.approx(0.0, abs=1e-6)
    assert br["excess_return"] == pytest.approx(0.0, abs=1e-9)
    assert br["tracking_error"] == pytest.approx(0.0, abs=1e-9)
    assert br["up_capture"] == pytest.approx(1.0, abs=1e-6)
    assert br["down_capture"] == pytest.approx(1.0, abs=1e-6)
    assert br["information_ratio"] is None  # 主动收益方差为 0


def test_benchmark_relative_skipped_when_too_short() -> None:
    # 只有 2 个净值点 → 1 个收益，n<2，不给基准相对块
    m = compute_metrics([100, 101], benchmark_values=[100, 100.5])
    assert "benchmark_relative" not in m


def test_known_numeric_values() -> None:
    """净值 [100,110,99,108.9] → 收益 [+10%,-10%,+10%]，对拍手算闭式值（年化 244）。"""
    m = compute_metrics([100.0, 110.0, 99.0, 108.9])
    a, r = m["absolute"], m["risk_adjusted"]
    assert a["cumulative_return"] == pytest.approx(0.089, abs=1e-6)
    assert a["max_drawdown"] == pytest.approx(-0.1, abs=1e-6)
    assert a["annual_volatility"] == pytest.approx(1.8037, abs=1e-3)
    assert r["sharpe"] == pytest.approx(4.5092, abs=1e-3)
    assert r["sortino"] == pytest.approx(9.0185, abs=1e-3)


def test_matches_empyrical() -> None:
    """与 empyrical 对拍（缺库则跳过）：sharpe/sortino/年化波动/最大回撤 口径一致。"""
    ep = pytest.importorskip("empyrical")
    import numpy as np

    values = [100.0, 110.0, 99.0, 108.9, 103.0, 109.0]
    rets = np.array([values[i] / values[i - 1] - 1 for i in range(1, len(values))])
    m = compute_metrics(values, periods_per_year=244)
    assert m["risk_adjusted"]["sharpe"] == pytest.approx(float(ep.sharpe_ratio(rets, annualization=244)), abs=1e-4)
    assert m["risk_adjusted"]["sortino"] == pytest.approx(float(ep.sortino_ratio(rets, annualization=244)), abs=1e-4)
    assert m["absolute"]["annual_volatility"] == pytest.approx(float(ep.annual_volatility(rets, annualization=244)), abs=1e-4)
    assert m["absolute"]["max_drawdown"] == pytest.approx(float(ep.max_drawdown(rets)), abs=1e-6)
