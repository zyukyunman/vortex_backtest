"""ADVERSARIAL N8 公司行动入账测试（design/18 N8 真实账户口径）。

攻击面：``session_engine.apply_corporate_actions`` (session_engine.py:279-340) 与其在
``advance()`` (session_engine.py:422-423) / ``app.advance_session`` (app.py:271-294) 的接线，
以及 ``gateway_adapter.load_dividends`` (gateway_adapter.py:167-206) 的取数窗口。

本文件用 **合成** dividend 行（带 ex_date；真实盘缺 ex_date → N8 在真实盘休眠）。绝大多数用例直接
驱动 ``advance()`` / ``apply_corporate_actions()``。RAW+入账 vs qfq 金标对比时费用/滑点关闭以隔离账务。

约定：PASS 用例 = 回归覆盖（断言「正确/设计意图」行为）。xfail(strict=False) 用例 = 已暴露的真实缺陷，
保留为可复现 repro，保证整套绿。每个 xfail 的 reason 携带 BUG-id。
"""
from __future__ import annotations

import pandas as pd
import pytest

from vortex_backtest.gateway_adapter import GatewayDataAdapter
from vortex_backtest.market_rules import AShareRuleEngine
from vortex_backtest.replay_engine import Position, daily_from_minutes
from vortex_backtest.session_engine import (
    SessionRuntime,
    advance,
    apply_corporate_actions,
    finalize,
)

SYM = "600519.SH"


# --------------------------------------------------------------------------- helpers


def _rt(positions, *, cash=0.0, sim_time=None, current_date_key=None, **kw) -> SessionRuntime:
    base = dict(
        session_id="s", strategy_id="s", sim_time=sim_time, cash=cash, initial_cash=1_000_000.0,
        positions=positions, last_prices={}, open_orders=[], trade_counter=0,
        current_date_key=current_date_key, universe=[], fill_timing="this_bar",
        default_price_type="close", slippage_bps=0.0,
    )
    base.update(kw)
    return SessionRuntime(**base)


def _div(symbol, ex_date, *, cash_div_tax=0.0, stk_div=0.0, stk_bo_rate=0.0, stk_co_rate=0.0):
    return {"symbol": symbol, "ex_date": ex_date, "cash_div_tax": cash_div_tax,
            "stk_div": stk_div, "stk_bo_rate": stk_bo_rate, "stk_co_rate": stk_co_rate}


def _bar(day, *, raw, qfq=None, vol=1_000_000, symbol=SYM):
    """单根 14:57 收盘 bar。raw=撮合/估值 RAW 价；qfq 缺省=raw。"""
    px = raw if qfq is None else qfq
    return {
        "symbol": symbol, "trade_time": f"{day} 14:57:00", "date": int(day.replace("-", "")),
        "open": raw, "high": raw, "low": raw, "close": raw,
        "open_qfq": px, "close_qfq": px, "volume": vol, "board": "主板",
        "suspended": False, "up_limit": round(raw * 1.1, 2), "down_limit": round(raw * 0.9, 2),
    }


def _frame(rows):
    return pd.DataFrame(rows)


# ==========================================================================================
# GROUP 1 — PURE SPLIT: NAV continuity + exact qfq equivalence (regression / intended)
# ==========================================================================================


def _run_split_pair():
    """构造纯拆股(10送10)的 RAW+入账 与 qfq 两条路，返回 (rt_raw, rt_qfq, cal)。

    固定锚 adj[0]=1；D2 拆股 adj×2，raw 减半 → qfq=raw×adj 连续=60。无费用？买入端仍有撮合费。
    """
    qty, initial, cal = 90_000, 10_000_000.0, [20260506, 20260507]
    rt_raw = _rt({}, cash=initial, universe=[SYM]); rt_raw.initial_cash = initial
    advance(rt_raw, _frame([_bar("2026-05-06", raw=60.0), _bar("2026-05-07", raw=30.0)]),
            rules=AShareRuleEngine(),
            orders=[{"request_id": "b1", "symbol": SYM, "side": 1, "quantity": qty}],
            to="2026-05-07 14:57:00", dividends=[_div(SYM, 20260507, stk_div=1.0)])
    rt_qfq = _rt({}, cash=initial, universe=[SYM]); rt_qfq.initial_cash = initial
    advance(rt_qfq, _frame([_bar("2026-05-06", raw=60.0, qfq=60.0),
                            _bar("2026-05-07", raw=30.0, qfq=60.0)]),
            rules=AShareRuleEngine(),
            orders=[{"request_id": "b1", "symbol": SYM, "side": 1, "quantity": qty}],
            to="2026-05-07 14:57:00", dividends=None)
    return rt_raw, rt_qfq, cal


def test_pure_split_total_return_exactly_equals_qfq():
    """纯拆股：RAW+送转入账 的 total_return 与 qfq **精确**相等（finalize 用最终持仓，账务自洽）。"""
    rt_raw, rt_qfq, cal = _run_split_pair()
    tr_raw = finalize(rt_raw, cal)["total_return"]
    tr_qfq = finalize(rt_qfq, cal)["total_return"]
    assert tr_raw == tr_qfq, f"纯拆股 RAW+入账({tr_raw}) != qfq({tr_qfq})"


def test_pure_split_nav_series_continuous_no_phantom_drawdown():
    """除权日 NAV 连续性 / max_drawdown：RAW 应与 qfq 一致（拆股不改变市值）。

    缺陷：advance 逐 bar 先拍快照（line 411-415）再在末尾 apply_corporate_actions（line 423）。
    除权日那根 bar 的快照仍是 **未拆股的 90000 股 × 除权后减半 RAW 价 30** = 2.7M（应为 180000×30=5.4M）→
    日级 NAV 在除权日骤降一半 → max_drawdown 出现 -27% 幻影回撤（qfq 路无）。total_return 反而正确（用最终持仓）。
    风险指标(回撤/日收益序列)因此被污染。
    """
    rt_raw, rt_qfq, cal = _run_split_pair()
    s_raw = finalize(rt_raw, cal)
    s_qfq = finalize(rt_qfq, cal)
    navs_raw = [d["total_value"] for d in daily_from_minutes(rt_raw.snapshots, 1e7, cal)]
    navs_qfq = [d["total_value"] for d in daily_from_minutes(rt_qfq.snapshots, 1e7, cal)]
    # 拆股不改变持仓市值 → 两日 NAV 应几乎相等（仅小额费用差），与 qfq 序列一致。
    assert abs(navs_raw[0] - navs_raw[1]) < navs_raw[0] * 0.01, f"除权日 NAV 骤降(幻影): {navs_raw}"
    assert abs(s_raw["max_drawdown"] - s_qfq["max_drawdown"]) < 1e-4, (
        f"RAW max_drawdown={s_raw['max_drawdown']} 与 qfq={s_qfq['max_drawdown']} 不一致(幻影回撤)"
    )


# ==========================================================================================
# GROUP 2 — CASH DIVIDEND ONLY (regression)
# ==========================================================================================


def test_cash_dividend_books_cash_cost_unchanged():
    """现金分红：cash += qty×cash_div_tax，股数/成本不变。"""
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=100.0)
    rt = _rt({SYM: pos})
    apply_corporate_actions(rt, [_div(SYM, 20260506, cash_div_tax=2.0)],
                            lower=pd.Timestamp("2026-05-05 15:00:00"),
                            upper=pd.Timestamp("2026-05-06 15:00:00"))
    assert rt.cash == 2000.0
    assert pos.quantity == 1000
    assert pos.cost_basis == 100.0


def test_cash_div_vs_qfq_within_known_residual():
    """现金分红口径：RAW+入账 一阶吻合 qfq，残差 = 未再投红利二阶项，且 RAW <= qfq+eps。"""
    # 固定锚 adj[0]=1；D2 现金 3/股 ex（adj=1×(100+3)/100=1.03）。
    cal = [20260506, 20260507, 20260508]
    qty, initial = 90_000, 10_000_000.0

    rt_raw = _rt({}, cash=initial, universe=[SYM]); rt_raw.initial_cash = initial
    bars_raw = _frame([_bar("2026-05-06", raw=100.0), _bar("2026-05-07", raw=100.0),
                       _bar("2026-05-08", raw=110.0)])
    advance(rt_raw, bars_raw, rules=AShareRuleEngine(),
            orders=[{"request_id": "b1", "symbol": SYM, "side": 1, "quantity": qty}],
            to="2026-05-08 14:57:00", dividends=[_div(SYM, 20260507, cash_div_tax=3.0)])
    tr_raw = finalize(rt_raw, cal)["total_return"]

    rt_qfq = _rt({}, cash=initial, universe=[SYM]); rt_qfq.initial_cash = initial
    bars_qfq = _frame([_bar("2026-05-06", raw=100.0, qfq=100.0),
                       _bar("2026-05-07", raw=100.0, qfq=103.0),
                       _bar("2026-05-08", raw=110.0, qfq=113.3)])
    advance(rt_qfq, bars_qfq, rules=AShareRuleEngine(),
            orders=[{"request_id": "b1", "symbol": SYM, "side": 1, "quantity": qty}],
            to="2026-05-08 14:57:00", dividends=None)
    tr_qfq = finalize(rt_qfq, cal)["total_return"]

    assert abs(tr_raw - tr_qfq) < 0.01
    assert tr_raw <= tr_qfq + 1e-9


# ==========================================================================================
# GROUP 3 — FRACTIONAL SHARE handling (A-share reality: floor, no cash residual)
# ==========================================================================================


def test_fractional_share_floored_no_cash_residual():
    """非整数送股：130×0.33=42.9 → int()=42（向下取整），现金不变。

    A 股现实：碎股不入账、不折现金（实际 A 股送股本就按比例取整到股，余数归零，无碎股折现）。
    断言「碎股被丢弃且 cash 不变」是当前设计意图（design/18 N8 仅 cash/qty/cost）。
    """
    pos = Position(quantity=130, sellable_quantity=130, cost_basis=10.0)
    rt = _rt({"000001.SZ": pos})
    apply_corporate_actions(rt, [_div("000001.SZ", 20260506, stk_div=0.33)],
                            lower=None, upper=pd.Timestamp("2026-05-06 15:00:00"))
    assert pos.quantity == 130 + 42  # int(130*0.33)=42
    assert rt.cash == 0.0  # 碎股 0.9 股不折现金（设计意图）


# ==========================================================================================
# GROUP 4 — DOUBLE-COUNT / BOUNDARY guards
# ==========================================================================================


def test_ex_date_on_boundary_credited_exactly_once():
    """ex_ts 恰落在两次 advance 的 sim_time 边界 → 入账恰好一次（(lower,upper] 半开于 lower）。"""
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=100.0)
    rt = _rt({SYM: pos})
    div = [_div(SYM, 20260506, cash_div_tax=1.0)]
    ex_ts = pd.Timestamp("2026-05-06 00:00:00")  # _ex_timestamp(20260506) = 当日 00:00
    apply_corporate_actions(rt, div, lower=None, upper=ex_ts)               # 闭于 upper → 入账
    apply_corporate_actions(rt, div, lower=ex_ts, upper=pd.Timestamp("2026-05-06 14:57"))  # 半开于 lower → 跳过
    assert rt.cash == 1000.0  # 恰好一次


def test_ex_date_split_across_two_intraday_advances_credited_once():
    """同一除权日跨两次盘中 advance（早盘/午盘）→ 入账恰好一次（ex_ts=00:00 落在首步窗口）。"""
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=100.0)
    rt = _rt({SYM: pos})
    div = [_div(SYM, 20260506, cash_div_tax=1.0)]
    # advance1: (None, 09:31] 含 ex_ts 00:00 → 入账。advance2: (09:31, 14:57] 不含 → 跳过。
    apply_corporate_actions(rt, div, lower=None, upper=pd.Timestamp("2026-05-06 09:31"))
    apply_corporate_actions(rt, div, lower=pd.Timestamp("2026-05-06 09:31"),
                            upper=pd.Timestamp("2026-05-06 14:57"))
    assert rt.cash == 1000.0


def test_duplicate_dividend_row_in_single_advance_not_double_counted():
    """单次 advance 内同一分红行出现两次（如上游合并漏掉/网关多状态行）→ 应入账一次，不双计。

    当前缺陷：apply_corporate_actions 直接遍历 dividends 列表无去重；去重仅在 load_dividends 里做。
    引擎契约层应自身幂等（防御性），否则任何绕过 load_dividends 的调用方都会双计现金/送股。
    """
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=100.0)
    rt = _rt({SYM: pos})
    d = _div(SYM, 20260506, cash_div_tax=1.0)
    apply_corporate_actions(rt, [d, dict(d)], lower=None, upper=pd.Timestamp("2026-05-06 14:57"))
    assert rt.cash == 1000.0, f"同一除权行重复 → 双计现金: {rt.cash}"


# ==========================================================================================
# GROUP 5 — LEAD A: 粗粒度 advance，除权日早于建仓日 → 幻影分红（真实账户口径下应为 0）
# ==========================================================================================


def _cash_delta_minus_fills(rt, initial):
    """公司行动入账的现金部分（隔离撮合费用/买入支出）。

    撮合在 advance 内先于 apply_corporate_actions 完成；最后一笔成交的 ``cash_after`` 即「公司行动前」现金。
    无成交则用 ``initial``。差额 = 公司行动入账现金。
    """
    pre_ca_cash = float(rt.trades[-1]["cash_after"]) if rt.trades else initial
    return rt.cash - pre_ca_cash


def test_lead_a_phantom_dividend_buy_after_ex_date_in_coarse_advance():
    """LEAD A（真实缺陷）：粗 advance D1..D5 一步内，ex_date=D1，建仓在 D3（除权日**之后**）。

    真实账户：除权日 D1 你 0 持仓 → 应得 0 现金分红。但 apply_corporate_actions 在 advance **末尾**对
    **最终持仓**入账，且 D1∈(lower,upper] → 给了你买入前不该有的分红 = 幻影现金 = 账务高估 + 隐性未来函数。
    """
    initial = 10_000_000.0
    rt = _rt({}, cash=initial, universe=[SYM], current_date_key=None); rt.initial_cash = initial
    bars = _frame([
        _bar("2026-05-06", raw=100.0), _bar("2026-05-07", raw=100.0),
        _bar("2026-05-08", raw=100.0), _bar("2026-05-11", raw=100.0),
        _bar("2026-05-12", raw=100.0),
    ])
    # A 语义：显式 trade_date=D3 → 在 D3 (除权日 D1 之后) 才成交建仓。
    advance(rt, bars, rules=AShareRuleEngine(),
            orders=[{"request_id": "b1", "symbol": SYM, "side": 1, "quantity": 1000,
                     "trade_date": "2026-05-08", "exec_time": "14:57:00"}],
            to="2026-05-12 14:57:00",
            dividends=[_div(SYM, 20260506, cash_div_tax=5.0)])  # ex_date=D1 (建仓前)
    # 除权日 D1 持仓应为 0 → 入账现金应为 0（费用/买入已隔离）。
    booked = _cash_delta_minus_fills(rt, initial)
    assert abs(booked) < 1.0, (
        f"幻影分红：在除权日(D1)之后(D3)建仓却被入账 {booked:.0f} 现金；除权日持仓应为 0。cash={rt.cash}"
    )


def test_lead_a_holding_through_ex_date_is_correct():
    """对照组（应 PASS）：若在除权日**之前**(D0)建仓并持有跨除权日 → 入账正确 5000（非全盘误报）。"""
    initial = 10_000_000.0
    rt = _rt({}, cash=initial, universe=[SYM], current_date_key=None); rt.initial_cash = initial
    bars = _frame([
        _bar("2026-05-05", raw=100.0), _bar("2026-05-06", raw=100.0),
        _bar("2026-05-07", raw=100.0),
    ])
    advance(rt, bars, rules=AShareRuleEngine(),
            orders=[{"request_id": "b1", "symbol": SYM, "side": 1, "quantity": 1000,
                     "trade_date": "2026-05-05", "exec_time": "14:57:00"}],
            to="2026-05-07 14:57:00",
            dividends=[_div(SYM, 20260506, cash_div_tax=5.0)])  # ex_date=D1，建仓 D0 之后
    booked = _cash_delta_minus_fills(rt, initial)
    assert abs(booked - 5000.0) < 1.0  # 正确入账 5000（1000 股 × 5）


# ==========================================================================================
# GROUP 6 — LEAD B: load_dividends 无窗口 → 快照只取每 symbol 最新一条
# 粗窗口含两个 ex_date 时，更早的被静默丢弃 → 引擎永不入账。
# ==========================================================================================


class _SnapshotOnlyAdapter(GatewayDataAdapter):
    """忠实模拟网关路由（query.py:gateway_query）：
    - 带 ``window.range`` + symbols → read_symbols：返回窗口内**全部** ≤ as_of 的可见行。
    - 无 window → count=1 快照：每 symbol 仅 ≤ as_of 的**最新一条**（query.py:127）。

    N8-2 的修复 = load_dividends 现带 window（由 app 透传 start）→ 走 read_symbols 拿回全部除权行，
    不再被 count=1 快照截成最近一笔。本 stub 据此区分两条路由以回归该修复。
    """

    def __init__(self, rows: list[dict]):
        super().__init__(base_url="http://stub", token="x")
        self._rows = rows  # 每个 dict 须含 symbol, ex_date(yyyymmdd 字符串), effective_from, cash_div_tax...

    def _query(self, as_of: str, datasets: list[dict]) -> dict:
        sub = datasets[0]
        want = set(sub.get("symbols") or [])
        as_of_norm = str(as_of)
        has_window = bool((sub.get("window") or {}).get("range"))
        # effective_from<=as_of 的可见行，按 ex_date 升序。
        visible = [r for r in sorted(self._rows, key=lambda x: str(x["ex_date"]))
                   if r["symbol"] in want and str(r.get("effective_from", "")) <= as_of_norm]
        if has_window:
            rows = visible  # read_symbols：窗口内全部可见行（N8-2 修复路径）
        else:
            per_sym: dict[str, dict] = {}
            for r in visible:
                per_sym[r["symbol"]] = r  # 升序 → 最后留下=最新（count=1 快照）
            rows = list(per_sym.values())
        cols = ["symbol", "ex_date", "cash_div_tax", "stk_div", "stk_bo_rate", "stk_co_rate"]
        return {"as_of": as_of, "results": {"dividend": {
            "columns": cols,
            "rows": [{c: r.get(c, 0.0) for c in cols} for r in rows],
            "row_count": len(rows),
        }}}


def test_lead_b_two_dividends_in_window_returned_with_window():
    """N8-2 修复回归：一只票在粗 advance 窗口内有两个除权日 → load_dividends 带 window(start) 取回**两笔**。

    修复前 load_dividends 无 window → 网关 count=1 快照只回最新一笔（早的 20260507 被静默丢、引擎少入账）。
    修复后带 ``start`` → 走 read_symbols 返回窗口内全部，两笔都被取回。
    """
    from datetime import date
    rows = [
        {"symbol": SYM, "ex_date": "20260507", "effective_from": "2026-04-01T09:30:00+08:00",
         "cash_div_tax": 2.0, "stk_div": 0.0, "stk_bo_rate": 0.0, "stk_co_rate": 0.0},
        {"symbol": SYM, "ex_date": "20260511", "effective_from": "2026-04-20T09:30:00+08:00",
         "cash_div_tax": 3.0, "stk_div": 0.0, "stk_bo_rate": 0.0, "stk_co_rate": 0.0},
    ]
    adapter = _SnapshotOnlyAdapter(rows)
    out = adapter.load_dividends(symbols={SYM}, as_of="2026-05-12T15:00:00", start=date(2026, 5, 1))
    ex_dates = sorted(d["ex_date"] for d in out)
    assert ex_dates == [20260507, 20260511], (
        f"带 window 应取回两笔，实得 {ex_dates}"
    )
    # 对照：不带 window（start=None）→ 仍是 count=1 快照，仅最新一笔（记录旧行为，证明修复点在 window）。
    out_nowin = adapter.load_dividends(symbols={SYM}, as_of="2026-05-12T15:00:00")
    assert sorted(d["ex_date"] for d in out_nowin) == [20260511]


def test_lead_b_single_dividend_path_ok():
    """对照组：单一除权日时即便 no-window 快照也取回该笔（证明 stub 与解析链正确）。"""
    rows = [{"symbol": SYM, "ex_date": "20260511", "effective_from": "2026-04-20T09:30:00+08:00",
             "cash_div_tax": 3.0, "stk_div": 0.0, "stk_bo_rate": 0.0, "stk_co_rate": 0.0}]
    out = _SnapshotOnlyAdapter(rows).load_dividends(symbols={SYM}, as_of="2026-05-12T15:00:00")
    assert len(out) == 1 and out[0]["ex_date"] == 20260511 and out[0]["cash_div_tax"] == 3.0


# ==========================================================================================
# GROUP 7 — T+1 sellable interaction with split bonus shares
# ==========================================================================================


def test_split_bonus_shares_sellable_flag_after_cross_day_unlock():
    """送股后 sellable：apply_corporate_actions 只动 quantity 不动 sellable_quantity。

    同一 advance 内送股 → quantity 翻倍但 sellable 维持旧值（红股当日锁仓，符合 T+1 直觉）。
    断言「下一日跨日 unlock 后 sellable 追平 quantity」（自愈），并记录当日锁仓现状。
    """
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=100.0)
    rt = _rt({SYM: pos})
    apply_corporate_actions(rt, [_div(SYM, 20260506, stk_div=1.0)],
                            lower=None, upper=pd.Timestamp("2026-05-06 14:57"))
    # 当日：红股未解锁
    assert pos.quantity == 2000
    assert pos.sellable_quantity == 1000  # 现状：红股当日锁仓（intended T+1-like）
    # 跨日 unlock 后追平
    from vortex_backtest.replay_engine import unlock_positions
    unlock_positions(rt.positions)
    assert pos.sellable_quantity == 2000


# ==========================================================================================
# GROUP 8 — ratio source: stk_div is total ratio (no double count of 送+转)
# ==========================================================================================


def test_stk_div_total_ratio_not_summed_with_bo_co():
    """stk_div=送+转总比例：用 stk_div 而非 stk_div+bo+co（否则送股双计）。"""
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=10.0)
    rt = _rt({"600508.SH": pos})
    apply_corporate_actions(rt, [_div("600508.SH", 20260506, stk_div=0.4, stk_bo_rate=0.3, stk_co_rate=0.1)],
                            lower=None, upper=pd.Timestamp("2026-05-06 15:00:00"))
    assert pos.quantity == 1400  # +int(1000*0.4)，非 +int(1000*0.8)


def test_negative_cash_div_does_not_silently_drain_cash():
    """脏数据防御：cash_div_tax 为负（脏行）不应静默扣减账户现金（应被滤/钳为 0）。

    真实账户分红只增不减；负现金分红是数据错误。当前 _to_float 直接透传负值 → cash 被扣减。
    """
    pos = Position(quantity=1000, sellable_quantity=1000, cost_basis=100.0)
    rt = _rt({SYM: pos}, cash=5000.0)
    apply_corporate_actions(rt, [_div(SYM, 20260506, cash_div_tax=-2.0)],
                            lower=None, upper=pd.Timestamp("2026-05-06 15:00:00"))
    # 负分红被钳为 0 → cash 不变。若被扣减则暴露缺陷。
    assert rt.cash == 5000.0, f"负 cash_div_tax 静默扣减现金: {rt.cash}"
