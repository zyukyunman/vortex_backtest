"""会话步进引擎（design/18 B2）：醒来 → 取数 → 决策 → 指定下次醒来。

把订单回放(A) 的"预解析全部订单 + 单 groupby 循环走完"，改成有状态、可逐步推进的会话：
- 会话持有单调 ``sim_time`` 时钟、账户(现金/持仓/T+1可卖/挂单)、股池、配置。
- ``advance(orders, to)`` 提交本步委托 → 撮合本步到期单 → 推进时钟到 ``to`` → 结算 → 回账户上下文。

**撮合/T+1/费用/滑点/NAV 内核 100% 复用** ``replay_engine`` 与 ``market_rules`` 的纯函数，签名不动。
A 是 B 的特例：所有订单带 ``exec_time`` 一次提交、单次 ``advance`` 到 end，逐 bar 撮合 == A。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

import pandas as pd

from .market_rules import AShareRuleEngine, FeeModel
from .models import Side
from .replay_engine import (
    Position,
    daily_from_minutes,
    execute_order,
    max_drawdown,
    minute_snapshot,
    position_rows,
    rejection_row,
    round_money,
    round_ratio,
    unlock_positions,
)
from .symbols import normalize_symbol

FILL_NEXT_BAR = "next_bar"
FILL_THIS_BAR = "this_bar"


def _norm_exec_time(value: str) -> str:
    v = str(value).strip()
    return v if v.count(":") == 2 else f"{v}:00"


def _normalize_session_order(order: Mapping[str, Any]) -> dict[str, Any]:
    """会话内提交的委托归一。trade_date 缺省留 None（由目标 bar 推断；A 模型显式给则用 A 语义）。"""
    td = order.get("trade_date")
    trade_date = (td if isinstance(td, date) else date.fromisoformat(str(td)[:10])) if td else None
    return {
        "order_batch_id": order.get("order_batch_id", "default"),
        "request_id": str(order.get("request_id") or order.get("id") or ""),
        "trade_date": trade_date,
        "symbol": normalize_symbol(str(order["symbol"])),
        "side": int(order["side"]),
        "quantity": int(order["quantity"]),
        "price_type": order.get("price_type"),
        "limit_price": order.get("limit_price"),
        "exec_time": order.get("exec_time"),
    }


@dataclass
class SessionRuntime:
    """会话内存态。由 store 行 hydrate，advance 后 dump 回 store。"""

    session_id: str
    strategy_id: str
    sim_time: pd.Timestamp | None
    cash: float
    initial_cash: float
    positions: dict[str, Position]
    last_prices: dict[str, float]
    open_orders: list[dict[str, Any]]  # {order, price_field, target_ts(iso)}
    trade_counter: int
    current_date_key: int | None
    universe: list[str]
    fill_timing: str = FILL_NEXT_BAR
    default_price_type: str = "close"
    slippage_bps: float = 0.0
    processed_advances: list[str] = field(default_factory=list)  # 已处理的 advance request_id（幂等去重）
    # 累积产物（落 store/CSV 由 app 层负责）
    trades: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    last_cancelled: list[str] = field(default_factory=list)  # 本步撤掉的挂单 request_id（瞬态）

    # ---------------------------------------------------------- (de)serialize

    @classmethod
    def hydrate(cls, row: Mapping[str, Any]) -> "SessionRuntime":
        cfg = json.loads(row.get("config_json") or "{}")
        positions = {
            sym: Position(**p) for sym, p in json.loads(row.get("positions_json") or "{}").items()
        }
        open_orders = json.loads(row.get("open_orders_json") or "[]")
        for oo in open_orders:  # trade_date iso → date
            oo["order"]["trade_date"] = date.fromisoformat(str(oo["order"]["trade_date"])[:10])
        sim = row.get("sim_time")
        return cls(
            session_id=str(row["session_id"]),
            strategy_id=str(cfg.get("strategy_id") or row["session_id"]),
            sim_time=pd.Timestamp(sim) if sim else None,
            cash=float(row["cash"]),
            initial_cash=float(row["initial_cash"]),
            positions=positions,
            last_prices=cfg.get("last_prices", {}),
            open_orders=open_orders,
            trade_counter=int(row.get("trade_counter") or 0),
            current_date_key=cfg.get("current_date_key"),
            universe=json.loads(row.get("universe_json") or "[]"),
            fill_timing=cfg.get("fill_timing", FILL_NEXT_BAR),
            default_price_type=cfg.get("default_price_type", "close"),
            slippage_bps=float(cfg.get("slippage_bps", 0.0)),
            processed_advances=list(cfg.get("processed_advances", [])),
        )

    def dump(self) -> dict[str, Any]:
        """返回 store.update_session 的字段（可变状态）。"""
        oo = []
        for o in self.open_orders:
            order = dict(o["order"])
            order["trade_date"] = order["trade_date"].isoformat() if isinstance(order["trade_date"], date) else order["trade_date"]
            oo.append({**o, "order": order})
        cfg = {
            "strategy_id": self.strategy_id,
            "fill_timing": self.fill_timing,
            "default_price_type": self.default_price_type,
            "slippage_bps": self.slippage_bps,
            "last_prices": self.last_prices,
            "current_date_key": self.current_date_key,
            "processed_advances": self.processed_advances[-200:],  # 保留近 200 个去重指纹
        }
        return {
            "sim_time": self.sim_time.isoformat() if self.sim_time is not None else None,
            "cash": round_money(self.cash),
            "positions_json": json.dumps(
                {s: {"quantity": p.quantity, "cost_basis": p.cost_basis, "sellable_quantity": p.sellable_quantity}
                 for s, p in self.positions.items()}
            ),
            "universe_json": json.dumps(list(self.universe)),
            "open_orders_json": json.dumps(oo),
            "config_json": json.dumps(cfg),
            "trade_counter": self.trade_counter,
        }

    # ------------------------------------------------------------ context out

    def account_context(self) -> dict[str, Any]:
        positions = position_rows(
            strategy_id=self.strategy_id, positions=self.positions, last_prices=self.last_prices
        )
        mv = sum(p["market_value"] for p in positions)
        return {
            "sim_time": self.sim_time.isoformat() if self.sim_time is not None else None,
            "cash": round_money(self.cash),
            "market_value": round_money(mv),
            "nav": round_money(self.cash + mv),
            "positions": positions,
            "open_orders": [
                {"symbol": o["order"]["symbol"], "side": o["order"]["side"],
                 "quantity": o["order"]["quantity"], "target": o["target_ts"]}
                for o in self.open_orders
            ],
        }


def _price_field(order: Mapping[str, Any], default_price_type: str) -> str:
    if order.get("exec_time"):
        return "close"  # exec_time 取 at-or-after bar 的 close（与 A 一致）
    return "open" if (order.get("price_type") or default_price_type) == "open" else "close"


def _symbol_axis(bars: pd.DataFrame) -> dict[str, list[pd.Timestamp]]:
    axis: dict[str, list[pd.Timestamp]] = {}
    if bars.empty:
        return axis
    for sym, rows in bars.groupby("symbol", sort=True):
        axis[str(sym)] = [pd.Timestamp(t) for t in rows.sort_values("trade_time")["trade_time"]]
    return axis


def _resolve_target(
    rt: SessionRuntime, order: Mapping[str, Any], axis: Mapping[str, list[pd.Timestamp]]
) -> pd.Timestamp | None:
    """把会话委托解析到一个目标 bar 时间戳（在本次 advance 的 frame 内）。"""
    times = axis.get(str(order["symbol"]))
    if not times:
        return None
    exec_time = order.get("exec_time")
    if exec_time:
        norm = _norm_exec_time(exec_time)
        if order.get("trade_date") is not None:  # A 语义：指定日 at-or-after exec_time 首个 bar
            try:
                target = pd.Timestamp(f"{order['trade_date'].isoformat()} {norm}")
            except (ValueError, TypeError):
                return None
            return next((t for t in times if t >= target), None)
        # 会话语义：sim_time 之后首个 时刻 ≥ exec_time 的 bar（自然落到正在推进进入的那天）
        lower = rt.sim_time
        for t in times:
            if (lower is None or t > lower) and t.strftime("%H:%M:%S") >= norm:
                return t
        return None
    if rt.sim_time is None:
        return times[0]
    if rt.fill_timing == FILL_THIS_BAR:  # 当前 bar（≤ sim_time 的最后一根，含 sim_time）
        cur = [t for t in times if t <= rt.sim_time]
        return cur[-1] if cur else None
    return next((t for t in times if t > rt.sim_time), None)  # next_bar：严格下一根


def _match_due_at_bar(
    rt: SessionRuntime, rules: AShareRuleEngine, timestamp: pd.Timestamp, row_by_symbol: dict[str, dict]
) -> None:
    """撮合 target==timestamp 的到期单（提取自 replay_engine._run_strategy:202-258，逻辑等价）。"""
    slip = rt.slippage_bps / 1e4
    still_open: list[dict[str, Any]] = []
    for entry in rt.open_orders:
        order = entry["order"]
        target = pd.Timestamp(entry["target_ts"])
        symbol = order["symbol"]
        if target != timestamp or symbol not in row_by_symbol:
            still_open.append(entry)
            continue
        row = row_by_symbol[symbol]
        price_field = entry["price_field"]
        fill_price = float(row[f"{price_field}_qfq"])
        raw_fill_price = float(row[price_field])
        exec_price = fill_price * (1 + slip) if int(order["side"]) == int(Side.BUY) else fill_price * (1 - slip)
        position = rt.positions.setdefault(symbol, Position())
        reason = rules.validate_order(
            order=order, bar=row, cash=rt.cash,
            position_quantity=position.quantity, sellable_quantity=position.sellable_quantity,
            fill_price=exec_price, raw_fill_price=raw_fill_price,
        )
        if reason is not None:
            rt.rejections.append(rejection_row(strategy_id=rt.strategy_id, order=order, reason=reason))
            continue
        qty = rules.executable_quantity(
            side=int(order["side"]), requested_quantity=int(order["quantity"]),
            volume=int(row["volume"]), symbol=symbol, board=str(row["board"]),
            position_quantity=position.quantity,
        )
        if qty <= 0:
            rt.rejections.append(rejection_row(strategy_id=rt.strategy_id, order=order, reason="volume_cap_below_lot"))
            continue
        rt.trade_counter += 1
        trade, rt.cash = execute_order(
            strategy_id=rt.strategy_id, trade_number=rt.trade_counter, order=order,
            quantity=qty, price=exec_price, cash=rt.cash, position=position, fee_model=rules.fee_model,
        )
        rt.trades.append(trade)
        if position.quantity == 0:
            rt.positions.pop(symbol, None)
    rt.open_orders = still_open


def _to_float(value: Any) -> float:
    try:
        f = float(value)
    except (ValueError, TypeError):
        return 0.0
    return 0.0 if f != f else f  # NaN → 0


def _ex_timestamp(ex_date: Any) -> pd.Timestamp | None:
    """yyyymmdd(int|str) → 当日 00:00 时间戳（除权日开始）。无法解析 → None。"""
    try:
        key = str(int(str(ex_date)[:8].replace("-", "")))
    except (ValueError, TypeError):
        return None
    if len(key) != 8:
        return None
    try:
        return pd.Timestamp(f"{key[:4]}-{key[4:6]}-{key[6:8]}")
    except (ValueError, TypeError):
        return None


def apply_corporate_actions(
    rt: SessionRuntime,
    dividends: list[Mapping[str, Any]] | None,
    *,
    lower: pd.Timestamp | None,
    upper: pd.Timestamp | None,
) -> list[dict[str, Any]]:
    """对 (lower, upper] 内除权的持仓 symbol 应用公司行动（design/18 N8 真实账户口径）。

    - 现金分红：``cash += qty × cash_div_tax``
    - 送转：    ``qty += int(qty × ratio)``，``ratio = stk_div``（送+转 总比例，实证 ≡ stk_bo_rate+stk_co_rate；
      stk_div 缺/为 0 → 回退 stk_bo_rate+stk_co_rate）
    - 成本：    ``cost_basis = 旧总成本 / 新股数``

    按 ``ex_date`` 升序应用（现金/成本链式正确：后一笔用前一笔 split 后的股数），仅对当前持仓(quantity>0)。
    替代前复权把分红吸进价——RAW 价在除权日跳变，由此处入账抵消。返回入账明细（供上下文/日志）。

    注：估值刷新仍依赖除权日有 bar 驱动 ``last_prices``（日/分钟步进成立）；本函数只动 cash/quantity/cost_basis。
    """
    applied: list[dict[str, Any]] = []
    if not dividends or upper is None:
        return applied
    upper = pd.Timestamp(upper)
    lower = pd.Timestamp(lower) if lower is not None else None
    items: list[tuple[pd.Timestamp, Mapping[str, Any]]] = []
    for d in dividends:
        ex_ts = _ex_timestamp(d.get("ex_date"))
        if ex_ts is not None:
            items.append((ex_ts, d))
    items.sort(key=lambda x: x[0])
    for ex_ts, d in items:
        if lower is not None and ex_ts <= lower:
            continue  # 已过窗口（前次 advance 已入账）
        if ex_ts > upper:
            continue  # 未到除权日
        sym = normalize_symbol(str(d["symbol"]))
        pos = rt.positions.get(sym)
        if pos is None or pos.quantity <= 0:
            continue  # 只对当前持仓入账
        old_qty = pos.quantity
        old_total_cost = old_qty * pos.cost_basis
        cash_div = _to_float(d.get("cash_div_tax"))
        ratio = _to_float(d.get("stk_div"))
        if ratio <= 0:
            ratio = _to_float(d.get("stk_bo_rate")) + _to_float(d.get("stk_co_rate"))
        add_shares = int(old_qty * ratio)
        new_qty = old_qty + add_shares
        cash_added = old_qty * cash_div
        rt.cash += cash_added
        pos.quantity = new_qty
        if new_qty > 0:
            pos.cost_basis = old_total_cost / new_qty
        applied.append({
            "strategy_id": rt.strategy_id,
            "symbol": sym,
            "ex_date": int(ex_ts.strftime("%Y%m%d")),
            "cash_dividend": round_money(cash_added),
            "shares_added": add_shares,
            "quantity": new_qty,
            "cost_basis": pos.cost_basis,
        })
    return applied


def advance(
    rt: SessionRuntime,
    bars: pd.DataFrame,
    *,
    rules: AShareRuleEngine,
    orders: list[Mapping[str, Any]] | None = None,
    set_universe: list[str] | None = None,
    to: str | pd.Timestamp | None = None,
    cancel: list[str] | None = None,
    dividends: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """推进一步：撤单 → 提交本步委托 → 撮合到期单 → 推进时钟到 ``to`` → 结算 → 回账户上下文。

    ``bars``：本步覆盖 (sim_time, to] 的富 bar（含 close_qfq/open_qfq/limit/volume/board/suspended/...），
    由网关/调用方提供。``to`` 必须 ≥ sim_time（单调）。``cancel``：撤掉队列里未成交的挂单 request_id
    （撤单-only = 传 cancel 且 ``to`` 取当前 sim_time，不推进时钟）。
    """
    to_ts = pd.Timestamp(to) if to is not None else (rt.sim_time)
    if rt.sim_time is not None and to_ts is not None and to_ts < rt.sim_time:
        raise ValueError(f"advance 时钟不单调：to={to_ts} < sim_time={rt.sim_time}")
    entry_sim_time = rt.sim_time  # 本步窗口下界（除权日检测：(entry_sim_time, 新 sim_time]）

    # 撤单：从停泊队列移除未成交挂单（先于本步新单/撮合）
    rt.last_cancelled = []
    if cancel:
        cancel_set = {str(c) for c in cancel}
        rt.last_cancelled = [o["order"]["request_id"] for o in rt.open_orders
                             if o["order"]["request_id"] in cancel_set]
        rt.open_orders = [o for o in rt.open_orders
                          if o["order"]["request_id"] not in cancel_set]

    if set_universe is not None:
        rt.universe = [normalize_symbol(s) for s in set_universe]

    axis = _symbol_axis(bars)
    fallback_date = (rt.sim_time.date() if rt.sim_time is not None else (to_ts.date() if to_ts is not None else date.today()))
    # 解析并停泊新订单
    for raw in orders or []:
        order = _normalize_session_order(raw)
        target = _resolve_target(rt, order, axis)
        if target is None:
            if order["trade_date"] is None:
                order["trade_date"] = fallback_date
            rt.rejections.append(rejection_row(strategy_id=rt.strategy_id, order=order, reason="no_market_data"))
            continue
        if order["trade_date"] is None:  # 会话委托 → trade_date 取目标 bar 当日（供成交记录）
            order["trade_date"] = target.date()
        rt.open_orders.append({"order": order, "price_field": _price_field(order, rt.default_price_type), "target_ts": target.isoformat()})

    # 逐 bar 推进：严格 (sim_time, to]，避免跨 advance 重复处理边界 bar；
    # 首步 sim_time 为 None 时含 frame 全部 ≤ to。
    if not bars.empty:
        lower = rt.sim_time
        for timestamp, rows in bars.groupby("trade_time", sort=True):
            ts = pd.Timestamp(timestamp)
            if to_ts is not None and ts > to_ts:
                break
            if lower is not None and ts <= lower:
                continue  # 已处理过的历史 bar（含上一步的边界 bar）
            row_dicts = [r.to_dict() for _, r in rows.iterrows()]
            row_by_symbol = {str(r["symbol"]): r for r in row_dicts}
            row_date_key = int(row_dicts[0]["date"])
            if rt.current_date_key != row_date_key:  # 跨日 → T+1 解锁
                unlock_positions(rt.positions)
                rt.current_date_key = row_date_key
            for sym, r in row_by_symbol.items():
                rt.last_prices[sym] = float(r["close_qfq"])
            _match_due_at_bar(rt, rules, ts, row_by_symbol)
            rt.snapshots.append(minute_snapshot(
                strategy_id=rt.strategy_id, timestamp=ts, cash=rt.cash,
                positions=rt.positions, last_prices=rt.last_prices,
                trades=rt.trades, rejections=rt.rejections, frequency="1min",
            ))
            rt.sim_time = ts

    # 时钟推进到 to（即便中间没有 bar，如越过无行情的时段/休市）
    if to_ts is not None and (rt.sim_time is None or to_ts > rt.sim_time):
        rt.sim_time = to_ts

    # 公司行动入账：检测 (entry_sim_time, 新 sim_time] 内持仓的除权日，应用现金/送转（N8）
    corporate_actions = apply_corporate_actions(rt, dividends, lower=entry_sim_time, upper=rt.sim_time)
    return {**rt.account_context(), "corporate_actions": corporate_actions}


def finalize(rt: SessionRuntime, calendar: list[int]) -> dict[str, Any]:
    """close 收尾：日级归约 + 汇总（design/18 B2 的 reducer 尾）。"""
    daily = daily_from_minutes(rt.snapshots, rt.initial_cash, calendar)
    final_positions = position_rows(
        strategy_id=rt.strategy_id, positions=rt.positions, last_prices=rt.last_prices
    )
    market_value = sum(p["market_value"] for p in final_positions)
    total_value = rt.cash + market_value
    return {
        "strategy_id": rt.strategy_id,
        "initial_cash": round_money(rt.initial_cash),
        "cash": round_money(rt.cash),
        "market_value": round_money(market_value),
        "total_value": round_money(total_value),
        "total_return": round_ratio(total_value / rt.initial_cash - 1 if rt.initial_cash else 0.0),
        "max_drawdown": max_drawdown(daily),
        "realized_pnl": round_money(sum(float(t.get("realized_pnl", 0.0)) for t in rt.trades)),
        "positions": final_positions,
        "trades": rt.trades,
        "rejections": rt.rejections,
        "daily": daily,
    }
