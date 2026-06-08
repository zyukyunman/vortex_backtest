"""金标：真实账户口径(RAW 不复权 + 除权日公司行动入账) 的 total return ≈ N5 前复权口径（design/18 N8）。

两条路应对同一现实殊途同归：
- 前复权(qfq)：把分红/送转吸进价（隐含红利再投），价连续，return = 含红利总收益。
- 真实账户(raw)：RAW 价在除权日跳变，分红现金入账、送转加股、成本稀释——账户净值口径。

差异只剩"现金分红是否再投"的二阶项（前复权再投、真实账户留现金）→ 收益率应一阶吻合。
送转/纯拆股（无现金）则**精确**等价。场景按固定锚 adj 自洽构造：qfq = raw × adj ÷ adj[会话起始]，
分红与 adj 跳变互恰（现金 adj_t/adj_{t-1}=(raw_t+div)/raw_t；拆股按股数比）。
"""
from __future__ import annotations

import pandas as pd

from vortex_backtest.market_rules import AShareRuleEngine
from vortex_backtest.replay_engine import Position
from vortex_backtest.session_engine import SessionRuntime, advance, finalize

SYM = "600519.SH"


def _bars(scenario: list[tuple], *, mode: str) -> pd.DataFrame:
    """scenario: [(date, raw_close, qfq_close)]。mode=raw → *_qfq=raw；mode=qfq → *_qfq=qfq。"""
    rows = []
    for day, raw, qfq in scenario:
        px = raw if mode == "raw" else qfq
        rows.append({
            "symbol": SYM, "trade_time": f"{day} 14:57:00", "date": int(day.replace("-", "")),
            "open": raw, "high": raw, "low": raw, "close": raw,
            "open_qfq": px, "close_qfq": px,
            "volume": 1_000_000, "board": "主板", "suspended": False,
            "up_limit": round(raw * 1.1, 2), "down_limit": round(raw * 0.9, 2),
        })
    return pd.DataFrame(rows)


def _run(scenario: list[tuple], dividends, *, qty: int = 90_000, initial: float = 10_000_000.0) -> float:
    """买 qty 持有到末日，一次 advance 走完。dividends=None → qfq 口径；否则 raw 口径。"""
    mode = "qfq" if dividends is None else "raw"
    rt = SessionRuntime(
        session_id="s", strategy_id="s", sim_time=None, cash=initial, initial_cash=initial,
        positions={}, last_prices={}, open_orders=[], trade_counter=0, current_date_key=None,
        universe=[SYM], fill_timing="this_bar", default_price_type="close", slippage_bps=0.0,
    )
    bars = _bars(scenario, mode=mode)
    last_day = scenario[-1][0]
    advance(rt, bars, rules=AShareRuleEngine(),
            orders=[{"request_id": "b1", "symbol": SYM, "side": 1, "quantity": qty}],
            to=f"{last_day} 14:57:00", dividends=dividends)
    calendar = [int(d.replace("-", "")) for d, _, _ in scenario]
    return finalize(rt, calendar)["total_return"]


def test_pure_split_exact_equivalence():
    """纯拆股(10送10，无现金)：raw+送转入账 与 qfq 收益率**精确**相等。"""
    # 固定锚 adj[0]=1；D2 拆股 adj×2，raw 减半 → qfq=raw×adj 连续=60
    scenario = [("2026-05-06", 60.0, 60.0), ("2026-05-07", 30.0, 60.0)]
    tr_qfq = _run(scenario, None)
    tr_raw = _run(scenario, [{"symbol": SYM, "ex_date": 20260507, "stk_div": 1.0}])
    assert tr_raw == tr_qfq


def test_cash_div_and_split_returns_match_within_tolerance():
    """现金分红 + 拆股的买入持有：两口径收益率一阶吻合（残差=未再投红利的二阶项）。"""
    # 固定锚 adj[0]=1。qfq = raw × adj。
    #  D1 raw100 adj1.00 qfq100   ← 买入
    #  D2 raw100 adj1.03 qfq103   现金分红 3/股 ex D2（adj=1×(100+3)/100=1.03）
    #  D3 raw110 adj1.03 qfq113.3 涨
    #  D4 raw55  adj2.06 qfq113.3 拆股 10送10 ex D4（adj×2，raw 减半）
    #  D5 raw60  adj2.06 qfq123.6 涨
    scenario = [
        ("2026-05-06", 100.0, 100.0),
        ("2026-05-07", 100.0, 103.0),
        ("2026-05-08", 110.0, 113.3),
        ("2026-05-11", 55.0, 113.3),
        ("2026-05-12", 60.0, 123.6),
    ]
    tr_qfq = _run(scenario, None)
    tr_raw = _run(scenario, [
        {"symbol": SYM, "ex_date": 20260507, "cash_div_tax": 3.0},
        {"symbol": SYM, "ex_date": 20260511, "stk_div": 1.0},
    ])
    # 一阶吻合：绝对差 < 1 个百分点（缺现金分红入账会 >3pp、缺拆股会数十 pp，均被本断言拦下）
    assert abs(tr_raw - tr_qfq) < 0.01
    # 方向：真实账户留现金不再投 → 略低于前复权（再投）
    assert tr_raw <= tr_qfq + 1e-9
    # 两路都实现了正收益（场景净上涨），非退化
    assert tr_raw > 0.15 and tr_qfq > 0.15
