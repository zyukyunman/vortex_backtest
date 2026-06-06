"""共享测试夹具（新增，纯增量，不改动生产代码）。

提供一个灵活的「分钟行情 workspace 构造器」，让各测试文件能按需拼出
任意交易日 / 任意 OHLCV / 成交量 / 涨跌停 / 停牌 / ST 的本地 Parquet 数据集，
喂给 `TushareMinuteDataLoader` 与 HTTP 层做确定性回测。

设计要点：
- 每个交易日默认放两根 bar：09:31（开盘撮合点）与 14:57（收盘撮合点），
  价格可分别指定，从而精确断言「按 open 还是 close 成交」。
- `adj_factor` 默认 1.0（raw == qfq），需要测前复权时再覆盖。
- 缺省的 calendar / instruments / stock_st / suspend_d 一律落盘，贴近真实目录结构。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pytest


def pytest_configure(config: pytest.Config) -> None:
    # 注册自定义 marker，避免 PytestUnknownMarkWarning（不依赖 pyproject 改动）
    config.addinivalue_line("markers", "slow: 需要真实 Tushare 数据/较慢的集成测试")
    config.addinivalue_line("markers", "integration: 端到端集成测试")


def _write_parquet(workspace: Path, dataset: str, rows: list[dict[str, Any]]) -> None:
    root = workspace / "data" / dataset
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(root / "data.parquet", index=False)


@dataclass
class MinuteWorkspaceBuilder:
    """链式构造分钟行情 workspace。

    用法::

        ws = (builder
              .day("2026-01-02", "000001.SZ", open=10.0, close=10.2, volume=100000)
              .day("2026-01-05", "000001.SZ", open=10.2, close=10.5, volume=100000)
              .build())
    """

    root: Path
    _minutes: list[dict[str, Any]] = field(default_factory=list)
    _adj: dict[tuple[str, int], float] = field(default_factory=dict)
    _limits: dict[tuple[str, int], tuple[float, float]] = field(default_factory=dict)
    _suspend: set[tuple[str, int]] = field(default_factory=set)
    _st: set[tuple[str, int]] = field(default_factory=set)
    _symbols: set[str] = field(default_factory=set)
    _dates: set[int] = field(default_factory=set)

    @staticmethod
    def _date_key(day: str) -> int:
        return int(day.replace("-", ""))

    def day(
        self,
        day: str,
        symbol: str,
        *,
        open: float,
        close: float,
        high: float | None = None,
        low: float | None = None,
        volume: int = 100_000,
        up_limit: float | None = None,
        down_limit: float | None = None,
        adj_factor: float = 1.0,
        suspended: bool = False,
        is_st: bool = False,
        open_time: str = "09:31:00",
        close_time: str = "14:57:00",
    ) -> "MinuteWorkspaceBuilder":
        """给某标的某交易日放一根开盘 bar + 一根收盘 bar。

        涨跌停默认按 ±10%（基于 open 的近似），可显式覆盖。
        """
        key = self._date_key(day)
        self._symbols.add(symbol)
        self._dates.add(key)
        hi = high if high is not None else max(open, close)
        lo = low if low is not None else min(open, close)
        for minute, ts, o, c in (
            (open_time, f"{day} {open_time}", open, open),
            (close_time, f"{day} {close_time}", close, close),
        ):
            self._minutes.append(
                {
                    "symbol": symbol,
                    "date": key,
                    "trade_time": ts,
                    "minute": minute,
                    "freq": "1min",
                    "open": o,
                    "high": hi,
                    "low": lo,
                    "close": c,
                    "volume": volume,
                    "amount": float(volume) * c,
                }
            )
        self._adj[(symbol, key)] = adj_factor
        self._limits[(symbol, key)] = (
            up_limit if up_limit is not None else round(open * 1.1, 2),
            down_limit if down_limit is not None else round(open * 0.9, 2),
        )
        if suspended:
            self._suspend.add((symbol, key))
        if is_st:
            self._st.add((symbol, key))
        return self

    def adj(self, symbol: str, day: str, factor: float) -> "MinuteWorkspaceBuilder":
        self._adj[(symbol, self._date_key(day))] = factor
        return self

    def build(self) -> Path:
        _write_parquet(self.root, "stk_mins", self._minutes)
        _write_parquet(
            self.root,
            "adj_factor",
            [
                {"symbol": sym, "date": dk, "adj_factor": factor}
                for (sym, dk), factor in self._adj.items()
            ],
        )
        _write_parquet(
            self.root,
            "stk_limit",
            [
                {"symbol": sym, "date": dk, "up_limit": up, "down_limit": down}
                for (sym, dk), (up, down) in self._limits.items()
            ],
        )
        _write_parquet(
            self.root,
            "calendar",
            [{"cal_date": str(dk)} for dk in sorted(self._dates)],
        )
        _write_parquet(
            self.root,
            "instruments",
            [
                {
                    "symbol": sym,
                    "name": "fixture",
                    "list_date": "20000101",
                    "delist_date": None,
                    "industry": "test",
                    "market_cap": 100.0,
                }
                for sym in sorted(self._symbols)
            ],
        )
        _write_parquet(
            self.root,
            "suspend_d",
            [
                {"symbol": sym, "date": dk, "suspend_type": "S"}
                for (sym, dk) in sorted(self._suspend)
            ],
        )
        _write_parquet(
            self.root,
            "stock_st",
            [{"symbol": sym, "date": dk} for (sym, dk) in sorted(self._st)],
        )
        return self.root


@pytest.fixture
def workspace_builder(tmp_path: Path) -> MinuteWorkspaceBuilder:
    """返回一个空的构造器；测试自行 .day(...).build()。"""
    return MinuteWorkspaceBuilder(root=tmp_path / "workspace")
