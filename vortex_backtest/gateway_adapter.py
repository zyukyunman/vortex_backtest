"""GatewayDataAdapter（design/18 B4）：经 vortex_data 取数网关取富 bar，替代直读共享 parquet。

与 ``data_adapter.TushareMinuteDataLoader`` 产出同一"富 bar"接口（engine 内核无需改签名），但：
- 数据经 ``POST {VORTEX_DATA_URL}/api/v1/data`` 取，**服务端按 as_of 强制 point-in-time**（防未来函数）。
- 复权用**前复权 + PIT 锚点** ``price_qfq = price × adj_factor ÷ (≤as_of 最新 adj_factor)``——
  最新可见日 multiplier≈1（价位量级真实、可现金结算），且锚点 ≤ as_of 不含未来除权（PIT 安全）。
  （直接后复权 ×adj_factor 量级被累计因子放大数倍，会把现金校验打成 insufficient_cash；design/18 §7、契约 C2。）
- 缺字段优雅降级：某 symbol 在某 as_of 缺行情/复权 → 跳过该 symbol，不中断会话。
"""
from __future__ import annotations

import os
from datetime import date

import httpx
import pandas as pd

from .data_adapter import TushareMinuteDataset, date_key, normalize_columns, round_series
from .symbols import market_board, normalize_symbol

_TOKEN_ENV = "VORTEX_DATA_DASHBOARD_TOKEN"
_URL_ENV = "VORTEX_DATA_URL"


class GatewayDataError(RuntimeError):
    pass


class GatewayDataAdapter:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = (base_url or os.getenv(_URL_ENV) or "").rstrip("/")
        self.token = token or os.getenv(_TOKEN_ENV)
        self.timeout = timeout

    # ---------------------------------------------------------------- HTTP

    def _query(self, as_of: str, datasets: list[dict]) -> dict:
        if not self.base_url:
            raise GatewayDataError("VORTEX_DATA_URL 未配置")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-API-Token"] = self.token
        # trust_env=False：服务对服务直连，绕过环境里的 HTTP(S)/SOCKS 代理（localhost 不该走代理）。
        with httpx.Client(trust_env=False, timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/api/v1/data",
                json={"as_of": as_of, "datasets": datasets},
                headers=headers,
            )
        if resp.status_code != 200:
            raise GatewayDataError(f"gateway {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    @staticmethod
    def _df(result: dict, dataset: str) -> pd.DataFrame:
        block = (result.get("results") or {}).get(dataset) or {}
        rows = block.get("rows") or []
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    # ---------------------------------------------------------------- load

    def load(
        self,
        *,
        symbols: set[str],
        start_date: date,
        end_date: date,
        as_of: str,
        anchor_date: date | None = None,
    ) -> TushareMinuteDataset:
        """取 [start, end] 窗口、≤ as_of 的富 bar（前复权 PIT 锚点）。

        ``anchor_date``（会话起始）给定时 → **固定锚**：锚在 ≤anchor_date 的因子，全程不变，
        跨除权日价格连续、total return 正确（design/18 N5）。不给则退回 per-as_of 最新（旧行为）。
        """
        syms = sorted({normalize_symbol(s) for s in symbols})
        start_s, end_s = str(date_key(start_date)), str(date_key(end_date))
        rng = {"range": {"start": start_s, "end": end_s}}
        # 固定锚需要从 anchor_date 起的 adj（拿到锚 + 各 bar 当日因子）；否则只取窗口。
        adj_start = str(date_key(anchor_date)) if anchor_date is not None else start_s
        adj_rng = {"range": {"start": adj_start, "end": end_s}}
        result = self._query(as_of, [
            {"dataset": "stk_mins", "symbols": syms, "window": rng, "level": "1min"},
            {"dataset": "adj_factor", "symbols": syms, "window": adj_rng},
            {"dataset": "stk_limit", "symbols": syms, "window": rng},
            {"dataset": "suspend_d", "symbols": syms, "window": rng},
            {"dataset": "stock_st", "symbols": syms, "window": rng},
        ])

        minutes = normalize_columns(self._df(result, "stk_mins"))
        if minutes.empty:
            raise ValueError("minute_data_missing")
        minutes["symbol"] = minutes["symbol"].map(normalize_symbol)
        minutes["date"] = minutes["date"].map(int)
        minutes = minutes[minutes.get("freq", "1min") == "1min"].copy()
        minutes["trade_time"] = pd.to_datetime(minutes["trade_time"])

        adj = normalize_columns(self._df(result, "adj_factor"))
        if adj.empty:
            raise ValueError("adjustment_data_missing")
        adj["symbol"] = adj["symbol"].map(normalize_symbol)
        adj["date"] = adj["date"].map(int)
        adj = adj[["symbol", "date", "adj_factor"]]

        limits = normalize_columns(self._df(result, "stk_limit"))
        if limits.empty:
            raise ValueError("market_rules_data_missing")
        limits["symbol"] = limits["symbol"].map(normalize_symbol)
        limits["date"] = limits["date"].map(int)
        limits = limits[["symbol", "date", "up_limit", "down_limit"]]

        # 前复权 PIT 锚点。multiplier = adj_factor[bar] / 锚。
        # - 固定锚(anchor_date)：锚=会话起始的因子（adj 窗口最早一行/symbol）→ 全程不变 → 跨除权连续、return 正确。
        # - per-as_of(无 anchor_date)：锚=≤as_of 最新（adj 窗口最新一行）→ 量级真实但跨除权会漂移。
        # 直接后复权(×adj_factor)量级被累计因子放大数倍 → insufficient_cash，故都要除以锚。
        keep = "first" if anchor_date is not None else "last"
        latest = (
            adj.sort_values(["symbol", "date"]).drop_duplicates("symbol", keep=keep)
            [["symbol", "adj_factor"]].rename(columns={"adj_factor": "_latest_adj"})
        )
        enriched = minutes.merge(adj, on=["symbol", "date"], how="left", validate="many_to_one")
        enriched = enriched[enriched["adj_factor"].notna()].copy()  # 缺复权优雅降级
        if enriched.empty:
            raise ValueError("adjustment_data_missing")
        enriched = enriched.merge(latest, on="symbol", how="left")
        enriched["_mult"] = enriched["adj_factor"] / enriched["_latest_adj"]

        for col in ("open", "high", "low", "close"):
            if col in enriched.columns:
                enriched[f"{col}_qfq"] = round_series(enriched[col] * enriched["_mult"])

        enriched = enriched.merge(limits, on=["symbol", "date"], how="left", validate="many_to_one")
        enriched = enriched[enriched["up_limit"].notna() & enriched["down_limit"].notna()].copy()
        if enriched.empty:
            raise ValueError("market_rules_data_missing")
        enriched["limit_up_qfq"] = round_series(enriched["up_limit"] * enriched["_mult"])
        enriched["limit_down_qfq"] = round_series(enriched["down_limit"] * enriched["_mult"])
        enriched = enriched.drop(columns=["_latest_adj", "_mult"])

        enriched["suspended"] = self._flag(enriched, normalize_columns(self._df(result, "suspend_d")), "suspend_type", "S")
        enriched["is_st"] = self._flag(enriched, normalize_columns(self._df(result, "stock_st")), None, None)

        enriched["board"] = enriched["symbol"].map(market_board)
        enriched["volume"] = enriched.get("volume", 0)
        enriched["volume"] = enriched["volume"].fillna(0).astype(int)
        enriched = enriched.sort_values(["trade_time", "symbol"]).reset_index(drop=True)
        calendar = sorted(int(x) for x in enriched["date"].unique())
        return TushareMinuteDataset(minutes=enriched, calendar=calendar)

    @staticmethod
    def _flag(enriched: pd.DataFrame, table: pd.DataFrame, type_col: str | None, type_val: str | None) -> pd.Series:
        """把 (symbol,date) 事件表标成布尔列对齐到 enriched。缺表 → 全 False。"""
        false = pd.Series(False, index=enriched.index)
        if table.empty or not {"symbol", "date"}.issubset(table.columns):
            return false
        table = table.copy()
        table["symbol"] = table["symbol"].map(normalize_symbol)
        table["date"] = table["date"].map(int)
        if type_col and type_col in table.columns:
            table = table[table[type_col].fillna("").astype(str).str.upper().eq(type_val)]
        keys = table[["symbol", "date"]].drop_duplicates().assign(_flag=True)
        merged = enriched[["symbol", "date"]].merge(keys, on=["symbol", "date"], how="left")
        return merged["_flag"].fillna(False).astype(bool).values
