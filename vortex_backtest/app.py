from __future__ import annotations

import os
import secrets
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    AccountCreate,
    AccountOut,
    AccountSummaryOut,
    BacktestCreate,
    BacktestJobOut,
    DailySnapshotOut,
    OrderCreate,
    OrderOut,
    PositionOut,
    RejectionOut,
    SymbolCrosswalkOut,
    TradeOut,
)
from . import benchmark as benchmark_mod
from . import strategies as strat_mod
from .metrics import compute_metrics
from .store import DataStore, normalize_account, normalize_job, normalize_order
from .symbols import crosswalk
from .worker import JobWorker


def default_state_dir() -> Path:
    env_value = os.getenv("VORTEX_BACKTEST_STATE_DIR")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return (Path.cwd() / ".vortex_backtest").resolve()


def require_write_auth(
    authorization: str | None = Header(default=None),
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
) -> None:
    """写接口鉴权：
    - 配了 `VORTEX_BACKTEST_TOKEN`：必须带匹配 token（`Authorization: Bearer <t>` 或 `X-Auth-Token`）。
    - 没配 token：仅本机回环放行；绑到非回环 host 时拒绝（fail-closed，避免裸暴露写接口）。
    """
    configured = os.getenv("VORTEX_BACKTEST_TOKEN")
    host = os.getenv("VORTEX_BACKTEST_HOST", "127.0.0.1")
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    if not configured:
        if is_loopback:
            return
        raise HTTPException(
            status_code=403,
            detail={
                "error": "write_disabled",
                "hint": "set VORTEX_BACKTEST_TOKEN to enable write endpoints on a non-loopback host",
            },
        )
    presented: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    presented = presented or x_auth_token
    if not presented or not secrets.compare_digest(presented, configured):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


def create_app(state_dir: Path | None = None, *, run_worker: bool = True) -> FastAPI:
    store = DataStore(state_dir or default_state_dir())
    app = FastAPI(
        title="Vortex Backtest Service",
        version="0.1.0",
        description=(
            "A 股账户级订单回放回测服务。浏览器文档："
            "设计与使用指南见 /guide，交互式 API 见 /docs(Swagger) 与 /redoc，看板见 /ui/。"
        ),
    )
    app.state.store = store
    # 崩溃恢复：把上次残留的 running 作业重排回 queued（ADR-3）
    store.requeue_interrupted()
    if run_worker:
        worker = JobWorker(store)
        worker.start()
        app.state.worker = worker

    def get_store() -> DataStore:
        return store

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/accounts", response_model=AccountOut, status_code=201)
    def create_account(
        payload: AccountCreate,
        data_store: DataStore = Depends(get_store),
        _auth: None = Depends(require_write_auth),
    ) -> dict:
        try:
            return normalize_account(data_store.create_account(payload))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="account_id already exists") from exc

    @app.get("/symbols/{symbol}", response_model=SymbolCrosswalkOut)
    def get_symbol_crosswalk(symbol: str) -> dict:
        try:
            return crosswalk(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/accounts", response_model=list[AccountOut])
    def list_accounts(data_store: DataStore = Depends(get_store)) -> list[dict]:
        return [normalize_account(row) for row in data_store.list_accounts()]

    @app.get("/accounts/{account_id}", response_model=AccountOut)
    def get_account(
        account_id: str,
        data_store: DataStore = Depends(get_store),
    ) -> dict:
        return normalize_account(_get_account_or_404(data_store, account_id))

    @app.post("/accounts/{account_id}/orders", response_model=OrderOut, status_code=201)
    def create_order(
        account_id: str,
        payload: OrderCreate,
        data_store: DataStore = Depends(get_store),
        _auth: None = Depends(require_write_auth),
    ) -> dict:
        try:
            row = data_store.create_order(account_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="account not found") from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="request_id already exists for this account and order_batch_id",
            ) from exc
        return normalize_order(row)

    @app.get("/accounts/{account_id}/orders", response_model=list[OrderOut])
    def list_orders(
        account_id: str,
        data_store: DataStore = Depends(get_store),
        order_batch_id: str | None = Query(default=None),
        start_date: date | None = Query(default=None),
        end_date: date | None = Query(default=None),
    ) -> list[dict]:
        _get_account_or_404(data_store, account_id)
        return [
            normalize_order(row)
            for row in data_store.list_orders(
                account_id,
                order_batch_id=order_batch_id,
                start_date=start_date,
                end_date=end_date,
            )
        ]

    @app.post("/backtests", response_model=BacktestJobOut, status_code=202)
    def run_backtest(
        payload: BacktestCreate,
        data_store: DataStore = Depends(get_store),
        _auth: None = Depends(require_write_auth),
    ) -> dict:
        _get_account_or_404(data_store, payload.account_id)
        order_price_adjustment = payload.order_price_adjustment or payload.price_adjustment
        # 同步校验：不支持的参数立即 400（不入队）
        if payload.frequency != "1min":
            raise HTTPException(status_code=400, detail={"error": "unsupported_frequency"})
        if payload.price_adjustment.value != "qfq":
            raise HTTPException(status_code=400, detail={"error": "unsupported_price_adjustment"})
        if order_price_adjustment.value != "qfq":
            raise HTTPException(status_code=400, detail={"error": "unsupported_order_price_adjustment"})
        # 入队，后台 worker 执行（ADR-3）：立即返回 202 + job_id，客户端轮询状态
        job_id = str(uuid.uuid4())
        data_store.create_job(
            job_id,
            payload.account_id,
            payload.order_batch_id,
            payload.market_data_set_id,
            payload.frequency,
            payload.price_adjustment.value,
            order_price_adjustment.value,
            payload.default_price_type.value,
            payload.start_date,
            payload.end_date,
            request_json=payload.model_dump_json(),
        )
        return normalize_job(data_store.get_job(job_id))

    def _job_out(row: dict) -> dict:
        """normalize_job + 派生 strategy_ids(便于列表直接显示策略名,不暴露内部路径)。"""
        out = normalize_job(row)
        sids: list[str] = []
        for run in strat_mod._runs_from_job(strat_mod.job_view(row)):
            sid = run.get("strategy_id")
            if sid and sid not in sids:
                sids.append(sid)
        out["strategy_ids"] = sids
        return out

    @app.get("/backtests", response_model=list[BacktestJobOut])
    def list_backtests(
        data_store: DataStore = Depends(get_store),
        account_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ) -> list[dict]:
        return [
            _job_out(row)
            for row in data_store.list_jobs(account_id=account_id, status=status)
        ]

    @app.get("/backtests/{job_id}", response_model=BacktestJobOut)
    def get_backtest(
        job_id: str,
        data_store: DataStore = Depends(get_store),
    ) -> dict:
        return _job_out(_get_job_or_404(data_store, job_id))

    @app.get("/backtests/{job_id}/summary", response_model=AccountSummaryOut)
    def get_backtest_summary(
        job_id: str,
        data_store: DataStore = Depends(get_store),
    ) -> dict:
        return _completed_summary_or_404(data_store, job_id)

    @app.get("/backtests/{job_id}/daily", response_model=list[DailySnapshotOut])
    def get_backtest_daily_snapshots(
        job_id: str,
        data_store: DataStore = Depends(get_store),
    ) -> list[dict]:
        summary = _completed_summary_or_404(data_store, job_id)
        return summary.get("daily", [])

    @app.get("/backtests/{job_id}/daily/{trade_date}", response_model=DailySnapshotOut)
    def get_backtest_daily_snapshot(
        job_id: str,
        trade_date: date,
        data_store: DataStore = Depends(get_store),
    ) -> dict:
        summary = _completed_summary_or_404(data_store, job_id)
        trade_date_text = trade_date.isoformat()
        for snapshot in summary.get("daily", []):
            if snapshot["trade_date"] == trade_date_text:
                return snapshot
        raise HTTPException(status_code=404, detail="daily snapshot not found")

    @app.get("/backtests/{job_id}/trades", response_model=list[TradeOut])
    def get_backtest_trades(
        response: Response,
        job_id: str,
        data_store: DataStore = Depends(get_store),
        trade_date: date | None = Query(default=None),
        symbol: str | None = Query(default=None),
        strategy_id: str | None = Query(default=None),
        limit: int | None = Query(default=None, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        summary = _completed_summary_or_404(data_store, job_id)
        trades = list(summary.get("trades", []))
        if trade_date is not None:
            trade_date_text = trade_date.isoformat()
            trades = [trade for trade in trades if trade["trade_date"] == trade_date_text]
        if symbol is not None:
            trades = [trade for trade in trades if trade.get("symbol") == symbol]
        if strategy_id is not None:
            trades = [trade for trade in trades if trade.get("strategy_id") == strategy_id]
        # 过滤后、分页前的总数，供前端服务端分页计算页数
        response.headers["X-Total-Count"] = str(len(trades))
        if offset:
            trades = trades[offset:]
        if limit is not None:
            trades = trades[:limit]
        return trades

    @app.get("/backtests/{job_id}/rejections", response_model=list[RejectionOut])
    def get_backtest_rejections(
        response: Response,
        job_id: str,
        data_store: DataStore = Depends(get_store),
        trade_date: date | None = Query(default=None),
        reason: str | None = Query(default=None),
        strategy_id: str | None = Query(default=None),
        limit: int | None = Query(default=None, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        summary = _completed_summary_or_404(data_store, job_id)
        rejections = list(summary.get("rejections", []))
        if trade_date is not None:
            trade_date_text = trade_date.isoformat()
            rejections = [r for r in rejections if r["trade_date"] == trade_date_text]
        if reason is not None:
            rejections = [r for r in rejections if r.get("reason") == reason]
        if strategy_id is not None:
            rejections = [r for r in rejections if r.get("strategy_id") == strategy_id]
        response.headers["X-Total-Count"] = str(len(rejections))
        if offset:
            rejections = rejections[offset:]
        if limit is not None:
            rejections = rejections[:limit]
        return rejections

    @app.get("/backtests/{job_id}/rejections/summary")
    def get_backtest_rejection_summary(
        job_id: str,
        data_store: DataStore = Depends(get_store),
    ) -> dict:
        summary = _completed_summary_or_404(data_store, job_id)
        counts: dict[str, int] = {}
        for rejection in summary.get("rejections", []):
            key = str(rejection.get("reason", "unknown"))
            counts[key] = counts.get(key, 0) + 1
        ordered = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
        return {"counts": ordered, "total": sum(counts.values())}

    def _daily_for(summary: dict, strategy_id: str | None) -> list[dict]:
        if strategy_id:
            for strat in summary.get("strategies", []):
                if strat.get("strategy_id") == strategy_id:
                    return strat.get("daily", [])
            raise HTTPException(status_code=404, detail="strategy not found")
        return summary.get("daily", [])

    @app.get("/backtests/{job_id}/equity")
    def get_backtest_equity(
        job_id: str,
        data_store: DataStore = Depends(get_store),
        strategy_id: str | None = Query(default=None),
        benchmark: str | None = Query(default=None),
        rebase: bool = Query(default=False),
    ) -> dict:
        """日级净值 + 回撤（仅日级）；可选基准对标（rebase 到 100 同起点）。"""
        summary = _completed_summary_or_404(data_store, job_id)
        job = normalize_job(_get_job_or_404(data_store, job_id))
        daily = _daily_for(summary, strategy_id)
        dates = [row["trade_date"] for row in daily]
        equity = [float(row["total_value"]) for row in daily]
        drawdown = [float(row.get("drawdown", 0.0)) for row in daily]
        try:
            initial_cash = float(_get_account_or_404(data_store, job["account_id"])["initial_cash"])
        except HTTPException:
            initial_cash = equity[0] if equity else 0.0
        rebased = bool(rebase) or bool(benchmark)
        result: dict = {"dates": dates, "drawdown": drawdown, "rebase": rebased}
        if rebased and equity and equity[0]:
            base0 = equity[0]
            result["equity"] = [round(v / base0, 6) for v in equity]  # 起点 1.0
            result["baseline"] = 1.0
        else:
            result["equity"] = equity
            result["baseline"] = initial_cash
        if benchmark:
            series = benchmark_mod.benchmark_series(benchmark, dates, base=1.0)
            result["benchmark"] = {
                "symbol": benchmark,
                "values": series,
                "available": series is not None,
            }
        return result

    @app.get("/backtests/{job_id}/minutes")
    def get_backtest_minutes(
        job_id: str,
        response: Response,
        data_store: DataStore = Depends(get_store),
        limit: int = Query(default=2000, ge=0),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        """逐分钟净值（组合）：timestamp/cash/market_value/total_value。

        分页（limit/offset，limit=0 取到末尾），总数在响应头 `X-Total-Count`。
        数据来自回测产物 `minute_equity.csv`（不进 SQLite，规避膨胀）。
        """
        summary = _completed_summary_or_404(data_store, job_id)
        rows = _read_minute_equity(summary)
        response.headers["X-Total-Count"] = str(len(rows))
        return rows[offset:] if limit == 0 else rows[offset : offset + limit]

    @app.get("/backtests/{job_id}/metrics")
    def get_backtest_metrics(
        job_id: str,
        data_store: DataStore = Depends(get_store),
        strategy_id: str | None = Query(default=None),
        benchmark: str | None = Query(default=None),
    ) -> dict:
        """绩效指标（绝对 / 风险调整 / 基准相对）+ 短样本护栏。"""
        summary = _completed_summary_or_404(data_store, job_id)
        daily = _daily_for(summary, strategy_id)
        values = [float(row["total_value"]) for row in daily]
        dates = [row["trade_date"] for row in daily]
        bench_values = None
        bench_info = None
        if benchmark:
            series = benchmark_mod.benchmark_series(benchmark, dates)
            bench_values = series
            bench_info = {"symbol": benchmark, "available": series is not None}
        metrics = compute_metrics(values, benchmark_values=bench_values)
        if bench_info is not None:
            metrics["benchmark"] = bench_info
        return metrics

    @app.get("/benchmarks")
    def get_benchmarks() -> dict:
        return benchmark_mod.list_benchmarks()

    @app.post("/backtests/{job_id}/cancel", response_model=BacktestJobOut)
    def cancel_backtest(
        job_id: str,
        data_store: DataStore = Depends(get_store),
        _auth: None = Depends(require_write_auth),
    ) -> dict:
        """取消作业：排队中→cancelled；运行中无法安全中断（同步执行）→409；终态→409。"""
        job = normalize_job(_get_job_or_404(data_store, job_id))
        status = job["status"]
        if status == "queued":
            data_store.cancel_queued_job(job_id)
            return normalize_job(data_store.get_job(job_id))
        if status == "running":
            raise HTTPException(
                status_code=409,
                detail={"error": "cannot_cancel_running", "hint": "运行中作业暂不支持中断，待其完成"},
            )
        raise HTTPException(status_code=409, detail={"error": "not_cancellable", "status": status})

    # ---- 策略中心(按 strategy_id 从回测派生的只读聚合) ----
    def _aggregates(account: str | None, best_metric: str) -> list[dict]:
        rows = store.list_jobs(account_id=account)
        views = [strat_mod.job_view(row) for row in rows]
        meta = store.get_strategy_meta(account) if account else {}
        return strat_mod.aggregate(views, meta=meta, best_metric=best_metric)

    def _strategy_daily(job_id: str, strategy_id: str) -> list[dict]:
        try:
            job = normalize_job(_get_job_or_404(store, job_id))
        except HTTPException:
            return []
        summary = job.get("summary") or {}
        for strat in summary.get("strategies", []) or []:
            if strat.get("strategy_id") == strategy_id:
                return strat.get("daily", []) or []
        return summary.get("daily", []) or []

    def _strategy_entry(job_id: str, strategy_id: str) -> dict:
        try:
            job = normalize_job(_get_job_or_404(store, job_id))
        except HTTPException:
            return {}
        for strat in (job.get("summary") or {}).get("strategies", []) or []:
            if strat.get("strategy_id") == strategy_id:
                return strat
        return {}

    def _equity_from_daily(daily: list[dict], benchmark: str | None) -> dict:
        dates = [d["trade_date"] for d in daily]
        eq = [float(d["total_value"]) for d in daily]
        result: dict = {"dates": dates, "drawdown": [float(d.get("drawdown", 0.0)) for d in daily], "rebase": True}
        result["equity"] = [round(v / eq[0], 6) for v in eq] if (eq and eq[0]) else eq  # 起点 1.0
        result["baseline"] = 1.0
        if benchmark:
            series = benchmark_mod.benchmark_series(benchmark, dates, base=1.0)
            result["benchmark"] = {"symbol": benchmark, "values": series, "available": series is not None}
        return result

    @app.get("/strategies")
    def list_strategies(
        data_store: DataStore = Depends(get_store),
        account_id: str | None = Query(default=None),
        best_metric: str = Query(default="total_return"),
    ) -> list[dict]:
        aggs = _aggregates(account_id, best_metric)
        for agg in aggs:
            agg.pop("runs", None)  # 列表页不带全部 run,详情页才带
        return aggs

    @app.get("/strategies/compare")
    def compare_strategies(
        ids: str = Query(...),
        data_store: DataStore = Depends(get_store),
        account_id: str | None = Query(default=None),
        benchmark: str | None = Query(default=None),
    ) -> dict:
        wanted = [x.strip() for x in ids.split(",") if x.strip()]
        by_id = {a["strategy_id"]: a for a in _aggregates(account_id, "total_return")}
        out = []
        for sid in wanted:
            agg = by_id.get(sid)
            if not agg:
                continue
            latest_done = next((r for r in agg["runs"] if r["status"] == "completed"), None)
            entry = {"strategy_id": sid, "latest": agg["latest"], "best": agg["best"]}
            if latest_done:
                entry["equity"] = _equity_from_daily(_strategy_daily(latest_done["job_id"], sid), benchmark)
            out.append(entry)
        return {"strategies": out, "benchmark": benchmark}

    @app.get("/leaderboard")
    def get_leaderboard(
        data_store: DataStore = Depends(get_store),
        account_id: str | None = Query(default=None),
        metric: str = Query(default="total_return"),
        scope: str = Query(default="best"),
        top: int = Query(default=20, ge=1, le=100),
    ) -> list[dict]:
        aggs = _aggregates(account_id, metric if metric in strat_mod.METRICS else "total_return")
        return strat_mod.leaderboard(aggs, metric=metric, scope=scope, top=top)

    @app.get("/strategies/{strategy_id}")
    def get_strategy(
        strategy_id: str,
        data_store: DataStore = Depends(get_store),
        account_id: str | None = Query(default=None),
        best_metric: str = Query(default="total_return"),
        benchmark: str | None = Query(default=None),
    ) -> dict:
        for agg in _aggregates(account_id, best_metric):
            if agg["strategy_id"] == strategy_id:
                latest_done = next((r for r in agg["runs"] if r["status"] == "completed"), None)
                if latest_done:
                    entry = _strategy_entry(latest_done["job_id"], strategy_id)
                    daily = entry.get("daily") or _strategy_daily(latest_done["job_id"], strategy_id)
                    agg["equity"] = _equity_from_daily(daily, benchmark)
                    agg["positions"] = entry.get("positions", []) or []
                    agg["trades"] = (entry.get("trades", []) or [])[-50:]
                    agg["latest_job_id"] = latest_done["job_id"]
                return agg
        raise HTTPException(status_code=404, detail="strategy not found")

    @app.put("/strategies/{strategy_id}/meta")
    def put_strategy_meta(
        strategy_id: str,
        account_id: str = Query(...),
        payload: dict | None = Body(default=None),
        data_store: DataStore = Depends(get_store),
        _auth: None = Depends(require_write_auth),
    ) -> dict:
        _get_account_or_404(data_store, account_id)
        data = payload or {}
        return data_store.set_strategy_meta(
            account_id, strategy_id,
            favorite=data.get("favorite"), pinned=data.get("pinned"), tags=data.get("tags"),
        )

    @app.get("/accounts/{account_id}/summary", response_model=AccountSummaryOut)
    def get_latest_account_summary(
        account_id: str,
        data_store: DataStore = Depends(get_store),
    ) -> dict:
        _get_account_or_404(data_store, account_id)
        try:
            job = normalize_job(data_store.latest_completed_job(account_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="completed backtest not found") from exc
        return job["summary"]

    @app.get("/accounts/{account_id}/positions", response_model=list[PositionOut])
    def get_latest_positions(
        account_id: str,
        data_store: DataStore = Depends(get_store),
    ) -> list[dict]:
        summary = get_latest_account_summary(account_id, data_store)
        return summary["positions"]

    # 托管只读看板（P5 壳）：/ui/ 提供静态 SPA，/ 重定向到看板。
    web_dir = Path(__file__).resolve().parent / "web"
    if web_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(web_dir), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def dashboard_root() -> RedirectResponse:
            return RedirectResponse(url="/ui/")

    # 技术文档站：/guide 返回精修的静态 HTML（系统设计 / HTTP 接口协议 / 环境部署）。
    # 不再走 Markdown 现场渲染；交互式 API 见 FastAPI 自带的 /docs(Swagger) 与 /redoc。
    guide_html = web_dir / "guide.html"

    @app.get("/guide", include_in_schema=False)
    def guide_home() -> HTMLResponse:
        try:
            return HTMLResponse(guide_html.read_text(encoding="utf-8"))
        except OSError as exc:
            raise HTTPException(status_code=404, detail="guide not found") from exc

    return app


def _get_account_or_404(data_store: DataStore, account_id: str) -> dict:
    try:
        return data_store.get_account(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="account not found") from exc


def _get_job_or_404(data_store: DataStore, job_id: str) -> dict:
    try:
        return data_store.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="backtest job not found") from exc


def _completed_summary_or_404(data_store: DataStore, job_id: str) -> dict:
    job = normalize_job(_get_job_or_404(data_store, job_id))
    if job["status"] != "completed" or not job["summary"]:
        raise HTTPException(status_code=404, detail="completed summary not found")
    return job["summary"]


def _read_minute_equity(summary: dict) -> list[dict]:
    """读回测产物 minute_equity.csv → 逐分钟净值行（缺文件返回空）。"""
    import csv

    path = (summary.get("artifacts") or {}).get("minute_equity")
    if not path or not Path(path).exists():
        return []
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append(
                    {
                        "timestamp": r.get("timestamp"),
                        "cash": float(r.get("cash") or 0.0),
                        "market_value": float(r.get("market_value") or 0.0),
                        "total_value": float(r.get("total_value") or 0.0),
                    }
                )
            except (TypeError, ValueError):
                continue
    return rows


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "vortex_backtest.app:app",
        host=os.getenv("VORTEX_BACKTEST_HOST", "127.0.0.1"),
        port=int(os.getenv("VORTEX_BACKTEST_PORT", "8767")),
        reload=os.getenv("VORTEX_BACKTEST_RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    main()
