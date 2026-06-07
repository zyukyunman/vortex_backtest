"""金标准用例：手算期望值，锁定 replay 引擎在 A 股口径下的正确性（design/15 Phase 4）。

约定：`adj_factor` 默认 1.0 → qfq == raw，价格即真实价，便于逐笔手算。
默认费率：佣金 0.03%（最低 5 元）、印花税 0.05%（仅卖出）、过户费 0.001%。
撮合点：每个交易日 09:31（open）与 14:57（close）各一根 bar。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="读取 parquet 需要 pyarrow")

from vortex_backtest.data_adapter import TushareMinuteDataLoader  # noqa: E402
from vortex_backtest.replay_engine import MinuteReplayEngine  # noqa: E402


def _order(rid: str, day: str, symbol: str, side: int, qty: int, price_type: str = "close", **kw: Any) -> dict[str, Any]:
    return {
        "request_id": rid,
        "order_batch_id": "default",
        "trade_date": day,
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "price_type": price_type,
        **kw,
    }


def _run(
    ws: Path,
    tmp: Path,
    *,
    orders: list[dict[str, Any]],
    start: date,
    end: date,
    cash: float = 1_000_000.0,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    engine = MinuteReplayEngine(data_loader=TushareMinuteDataLoader(ws))
    return engine.run(
        job_id="golden",
        account={"account_id": "a", "initial_cash": cash, "engine": "replay"},
        orders=orders,
        report_dir=tmp / "rep",
        start_date=start,
        end_date=end,
        execution=execution,
    )


def _one(rows: list[dict[str, Any]], **match: Any) -> dict[str, Any]:
    hits = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    assert len(hits) == 1, f"期望唯一匹配 {match}，实得 {len(hits)}：{rows}"
    return hits[0]


# ── Case A：买入费用与成本 ───────────────────────────────────────────────
def test_buy_fees_and_cost(tmp_path, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    s = _run(ws, tmp_path, orders=[_order("b", "2026-01-02", "000001.SZ", 1, 1000)],
             start=date(2026, 1, 2), end=date(2026, 1, 2))
    t = _one(s["trades"], request_id="b")
    assert (t["quantity"], t["requested_quantity"], t["price"], t["amount"]) == (1000, 1000, 10.0, 10000.0)
    assert t["commission"] == pytest.approx(5.0)       # max(10000*0.0003=3, 5)
    assert t["stamp_tax"] == pytest.approx(0.0)         # 买入无印花
    assert t["transfer_fee"] == pytest.approx(0.1)      # 10000*0.00001
    assert t["realized_pnl"] == pytest.approx(0.0)
    assert s["cash"] == pytest.approx(989_994.9)        # 1e6 - 10000 - 5.1
    assert s["market_value"] == pytest.approx(10_000.0)
    assert s["total_value"] == pytest.approx(999_994.9)


# ── Case B：卖出已实现盈亏 + 印花税 + T+1 解锁 ───────────────────────────
def test_sell_realized_pnl_and_stamp_tax(tmp_path, workspace_builder):
    ws = (
        workspace_builder
        .day("2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .day("2026-01-05", "000001.SZ", open=11.0, close=11.0, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .build()
    )
    s = _run(ws, tmp_path, orders=[
        _order("b", "2026-01-02", "000001.SZ", 1, 1000),
        _order("s", "2026-01-05", "000001.SZ", 2, 1000),
    ], start=date(2026, 1, 2), end=date(2026, 1, 5))
    sell = _one(s["trades"], request_id="s")
    assert sell["stamp_tax"] == pytest.approx(5.5)          # 11000*0.0005
    assert sell["realized_pnl"] == pytest.approx(989.39)    # 1000*(11-10) - (5+5.5+0.11)
    assert s["realized_pnl"] == pytest.approx(989.39)
    assert s["cash"] == pytest.approx(1_000_984.29)         # 989994.9 + 11000 - 10.61
    assert s["market_value"] == pytest.approx(0.0)
    assert s["total_value"] == pytest.approx(1_000_984.29)


# ── Case C：T+1 当日买入不可卖 ──────────────────────────────────────────
def test_t_plus_1_blocks_same_day_sell(tmp_path, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.5, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    s = _run(ws, tmp_path, orders=[
        _order("b", "2026-01-02", "000001.SZ", 1, 1000, price_type="open"),
        _order("s", "2026-01-02", "000001.SZ", 2, 1000, price_type="close"),
    ], start=date(2026, 1, 2), end=date(2026, 1, 2))
    assert [t["request_id"] for t in s["trades"]] == ["b"]
    assert _one(s["rejections"], request_id="s")["reason"] == "t_plus_1_not_sellable"


# ── Case D：涨停不可买 ──────────────────────────────────────────────────
def test_limit_up_blocks_buy(tmp_path, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=11.0, close=11.0, volume=1_000_000, up_limit=11.0, down_limit=9.0
    ).build()
    s = _run(ws, tmp_path, orders=[_order("b", "2026-01-02", "000001.SZ", 1, 1000, price_type="open")],
             start=date(2026, 1, 2), end=date(2026, 1, 2))
    assert s["trades"] == []
    assert _one(s["rejections"], request_id="b")["reason"] == "limit_up_buy_blocked"


# ── Case E：科创板最小 200 股 ───────────────────────────────────────────
def test_star_min_lot_200(tmp_path, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "688981.SH", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    s = _run(ws, tmp_path, orders=[
        _order("small", "2026-01-02", "688981.SH", 1, 150),
        _order("ok", "2026-01-02", "688981.SH", 1, 250),
    ], start=date(2026, 1, 2), end=date(2026, 1, 2))
    assert _one(s["rejections"], request_id="small")["reason"] == "invalid_lot_size"
    assert _one(s["trades"], request_id="ok")["quantity"] == 250


# ── Case F：量能上限 → 部分成交（含整手向下取整）─────────────────────────
def test_partial_fill_by_volume_cap(tmp_path, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=350, up_limit=99.0, down_limit=1.0
    ).build()
    s = _run(ws, tmp_path, orders=[_order("b", "2026-01-02", "000001.SZ", 1, 1000)],
             start=date(2026, 1, 2), end=date(2026, 1, 2))
    t = _one(s["trades"], request_id="b")
    assert t["requested_quantity"] == 1000          # 原始下单
    assert t["quantity"] == 300                     # min(1000, floor(350)) 再向下取整到整手
    assert t["amount"] == pytest.approx(3000.0)


# ── Case G：当日无行情 → no_market_data 拒单（不凭空消失）─────────────────
def test_order_on_day_without_data_is_rejected(tmp_path, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    s = _run(ws, tmp_path, orders=[_order("ghost", "2026-01-05", "000001.SZ", 1, 1000)],
             start=date(2026, 1, 2), end=date(2026, 1, 5))
    assert s["trades"] == []
    assert _one(s["rejections"], request_id="ghost")["reason"] == "no_market_data"


# ── Case H：分钟级——exec_time 在指定分钟成交，取该分钟价（首/末分钟价不同）──────────
def test_minute_level_exec_time_fills_at_that_minute(tmp_path, workspace_builder):
    # 09:31 bar 价=10（开盘），14:57 bar 价=11（收盘）
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=11.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    s = _run(ws, tmp_path, orders=[
        _order("am", "2026-01-02", "000001.SZ", 1, 1000, exec_time="09:31"),
        _order("pm", "2026-01-02", "000001.SZ", 1, 1000, exec_time="14:57"),
    ], start=date(2026, 1, 2), end=date(2026, 1, 2))
    assert _one(s["trades"], request_id="am")["price"] == pytest.approx(10.0)   # 首分钟价
    assert _one(s["trades"], request_id="pm")["price"] == pytest.approx(11.0)   # 末分钟价
    # 逐分钟净值 artifact 应已落盘且含数据行
    mins = Path(s["artifacts"]["minute_equity"])
    assert mins.exists()
    assert sum(1 for _ in mins.open(encoding="utf-8")) > 1


# ── Case I：exec_time 落在两根 bar 之间 → at-or-after 取下一根可成交 bar ───────────
def test_minute_level_exec_time_at_or_after(tmp_path, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=11.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    s = _run(ws, tmp_path, orders=[_order("mid", "2026-01-02", "000001.SZ", 1, 1000, exec_time="10:00")],
             start=date(2026, 1, 2), end=date(2026, 1, 2))
    # 10:00 之后首个 bar 是 14:57 → 按 11.0 成交
    assert _one(s["trades"], request_id="mid")["price"] == pytest.approx(11.0)


# ── Case J：exec_time 晚于当日收盘 → no_market_data ─────────────────────────
def test_minute_level_exec_time_after_close_rejected(tmp_path, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=11.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    s = _run(ws, tmp_path, orders=[_order("late", "2026-01-02", "000001.SZ", 1, 1000, exec_time="15:30")],
             start=date(2026, 1, 2), end=date(2026, 1, 2))
    assert s["trades"] == []
    assert _one(s["rejections"], request_id="late")["reason"] == "no_market_data"


# ── Case K：日级向后兼容——price_type open 取首分钟价、close 取末分钟价 ─────────────
def test_day_level_open_vs_close_backward_compat(tmp_path, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=11.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    s = _run(ws, tmp_path, orders=[
        _order("o", "2026-01-02", "000001.SZ", 1, 1000, price_type="open"),
        _order("c", "2026-01-02", "000001.SZ", 1, 1000, price_type="close"),
    ], start=date(2026, 1, 2), end=date(2026, 1, 2))
    assert _one(s["trades"], request_id="o")["price"] == pytest.approx(10.0)
    assert _one(s["trades"], request_id="c")["price"] == pytest.approx(11.0)
