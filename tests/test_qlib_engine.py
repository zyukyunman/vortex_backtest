"""QlibReplayEngine 纯函数单测（不依赖 pyqlib）。

覆盖分钟→日级会话 bar 的归约口径，以及 qlib float32 存价的 tick 回正。
qlib 仅在 run() 内惰性 import，故这些函数本机可测。
"""
from __future__ import annotations

import pandas as pd

from vortex_backtest.qlib_engine import (
    _aggregate_minute_to_daily,
    _bars_by_symbol_date,
    from_qlib_code,
    to_qlib_code,
)

_FIELDS = ["$open", "$high", "$low", "$close", "$volume",
           "$factor", "$change", "$limit_up", "$limit_down", "$paused"]


def _minute_frame() -> pd.DataFrame:
    """SH600000 两个交易日的分钟 bar；故意乱序构造以验证 sort_index。"""
    rows = [
        # (inst, ts, open, high, low, close, vol, factor, change, up, down, paused)
        # 06-02（先放，验证排序）
        ("SH600000", "2026-06-02 15:00:00", 10.40, 10.55, 10.40, 11.819999694824219, 80, 0.5, 0.0, 11.2, 9.2, 0),
        ("SH600000", "2026-06-02 09:30:00", 10.30, 10.45, 10.25, 10.35, 300, 0.5, 0.0, 11.2, 9.2, 0),
        # 06-01（分钟乱序）
        ("SH600000", "2026-06-01 09:31:00", 10.05, 10.15, 10.00, 10.10, 200, 0.5, 0.01, 11.0, 9.0, 0),
        ("SH600000", "2026-06-01 15:00:00", 10.20, 10.25, 10.15, 10.22, 50, 0.5, 0.01, 11.0, 9.0, 0),
        ("SH600000", "2026-06-01 09:30:00", 10.00, 10.10, 9.95, 10.05, 100, 0.5, 0.01, 11.0, 9.0, 0),
    ]
    idx = pd.MultiIndex.from_tuples(
        [(r[0], pd.Timestamp(r[1])) for r in rows], names=["instrument", "datetime"]
    )
    data = {field: [r[2 + i] for r in rows] for i, field in enumerate(_FIELDS)}
    return pd.DataFrame(data, index=idx)


def test_symbol_code_roundtrip() -> None:
    assert to_qlib_code("600000.SH") == "SH600000"
    assert from_qlib_code("SH600000") == "600000.SH"


def test_aggregate_minute_to_daily_session_ohlcv() -> None:
    daily = _aggregate_minute_to_daily(_minute_frame(), pd)
    d1 = daily.loc[("SH600000", pd.Timestamp("2026-06-01"))]
    assert d1["$open"] == 10.00        # 首分钟
    assert d1["$close"] == 10.22       # 末分钟
    assert d1["$high"] == 10.25        # 日内极值
    assert d1["$low"] == 9.95
    assert d1["$volume"] == 350        # 100+200+50 日内累加
    assert d1["$factor"] == 0.5        # 日级广播常数
    assert d1["$limit_up"] == 11.0 and d1["$limit_down"] == 9.0
    assert d1["$paused"] == 0

    d2 = daily.loc[("SH600000", pd.Timestamp("2026-06-02"))]
    assert d2["$open"] == 10.30 and d2["$volume"] == 380


def test_bars_from_aggregated_minutes_round_and_qfq() -> None:
    daily = _aggregate_minute_to_daily(_minute_frame(), pd)
    bars = _bars_by_symbol_date(daily, pd)

    b1 = bars[("600000.SH", 20260601)]
    assert b1["open"] == 10.00 and b1["close"] == 10.22 and b1["volume"] == 350
    assert b1["factor"] == 0.5
    assert b1["up_limit"] == 11.0                 # raw 涨停（tick 校验用）
    assert b1["limit_up_qfq"] == 11.0 * 0.5       # qfq 涨停
    assert b1["suspended"] is False

    # qlib float32 存价（11.8199996…）末分钟 close → round(2) 回 11.82，过 tick
    b2 = bars[("600000.SH", 20260602)]
    assert b2["close"] == 11.82
