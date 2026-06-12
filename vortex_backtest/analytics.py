"""分析/报告纯函数层（spec 2026-06-12 §3/§5）。

输入输出全为 python 基本类型，不读文件不碰网络——指标口径的单一真值源，金标可测。
序列约定：``series = {"YYYY-MM-DD": value}``（日频净值/收盘）。
"""
from __future__ import annotations

import math
from datetime import date  # noqa: F401  (Task 3/6 调仓记录/多粒度持仓使用)
from typing import Any, Mapping

TRADING_DAYS = 252
LOW_CONFIDENCE_DAYS = 60
# 日收益样本 std 低于此阈值视为 0（浮点噪声层面；真实日波动率 ~1e-3 量级）。
# 恒定收益序列因 float 舍入 std≈1e-16~1e-13 恒非零，不清零会伪造天文数字 sharpe。
_STD_EPS = 1e-9


def _sorted_items(series: Mapping[str, float]) -> tuple[list[str], list[float]]:
    dates = sorted(series)
    return dates, [float(series[d]) for d in dates]


def _returns(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1]]


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:  # 样本标准差（ddof=1）
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _max_drawdown(values: list[float]) -> float:
    hw, mdd = float("-inf"), 0.0
    for v in values:
        hw = max(hw, v)
        if hw > 0:
            mdd = min(mdd, v / hw - 1)
    return mdd


def perf_stats(series: Mapping[str, float], *, rf: float = 0.0) -> dict[str, Any]:
    """绝对类指标包。不足 2 个点 → 比率类置 None（不伪装）。"""
    _, values = _sorted_items(series)
    if len(values) < 2:
        return {"total_return": 0.0, "annual_return": None, "sharpe": None,
                "volatility": None, "max_drawdown": 0.0, "win_days_ratio": None,
                "n_days": len(values)}
    rets = _returns(values)
    n = len(rets)
    tr = values[-1] / values[0] - 1 if values[0] else 0.0
    std = _std(rets)
    if std < _STD_EPS:  # 浮点噪声层面的“恒定收益”→ 按 std=0 处理（sharpe None、vol=0）
        std = 0.0
    return {
        "total_return": tr,
        "annual_return": (1 + tr) ** (TRADING_DAYS / n) - 1 if n else None,
        "sharpe": ((_mean(rets) - rf / TRADING_DAYS) / std) * math.sqrt(TRADING_DAYS) if std > 0 else None,
        "volatility": std * math.sqrt(TRADING_DAYS),
        "max_drawdown": _max_drawdown(values),
        "win_days_ratio": sum(1 for r in rets if r > 0) / n if n else None,
        "n_days": len(values),
    }


def equity_curve(series: Mapping[str, float], benchmark: Mapping[str, float] | None = None) -> dict[str, Any]:
    """起点 1.0 的对齐净值曲线 + 逐日回撤。基准对齐到策略日期轴，缺数日 forward-fill。"""
    dates, values = _sorted_items(series)
    if not values:
        return {"dates": [], "strategy": [], "benchmark": None, "drawdown": []}
    base = values[0] or 1.0
    strat = [round(v / base, 6) for v in values]
    hw, dd = float("-inf"), []
    for v in strat:
        hw = max(hw, v)
        dd.append(round(v / hw - 1, 6) if hw > 0 else 0.0)
    bench = None
    if benchmark:
        bbase, carry, bench = None, None, []
        for d in dates:
            if d in benchmark:
                carry = float(benchmark[d])
                if bbase is None:
                    bbase = carry or 1.0
            bench.append(round(carry / bbase, 6) if carry is not None and bbase else None)
    return {"dates": dates, "strategy": strat, "benchmark": bench, "drawdown": dd}
