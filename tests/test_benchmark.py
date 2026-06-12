"""benchmark：fixture parquet 验证代码裁剪/窗口/名称/缺数。"""
import pandas as pd
import pytest

from vortex_backtest import benchmark


@pytest.fixture
def ws(tmp_path):
    idx = tmp_path / "data" / "index_daily" / "date=20260203"
    idx.mkdir(parents=True)
    pd.DataFrame({
        "symbol": ["000300.SH", "000300.SH", "000905.SH"],
        "date": ["20260203", "20260204", "20260203"],
        "close": [4100.0, 4150.0, 6000.0],
    }).to_parquet(idx / "data.parquet")
    sw = tmp_path / "data" / "sw_daily" / "date=20260203"
    sw.mkdir(parents=True)
    pd.DataFrame({
        "symbol": ["801120.SI"], "date": ["20260203"], "name": ["食品饮料"], "close": [8000.0],
    }).to_parquet(sw / "data.parquet")
    return tmp_path


def test_load_series_window_and_name(ws):
    series, name = benchmark.load_series("000300.SH", 20260203, 20260203, workspace=ws)
    assert series == {"2026-02-03": 4100.0}        # 窗口裁掉 0204
    assert name == "沪深300"                        # 常用指数名映射
    sw_series, sw_name = benchmark.load_series("801120.SI", 20260101, 20261231, workspace=ws)
    assert sw_series == {"2026-02-03": 8000.0} and sw_name == "食品饮料"   # sw_daily 带 name 列


def test_load_series_missing_code(ws):
    series, name = benchmark.load_series("999999.XX", 20260101, 20261231, workspace=ws)
    assert series == {} and name == "999999.XX"


def test_load_series_code_exists_but_window_empty(ws):
    series, name = benchmark.load_series("000300.SH", 20300101, 20301231, workspace=ws)
    assert series == {} and name == "沪深300"      # 代码存在但窗口外：空序列 + 静态名


def test_list_benchmarks(ws):
    items = benchmark.list_benchmarks(workspace=ws)
    codes = {i["code"]: i for i in items}
    assert codes["000300.SH"]["name"] == "沪深300" and codes["000300.SH"]["source"] == "index_daily"
    assert codes["801120.SI"]["name"] == "食品饮料" and codes["801120.SI"]["source"] == "sw_daily"
    assert "999999.XX" not in codes
