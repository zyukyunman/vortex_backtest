"""基准序列直读（spec 2026-06-12 §3/§4.5）：workspace `index_daily`/`sw_daily` 收盘序列与目录。

与 data_adapter 同款本地直读模式（pyarrow via pandas），只读不写。
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from .data_adapter import DEFAULT_WORKSPACE

_SOURCES = ("index_daily", "sw_daily")
# index_daily 无名称列 → 常用指数静态名映射（找不到回代码本身）
_COMMON_INDEX_NAMES = {
    "000300.SH": "沪深300", "000001.SH": "上证指数", "000905.SH": "中证500",
    "000016.SH": "上证50", "000852.SH": "中证1000", "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
}


def _workspace(workspace: Path | None) -> Path:
    return Path(workspace) if workspace else Path(os.getenv("VORTEX_WORKSPACE", str(DEFAULT_WORKSPACE)))


def _read(dataset: str, workspace: Path | None) -> pd.DataFrame:
    root = _workspace(workspace) / "data" / dataset
    files = sorted(root.rglob("*.parquet")) if root.exists() else []
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def load_series(code: str, start_key: int, end_key: int, *,
                workspace: Path | None = None) -> tuple[dict[str, float], str]:
    """→ ({iso_date: close}, 名称)。两源依次找；都无该代码 → ({}, 静态名或代码)。"""
    for ds in _SOURCES:
        df = _read(ds, workspace)
        if df.empty or not {"symbol", "date", "close"}.issubset(df.columns):
            continue
        df = df[df["symbol"] == code].copy()
        if df.empty:
            continue
        df["_d"] = pd.to_numeric(df["date"].astype(str).str.replace("-", "", regex=False).str[:8],
                                 errors="coerce")
        df = df.dropna(subset=["_d"]).astype({"_d": int})
        df = df[(df["_d"] >= start_key) & (df["_d"] <= end_key)].sort_values("_d")
        series = {f"{str(k)[:4]}-{str(k)[4:6]}-{str(k)[6:8]}": float(c)
                  for k, c in zip(df["_d"], df["close"])}
        name = (str(df["name"].iloc[0]) if "name" in df.columns and len(df)
                else _COMMON_INDEX_NAMES.get(code, code))
        return series, name
    return {}, _COMMON_INDEX_NAMES.get(code, code)


def list_benchmarks(*, workspace: Path | None = None) -> list[dict[str, str]]:
    """基准目录：index_daily 中存在的常用指数（静态名）+ sw_daily 全量（自带名称列）。"""
    out: list[dict[str, str]] = []
    idx = _read("index_daily", workspace)
    have = set(idx["symbol"].unique()) if not idx.empty and "symbol" in idx.columns else set()
    for code, name in _COMMON_INDEX_NAMES.items():
        if code in have:
            out.append({"code": code, "name": name, "source": "index_daily"})
    sw = _read("sw_daily", workspace)
    if not sw.empty and "symbol" in sw.columns:
        names = sw.groupby("symbol")["name"].first().to_dict() if "name" in sw.columns else {}
        for code in sorted(sw["symbol"].astype(str).unique()):
            out.append({"code": code, "name": str(names.get(code, code)), "source": "sw_daily"})
    return out
