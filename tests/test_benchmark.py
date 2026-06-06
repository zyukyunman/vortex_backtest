"""B5 基准序列单测：读 index_daily 合成 parquet、对齐回测交易日并 rebase。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from vortex_backtest.benchmark import (
    align_rebase,
    benchmark_series,
    list_benchmarks,
    load_index_closes,
)


def _write_index(tmp_path: Path) -> Path:
    d = tmp_path / "index_daily"
    d.mkdir()
    pd.DataFrame(
        [
            {"symbol": "000300.SH", "date": 20260102, "close": 4000.0},
            {"symbol": "000300.SH", "date": 20260105, "close": 4040.0},
            {"symbol": "000905.SH", "date": 20260102, "close": 6000.0},
        ]
    ).to_parquet(d / "part.parquet", index=False)
    return d


def test_load_index_closes_filters_symbol_and_range(tmp_path: Path) -> None:
    d = _write_index(tmp_path)
    closes = load_index_closes("000300.SH", date(2026, 1, 1), date(2026, 1, 31), data_dir=d)
    assert closes == {"2026-01-02": 4000.0, "2026-01-05": 4040.0}


def test_align_rebase_and_forward_fill(tmp_path: Path) -> None:
    d = _write_index(tmp_path)
    closes = load_index_closes("000300.SH", data_dir=d)
    assert align_rebase(["2026-01-02", "2026-01-05"], closes) == [100.0, 101.0]
    # 中间缺失日前向填充
    filled = align_rebase(["2026-01-02", "2026-01-03", "2026-01-05"], closes)
    assert filled == [100.0, 100.0, 101.0]
    assert benchmark_series("000300.SH", ["2026-01-02", "2026-01-05"], data_dir=d) == [100.0, 101.0]


def test_missing_data_dir_is_safe() -> None:
    assert load_index_closes("000300.SH", data_dir="/nonexistent/vortex/xyz") == {}
    cat = list_benchmarks(data_dir="/nonexistent/vortex/xyz")
    assert cat["available"] is False
    assert cat["default"] == "000300.SH"
    assert any(item["symbol"] == "000300.SH" for item in cat["items"])
