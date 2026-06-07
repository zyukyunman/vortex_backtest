from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from .data_adapter import DEFAULT_WORKSPACE, TushareMinuteDataLoader, date_from_key, date_key
from .market_rules import AShareRuleEngine, FeeModel
from .models import Side
from .symbols import normalize_symbol

# 已删除未使用的 backtrader feed 死类（引擎为纯 Python 撮合，不依赖第三方回测框架；见 design/15）


@dataclass
class Position:
    quantity: int = 0
    sellable_quantity: int = 0
    cost_basis: float = 0.0


class MinuteReplayEngine:
    def __init__(
        self,
        *,
        data_loader: TushareMinuteDataLoader | None = None,
        rules: AShareRuleEngine | None = None,
    ):
        workspace = Path(os.getenv("VORTEX_DATA_WORKSPACE", str(DEFAULT_WORKSPACE)))
        self.data_loader = data_loader or TushareMinuteDataLoader(workspace)
        self.rules = rules or AShareRuleEngine()

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
        market_data_set_id: str = "tushare-workspace",
        frequency: str = "1min",
        price_adjustment: str = "qfq",
        order_price_adjustment: str = "qfq",
        default_price_type: str = "close",
        strategies: list[Mapping[str, Any]] | None = None,
        execution: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if frequency != "1min":
            raise ValueError("unsupported_frequency")
        if price_adjustment != "qfq":
            raise ValueError("unsupported_price_adjustment")
        if order_price_adjustment != "qfq":
            raise ValueError("unsupported_order_price_adjustment")

        resolved_strategies = resolve_strategies(
            account=account,
            orders=orders,
            order_batch_id=order_batch_id,
            strategies=strategies or [],
        )
        resolved_start, resolved_end = resolve_dates(
            orders=orders,
            start_date=start_date,
            end_date=end_date,
        )
        all_symbols = {
            symbol
            for strategy in resolved_strategies
            for symbol in strategy["symbols"]
        }
        if not all_symbols:
            raise ValueError("no_symbols")

        dataset = self.data_loader.load(
            symbols=all_symbols,
            start_date=resolved_start,
            end_date=resolved_end,
        )
        # 可配置费率/滑点/参与率（P6）：有 execution 就按它构造规则引擎，否则用默认 self.rules
        if execution:
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
            slippage_bps = float(cfg.get("slippage_bps", 0.0))
        else:
            rules = self.rules
            slippage_bps = 0.0
        strategy_summaries = [
            self._run_strategy(
                strategy=strategy,
                all_orders=orders,
                minutes=dataset.minutes,
                calendar=dataset.calendar,
                default_price_type=default_price_type,
                rules=rules,
                slippage_bps=slippage_bps,
            )
            for strategy in resolved_strategies
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

    def _run_strategy(
        self,
        *,
        strategy: Mapping[str, Any],
        all_orders: list[dict[str, Any]],
        minutes: pd.DataFrame,
        calendar: list[int],
        default_price_type: str,
        rules: AShareRuleEngine,
        slippage_bps: float = 0.0,
    ) -> dict[str, Any]:
        strategy_id = str(strategy["strategy_id"])
        order_batch_id = str(strategy["order_batch_id"])
        symbols = set(strategy["symbols"])
        initial_cash = float(strategy["initial_cash"])
        strategy_minutes = minutes[minutes["symbol"].isin(symbols)].copy()
        if strategy_minutes.empty:
            raise ValueError("minute_data_missing")

        orders = [
            normalize_order_row(order)
            for order in all_orders
            if order.get("order_batch_id", "default") == order_batch_id
            and normalize_symbol(order["symbol"]) in symbols
        ]
        orders_by_key: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
        for order in orders:
            price_type = order.get("price_type") or default_price_type
            orders_by_key[(order["symbol"], date_key(order["trade_date"]), price_type)].append(order)

        execution_times = execution_time_index(strategy_minutes)
        cash = initial_cash
        positions: dict[str, Position] = {}
        last_prices: dict[str, float] = {}
        trades: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        minute_snapshots: list[dict[str, Any]] = []
        current_date_key: int | None = None
        trade_counter = 0

        for timestamp, rows in strategy_minutes.groupby("trade_time", sort=True):
            timestamp = pd.Timestamp(timestamp)
            row_dicts = [row.to_dict() for _, row in rows.iterrows()]
            row_by_symbol = {str(row["symbol"]): row for row in row_dicts}
            row_date_key = int(row_dicts[0]["date"])
            if current_date_key != row_date_key:
                unlock_positions(positions)
                current_date_key = row_date_key

            for symbol, row in row_by_symbol.items():
                last_prices[symbol] = float(row["close_qfq"])
                for price_type in ("open", "close"):
                    if execution_times.get((symbol, row_date_key, price_type)) != timestamp:
                        continue
                    for order in orders_by_key.get((symbol, row_date_key, price_type), []):
                        fill_price = float(row[f"{price_type}_qfq"])
                        raw_fill_price = float(row[price_type])
                        # 滑点：买入抬价、卖出压价（撮合/估值仍用 qfq 价）。bug#1 修复——
                        # 现金充足性必须按**含滑点的成交价 exec_price** 校验，与下方 execute_order
                        # 实际扣款口径一致；否则临界买单滑点未计入 → 成交后现金被打成负数。
                        slip = slippage_bps / 1e4
                        exec_price = (
                            fill_price * (1 + slip)
                            if int(order["side"]) == int(Side.BUY)
                            else fill_price * (1 - slip)
                        )
                        position = positions.setdefault(symbol, Position())
                        reason = rules.validate_order(
                            order=order,
                            bar=row,
                            cash=cash,
                            position_quantity=position.quantity,
                            sellable_quantity=position.sellable_quantity,
                            fill_price=exec_price,
                            raw_fill_price=raw_fill_price,
                        )
                        if reason is not None:
                            rejections.append(
                                rejection_row(
                                    strategy_id=strategy_id,
                                    order=order,
                                    reason=reason,
                                )
                            )
                            continue
                        executable_quantity = rules.executable_quantity(
                            side=int(order["side"]),
                            requested_quantity=int(order["quantity"]),
                            volume=int(row["volume"]),
                            symbol=symbol,
                            board=str(row["board"]),
                            position_quantity=position.quantity,
                        )
                        if executable_quantity <= 0:
                            rejections.append(
                                rejection_row(
                                    strategy_id=strategy_id,
                                    order=order,
                                    reason="volume_cap_below_lot",
                                )
                            )
                            continue
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

            minute_snapshots.append(
                minute_snapshot(
                    strategy_id=strategy_id,
                    timestamp=timestamp,
                    cash=cash,
                    positions=positions,
                    last_prices=last_prices,
                    trades=trades,
                    rejections=rejections,
                    frequency="1min",
                )
            )

        daily = daily_from_minutes(minute_snapshots, initial_cash, calendar)
        final_positions = position_rows(
            strategy_id=strategy_id,
            positions=positions,
            last_prices=last_prices,
        )
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
            "max_drawdown": max_drawdown(daily),
            "positions": final_positions,
            "trades": trades,
            "rejections": rejections,
            "daily": daily,
        }


def resolve_strategies(
    *,
    account: Mapping[str, Any],
    orders: list[dict[str, Any]],
    order_batch_id: str,
    strategies: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not strategies:
        symbols = sorted({normalize_symbol(order["symbol"]) for order in orders})
        return [
            {
                "strategy_id": "default",
                "strategy_type": "order_replay",
                "order_batch_id": order_batch_id,
                "initial_cash": float(account["initial_cash"]),
                "symbols": symbols,
            }
        ]

    resolved: list[dict[str, Any]] = []
    for strategy in strategies:
        strategy_type = str(strategy.get("strategy_type", "order_replay"))
        if strategy_type != "order_replay":
            raise ValueError("unsupported_strategy_type")
        params = dict(strategy.get("params") or {})
        strategy_order_batch = str(params.get("order_batch_id") or order_batch_id)
        symbols = {normalize_symbol(symbol) for symbol in strategy.get("symbols") or []}
        if not symbols:
            symbols = {
                normalize_symbol(order["symbol"])
                for order in orders
                if order.get("order_batch_id", "default") == strategy_order_batch
            }
        resolved.append(
            {
                "strategy_id": str(strategy["strategy_id"]),
                "strategy_type": strategy_type,
                "order_batch_id": strategy_order_batch,
                "initial_cash": float(strategy.get("initial_cash") or account["initial_cash"]),
                "symbols": sorted(symbols),
            }
        )
    return resolved


def resolve_dates(
    *,
    orders: list[dict[str, Any]],
    start_date: date | None,
    end_date: date | None,
) -> tuple[date, date]:
    if start_date and end_date:
        return start_date, end_date
    order_dates = [date.fromisoformat(str(order["trade_date"])) for order in orders]
    if not order_dates:
        raise ValueError("start_end_required")
    return start_date or min(order_dates), end_date or max(order_dates)


def normalize_order_row(order: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": order.get("id"),
        "order_batch_id": order.get("order_batch_id", "default"),
        "request_id": str(order["request_id"]),
        "trade_date": date.fromisoformat(str(order["trade_date"])),
        "symbol": normalize_symbol(str(order["symbol"])),
        "side": int(order["side"]),
        "quantity": int(order["quantity"]),
        "price_type": order.get("price_type"),
        "limit_price": order.get("limit_price"),
    }


def execution_time_index(minutes: pd.DataFrame) -> dict[tuple[str, int, str], pd.Timestamp]:
    result: dict[tuple[str, int, str], pd.Timestamp] = {}
    for (symbol, row_date), rows in minutes.groupby(["symbol", "date"], sort=True):
        ordered = rows.sort_values("trade_time")
        result[(str(symbol), int(row_date), "open")] = pd.Timestamp(ordered.iloc[0]["trade_time"])
        result[(str(symbol), int(row_date), "close")] = pd.Timestamp(ordered.iloc[-1]["trade_time"])
    return result


def unlock_positions(positions: Mapping[str, Position]) -> None:
    for position in positions.values():
        position.sellable_quantity = position.quantity


def execute_order(
    *,
    strategy_id: str,
    trade_number: int,
    order: Mapping[str, Any],
    quantity: int,
    price: float,
    cash: float,
    position: Position,
    fee_model: FeeModel,
) -> tuple[dict[str, Any], float]:
    side = int(order["side"])
    amount = quantity * price
    costs = fee_model.costs(side=side, quantity=quantity, price=price)
    total_fee = sum(costs.values())
    if side == int(Side.BUY):
        old_value = position.quantity * position.cost_basis
        position.quantity += quantity
        position.cost_basis = (old_value + amount) / position.quantity
        cash_after = cash - amount - total_fee
    else:
        position.quantity -= quantity
        position.sellable_quantity = max(position.sellable_quantity - quantity, 0)
        cash_after = cash + amount - total_fee
        if position.quantity == 0:
            position.cost_basis = 0.0
    return (
        {
            "strategy_id": strategy_id,
            "trade_id": f"{strategy_id}-{trade_number}",
            "request_id": order["request_id"],
            "trade_date": order["trade_date"].isoformat(),
            "symbol": order["symbol"],
            "side": side,
            "side_name": "BUY" if side == int(Side.BUY) else "SELL",
            "quantity": quantity,
            "price": round_money(price),
            "amount": round_money(amount),
            "commission": round_money(costs["commission"]),
            "stamp_tax": round_money(costs["stamp_tax"]),
            "transfer_fee": round_money(costs["transfer_fee"]),
            "cash_after": round_money(cash_after),
        },
        cash_after,
    )


def rejection_row(
    *,
    strategy_id: str,
    order: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "request_id": order["request_id"],
        "trade_date": order["trade_date"].isoformat(),
        "symbol": order["symbol"],
        "side": int(order["side"]),
        "side_name": "BUY" if int(order["side"]) == int(Side.BUY) else "SELL",
        "quantity": int(order["quantity"]),
        "reason": reason,
    }


def minute_snapshot(
    *,
    strategy_id: str,
    timestamp: pd.Timestamp,
    cash: float,
    positions: Mapping[str, Position],
    last_prices: Mapping[str, float],
    trades: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
    frequency: str,
) -> dict[str, Any]:
    positions_list = position_rows(
        strategy_id=strategy_id,
        positions=positions,
        last_prices=last_prices,
    )
    ts_text = timestamp.to_pydatetime().isoformat()
    trade_date = timestamp.date().isoformat()
    snapshot_trades = [trade for trade in trades if trade["trade_date"] == trade_date]
    snapshot_rejections = [
        rejection for rejection in rejections if rejection["trade_date"] == trade_date
    ]
    return {
        "strategy_id": strategy_id,
        "timestamp": ts_text,
        "frequency": frequency,
        "cash": round_money(cash),
        "market_value": round_money(sum(item["market_value"] for item in positions_list)),
        "total_value": round_money(cash + sum(item["market_value"] for item in positions_list)),
        "positions": positions_list,
        "trades": snapshot_trades,
        "rejections": snapshot_rejections,
    }


def position_rows(
    *,
    strategy_id: str,
    positions: Mapping[str, Position],
    last_prices: Mapping[str, float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, position in sorted(positions.items()):
        if position.quantity <= 0:
            continue
        last_price = float(last_prices.get(symbol, position.cost_basis))
        market_value = position.quantity * last_price
        unrealized_pnl = (last_price - position.cost_basis) * position.quantity
        rows.append(
            {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "quantity": position.quantity,
                "available_quantity": position.sellable_quantity,
                "cost_basis": round_money(position.cost_basis),
                "last_price": round_money(last_price),
                "market_value": round_money(market_value),
                "unrealized_pnl": round_money(unrealized_pnl),
                "unrealized_pnl_ratio": round_ratio(
                    unrealized_pnl / (position.cost_basis * position.quantity)
                    if position.cost_basis and position.quantity
                    else 0.0
                ),
            }
        )
    return rows


def daily_from_minutes(
    minutes: list[dict[str, Any]],
    initial_cash: float,
    calendar: list[int] | None = None,
) -> list[dict[str, Any]]:
    """逐策略日级净值：把分钟快照归约为每个交易日的 EOD 行。

    bug#2 修复：日级轴用**完整交易日历** `calendar`（各标的交易日并集），而非"该策略当日有
    快照的日期"。策略当日无快照（标的停牌/无数据）时 **forward-fill** 上一已知现金/持仓/市值
    （首个快照前为纯现金持有 `initial_cash`），避免缺席日被当成凭空蒸发、聚合后组合净值失真。
    """
    if not minutes:
        return []
    strategy_id = str(minutes[0]["strategy_id"])
    by_date: dict[str, dict[str, Any]] = {}
    for snapshot in minutes:
        by_date[snapshot["timestamp"][:10]] = snapshot
    if calendar:
        axis = [date_from_key(key).isoformat() for key in sorted(set(calendar))]
    else:
        axis = sorted(by_date)
    if not axis:
        return []

    daily: list[dict[str, Any]] = []
    previous_total = float(initial_cash)
    high_watermark = float(initial_cash)
    # 首个快照前：纯现金持有（无持仓、市值 0）
    carry_cash = float(initial_cash)
    carry_market_value = 0.0
    carry_total = float(initial_cash)
    carry_positions: list[dict[str, Any]] = []
    for trade_date in axis:
        snapshot = by_date.get(trade_date)
        if snapshot is not None:
            carry_cash = float(snapshot["cash"])
            carry_market_value = float(snapshot["market_value"])
            carry_total = float(snapshot["total_value"])
            carry_positions = snapshot["positions"]
            day_trades = [t for t in snapshot["trades"] if t["trade_date"] == trade_date]
            day_rejections = [r for r in snapshot["rejections"] if r["trade_date"] == trade_date]
        else:
            # 缺席日：持仓/现金不变，持仓按最后价估值（forward-fill）
            day_trades = []
            day_rejections = []
        high_watermark = max(high_watermark, carry_total)
        daily.append(
            {
                "strategy_id": strategy_id,
                "trade_date": trade_date,
                "cash": round_money(carry_cash),
                "market_value": round_money(carry_market_value),
                "total_value": round_money(carry_total),
                "daily_pnl": round_money(carry_total - previous_total),
                "total_return": round_ratio(carry_total / initial_cash - 1 if initial_cash else 0.0),
                "drawdown": round_ratio(carry_total / high_watermark - 1 if high_watermark else 0.0),
                "positions": carry_positions,
                "trades": day_trades,
                "rejections": day_rejections,
            }
        )
        previous_total = carry_total
    return daily


def aggregate_summaries(
    *,
    account: Mapping[str, Any],
    job_id: str,
    order_batch_id: str,
    market_data_set_id: str,
    frequency: str,
    price_adjustment: str,
    order_price_adjustment: str,
    default_price_type: str,
    strategy_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    initial_cash = sum(float(item["initial_cash"]) for item in strategy_summaries)
    cash = sum(float(item["cash"]) for item in strategy_summaries)
    market_value = sum(float(item["market_value"]) for item in strategy_summaries)
    trades = flatten(item["trades"] for item in strategy_summaries)
    rejections = flatten(item["rejections"] for item in strategy_summaries)
    positions = flatten(item["positions"] for item in strategy_summaries)
    daily = aggregate_daily(strategy_summaries, initial_cash)
    total_value = cash + market_value
    return {
        "account_id": account["account_id"],
        "job_id": job_id,
        "order_batch_id": order_batch_id,
        "market_data_set_id": market_data_set_id,
        "frequency": frequency,
        "price_adjustment": price_adjustment,
        "order_price_adjustment": order_price_adjustment,
        "default_price_type": default_price_type,
        "cash": round_money(cash),
        "market_value": round_money(market_value),
        "total_value": round_money(total_value),
        "total_return": round_ratio(total_value / initial_cash - 1 if initial_cash else 0.0),
        "max_drawdown": max_drawdown(daily),
        "positions": positions,
        "trades": trades,
        "rejections": rejections,
        "daily": daily,
        "strategies": strategy_summaries,
        "artifacts": {},
    }


def aggregate_daily(strategy_summaries: list[dict[str, Any]], initial_cash: float) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for summary in strategy_summaries:
        for snapshot in summary["daily"]:
            grouped[snapshot["trade_date"]].append(snapshot)
    daily: list[dict[str, Any]] = []
    previous_total = initial_cash
    high_watermark = initial_cash
    for trade_date, snapshots in sorted(grouped.items()):
        cash = sum(float(item["cash"]) for item in snapshots)
        market_value = sum(float(item["market_value"]) for item in snapshots)
        total_value = cash + market_value
        high_watermark = max(high_watermark, total_value)
        daily.append(
            {
                "trade_date": trade_date,
                "cash": round_money(cash),
                "market_value": round_money(market_value),
                "total_value": round_money(total_value),
                "daily_pnl": round_money(total_value - previous_total),
                "total_return": round_ratio(total_value / initial_cash - 1 if initial_cash else 0.0),
                "drawdown": round_ratio(total_value / high_watermark - 1 if high_watermark else 0.0),
                "positions": flatten(item["positions"] for item in snapshots),
                "trades": flatten(item["trades"] for item in snapshots),
                "rejections": flatten(item["rejections"] for item in snapshots),
            }
        )
        previous_total = total_value
    return daily


def aggregate_minutes(strategy_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for summary in strategy_summaries:
        for snapshot in summary["minutes"]:
            grouped[snapshot["timestamp"]].append(snapshot)
    result: list[dict[str, Any]] = []
    for timestamp, snapshots in sorted(grouped.items()):
        cash = sum(float(item["cash"]) for item in snapshots)
        market_value = sum(float(item["market_value"]) for item in snapshots)
        result.append(
            {
                "timestamp": timestamp,
                "frequency": "1min",
                "cash": round_money(cash),
                "market_value": round_money(market_value),
                "total_value": round_money(cash + market_value),
                "positions": flatten(item["positions"] for item in snapshots),
                "trades": flatten(item["trades"] for item in snapshots),
                "rejections": flatten(item["rejections"] for item in snapshots),
            }
        )
    return result


def write_reports(report_dir: Path, summary: dict[str, Any]) -> dict[str, str]:
    artifacts = {
        "account_summary": report_dir / "account_summary.json",
        "daily_equity": report_dir / "daily_equity.csv",
        "trades": report_dir / "trades.csv",
        "positions": report_dir / "positions.csv",
        "rejections": report_dir / "rejections.csv",
    }
    write_csv(artifacts["daily_equity"], summary["daily"])
    write_csv(artifacts["trades"], summary["trades"])
    write_csv(artifacts["positions"], summary["positions"])
    write_csv(artifacts["rejections"], summary["rejections"])
    return {key: str(path) for key, path in artifacts.items()}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flat_rows = [flatten_row(row) for row in rows]
    fieldnames = sorted({key for row in flat_rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flat_rows)


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def slim_equity_rows(rows: list[dict[str, Any]], time_key: str) -> list[dict[str, Any]]:
    return [
        {
            time_key: row[time_key],
            "cash": row["cash"],
            "market_value": row["market_value"],
            "total_value": row["total_value"],
        }
        for row in rows
    ]


def flatten_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
        for key, value in row.items()
    }


def flatten(values: Iterable[Iterable[dict[str, Any]]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for items in values:
        result.extend(items)
    return result


def max_drawdown(daily: list[dict[str, Any]]) -> float:
    if not daily:
        return 0.0
    return round_ratio(min(float(item["drawdown"]) for item in daily))


def round_money(value: float) -> float:
    return round(float(value), 4)


def round_ratio(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return round(float(value), 8)

