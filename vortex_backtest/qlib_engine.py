"""Qlib 后端回放引擎（P2/P3，ADR-1）。

把 Qlib 当**数据层**：`qlib.init` 读 vortex_data 导出的 FileStorage，`D.features` 取
分钟级 `$open/$close/$factor/$limit_up/$limit_down/$volume/$paused`；撮合与 A 股规则
**复用 `market_rules`**（T+1、手数、涨跌停、费用、滑点），产出与自研引擎**同款的日级
summary**，从而无缝插回现有异步作业 / 日级报告 / CLI。

口径（与自研引擎一致）：
- vortex_data 导出的 `$close` 是 **raw**，`$factor` 已归一化到该标的全历史最新=1（即 qfq 乘子）。
- 成交/估值用 **qfq = close*factor**；tick / 用户 limit_price / 涨跌停等合法性对 **raw** 价判定。
- 回测**统一分钟级**：读 1min FileStorage，引擎把分钟 bar 归约为当日会话 bar 后回放
  （订单是日级 trade_date + open/close；分钟是日线超集，A 股下单总在交易时段内）。
  不再支持日级回放；非 1min 频率直接拒（`unsupported_frequency`）。

依赖说明：
- `qlib` **延迟导入**（仅在 run() 内），因此不装 qlib 也能 import 本模块、跑单测；
  本机 macOS arm64 装不上 pyqlib，本引擎在 **linux/amd64 镜像**里跑（见 design/12）。
- provider 路径取 `$VORTEX_QLIB_PROVIDER_URI`（默认 `/qlib`）。
"""
from __future__ import annotations

import math
import os
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .backtrader_adapter import (
    Position,
    aggregate_summaries,
    execute_order,
    normalize_order_row,
    position_rows,
    rejection_row,
    resolve_dates,
    resolve_strategies,
    round_money,
    round_ratio,
    write_json,
    write_reports,
)
from .market_rules import AShareRuleEngine, FeeModel
from .models import Side
from .symbols import market_board, normalize_symbol

_QLIB_FIELDS = [
    "$open", "$high", "$low", "$close", "$volume",
    "$factor", "$change", "$limit_up", "$limit_down", "$paused",
]


def to_qlib_code(symbol: str) -> str:
    """`600000.SH` → `SH600000`。"""
    s = str(symbol).strip().upper()
    code, _, exchange = s.partition(".")
    return f"{exchange}{code}" if exchange else s


def from_qlib_code(code: str) -> str:
    """`SH600000` → `600000.SH`。"""
    c = str(code).strip().upper()
    for exchange in ("SH", "SZ", "BJ"):
        if c.startswith(exchange):
            return f"{c[len(exchange):]}.{exchange}"
    return c


def _f(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return f


def _round2(value: float) -> float:
    """qlib 以 float32 存价（如 11.8199996…）；A 股价在 0.01 网格上，回到 2 位小数，
    既恢复真实价、又让 tick 校验（0.01 对齐）通过。"""
    return value if math.isnan(value) else round(value, 2)


class QlibReplayEngine:
    def __init__(self, *, provider_uri: str | None = None, rules: AShareRuleEngine | None = None):
        self.provider_uri = provider_uri or os.getenv("VORTEX_QLIB_PROVIDER_URI", "/qlib")
        self.rules = rules or AShareRuleEngine()
        self._inited = False

    def _init_qlib(self) -> None:
        if self._inited:
            return
        import qlib
        from qlib.constant import REG_CN

        qlib.init(
            provider_uri=self.provider_uri,
            region=REG_CN,
            expression_cache=None,
            dataset_cache=None,
        )
        self._inited = True

    def run(
        self,
        *,
        job_id: str,
        account: Mapping[str, Any],
        orders: list[dict[str, Any]],
        report_dir: Path,
        start_date: date | None,
        end_date: date | None,
        order_batch_id: str = "default",
        market_data_set_id: str = "qlib-export",
        frequency: str = "1min",
        price_adjustment: str = "qfq",
        order_price_adjustment: str = "qfq",
        default_price_type: str = "close",
        strategies: list[Mapping[str, Any]] | None = None,
        execution: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        # 回测统一分钟级：A 股下单总在交易时段内，分钟是日线超集；不再支持日级回放。
        if str(frequency).lower() not in {"1min", "min", "minute"}:
            raise ValueError("unsupported_frequency")
        self._init_qlib()
        import pandas as pd
        from qlib.data import D

        resolved = resolve_strategies(
            account=account, orders=orders, order_batch_id=order_batch_id, strategies=strategies or []
        )
        resolved_start, resolved_end = resolve_dates(orders=orders, start_date=start_date, end_date=end_date)
        all_symbols = {symbol for strategy in resolved for symbol in strategy["symbols"]}
        if not all_symbols:
            raise ValueError("no_symbols")

        codes = sorted(to_qlib_code(symbol) for symbol in all_symbols)
        # 分钟频 end_time 必须顶到当日 23:59:59，否则 qlib 把裸日期当 00:00:00，
        # 会丢掉最后一个交易日的全部盘中分钟（09:30–15:00 均晚于 00:00）。
        frame = D.features(
            codes,
            _QLIB_FIELDS,
            start_time=f"{resolved_start.isoformat()} 00:00:00",
            end_time=f"{resolved_end.isoformat()} 23:59:59",
            freq="1min",
        )
        if frame is None or frame.empty:
            raise ValueError("minute_data_missing")
        # 订单是日级（trade_date + open/close），把分钟 bar 归约为当日会话 bar：
        # open=首分钟 / close=末分钟 / high·low=日内极值 / volume=日内累加（量能上限按全日量），
        # 日级广播字段（factor/涨跌停/paused）整日为常数 → 取末值/极值。
        frame = _aggregate_minute_to_daily(frame, pd)
        bars = _bars_by_symbol_date(frame, pd)
        if not bars:
            raise ValueError("minute_data_missing")
        trading_days = sorted({day for (_symbol, day) in bars})

        rules, slippage_bps = self._resolve_execution(execution)

        strategy_summaries = [
            self._run_strategy(
                strategy=strategy,
                all_orders=orders,
                bars=bars,
                trading_days=trading_days,
                default_price_type=default_price_type,
                rules=rules,
                slippage_bps=slippage_bps,
            )
            for strategy in resolved
        ]
        summary = aggregate_summaries(
            account=account,
            job_id=job_id,
            order_batch_id=order_batch_id,
            market_data_set_id=market_data_set_id,
            frequency=frequency,
            price_adjustment=price_adjustment,
            order_price_adjustment=order_price_adjustment,
            default_price_type=default_price_type,
            strategy_summaries=strategy_summaries,
        )
        report_dir.mkdir(parents=True, exist_ok=True)
        summary["artifacts"] = write_reports(report_dir, summary)
        write_json(Path(summary["artifacts"]["account_summary"]), summary)
        return summary

    def _resolve_execution(self, execution: Mapping[str, Any] | None) -> tuple[AShareRuleEngine, float]:
        if not execution:
            return self.rules, 0.0
        cfg = dict(execution)
        rules = AShareRuleEngine(
            fee_model=FeeModel(
                commission_rate=float(cfg.get("commission_rate", 0.0003)),
                min_commission=float(cfg.get("min_commission", 5.0)),
                stamp_tax_rate=float(cfg.get("stamp_tax_rate", 0.0005)),
                transfer_fee_rate=float(cfg.get("transfer_fee_rate", 0.00001)),
            ),
            max_volume_participation=float(cfg.get("max_volume_participation", 1.0)),
        )
        return rules, float(cfg.get("slippage_bps", 0.0))

    def _run_strategy(
        self,
        *,
        strategy: Mapping[str, Any],
        all_orders: list[dict[str, Any]],
        bars: dict[tuple[str, int], dict[str, Any]],
        trading_days: list[int],
        default_price_type: str,
        rules: AShareRuleEngine,
        slippage_bps: float,
    ) -> dict[str, Any]:
        strategy_id = str(strategy["strategy_id"])
        order_batch_id = str(strategy["order_batch_id"])
        symbols = set(strategy["symbols"])
        initial_cash = float(strategy["initial_cash"])

        orders = [
            normalize_order_row(order)
            for order in all_orders
            if order.get("order_batch_id", "default") == order_batch_id
            and normalize_symbol(order["symbol"]) in symbols
        ]
        orders_by_day: dict[int, list[dict[str, Any]]] = {}
        for order in orders:
            orders_by_day.setdefault(int(order["trade_date"].strftime("%Y%m%d")), []).append(order)

        cash = initial_cash
        positions: dict[str, Position] = {}
        last_prices: dict[str, float] = {}
        trades: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        daily: list[dict[str, Any]] = []
        trade_counter = 0
        previous_total = initial_cash
        high_watermark = initial_cash

        for day in trading_days:
            # T+1：进入新交易日，前一日及更早买入的量解锁为可卖
            for position in positions.values():
                position.sellable_quantity = position.quantity
            # 更新当日可见标的的最新价（qfq）
            for symbol in symbols:
                bar = bars.get((symbol, day))
                if bar is not None and not math.isnan(bar["close"]):
                    last_prices[symbol] = bar["close"] * bar["factor"]

            for order in orders_by_day.get(day, []):
                symbol = order["symbol"]
                bar = bars.get((symbol, day))
                if bar is None:
                    rejections.append(rejection_row(strategy_id=strategy_id, order=order, reason="no_market_data"))
                    continue
                price_type = order.get("price_type") or default_price_type
                raw_fill_price = _f(bar.get(price_type, bar.get("close")))
                if math.isnan(raw_fill_price):
                    rejections.append(rejection_row(strategy_id=strategy_id, order=order, reason="no_market_data"))
                    continue
                fill_price = raw_fill_price * bar["factor"]
                position = positions.setdefault(symbol, Position())
                reason = rules.validate_order(
                    order=order,
                    bar=bar,
                    cash=cash,
                    position_quantity=position.quantity,
                    sellable_quantity=position.sellable_quantity,
                    fill_price=fill_price,
                    raw_fill_price=raw_fill_price,
                )
                if reason is not None:
                    rejections.append(rejection_row(strategy_id=strategy_id, order=order, reason=reason))
                    continue
                executable_quantity = rules.executable_quantity(
                    side=int(order["side"]),
                    requested_quantity=int(order["quantity"]),
                    volume=int(bar["volume"]),
                    symbol=symbol,
                    board=str(bar["board"]),
                    position_quantity=position.quantity,
                )
                if executable_quantity <= 0:
                    rejections.append(rejection_row(strategy_id=strategy_id, order=order, reason="volume_cap_below_lot"))
                    continue
                slip = slippage_bps / 1e4
                exec_price = fill_price * (1 + slip) if int(order["side"]) == int(Side.BUY) else fill_price * (1 - slip)
                trade_counter += 1
                trade, cash = execute_order(
                    strategy_id=strategy_id,
                    trade_number=trade_counter,
                    order=order,
                    quantity=executable_quantity,
                    price=exec_price,
                    cash=cash,
                    position=position,
                    fee_model=rules.fee_model,
                )
                trades.append(trade)
                if position.quantity == 0:
                    positions.pop(symbol, None)

            # 当日日终快照
            day_positions = position_rows(strategy_id=strategy_id, positions=positions, last_prices=last_prices)
            market_value = sum(item["market_value"] for item in day_positions)
            total_value = cash + market_value
            high_watermark = max(high_watermark, total_value)
            trade_date = f"{str(day)[:4]}-{str(day)[4:6]}-{str(day)[6:]}"
            daily.append(
                {
                    "strategy_id": strategy_id,
                    "trade_date": trade_date,
                    "cash": round_money(cash),
                    "market_value": round_money(market_value),
                    "total_value": round_money(total_value),
                    "daily_pnl": round_money(total_value - previous_total),
                    "total_return": round_ratio(total_value / initial_cash - 1 if initial_cash else 0.0),
                    "drawdown": round_ratio(total_value / high_watermark - 1 if high_watermark else 0.0),
                    "positions": day_positions,
                    "trades": [t for t in trades if t["trade_date"] == trade_date],
                    "rejections": [r for r in rejections if r["trade_date"] == trade_date],
                }
            )
            previous_total = total_value

        final_positions = position_rows(strategy_id=strategy_id, positions=positions, last_prices=last_prices)
        market_value = sum(item["market_value"] for item in final_positions)
        total_value = cash + market_value
        return {
            "strategy_id": strategy_id,
            "strategy_type": str(strategy["strategy_type"]),
            "order_batch_id": order_batch_id,
            "initial_cash": round_money(initial_cash),
            "cash": round_money(cash),
            "market_value": round_money(market_value),
            "total_value": round_money(total_value),
            "total_return": round_ratio(total_value / initial_cash - 1 if initial_cash else 0.0),
            "max_drawdown": round_ratio(min((float(d["drawdown"]) for d in daily), default=0.0)),
            "positions": final_positions,
            "trades": trades,
            "rejections": rejections,
            "daily": daily,
        }


def _aggregate_minute_to_daily(frame: Any, pd: Any) -> Any:
    """分钟 D.features（MultiIndex[instrument,datetime]）→ 日级会话 bar（同结构同列）。

    open=当日首分钟、close=末分钟、high/low=日内极值、volume=日内累加；日级广播字段
    （factor/change/limit_up/limit_down/paused）整日为常数，取末值/极值。`first`/`last`
    默认跳过 NaN，故尾部空 bar 不会把 close 带成 NaN。归约后交给 `_bars_by_symbol_date`，
    与日频走完全相同的建 bar / 回放路径。
    """
    df = frame.sort_index()
    instruments = df.index.get_level_values(0)
    days = pd.to_datetime(df.index.get_level_values(1)).normalize()
    agg_map = {
        "$open": "first", "$high": "max", "$low": "min", "$close": "last",
        "$volume": "sum", "$factor": "last", "$change": "last",
        "$limit_up": "last", "$limit_down": "last", "$paused": "max",
    }
    present = {col: how for col, how in agg_map.items() if col in df.columns}
    daily = df.groupby([instruments, days]).agg(present)
    daily.index = daily.index.set_names(["instrument", "datetime"])
    return daily


def _bars_by_symbol_date(frame: Any, pd: Any) -> dict[tuple[str, int], dict[str, Any]]:
    """qlib D.features 结果（MultiIndex[instrument,datetime]）→ {(symbol, yyyymmdd): bar}。"""
    renamed = frame.rename(columns=lambda c: str(c).lstrip("$"))
    bars: dict[tuple[str, int], dict[str, Any]] = {}
    for (instrument, dt), row in renamed.iterrows():
        symbol = from_qlib_code(str(instrument))
        day = int(pd.Timestamp(dt).strftime("%Y%m%d"))
        close_raw = _round2(_f(row.get("close")))
        factor = _f(row.get("factor"))
        if math.isnan(factor) or factor == 0.0:
            factor = 1.0
        up_limit = _round2(_f(row.get("limit_up")))
        down_limit = _round2(_f(row.get("limit_down")))
        paused = _f(row.get("paused"))
        bars[(symbol, day)] = {
            "symbol": symbol,
            "date": day,
            "board": market_board(symbol),
            "open": _round2(_f(row.get("open"))),
            "close": close_raw,
            "volume": int(_f(row.get("volume")) or 0) if not math.isnan(_f(row.get("volume"))) else 0,
            "factor": factor,
            # 涨跌停缺失时给极端值，等价于不限制（避免 NaN 比较）
            "up_limit": up_limit if not math.isnan(up_limit) else float("inf"),
            "down_limit": down_limit if not math.isnan(down_limit) else float("-inf"),
            # qfq 列（供需要时使用）
            "limit_up_qfq": (up_limit * factor) if not math.isnan(up_limit) else float("inf"),
            "limit_down_qfq": (down_limit * factor) if not math.isnan(down_limit) else float("-inf"),
            "suspended": bool(paused) if not math.isnan(paused) else math.isnan(close_raw),
        }
    return bars
