from __future__ import annotations

from datetime import date, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .symbols import normalize_symbol


class Side(IntEnum):
    BUY = 1
    SELL = 2

    @property
    def label(self) -> str:
        return self.name


class PriceType(StrEnum):
    OPEN = "open"
    CLOSE = "close"


class PriceAdjustment(StrEnum):
    QFQ = "qfq"
    HFQ = "hfq"
    RAW = "raw"


class EngineName(StrEnum):
    BACKTRADER = "backtrader"


class Frequency(StrEnum):
    MINUTE_1 = "1min"


class StrategyCreate(BaseModel):
    strategy_id: str = Field(..., min_length=1, max_length=96)
    strategy_type: str = Field(default="order_replay", min_length=1, max_length=64)
    initial_cash: float | None = Field(default=None, gt=0)
    symbols: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: list[str]) -> list[str]:
        return [normalize_symbol(item) for item in value]


class AccountCreate(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=64)
    initial_cash: float = Field(..., gt=0)
    engine: EngineName = EngineName.BACKTRADER
    name: str | None = Field(default=None, max_length=128)


class AccountOut(BaseModel):
    account_id: str
    name: str | None = None
    initial_cash: float
    engine: EngineName
    created_at: datetime


class OrderCreate(BaseModel):
    order_batch_id: str = Field(default="default", min_length=1, max_length=96)
    request_id: str = Field(..., min_length=1, max_length=96)
    trade_date: date
    symbol: str = Field(..., min_length=1, max_length=32)
    side: Side
    quantity: int = Field(..., gt=0)
    price_type: PriceType | None = None
    limit_price: float | None = Field(default=None, gt=0)
    comment: str | None = Field(default=None, max_length=512)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return normalize_symbol(value)

    @field_validator("side", mode="before")
    @classmethod
    def side_must_be_numeric(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("side must be numeric enum: 1=BUY, 2=SELL")
        return value


class OrderOut(OrderCreate):
    id: int
    account_id: str
    side_name: str
    created_at: datetime


class ExecutionConfig(BaseModel):
    """可按回测覆盖的撮合/费用参数（P6）。缺省值等于原硬编码值，省略则行为不变。"""

    commission_rate: float = Field(default=0.0003, ge=0)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax_rate: float = Field(default=0.0005, ge=0)
    transfer_fee_rate: float = Field(default=0.00001, ge=0)
    max_volume_participation: float = Field(default=1.0, gt=0, le=1.0)
    slippage_bps: float = Field(default=0.0, ge=0)


class BacktestCreate(BaseModel):
    account_id: str
    order_batch_id: str = Field(default="default", min_length=1, max_length=96)
    market_data_set_id: str = Field(default="default-qfq", min_length=1, max_length=96)
    frequency: str = Field(default="1min", min_length=1, max_length=16)
    price_adjustment: PriceAdjustment = PriceAdjustment.QFQ
    order_price_adjustment: PriceAdjustment | None = None
    default_price_type: PriceType = PriceType.CLOSE
    start_date: date | None = None
    end_date: date | None = None
    strategies: list[StrategyCreate] = Field(default_factory=list)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)


class BacktestJobOut(BaseModel):
    job_id: str
    account_id: str
    order_batch_id: str
    market_data_set_id: str
    frequency: Frequency
    price_adjustment: PriceAdjustment
    order_price_adjustment: PriceAdjustment
    default_price_type: PriceType
    status: str
    start_date: date | None = None
    end_date: date | None = None
    created_at: datetime
    completed_at: datetime | None = None
    report_dir: Path | None = None
    summary: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


class PositionOut(BaseModel):
    strategy_id: str | None = None
    symbol: str
    quantity: int
    available_quantity: int
    cost_basis: float
    last_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_ratio: float


class TradeOut(BaseModel):
    strategy_id: str | None = None
    trade_id: str
    request_id: str
    trade_date: date
    symbol: str
    side: Side
    side_name: str
    quantity: int
    price: float
    amount: float
    commission: float
    stamp_tax: float
    transfer_fee: float
    cash_after: float


class RejectionOut(BaseModel):
    strategy_id: str | None = None
    request_id: str
    trade_date: date
    symbol: str
    side: Side
    side_name: str
    quantity: int
    reason: str


class DailySnapshotOut(BaseModel):
    strategy_id: str | None = None
    trade_date: date
    cash: float
    market_value: float
    total_value: float
    daily_pnl: float
    total_return: float
    drawdown: float
    positions: list[PositionOut]
    trades: list[TradeOut]
    rejections: list[RejectionOut]


class MinuteSnapshotOut(BaseModel):
    strategy_id: str | None = None
    timestamp: datetime
    frequency: Frequency
    cash: float
    market_value: float
    total_value: float
    positions: list[PositionOut] = Field(default_factory=list)
    trades: list[TradeOut] = Field(default_factory=list)
    rejections: list[RejectionOut] = Field(default_factory=list)


class StrategySummaryOut(BaseModel):
    strategy_id: str
    strategy_type: str
    order_batch_id: str
    initial_cash: float
    cash: float
    market_value: float
    total_value: float
    total_return: float
    max_drawdown: float
    positions: list[PositionOut]
    trades: list[TradeOut]
    rejections: list[RejectionOut]
    daily: list[DailySnapshotOut] = Field(default_factory=list)


class AccountSummaryOut(BaseModel):
    account_id: str
    job_id: str
    order_batch_id: str
    market_data_set_id: str
    frequency: Frequency
    price_adjustment: PriceAdjustment
    order_price_adjustment: PriceAdjustment
    default_price_type: PriceType
    cash: float
    market_value: float
    total_value: float
    total_return: float
    max_drawdown: float
    positions: list[PositionOut]
    trades: list[TradeOut]
    rejections: list[RejectionOut]
    daily: list[DailySnapshotOut] = Field(default_factory=list)
    strategies: list[StrategySummaryOut] = Field(default_factory=list)
    artifacts: dict[str, str]


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
