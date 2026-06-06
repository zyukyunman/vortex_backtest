"""策略中心聚合(派生模型)。

策略 = 跨回测作业里出现过的 `strategy_id`;一次带该 strategy_id 的回测 = 它的一次 run。
本模块把作业(store 原始行)聚合成"按策略"的视图:历次 run、最新、最优、运行中、排行榜、A/B 对比。
纯函数、便于单测;不引入"建策略"写模型(标签/收藏放 store.strategy_meta)。
"""
from __future__ import annotations

import json
from typing import Any

from .metrics import compute_metrics
from .symbols import market_board

# 可排序指标(均"越大越优":max_drawdown 为负,越接近 0 即值越大越好)
METRICS = ("total_return", "annual_return", "sharpe", "sortino", "calmar", "max_drawdown")


def _run_metrics(daily: list[dict]) -> dict:
    values = [float(d["total_value"]) for d in daily if d.get("total_value") is not None]
    if len(values) < 2:
        return {k: None for k in ("total_return", "annual_return", "annual_volatility",
                                  "sharpe", "sortino", "calmar", "max_drawdown")} | {"sample_days": len(values)}
    m = compute_metrics(values)
    a, r = m["absolute"], m["risk_adjusted"]
    return {
        "total_return": a["cumulative_return"], "annual_return": a["annual_return"],
        "annual_volatility": a["annual_volatility"], "max_drawdown": a["max_drawdown"],
        "sharpe": r["sharpe"], "sortino": r["sortino"], "calmar": r["calmar"],
        "sample_days": m["sample_days"],
    }


def _symbols_of(strat: dict) -> list[str]:
    syms: set[str] = set()
    for p in strat.get("positions", []) or []:
        if p.get("symbol"):
            syms.add(p["symbol"])
    for t in strat.get("trades", []) or []:
        if t.get("symbol"):
            syms.add(t["symbol"])
    return sorted(syms)


def _board(symbols: list[str]) -> str:
    boards = {market_board(s) for s in symbols} if symbols else set()
    if not boards:
        return "—"
    return next(iter(boards)) if len(boards) == 1 else "混合"


def job_view(row: dict) -> dict:
    """store 原始行 → 聚合所需的轻量视图(解析 summary/request/progress)。"""
    return {
        "job_id": row["job_id"], "status": row["status"], "account_id": row.get("account_id"),
        "created_at": row.get("created_at"), "start_date": row.get("start_date"), "end_date": row.get("end_date"),
        "summary": json.loads(row["summary_json"]) if row.get("summary_json") else None,
        "request": json.loads(row["request_json"]) if row.get("request_json") else None,
        "progress": json.loads(row["progress_json"]) if row.get("progress_json") else None,
    }


def _runs_from_job(job: dict) -> list[dict]:
    base = {"job_id": job["job_id"], "created_at": job.get("created_at"),
            "start_date": job.get("start_date"), "end_date": job.get("end_date"), "status": job["status"]}
    if job["status"] == "completed" and job.get("summary"):
        runs = []
        for strat in job["summary"].get("strategies", []) or []:
            run = dict(base)
            run["strategy_id"] = strat.get("strategy_id") or "default"
            run["symbols"] = _symbols_of(strat)
            run.update(_run_metrics(strat.get("daily", []) or []))
            runs.append(run)
        return runs
    # 运行中/排队/失败:从 request 取 strategy_id(无指标),失败附原因
    req = job.get("request") or {}
    sids = [s.get("strategy_id") for s in (req.get("strategies") or []) if s.get("strategy_id")] or ["default"]
    runs = []
    for sid in sids:
        run = dict(base)
        run["strategy_id"] = sid
        run["symbols"] = []
        run["progress"] = job.get("progress")
        if job["status"] == "failed":
            run["error"] = (job.get("summary") or {}).get("error")
        runs.append(run)
    return runs


def _slim(run: dict | None) -> dict | None:
    if run is None:
        return None
    keys = ("job_id", "created_at", "start_date", "end_date", "status", "total_return",
            "annual_return", "max_drawdown", "sharpe", "sortino", "calmar", "sample_days",
            "progress", "error", "symbols")
    return {k: run[k] for k in keys if k in run}


def _best(runs: list[dict], metric: str) -> dict | None:
    cand = [r for r in runs if r.get("status") == "completed" and r.get(metric) is not None]
    return max(cand, key=lambda r: r[metric]) if cand else None


def aggregate(jobs: list[dict], *, meta: dict | None = None, best_metric: str = "total_return") -> list[dict]:
    meta = meta or {}
    by: dict[str, list[dict]] = {}
    for job in jobs:
        for run in _runs_from_job(job):
            by.setdefault(run["strategy_id"], []).append(run)
    out = []
    for sid, runs in by.items():
        runs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        latest = runs[0]
        best = _best(runs, best_metric)
        symbols = sorted({s for r in runs for s in (r.get("symbols") or [])})
        m = meta.get(sid, {})
        out.append({
            "strategy_id": sid,
            "n_runs": len(runs),
            "running": any(r["status"] in ("running", "queued") for r in runs),
            "last_run_at": latest.get("created_at"),
            "latest": _slim(latest),
            "best": _slim(best),
            "symbols": symbols,
            "board": _board(symbols),
            "favorite": bool(m.get("favorite")),
            "pinned": bool(m.get("pinned")),
            "tags": list(m.get("tags") or []),
            "runs": [_slim(r) for r in runs],
        })
    # 置顶优先,其次最近运行
    out.sort(key=lambda a: (not a["pinned"], str(a["last_run_at"] or "")), reverse=False)
    out.sort(key=lambda a: a["pinned"], reverse=True)
    return out


def leaderboard(aggregates: list[dict], *, metric: str = "total_return", scope: str = "best", top: int = 20) -> list[dict]:
    metric = metric if metric in METRICS else "total_return"
    scope = scope if scope in ("best", "latest") else "best"
    rows = []
    for a in aggregates:
        src = a.get(scope) or {}
        val = src.get(metric)
        if val is None:
            continue
        rows.append({"strategy_id": a["strategy_id"], "metric": metric, "scope": scope, "value": val,
                     "job_id": src.get("job_id"), "board": a["board"], "n_runs": a["n_runs"],
                     "favorite": a["favorite"], "symbols": a.get("symbols", []),
                     "metrics": {k: src.get(k) for k in ("total_return", "annual_return", "sharpe", "sortino", "calmar", "max_drawdown")}})
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows[:top]
