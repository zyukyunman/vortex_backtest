"""基准序列（B5，design/13 §6.2）。

从 vortex_data 的 `index_daily` 读指数日收盘，按回测交易日对齐并 rebase。
数据源目录由 `$VORTEX_INDEX_DATA_DIR` 指定（指向 `.../workspace/data/index_daily`）。
用 pyarrow.dataset 读 parquet（兼容 hive 分区）；`date` 列容忍 int(YYYYMMDD) 或字符串。

服务"直接读盘"（项目决策）：基准走 index_daily，与选股的 qlib provider 解耦。
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Sequence

# 可对标指数目录（design/13 §6.2，已确认 index_daily 覆盖）
BENCHMARKS: list[dict[str, str]] = [
    {"symbol": "000300.SH", "name": "沪深300"},
    {"symbol": "000905.SH", "name": "中证500"},
    {"symbol": "000016.SH", "name": "上证50"},
    {"symbol": "399006.SZ", "name": "创业板指"},
]
DEFAULT_BENCHMARK = "000300.SH"


def index_data_dir() -> Path | None:
    value = os.getenv("VORTEX_INDEX_DATA_DIR")
    return Path(value).expanduser() if value else None


def benchmark_available(data_dir: str | Path | None = None) -> bool:
    base = Path(data_dir) if data_dir else index_data_dir()
    return bool(base and Path(base).exists())


def list_benchmarks(data_dir: str | Path | None = None) -> dict:
    return {"available": benchmark_available(data_dir), "default": DEFAULT_BENCHMARK, "items": list(BENCHMARKS)}


def _to_ymd(value: object) -> str | None:
    """int/str 日期 → 'YYYYMMDD'（8 位）。"""
    if value is None:
        return None
    s = str(value).strip().replace("-", "")
    if len(s) >= 8 and s[:8].isdigit():
        return s[:8]
    return None


def _ymd_to_iso(ymd: str) -> str:
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"


def _date_to_ymd(value: date | str) -> str:
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    return str(value).replace("-", "")[:8]


def load_index_closes(
    symbol: str,
    start: date | str | None = None,
    end: date | str | None = None,
    *,
    data_dir: str | Path | None = None,
) -> dict[str, float]:
    """返回 {ISO 日期: 收盘}。无数据/目录则返回 {}。"""
    base = Path(data_dir) if data_dir else index_data_dir()
    if base is None or not Path(base).exists():
        return {}
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    dataset = ds.dataset(str(base), format="parquet", partitioning="hive")
    try:
        table = dataset.to_table(columns=["symbol", "date", "close"], filter=pc.field("symbol") == symbol)
    except Exception:  # noqa: BLE001 - 退化为全量后内存过滤
        table = dataset.to_table(columns=["symbol", "date", "close"])
    syms = table.column("symbol").to_pylist()
    dates = table.column("date").to_pylist()
    closes = table.column("close").to_pylist()

    lo = _date_to_ymd(start) if start else None
    hi = _date_to_ymd(end) if end else None
    out: dict[str, float] = {}
    for sym, d, c in zip(syms, dates, closes):
        if sym != symbol or c is None:
            continue
        ymd = _to_ymd(d)
        if ymd is None:
            continue
        if lo and ymd < lo:
            continue
        if hi and ymd > hi:
            continue
        out[_ymd_to_iso(ymd)] = float(c)
    return out


def align_rebase(
    iso_dates: Sequence[str], closes: dict[str, float], base: float = 100.0
) -> list[float] | None:
    """把 `closes` 对齐到回测交易日 `iso_dates`（前向填充），再 rebase 到 `base`。
    无任何交集则返回 None。"""
    if not iso_dates or not closes:
        return None
    # 起点之前若缺，用首个可得收盘回填，保证序列从 base 开始
    first_close: float | None = None
    for d in iso_dates:
        if d in closes:
            first_close = closes[d]
            break
    if first_close is None:
        return None
    aligned: list[float] = []
    last = first_close
    for d in iso_dates:
        if d in closes:
            last = closes[d]
        aligned.append(last)
    anchor = aligned[0]
    if not anchor:
        return None
    return [round(v / anchor * base, 6) for v in aligned]


def benchmark_series(
    symbol: str,
    iso_dates: Sequence[str],
    *,
    base: float = 100.0,
    data_dir: str | Path | None = None,
) -> list[float] | None:
    """便捷：读指数收盘并对齐 rebase 到 `iso_dates`。"""
    if not iso_dates:
        return None
    closes = load_index_closes(symbol, iso_dates[0], iso_dates[-1], data_dir=data_dir)
    return align_rebase(iso_dates, closes, base=base)
