from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .models import Side
from .symbols import market_board


@dataclass(frozen=True)
class FeeModel:
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001

    def costs(self, *, side: int, quantity: int, price: float) -> dict[str, float]:
        amount = quantity * price
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp_tax = amount * self.stamp_tax_rate if side == int(Side.SELL) else 0.0
        transfer_fee = amount * self.transfer_fee_rate
        return {
            "commission": round(commission, 4),
            "stamp_tax": round(stamp_tax, 4),
            "transfer_fee": round(transfer_fee, 4),
        }


class AShareRuleEngine:
    def __init__(
        self,
        *,
        fee_model: FeeModel | None = None,
        max_volume_participation: float = 1.0,
    ):
        self.fee_model = fee_model or FeeModel()
        self.max_volume_participation = max_volume_participation

    def validate_order(
        self,
        *,
        order: Mapping[str, Any],
        bar: Mapping[str, Any],
        cash: float,
        position_quantity: int,
        sellable_quantity: int,
        fill_price: float,
        raw_fill_price: float | None = None,
    ) -> str | None:
        side = int(order["side"])
        quantity = int(order["quantity"])
        symbol = str(order.get("symbol") or bar["symbol"])
        board = str(bar.get("board") or market_board(symbol))
        volume = int(float(bar.get("volume") or 0))
        # 修复 C1/口径：tick、用户 limit_price、涨跌停等「挂单合法性」一律对【真实价 raw】判定；
        # 撮合与估值仍用 qfq（fill_price）。raw_fill_price 缺省回退 fill_price，兼容旧调用与 adj=1 数据。
        raw_price = float(raw_fill_price if raw_fill_price is not None else fill_price)

        if bool(bar.get("suspended")):
            return "suspended"
        if volume <= 0:
            return "zero_volume"
        if not is_tick_aligned(raw_price):
            return "invalid_price_tick"
        if order.get("limit_price") is not None and not is_tick_aligned(float(order["limit_price"])):
            return "invalid_price_tick"
        if not self._valid_lot(symbol, board, side, quantity, position_quantity):
            return "invalid_lot_size"

        limit_price = order.get("limit_price")
        if limit_price is not None:
            limit_price = float(limit_price)
            if side == int(Side.BUY) and raw_price > limit_price:
                return "limit_price_not_marketable"
            if side == int(Side.SELL) and raw_price < limit_price:
                return "limit_price_not_marketable"

        up_limit = float(bar.get("up_limit", bar.get("limit_up_qfq")))
        down_limit = float(bar.get("down_limit", bar.get("limit_down_qfq")))
        if side == int(Side.BUY) and raw_price >= up_limit - 1e-8:
            return "limit_up_buy_blocked"
        if side == int(Side.SELL) and raw_price <= down_limit + 1e-8:
            return "limit_down_sell_blocked"

        if side == int(Side.BUY):
            executable_quantity = self.executable_quantity(
                side=side,
                requested_quantity=quantity,
                volume=volume,
                symbol=symbol,
                board=board,
                position_quantity=position_quantity,
            )
            if executable_quantity <= 0:
                return "volume_cap_below_lot"
            total_cost = executable_quantity * fill_price + sum(
                self.fee_model.costs(
                    side=side,
                    quantity=executable_quantity,
                    price=fill_price,
                ).values()
            )
            if total_cost > cash + 1e-8:
                return "insufficient_cash"
            return None

        if quantity > position_quantity:
            return "insufficient_position"
        if quantity > sellable_quantity:
            return "t_plus_1_not_sellable"
        executable_quantity = self.executable_quantity(
            side=side,
            requested_quantity=quantity,
            volume=volume,
            symbol=symbol,
            board=board,
            position_quantity=position_quantity,
        )
        if executable_quantity <= 0:
            return "volume_cap_below_lot"
        return None

    def executable_quantity(
        self,
        *,
        side: int,
        requested_quantity: int,
        volume: int,
        symbol: str,
        board: str,
        position_quantity: int,
    ) -> int:
        cap = int(math.floor(volume * self.max_volume_participation))
        quantity = min(int(requested_quantity), max(cap, 0))
        if side == int(Side.BUY):
            return round_down_buy_lot(quantity, board)
        return round_down_sell_lot(quantity, position_quantity)

    def _valid_lot(
        self,
        symbol: str,
        board: str,
        side: int,
        quantity: int,
        position_quantity: int,
    ) -> bool:
        if quantity <= 0:
            return False
        if side == int(Side.BUY):
            if board == "star":
                return quantity >= 200
            return quantity >= 100 and quantity % 100 == 0
        if quantity % 100 == 0:
            return True
        return quantity == position_quantity and position_quantity % 100 != 0


def is_tick_aligned(price: float) -> bool:
    cents = round(price * 100)
    return abs(price * 100 - cents) < 1e-8


def round_down_buy_lot(quantity: int, board: str) -> int:
    if board == "star":
        return quantity if quantity >= 200 else 0
    return quantity // 100 * 100


def round_down_sell_lot(quantity: int, position_quantity: int) -> int:
    if quantity == position_quantity and position_quantity % 100 != 0:
        return quantity
    return quantity // 100 * 100
