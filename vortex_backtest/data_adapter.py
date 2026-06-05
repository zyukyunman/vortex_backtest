from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd

from .symbols import market_board, normalize_symbol


DEFAULT_WORKSPACE = Path("/Users/zyukyunman/Documents/vortex_workspace")


@dataclass(frozen=True)
class TushareMinuteDataset:
    minutes: pd.DataFrame
    calendar: list[int]


class TushareMinuteDataLoader:
    def __init__(self, workspace: Path | str = DEFAULT_WORKSPACE):
        self.workspace = Path(workspace).expanduser().resolve()
        self.data_dir = self.workspace / "data"

    def load(
        self,
        *,
        symbols: set[str],
        start_date: date,
        end_date: date,
    ) -> TushareMinuteDataset:
        normalized_symbols = {normalize_symbol(symbol) for symbol in symbols}
        start_key = date_key(start_date)
        end_key = date_key(end_date)
        minutes = self._read_required("stk_mins", "minute_data_missing")
        minutes = normalize_columns(minutes)
        if minutes.empty:
            raise ValueError("minute_data_missing")
        minutes["symbol"] = minutes["symbol"].map(normalize_symbol)
        minutes["date"] = minutes["date"].map(int)
        minutes = minutes[
            minutes["symbol"].isin(normalized_symbols)
            & minutes["date"].between(start_key, end_key)
            & (minutes.get("freq", "1min") == "1min")
        ].copy()
        if minutes.empty:
            raise ValueError("minute_data_missing")
        minutes["trade_time"] = pd.to_datetime(minutes["trade_time"])

        adj_factor = self._read_required("adj_factor", "adjustment_data_missing")
        adj_factor = normalize_columns(adj_factor)
        if adj_factor.empty:
            raise ValueError("adjustment_data_missing")
        adj_factor["symbol"] = adj_factor["symbol"].map(normalize_symbol)
        adj_factor["date"] = adj_factor["date"].map(int)
        adj_factor = adj_factor[adj_factor["symbol"].isin(normalized_symbols)].copy()
        # 修复 C3：qfq 基准锚定「该标的全历史最新」复权因子，绝对价位不随回测窗口漂移。
        # latest 必须在按窗口裁剪之前、用全量 adj_factor 计算。
        latest = (
            adj_factor.sort_values(["symbol", "date"])
            .groupby("symbol", as_index=False)
            .tail(1)[["symbol", "adj_factor"]]
            .rename(columns={"adj_factor": "latest_adj_factor"})
        )
        adj_factor_window = adj_factor[adj_factor["date"].between(start_key, end_key)].copy()

        limits = self._read_required("stk_limit", "market_rules_data_missing")
        limits = normalize_columns(limits)
        if limits.empty:
            raise ValueError("market_rules_data_missing")
        limits["symbol"] = limits["symbol"].map(normalize_symbol)
        limits["date"] = limits["date"].map(int)
        limits = limits[
            limits["symbol"].isin(normalized_symbols) & limits["date"].between(start_key, end_key)
        ].copy()

        enriched = minutes.merge(
            adj_factor_window[["symbol", "date", "adj_factor"]],
            on=["symbol", "date"],
            how="left",
            validate="many_to_one",
        )
        if enriched["adj_factor"].isna().any():
            raise ValueError("adjustment_data_missing")
        enriched = enriched.merge(latest, on="symbol", how="left", validate="many_to_one")
        if enriched["latest_adj_factor"].isna().any():
            raise ValueError("adjustment_data_missing")
        enriched["qfq_multiplier"] = enriched["adj_factor"] / enriched["latest_adj_factor"]

        enriched = enriched.merge(
            limits[["symbol", "date", "up_limit", "down_limit"]],
            on=["symbol", "date"],
            how="left",
            validate="many_to_one",
        )
        if enriched["up_limit"].isna().any() or enriched["down_limit"].isna().any():
            raise ValueError("market_rules_data_missing")

        for column in ("open", "high", "low", "close"):
            enriched[f"{column}_qfq"] = round_series(enriched[column] * enriched["qfq_multiplier"])
        enriched["limit_up_qfq"] = round_series(enriched["up_limit"] * enriched["qfq_multiplier"])
        enriched["limit_down_qfq"] = round_series(enriched["down_limit"] * enriched["qfq_multiplier"])

        suspended = self._read_optional("suspend_d")
        enriched["suspended"] = False
        if not suspended.empty:
            suspended = normalize_columns(suspended)
            if {"symbol", "date"}.issubset(suspended.columns):
                suspended["symbol"] = suspended["symbol"].map(normalize_symbol)
                suspended["date"] = suspended["date"].map(int)
                if "suspend_type" in suspended.columns:
                    suspended = suspended[
                        suspended["suspend_type"].fillna("").astype(str).str.upper().eq("S")
                    ]
                suspended_keys = suspended[["symbol", "date"]].drop_duplicates()
                enriched = enriched.merge(
                    suspended_keys.assign(suspended_flag=True),
                    on=["symbol", "date"],
                    how="left",
                )
                enriched["suspended"] = enriched["suspended_flag"].fillna(False).astype(bool)
                enriched = enriched.drop(columns=["suspended_flag"])

        st_rows = self._read_optional("stock_st")
        enriched["is_st"] = False
        if not st_rows.empty and {"symbol", "date"}.issubset(normalize_columns(st_rows).columns):
            st_rows = normalize_columns(st_rows)
            st_rows["symbol"] = st_rows["symbol"].map(normalize_symbol)
            st_rows["date"] = st_rows["date"].map(int)
            st_keys = st_rows[["symbol", "date"]].drop_duplicates()
            enriched = enriched.merge(
                st_keys.assign(is_st_flag=True),
                on=["symbol", "date"],
                how="left",
            )
            enriched["is_st"] = enriched["is_st_flag"].fillna(False).astype(bool)
            enriched = enriched.drop(columns=["is_st_flag"])

        enriched["board"] = enriched["symbol"].map(market_board)
        if "volume" not in enriched.columns and "vol" in enriched.columns:
            enriched["volume"] = enriched["vol"]
        enriched["volume"] = enriched["volume"].fillna(0).astype(int)
        enriched = enriched.sort_values(["trade_time", "symbol"]).reset_index(drop=True)
        calendar = sorted(int(item) for item in enriched["date"].unique())
        return TushareMinuteDataset(minutes=enriched, calendar=calendar)

    def _read_required(self, dataset: str, error: str) -> pd.DataFrame:
        frame = self._read_optional(dataset)
        if frame.empty:
            raise ValueError(error)
        return frame

    def _read_optional(self, dataset: str) -> pd.DataFrame:
        root = self.data_dir / dataset
        if not root.exists():
            return pd.DataFrame()
        paths = sorted(root.rglob("*.parquet"))
        if not paths:
            return pd.DataFrame()
        frames = [pd.read_parquet(path) for path in paths]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def normalize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    rename = {
        "ts_code": "symbol",
        "trade_date": "date",
        "vol": "volume",
    }
    return frame.rename(columns={key: value for key, value in rename.items() if key in frame.columns})


def round_series(series: pd.Series) -> pd.Series:
    return series.astype(float).round(4)


def date_key(value: date) -> int:
    return int(value.strftime("%Y%m%d"))


def date_from_key(value: int) -> date:
    text = str(int(value))
    return date(int(text[:4]), int(text[4:6]), int(text[6:]))

