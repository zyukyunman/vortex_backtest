"""分析/报告端点 API 测试：手工构造 state（store 行 + JSONL 产物）+ fixture 基准 workspace。"""
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from vortex_backtest.app import create_app
from vortex_backtest.models import AccountCreate
from vortex_backtest.store import DataStore

SID = "rpt-test-session"
SID_SHORT = "rpt-short-session"   # calendar 只写 0203/0204 的窄会话（验证基准窗口夹断）


def _pos(qty, mv):
    return [{"strategy_id": "t", "symbol": "000001.SZ", "quantity": qty,
             "available_quantity": qty, "cost_basis": 10.0,
             "last_price": mv / qty if qty else 0.0, "market_value": mv,
             "unrealized_pnl": 0.0, "unrealized_pnl_ratio": 0.0}] if qty else []


def _snap(ts, cash, mv):
    return {"strategy_id": "t", "timestamp": ts, "frequency": "1min",
            "cash": cash, "market_value": mv, "total_value": cash + mv,
            "positions": _pos(10 if mv else 0, mv), "trades": [], "rejections": []}


def _write_jsonl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _seed_session(state, store, session_id, *, snaps, trades, cal_days):
    """建会话行 + 落 JSONL 产物（snapshots/trades/calendar），多会话复用。"""
    store.create_session(session_id=session_id, account_id="acct", level="1min",
                         start_date="2026-02-03", end_date="2026-02-05", sim_time=None,
                         initial_cash=1000.0, universe=["000001.SZ"], config={"strategy_id": "t"})
    sdir = state / "reports" / "sessions" / session_id
    sdir.mkdir(parents=True)
    _write_jsonl(sdir / "snapshots.jsonl", snaps)
    _write_jsonl(sdir / "trades.jsonl", trades)
    _write_jsonl(sdir / "calendar.jsonl", [{"d": d} for d in cal_days])


@pytest.fixture
def client(tmp_path, monkeypatch):
    # fixture 基准 workspace（同 test_benchmark 形状）
    idx = tmp_path / "ws" / "data" / "index_daily" / "date=20260203"
    idx.mkdir(parents=True)
    pd.DataFrame({"symbol": ["000300.SH"] * 3,
                  "date": ["20260203", "20260204", "20260205"],
                  "close": [4000.0, 4040.0, 4000.4]}).to_parquet(idx / "data.parquet")
    monkeypatch.setenv("VORTEX_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("VORTEX_DATA_URL", raising=False)
    monkeypatch.setenv("VORTEX_BACKTEST_HOST", "127.0.0.1")

    state = tmp_path / "state"
    store = DataStore(state)
    store.create_account(AccountCreate(account_id="acct", initial_cash=1000.0))

    snaps = [
        _snap("2026-02-03T09:31:00", 900.0, 100.0),
        _snap("2026-02-03T15:00:00", 900.0, 110.0),
        _snap("2026-02-04T15:00:00", 1012.0, 0.0),
    ]
    trades = [
        {"strategy_id": "t", "trade_id": "t-1", "request_id": "b1", "trade_date": "2026-02-03",
         "symbol": "000001.SZ", "side": 1, "side_name": "BUY", "requested_quantity": 10,
         "quantity": 10, "price": 10.0, "amount": 100.0, "commission": 5.0,
         "stamp_tax": 0.0, "transfer_fee": 0.001, "realized_pnl": 0.0, "cash_after": 900.0},
        {"strategy_id": "t", "trade_id": "t-2", "request_id": "s1", "trade_date": "2026-02-04",
         "symbol": "000001.SZ", "side": 2, "side_name": "SELL", "requested_quantity": 10,
         "quantity": 10, "price": 11.2, "amount": 112.0, "commission": 5.0,
         "stamp_tax": 0.056, "transfer_fee": 0.001, "realized_pnl": 2.0, "cash_after": 1012.0},
    ]
    _seed_session(state, store, SID, snaps=snaps, trades=trades,
                  cal_days=(20260203, 20260204, 20260205))
    # 窄会话：end_date 仍是 0205，但 calendar/快照只到 0204 → 策略 daily 止于 0204。
    _seed_session(state, store, SID_SHORT, snaps=snaps, trades=trades,
                  cal_days=(20260203, 20260204))
    return TestClient(create_app(state_dir=state))


def test_metrics_shape_and_benchmark(client):
    r = client.get(f"/sessions/{SID}/metrics?benchmark=000300.SH")
    assert r.status_code == 200
    m = r.json()
    assert m["benchmark_name"] == "沪深300" and m["low_confidence"] is True
    # 序列含 initial_cash 基线锚点(首交易日前一天=1000)，与 /summary 同口径 → TR=1012/1000-1
    assert m["strategy"]["n_days"] == 4  # 3 个交易日 + 1 个基线点
    assert m["strategy"]["total_return"] == pytest.approx(0.012)
    assert m["benchmark_stats"]["total_return"] == pytest.approx(4000.4 / 4000.0 - 1, rel=1e-9)
    assert m["relative"]["beta"] is not None
    assert m["annual"][0]["period"] == "2026" and m["monthly"][0]["period"] == "2026-02"


def test_metrics_benchmark_missing_degrades(client):
    m = client.get(f"/sessions/{SID}/metrics?benchmark=NOPE.XX").json()
    assert m["benchmark_stats"] is None and m["relative"] is None
    assert m["error"] == "benchmark_data_missing"
    assert m["strategy"]["total_return"] == pytest.approx(0.012)   # 绝对类照常(对 initial_cash)


def test_metrics_benchmark_window_capped_to_strategy_last_day(client):
    # 窄会话策略 daily 只到 0204：基准 0205 行须被夹掉（open/提前 close 的会话
    # 不得让基准统计覆盖策略未走到的日子），n_days 只数 0203/0204 两天。
    m = client.get(f"/sessions/{SID_SHORT}/metrics?benchmark=000300.SH").json()
    assert m["benchmark_stats"]["n_days"] == 2
    assert m["benchmark_stats"]["total_return"] == pytest.approx(4040.0 / 4000.0 - 1, rel=1e-9)


def test_equity_curve(client):
    eq = client.get(f"/sessions/{SID}/equity?benchmark=000300.SH").json()
    # 首位是 initial_cash 基线日(首交易日前一天)，strategy[0]=1.0 即基线=期初本金；
    # 基准在基线日无数据 → None(不回填)，自首个有数日起 1.0
    assert eq["dates"] == ["2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05"]
    assert eq["strategy"][0] == 1.0 and eq["benchmark"][0] is None
    assert eq["benchmark"][1] == 1.0
    assert len(eq["drawdown"]) == 4


def test_positions_granularities(client):
    daily = client.get(f"/sessions/{SID}/positions?granularity=daily").json()
    assert [d["timestamp"] for d in daily] == ["2026-02-03", "2026-02-04", "2026-02-05"]
    assert daily[0]["positions"][0]["weight"] == pytest.approx(110.0 / 1010.0)  # EOD
    weekly = client.get(f"/sessions/{SID}/positions?granularity=weekly").json()
    assert len(weekly) == 1 and weekly[0]["week"].startswith("2026-W")
    hourly = client.get(f"/sessions/{SID}/positions?granularity=hourly").json()
    assert [h["timestamp"] for h in hourly] == [
        "2026-02-03T10:30:00", "2026-02-03T15:00:00", "2026-02-04T15:00:00"]
    assert client.get(f"/sessions/{SID}/positions?granularity=minute").status_code == 422
    minute = client.get(f"/sessions/{SID}/positions?granularity=minute&date=2026-02-03").json()
    assert len(minute) == 2 and minute[0]["timestamp"] == "2026-02-03T09:31:00"
    assert client.get(f"/sessions/{SID}/positions?granularity=nope").status_code == 422


def test_positions_minute_pagination(client):
    rows = client.get(
        f"/sessions/{SID}/positions?granularity=minute&date=2026-02-03&limit=1&offset=1").json()
    assert len(rows) == 1 and rows[0]["timestamp"] == "2026-02-03T15:00:00"


def test_positions_week_filter_only_applies_to_weekly(client):
    # daily 粒度带 week 参数：忽略而非静默空表
    daily = client.get(f"/sessions/{SID}/positions?granularity=daily&week=2026-W06").json()
    assert [d["timestamp"] for d in daily] == ["2026-02-03", "2026-02-04", "2026-02-05"]


def test_rebalances(client):
    ev = client.get(f"/sessions/{SID}/rebalances").json()
    assert [e["trade_date"] for e in ev] == ["2026-02-03", "2026-02-04"]
    assert ev[0]["buys"][0]["avg_price"] == pytest.approx(10.0)
    assert ev[0]["fees_total"] == pytest.approx(5.001)              # 5.0 + 0 + 0.001
    assert ev[1]["realized_pnl_total"] == pytest.approx(2.0)
    assert ev[1]["fees_total"] == pytest.approx(5.057)              # 5.0 + 0.056 + 0.001
    assert ev[0]["position_diff"][0]["qty_before"] == 0
    assert ev[0]["position_diff"][0]["qty_after"] == 10
    assert ev[1]["position_diff"][0]["qty_after"] == 0


def test_rebalances_pagination(client):
    ev = client.get(f"/sessions/{SID}/rebalances?limit=1").json()
    assert len(ev) == 1 and ev[0]["trade_date"] == "2026-02-03"
    ev2 = client.get(f"/sessions/{SID}/rebalances?limit=1&offset=1").json()
    assert len(ev2) == 1 and ev2[0]["trade_date"] == "2026-02-04"


def test_benchmarks_catalog(client):
    items = client.get("/benchmarks").json()
    assert {"code": "000300.SH", "name": "沪深300", "source": "index_daily"} in items


def test_distributions_shape(client):
    d = client.get(f"/sessions/{SID}/distributions").json()
    # 收益直方图：基线序列日收益 +1% / +0.198% / 0% → 三个桶各 1
    h = d["return_histogram"]
    assert h["bucket_width"] == 0.005 and len(h["buckets"]) == 3
    assert sum(b["count"] for b in h["buckets"]) == 3
    # 回撤事件：fixture 序列单调不降 → 无回撤
    assert d["drawdown_episodes"] == []
    # 换手率：2 月 min(买100,卖112)/日均资产 (1010+1012+1012)/3
    mt = d["monthly_turnover"]
    assert len(mt) == 1 and mt[0]["month"] == "2026-02"
    # abs=1e-6：实现按 round(·,6) 落盘，残差超 approx 默认 rel 容差
    assert mt[0]["turnover"] == pytest.approx(100.0 / ((1010.0 + 1012.0 + 1012.0) / 3), abs=1e-6)
    assert d["turnover_mean"] == pytest.approx(mt[0]["turnover"], abs=1e-6)
    # 仓位序列：02-03 EOD 110/1010，02-04 起空仓
    ex = d["exposure"]
    assert ex["dates"] == ["2026-02-03", "2026-02-04", "2026-02-05"]
    assert ex["ratio"][0] == pytest.approx(110.0 / 1010.0, abs=1e-6) and ex["ratio"][1] == 0.0


def test_distributions_404(client):
    assert client.get("/sessions/nope/distributions").status_code == 404
