"""异步作业 worker（ADR-3）。

`POST /backtests` 只入队；真正的回测在这里执行：领取 queued 作业 → 跑引擎 →
complete/fail。生产用后台守护线程 `JobWorker` 循环执行；测试可直接调用
`drain_jobs(store)` 同步排空，保证确定性。引擎无关——迁移到 Qlib 后只换 `engine_for`。
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any

from .backtrader_adapter import BacktraderMinuteReplayEngine
from .models import BacktestCreate, EngineName
from .store import DataStore

# 可安全回传客户端的领域错误码；其余异常一律脱敏为 internal_error，完整堆栈只进服务端日志。
SAFE_ERROR_CODES = {
    "minute_data_missing",
    "adjustment_data_missing",
    "market_rules_data_missing",
    "no_symbols",
    "start_end_required",
    "unsupported_frequency",
    "unsupported_price_adjustment",
    "unsupported_order_price_adjustment",
    "unsupported_strategy_type",
    "missing_request_payload",
}


def engine_for(engine_name: str):
    engine = EngineName(engine_name)
    if engine == EngineName.BACKTRADER:
        return BacktraderMinuteReplayEngine()
    if engine == EngineName.QLIB:
        from .qlib_engine import QlibReplayEngine

        return QlibReplayEngine()
    raise ValueError(f"unsupported engine: {engine_name}")


def run_job(store: DataStore, job: dict[str, Any]) -> None:
    """执行一个已领取（running）的作业，结束时写 complete/fail。"""
    job_id = str(job["job_id"])
    request_json = job.get("request_json")
    try:
        if not request_json:
            raise ValueError("missing_request_payload")
        payload = BacktestCreate.model_validate_json(request_json)
        account = store.get_account(payload.account_id)
        order_price_adjustment = payload.order_price_adjustment or payload.price_adjustment
        orders = store.list_orders(
            payload.account_id,
            order_batch_id=None if payload.strategies else payload.order_batch_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
        )
        report_dir = store.report_root / job_id
        engine = engine_for(account["engine"])
        summary = engine.run(
            job_id=job_id,
            account=account,
            orders=orders,
            report_dir=report_dir,
            start_date=payload.start_date,
            end_date=payload.end_date,
            order_batch_id=payload.order_batch_id,
            market_data_set_id=payload.market_data_set_id,
            frequency=payload.frequency,
            price_adjustment=payload.price_adjustment.value,
            order_price_adjustment=order_price_adjustment.value,
            default_price_type=payload.default_price_type.value,
            strategies=[strategy.model_dump() for strategy in payload.strategies],
            execution=payload.execution.model_dump(),
        )
        store.complete_job(job_id, report_dir, summary)
    except Exception as exc:  # noqa: BLE001 - 失败原因统一落到作业状态
        code = str(exc)
        if code not in SAFE_ERROR_CODES:
            traceback.print_exc()  # 完整堆栈只进服务端日志，不外泄客户端
            code = "internal_error"
        store.fail_job(job_id, code)


def run_pending_once(store: DataStore) -> bool:
    """领取并执行一个 queued 作业；无则返回 False。"""
    job = store.claim_next_queued_job()
    if job is None:
        return False
    run_job(store, job)
    return True


def drain_jobs(store: DataStore) -> int:
    """同步排空所有 queued 作业（测试/一次性用）。返回执行数量。"""
    count = 0
    while run_pending_once(store):
        count += 1
    return count


class JobWorker:
    """后台守护线程：循环领取并执行 queued 作业。"""

    def __init__(self, store: DataStore, idle_sleep: float = 0.2):
        self.store = store
        self.idle_sleep = idle_sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="vortex-backtest-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = run_pending_once(self.store)
            except Exception:  # noqa: BLE001 - worker 线程不能因单个异常退出
                did_work = False
            if not did_work:
                time.sleep(self.idle_sleep)
