from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Side(IntEnum):
    BUY = 1
    SELL = 2

    @property
    def label(self) -> str:
        return self.name


class EngineName(StrEnum):
    # 自研 A 股分钟撮合引擎（前身误名 backtrader，已于 2026-06-07 正名；见 design/15）
    REPLAY = "replay"


class AccountCreate(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=64)
    initial_cash: float = Field(..., gt=0)
    engine: EngineName = EngineName.REPLAY
    name: str | None = Field(default=None, max_length=128)

    @field_validator("engine", mode="before")
    @classmethod
    def _coerce_legacy_engine(cls, value: Any) -> Any:
        # 兼容历史/旧客户端的引擎值（backtrader/qlib/rqalpha/ashare_replay）→ 统一 replay
        if isinstance(value, str) and value in {"backtrader", "qlib", "rqalpha", "ashare_replay"}:
            return "replay"
        return value


class AccountOut(BaseModel):
    account_id: str
    name: str | None = None
    initial_cash: float
    engine: EngineName
    created_at: datetime


class SymbolCrosswalkOut(BaseModel):
    symbol: str
    code: str
    exchange: str
    board: str
    tushare: str
    miniqmt: str
    vortex: str
    buy_min_shares: int
    sell_min_shares: int
