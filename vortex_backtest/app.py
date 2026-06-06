from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query

from .models import (
    AccountCreate,
    AccountOut,
    AccountSummaryOut,
    BacktestCreate,
    BacktestJobOut,
    DailySnapshotOut,
    EngineName,
    MinuteSnapshotOut,
    OrderCreate,
    OrderOut,
    PositionOut,
    RejectionOut,
    SymbolCrosswalkOut,
    TradeOut,
)
from .store import DataStore, normalize_account, normalize_job, normalize_order
from .symbols import crosswalk
from .worker import JobWorker


def default_state_dir() -> Path:
    env_value = os.getenv("VORTEX_BACKTEST_STATE_DIR")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return (Path.cwd() / ".vortex_backtest").resolve()


def create_app(state_dir: Path | None = None, *, run_worker: bool = True) -> FastAPI:
    store = DataStore(state_dir or default_state_dir())
    app = FastAPI(
        title="Vortex Backtest Service",
        version="0.1.0",
        description="HTTP service for account-scoped A-share order replay backtests.",
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

    @app.get("/backtests", response_model=list[BacktestJobOut])
    def list_backtests(
        data_store: DataStore = Depends(get_store),
        account_id: str | None = Query(default=None),
    ) -> list[dict]:
        return [normalize_job(row) for row in data_store.list_jobs(account_id=account_id)]

    @app.get("/backtests/{job_id}", response_model=BacktestJobOut)
    def get_backtest(
        job_id: str,
        data_store: DataStore = Depends(get_store),
    ) -> dict:
        return normalize_job(_get_job_or_404(data_store, job_id))

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

    @app.get("/backtests/{job_id}/minutes", response_model=list[MinuteSnapshotOut])
    def get_backtest_minute_snapshots(
        job_id: str,
        data_store: DataStore = Depends(get_store),
    ) -> list[dict]:
        summary = _completed_summary_or_404(data_store, job_id)
        return summary.get("minutes", [])

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
        job_id: str,
        data_store: DataStore = Depends(get_store),
        trade_date: date | None = Query(default=None),
    ) -> list[dict]:
        summary = _completed_summary_or_404(data_store, job_id)
        trades = list(summary.get("trades", []))
        if trade_date is not None:
            trade_date_text = trade_date.isoformat()
            trades = [trade for trade in trades if trade["trade_date"] == trade_date_text]
        return trades

    @app.get("/backtests/{job_id}/rejections", response_model=list[RejectionOut])
    def get_backtest_rejections(
        job_id: str,
        data_store: DataStore = Depends(get_store),
        trade_date: date | None = Query(default=None),
    ) -> list[dict]:
        summary = _completed_summary_or_404(data_store, job_id)
        rejections = list(summary.get("rejections", []))
        if trade_date is not None:
            trade_date_text = trade_date.isoformat()
            rejections = [
                rejection
                for rejection in rejections
                if rejection["trade_date"] == trade_date_text
            ]
        return rejections

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


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "vortex_backtest.app:app",
        host=os.getenv("VORTEX_BACKTEST_HOST", "127.0.0.1"),
        port=int(os.getenv("VORTEX_BACKTEST_PORT", "8765")),
        reload=os.getenv("VORTEX_BACKTEST_RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    main()
