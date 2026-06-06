"""真实 Tushare 分钟数据的端到端集成测试（标记 slow / integration）。

运行方式（在本机 .venv 内，已落盘 stk_mins）::

    export VORTEX_DATA_WORKSPACE=/path/to/vortex_workspace   # 含 data/stk_mins
    .venv/bin/python -m pytest -m integration -q

无数据或缺依赖时**自动跳过**，不会让常规 CI 变红。

设计原则：
- 从真实数据**发现**可用 symbol 与交易日，不硬编码具体价格。
- 只断言与价格无关的**不变量**（qfq>0、日期单调、净值有限、产出文件存在、T+1 生效）。
- 参考数据（adj_factor / stk_limit）在所选窗口不全时，按领域错误码 skip 而非 fail。
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from vortex_backtest.backtrader_adapter import BacktraderMinuteReplayEngine
from vortex_backtest.data_adapter import TushareMinuteDataLoader

# 缺依赖（如 pyarrow）则整文件跳过
pytest.importorskip("pyarrow", reason="读取 parquet 需要 pyarrow")
import pandas as pd  # noqa: E402


def _resolve_workspace() -> Path | None:
    candidates: list[Path] = []
    env = os.getenv("VORTEX_DATA_WORKSPACE")
    if env:
        candidates.append(Path(env).expanduser())
    # 常见布局兜底（README 默认路径 + 仓库相邻的 vortex_data/workspace）
    candidates.append(Path.home() / "Documents" / "vortex_workspace")
    candidates.append(Path(__file__).resolve().parents[2] / "vortex_data" / "workspace")
    for c in candidates:
        if (c / "data" / "stk_mins").exists():
            return c
    return None


WORKSPACE = _resolve_workspace()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.integration,
    pytest.mark.skipif(
        WORKSPACE is None,
        reason="未发现真实 stk_mins 数据；设 VORTEX_DATA_WORKSPACE 指向含 data/stk_mins 的 workspace 后运行",
    ),
]

_MISSING_CODES = {
    "minute_data_missing",
    "adjustment_data_missing",
    "market_rules_data_missing",
}


def _iso(day_key: int) -> str:
    s = str(int(day_key))
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


@pytest.fixture(scope="module")
def discovered() -> dict[str, Any]:
    """发现一个有分钟数据的 symbol 及其前若干个交易日。"""
    base = WORKSPACE / "data" / "stk_mins"
    preferred = ["000001.SZ", "600000.SH", "000002.SZ", "600519.SH"]
    chosen_parquet: Path | None = None
    chosen_symbol: str | None = None
    for sym in preferred:
        parts = list(base.rglob(f"symbol={sym}/*.parquet"))
        if parts:
            chosen_symbol, chosen_parquet = sym, parts[0]
            break
    if chosen_parquet is None:  # 回退：取任意一个 symbol 分区
        anyp = next(iter(base.rglob("symbol=*/*.parquet")), None)
        if anyp is None:
            pytest.skip("stk_mins 下未找到任何 symbol 分区")
        chosen_parquet = anyp
        marker = next(p for p in anyp.parts if p.startswith("symbol="))
        chosen_symbol = marker.split("=", 1)[1]

    df = pd.read_parquet(chosen_parquet)
    rename = {"ts_code": "symbol", "trade_date": "date", "vol": "volume"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "date" not in df.columns:
        pytest.skip(f"{chosen_symbol} 分钟数据缺 date/trade_date 列：{list(df.columns)}")
    days = sorted({int(x) for x in df["date"].unique()})
    if len(days) < 2:
        pytest.skip(f"{chosen_symbol} 可用交易日不足 2 天")
    return {"symbol": chosen_symbol, "days": days[:3]}


def _orders(symbol: str, day1: str, day2: str) -> list[dict[str, Any]]:
    base = {"symbol": symbol, "price_type": None, "limit_price": None, "id": 0, "account_id": "rt"}
    return [
        {**base, "order_batch_id": "default", "request_id": "buy-d1", "trade_date": day1, "side": 1, "quantity": 100},
        # 同日卖出：用于验证 T+1（若买单成交则应被拒）
        {**base, "order_batch_id": "default", "request_id": "sell-same-day", "trade_date": day1, "side": 2, "quantity": 100},
        # 次日卖出
        {**base, "order_batch_id": "default", "request_id": "sell-d2", "trade_date": day2, "side": 2, "quantity": 100},
    ]


# ----------------------------- 数据加载层 -----------------------------

def test_real_loader_qfq_invariants(discovered):
    symbol = discovered["symbol"]
    start, end = discovered["days"][0], discovered["days"][-1]
    loader = TushareMinuteDataLoader(WORKSPACE)
    try:
        dataset = loader.load(symbols={symbol}, start_date=date(*_ymd(start)), end_date=date(*_ymd(end)))
    except ValueError as exc:
        if str(exc) in _MISSING_CODES:
            pytest.skip(f"所选窗口参考数据不全：{exc}")
        raise

    m = dataset.minutes
    assert not m.empty
    for col in ("open_qfq", "high_qfq", "low_qfq", "close_qfq", "limit_up_qfq", "limit_down_qfq", "qfq_multiplier"):
        assert col in m.columns, f"缺少列 {col}"
    # qfq 价与乘子恒正
    assert (m["close_qfq"] > 0).all()
    assert (m[["open_qfq", "high_qfq", "low_qfq"]] > 0).all().all()
    assert (m["qfq_multiplier"] > 0).all()
    # 非停牌 bar：收盘落在 [跌停, 涨停] 区间（允许极小浮点误差）
    active = m[~m["suspended"].astype(bool)]
    eps = 1e-6
    assert (active["close_qfq"] <= active["limit_up_qfq"] * (1 + eps) + eps).all()
    assert (active["close_qfq"] >= active["limit_down_qfq"] * (1 - eps) - eps).all()
    # 日内一致性：high >= low
    assert (m["high_qfq"] >= m["low_qfq"] - eps).all()


# ----------------------------- 引擎层端到端 -----------------------------

def test_real_engine_end_to_end_invariants(discovered, tmp_path):
    symbol = discovered["symbol"]
    days = discovered["days"]
    day1, day2 = _iso(days[0]), _iso(days[1])
    engine = BacktraderMinuteReplayEngine(data_loader=TushareMinuteDataLoader(WORKSPACE))
    try:
        summary = engine.run(
            job_id="rt-job",
            account={"account_id": "rt", "initial_cash": 1_000_000.0, "engine": "backtrader"},
            orders=_orders(symbol, day1, day2),
            report_dir=tmp_path / "report",
            start_date=date(*_ymd(days[0])),
            end_date=date(*_ymd(days[1])),
        )
    except ValueError as exc:
        if str(exc) in _MISSING_CODES:
            pytest.skip(f"所选窗口参考数据不全：{exc}")
        raise

    # 口径
    assert summary["frequency"] == "1min" and summary["price_adjustment"] == "qfq"
    # 净值有限且为正
    assert summary["total_value"] > 0
    assert summary["cash"] >= -1e-6
    # 日级日期单调、唯一、落在窗口内
    daily_dates = [d["trade_date"] for d in summary["daily"]]
    assert daily_dates == sorted(set(daily_dates))
    assert all(day1 <= d <= day2 for d in daily_dates)
    # 成交记录不变量
    for t in summary["trades"]:
        assert t["price"] > 0 and t["amount"] > 0 and t["quantity"] > 0
    # 持仓估值一致性：market_value ≈ quantity * last_price
    for p in summary["positions"]:
        assert p["last_price"] > 0
        assert abs(p["market_value"] - p["quantity"] * p["last_price"]) <= 1e-2
    # 产出文件落盘
    assert Path(summary["artifacts"]["account_summary"]).exists()
    assert Path(summary["artifacts"]["daily_equity"]).exists()

    # T+1：若买单成交，则同日卖单必须被 t_plus_1_not_sellable 拒绝
    buy_filled = any(t["request_id"] == "buy-d1" for t in summary["trades"])
    if not buy_filled:
        pytest.skip("所选日买单未成交（可能涨停/停牌/流动性不足），跳过 T+1 强断言")
    same_day = next((r for r in summary["rejections"] if r["request_id"] == "sell-same-day"), None)
    assert same_day is not None and same_day["reason"] == "t_plus_1_not_sellable"


# ----------------------------- HTTP 层端到端 -----------------------------

def test_real_http_end_to_end(discovered, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from vortex_backtest.app import create_app
    from vortex_backtest.worker import drain_jobs

    symbol = discovered["symbol"]
    days = discovered["days"]
    day1, day2 = _iso(days[0]), _iso(days[1])
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(WORKSPACE))
    client = TestClient(create_app(tmp_path / "state", run_worker=False))

    assert client.post("/accounts", json={"account_id": "rt", "initial_cash": 1_000_000}).status_code == 201
    for o in _orders(symbol, day1, day2):
        body = {k: o[k] for k in ("order_batch_id", "request_id", "trade_date", "symbol", "side", "quantity")}
        assert client.post("/accounts/rt/orders", json=body).status_code == 201

    submit = client.post("/backtests", json={
        "account_id": "rt", "frequency": "1min", "price_adjustment": "qfq",
        "start_date": day1, "end_date": day2,
    })
    assert submit.status_code == 202
    job_id = submit.json()["job_id"]
    drain_jobs(client.app.state.store)
    job = client.get(f"/backtests/{job_id}").json()
    if job["status"] == "failed":
        if job["summary"].get("error") in _MISSING_CODES:
            pytest.skip(f"作业因参考数据不全失败：{job['summary']['error']}")
        raise AssertionError(f"回测意外失败：{job['summary']}")
    assert job["status"] == "completed"

    summary = client.get(f"/backtests/{job_id}/summary").json()
    assert summary["total_value"] > 0
    daily = client.get(f"/backtests/{job_id}/daily").json()
    assert len(daily) >= 1 and all(d["total_value"] > 0 for d in daily)


def _ymd(day_key: int) -> tuple[int, int, int]:
    s = str(int(day_key))
    return int(s[:4]), int(s[4:6]), int(s[6:])
