"""绩效指标（B4，design/13 §7.2）。

从**日净值序列**（`summary.daily[].total_value`）算绝对 / 风险调整 / 基准相对指标，纯 numpy。

口径：
- 日收益 `r_t = V_t / V_{t-1} - 1`（V = 日终 total_value）。
- 年化按 A 股 **244** 交易日（`PERIODS_PER_YEAR`，可配）；无风险利率 `rf` 以年化传入。
- **短样本护栏**：有效净值天数 `< MIN_SAMPLE_DAYS(60)` 时 `low_confidence=True`——风险调整 /
  年化类指标统计意义不足，前端应置灰；值仍给出便于调试。累计收益 / 回撤不受此限。
- 公式贴合 empyrical 习惯（sharpe/sortino/calmar/alpha-beta/IR/capture）。本模块不依赖 empyrical，
  以便镜像精简、单测可对拍手算值。
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np

PERIODS_PER_YEAR = 244
MIN_SAMPLE_DAYS = 60


def _num(x: object) -> float | None:
    """转可 JSON 的有限 float，否则 None（屏蔽 nan/inf/除零）。"""
    if x is None:
        return None
    try:
        f = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 6)


def daily_returns(values: Sequence[float]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(values[:-1], values[1:]):
        try:
            p, c = float(prev), float(cur)
        except (TypeError, ValueError):
            out.append(0.0)
            continue
        out.append(c / p - 1.0 if p else 0.0)
    return out


def max_drawdown(values: Sequence[float]) -> float | None:
    peak = float("-inf")
    mdd = 0.0
    seen = False
    for v in values:
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        seen = True
        peak = max(peak, x)
        if peak > 0:
            mdd = min(mdd, x / peak - 1.0)
    return mdd if seen else None


def _annualized(values: Sequence[float], n_periods: int, ppy: int) -> float | None:
    if n_periods < 1 or not values:
        return None
    v0, vn = float(values[0]), float(values[-1])
    if v0 <= 0 or vn <= 0:
        return None
    return (vn / v0) ** (ppy / n_periods) - 1.0


def _capture(strat: np.ndarray, bench: np.ndarray, mask: np.ndarray) -> float | None:
    if mask.sum() == 0:
        return None
    s = float(np.prod(1.0 + strat[mask]) - 1.0)
    b = float(np.prod(1.0 + bench[mask]) - 1.0)
    return s / b if b != 0 else None


def compute_metrics(
    values: Sequence[float],
    *,
    benchmark_values: Sequence[float] | None = None,
    periods_per_year: int = PERIODS_PER_YEAR,
    rf: float = 0.0,
) -> dict:
    """`values` = 日终净值序列。`benchmark_values`（可选）= 同日期对齐的基准净值序列
    （rebase 与否不影响收益/指标）。返回分组指标 + 样本护栏标记。"""
    vals = [float(v) for v in values]
    n_vals = len(vals)
    rets = np.array(daily_returns(vals), dtype=float)
    n = rets.size

    out: dict = {
        "sample_days": n_vals,
        "low_confidence": n_vals < MIN_SAMPLE_DAYS,
        "periods_per_year": periods_per_year,
    }

    cum = (vals[-1] / vals[0] - 1.0) if (n_vals >= 2 and vals[0]) else None
    ann = _annualized(vals, n, periods_per_year)
    vol = float(rets.std(ddof=1) * math.sqrt(periods_per_year)) if n >= 2 else None
    mdd = max_drawdown(vals)
    out["absolute"] = {
        "cumulative_return": _num(cum),
        "annual_return": _num(ann),
        "annual_volatility": _num(vol),
        "max_drawdown": _num(mdd),
    }

    sharpe = sortino = calmar = var95 = omega = None
    if n >= 2:
        per_rf = rf / periods_per_year
        sd = rets.std(ddof=1)
        if sd > 0:
            sharpe = float((rets - per_rf).mean() / sd * math.sqrt(periods_per_year))
        # 下行偏差 = 全样本 min(r-MAR,0) 的均方根（MAR=0，贴 empyrical 口径；
        # 注意分母是对**所有**期取均值，而非只对下行期）。
        downside = np.minimum(rets, 0.0)
        dd = math.sqrt(float((downside ** 2).mean())) if n else 0.0
        if dd > 0:
            sortino = float(rets.mean() / dd * math.sqrt(periods_per_year))
        if ann is not None and mdd is not None and mdd < 0:
            calmar = float(ann / abs(mdd))
        var95 = float(np.percentile(rets, 5))
        gains = float(rets[rets > 0].sum())
        losses = float(-rets[rets < 0].sum())
        if losses > 0:
            omega = gains / losses
    out["risk_adjusted"] = {
        "sharpe": _num(sharpe),
        "sortino": _num(sortino),
        "calmar": _num(calmar),
        "var_95": _num(var95),
        "omega": _num(omega),
    }

    if benchmark_values is not None and len(benchmark_values) == n_vals and n >= 2:
        bvals = [float(v) for v in benchmark_values]
        b = np.array(daily_returns(bvals), dtype=float)
        bcum = (bvals[-1] / bvals[0] - 1.0) if bvals[0] else None
        bann = _annualized(bvals, n, periods_per_year)
        active = rets - b
        astd = active.std(ddof=1)
        te = float(astd * math.sqrt(periods_per_year)) if n >= 2 else None
        ir = float(active.mean() / astd * math.sqrt(periods_per_year)) if astd > 0 else None
        bvar = float(b.var(ddof=1))
        beta = float(np.cov(rets, b, ddof=1)[0, 1] / bvar) if bvar > 0 else None
        alpha = (
            float(ann - beta * bann)
            if (ann is not None and bann is not None and beta is not None)
            else None
        )
        out["benchmark_relative"] = {
            "excess_return": _num(cum - bcum) if (cum is not None and bcum is not None) else None,
            "annual_excess": _num(ann - bann) if (ann is not None and bann is not None) else None,
            "alpha": _num(alpha),
            "beta": _num(beta),
            "information_ratio": _num(ir),
            "tracking_error": _num(te),
            "up_capture": _num(_capture(rets, b, b > 0.0)),
            "down_capture": _num(_capture(rets, b, b < 0.0)),
        }
    return out
