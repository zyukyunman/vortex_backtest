from __future__ import annotations

import re
from enum import StrEnum


CANONICAL_RE = re.compile(r"^(?P<code>\d{6})\.(?P<exchange>SZ|SH|BJ)$")


class SymbolFormat(StrEnum):
    VORTEX = "vortex"
    TUSHARE = "tushare"
    MINIQMT = "miniqmt"


def normalize_symbol(value: str) -> str:
    text = value.strip().upper()
    canonical = CANONICAL_RE.match(text)
    if canonical:
        return text

    raise ValueError(
        "symbol must use Vortex/Tushare/MiniQMT format like 000001.SZ"
    )


def split_symbol(symbol: str) -> tuple[str, str]:
    normalized = normalize_symbol(symbol)
    code, exchange = normalized.split(".", 1)
    return code, exchange


def market_board(symbol: str) -> str:
    code, exchange = split_symbol(symbol)
    if exchange == "BJ":
        return "bse"
    if exchange == "SH" and code.startswith("688"):
        return "star"
    if exchange == "SZ" and code.startswith(("300", "301")):
        return "chinext"
    return "main"


def min_order_shares(symbol: str, side_value: int) -> int:
    if side_value == 1 and market_board(symbol) == "star":
        return 200
    return 100


def is_valid_order_shares(symbol: str, side_value: int, quantity: int) -> bool:
    return quantity > 0 and quantity % 100 == 0 and quantity >= min_order_shares(symbol, side_value)


def crosswalk(symbol: str) -> dict[str, object]:
    normalized = normalize_symbol(symbol)
    code, exchange = split_symbol(normalized)
    return {
        "symbol": normalized,
        "code": code,
        "exchange": exchange,
        "board": market_board(normalized),
        "tushare": normalized,
        "miniqmt": normalized,
        "vortex": normalized,
        "buy_min_shares": min_order_shares(normalized, 1),
        "sell_min_shares": min_order_shares(normalized, 2),
    }
