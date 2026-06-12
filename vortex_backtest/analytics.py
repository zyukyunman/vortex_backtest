"""分析/报告纯函数层（spec 2026-06-12 §3/§5）。

输入输出全为 python 基本类型，不读文件不碰网络——指标口径的单一真值源，金标可测。
序列约定：``series = {"YYYY-MM-DD": value}``（日频净值/收盘）。
"""
from __future__ import annotations

import math
from datetime import date
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


def relative_stats(strategy: Mapping[str, float], benchmark: Mapping[str, float],
                   *, rf: float = 0.0) -> dict[str, Any]:
    """相对类指标：对齐两序列共同日期后做日收益 OLS/超额。重叠 < 3 点 → 全 None。"""
    common = sorted(set(strategy) & set(benchmark))
    none = {"excess_return": None, "information_ratio": None, "beta": None,
            "alpha": None, "tracking_error": None}
    if len(common) < 3:
        return none
    sv = [float(strategy[d]) for d in common]
    bv = [float(benchmark[d]) for d in common]
    rs, rb = _returns(sv), _returns(bv)
    if len(rs) != len(rb) or len(rs) < 2:
        return none
    n = len(rs)
    ms, mb = _mean(rs), _mean(rb)
    var_b = sum((b - mb) ** 2 for b in rb) / (n - 1)
    if var_b < _STD_EPS:  # 与 perf_stats 同口径：浮点噪声层面的"恒定基准"→ 按 var=0 处理（beta/alpha None）
        var_b = 0.0
    cov = sum((a - ms) * (b - mb) for a, b in zip(rs, rb)) / (n - 1)
    beta = cov / var_b if var_b > 0 else None
    alpha = ((ms - rf / TRADING_DAYS) - beta * (mb - rf / TRADING_DAYS)) * TRADING_DAYS if beta is not None else None
    diff = [a - b for a, b in zip(rs, rb)]
    te = _std(diff) * math.sqrt(TRADING_DAYS)
    if te < _STD_EPS:  # 浮点噪声层面的"完全跟踪"→ 按 TE=0 处理（IR None、tracking_error 0.0）
        te = 0.0
    return {
        "excess_return": (sv[-1] / sv[0] - 1) - (bv[-1] / bv[0] - 1) if sv[0] and bv[0] else None,
        "information_ratio": (_mean(diff) * TRADING_DAYS) / te if te > 0 else None,
        "beta": beta,
        "alpha": alpha,
        "tracking_error": te,
    }


def _period_returns(series: Mapping[str, float], keylen: int) -> dict[str, list[float]]:
    """日收益按期间分桶：r_i 归属其结束日所在期间（key = date[:keylen]）。"""
    dates, values = _sorted_items(series)
    out: dict[str, list[float]] = {}
    for i in range(1, len(values)):
        if values[i - 1]:
            out.setdefault(dates[i][:keylen], []).append(values[i] / values[i - 1] - 1)
    return out


def period_stats(strategy: Mapping[str, float], benchmark: Mapping[str, float] | None,
                 *, freq: str = "Y", rf: float = 0.0) -> list[dict[str, Any]]:
    """年度(freq='Y')/月度(freq='M')统计：组收益=组内日收益连乘；回撤/波动/夏普按组内序列。"""
    keylen = 4 if freq == "Y" else 7
    sbuckets = _period_returns(strategy, keylen)
    bbuckets = _period_returns(benchmark, keylen) if benchmark else {}
    dates, values = _sorted_items(strategy)
    bdates, bvalues = _sorted_items(benchmark) if benchmark else ([], [])
    rows: list[dict[str, Any]] = []
    for period in sorted(sbuckets):
        rets = sbuckets[period]
        cum = 1.0
        for r in rets:
            cum *= 1 + r
        sub_vals = [values[i] for i in range(len(values)) if dates[i][:keylen] == period]
        std = _std(rets)
        if std < _STD_EPS:  # 与 perf_stats 同口径：浮点噪声层面的“恒定收益”→ 按 std=0 处理
            std = 0.0
        brets = bbuckets.get(period)
        bret = None
        if brets:
            bcum = 1.0
            for r in brets:
                bcum *= 1 + r
            bret = bcum - 1
        bsub_vals = [bvalues[i] for i in range(len(bvalues)) if bdates[i][:keylen] == period]
        rows.append({
            "period": period,
            "strategy_return": cum - 1,
            "benchmark_return": bret,
            "excess": (cum - 1 - bret) if bret is not None else None,
            "max_drawdown": _max_drawdown(sub_vals),
            "benchmark_max_drawdown": _max_drawdown(bsub_vals) if bsub_vals else None,
            "volatility": std * math.sqrt(TRADING_DAYS) if len(rets) >= 2 else None,
            "sharpe": ((_mean(rets) - rf / TRADING_DAYS) / std) * math.sqrt(TRADING_DAYS) if std > 0 else None,
        })
    return rows


def _snapshot_view(row: Mapping[str, Any], *, timestamp: str) -> dict[str, Any]:
    """统一持仓快照视图：补每标的 weight（市值/总资产）。"""
    total = float(row.get("total_value") or 0.0)
    positions = []
    for p in row.get("positions") or []:
        q = dict(p)
        q["weight"] = round(float(p["market_value"]) / total, 6) if total else None
        positions.append(q)
    return {"timestamp": timestamp, "cash": row.get("cash"),
            "market_value": row.get("market_value"), "total_value": row.get("total_value"),
            "positions": positions}


def daily_views(daily_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_snapshot_view(r, timestamp=str(r["trade_date"])) for r in daily_rows]


def weekly_views(daily_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """每 ISO 周最后一个交易日的 EOD 行（输入按 trade_date 升序）。"""
    by_week: dict[str, Mapping[str, Any]] = {}
    for r in daily_rows:
        d = date.fromisoformat(str(r["trade_date"]))
        iso = d.isocalendar()
        by_week[f"{iso.year}-W{iso.week:02d}"] = r  # 升序输入 → 留每周最后一行
    return [dict(_snapshot_view(r, timestamp=str(r["trade_date"])), week=wk)
            for wk, r in sorted(by_week.items())]


# 小时档显式边界 (lo, hi]：午后档下界 13:00 开区间——午休/13:00 整的 bar 属竞价噪声，不入任何档
_HOUR_BUCKETS = (("00:00:00", "10:30:00"), ("10:30:00", "11:30:00"),
                 ("13:00:00", "14:00:00"), ("14:00:00", "15:00:00"))


def hourly_views(snapshot_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """每 (交易日, 小时档) 取该档窗口内最后一根 bar：(≤10:30]、(10:30,11:30]、(13:00,14:00]、(14:00,15:00]。"""
    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    for r in snapshot_rows:  # 升序
        ts = str(r["timestamp"])
        d, t = ts[:10], ts[11:19]
        for lo, hi in _HOUR_BUCKETS:
            if lo < t <= hi:
                out[(d, hi)] = r
                break
    return [_snapshot_view(r, timestamp=f"{d}T{b}") for (d, b), r in sorted(out.items())]


def minute_views(snapshot_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_snapshot_view(r, timestamp=str(r["timestamp"])) for r in snapshot_rows]


def rebalance_events(trades: list[Mapping[str, Any]],
                     daily_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 trade_date 聚合成交为调仓事件，附调仓前后持仓 diff（qty/weight）。

    before = 前一交易日 EOD（日历轴上首日 before=空仓）；after = 当日 EOD。
    """
    axis = [str(r["trade_date"]) for r in daily_rows]
    by_date = {str(r["trade_date"]): r for r in daily_rows}

    def eod(d: str | None) -> dict[str, tuple[int, float]]:
        row = by_date.get(d) if d else None
        if not row:
            return {}
        total = float(row.get("total_value") or 0.0)
        return {str(p["symbol"]): (int(p["quantity"]),
                                   float(p["market_value"]) / total if total else 0.0)
                for p in row.get("positions") or []}

    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for t in trades:
        by_day.setdefault(str(t["trade_date"]), []).append(t)

    events: list[dict[str, Any]] = []
    for day in sorted(by_day):
        rows = by_day[day]

        def side_agg(side: int) -> list[dict[str, Any]]:
            agg: dict[str, dict[str, Any]] = {}
            for t in rows:
                if int(t["side"]) != side:
                    continue
                a = agg.setdefault(str(t["symbol"]), {"symbol": str(t["symbol"]), "quantity": 0, "amount": 0.0})
                a["quantity"] += int(t["quantity"])
                a["amount"] += float(t["amount"])
            return [dict(a, avg_price=round(a["amount"] / a["quantity"], 4) if a["quantity"] else None)
                    for _, a in sorted(agg.items())]

        idx = axis.index(day) if day in axis else -1
        before = eod(axis[idx - 1]) if idx > 0 else {}
        after = eod(day)
        touched = {str(t["symbol"]) for t in rows}
        touched |= {s for s in set(before) | set(after)
                    if before.get(s, (0, 0.0))[0] != after.get(s, (0, 0.0))[0]}
        diff = [{"symbol": s,
                 "qty_before": before.get(s, (0, 0.0))[0], "qty_after": after.get(s, (0, 0.0))[0],
                 "weight_before": round(before.get(s, (0, 0.0))[1], 6),
                 "weight_after": round(after.get(s, (0, 0.0))[1], 6)}
                for s in sorted(touched)]
        day_row = by_date.get(day) or {}
        events.append({
            "trade_date": day, "n_trades": len(rows),
            "buys": side_agg(1), "sells": side_agg(2),
            "fees_total": round(sum(float(t.get("commission", 0)) + float(t.get("stamp_tax", 0))
                                    + float(t.get("transfer_fee", 0)) for t in rows), 4),
            "realized_pnl_total": round(sum(float(t.get("realized_pnl", 0)) for t in rows), 4),
            "position_diff": diff,
            "cash_after": day_row.get("cash"), "total_value_after": day_row.get("total_value"),
        })
    return events
