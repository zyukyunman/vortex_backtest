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


def _num(value) -> float:
    """分红字段→float；None/NaN/空 → 0.0（送转比例缺省即无该项）。"""
    try:
        f = float(value)
    except (ValueError, TypeError):
        return 0.0
    return 0.0 if f != f else f  # NaN → 0


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
        price_mode: str = "raw",
    ) -> TushareMinuteDataset:
        """取 [start, end] 窗口、≤ as_of 的富 bar。

        ``price_mode``：
        - ``"raw"``（默认，N8 真实账户口径）：撮合/估值用**不复权 RAW 价**（``close_qfq==close``、multiplier=1），
          分红送转改由除权日显式入账（见 ``load_dividends`` + ``session_engine.apply_corporate_actions``）。
          不取 adj_factor，跨除权 RAW 价会跳变（被公司行动入账抵消）。
        - ``"qfq"``（金标 oracle）：前复权 PIT 锚点 ``price = close × adj_factor ÷ 锚``。
          ``anchor_date`` 给定 → 固定锚（会话起始因子，跨除权连续）；否则 per-as_of 最新（design/18 N5）。
        """
        syms = sorted({normalize_symbol(s) for s in symbols})
        start_s, end_s = str(date_key(start_date)), str(date_key(end_date))
        rng = {"range": {"start": start_s, "end": end_s}}
        datasets = [
            {"dataset": "stk_mins", "symbols": syms, "window": rng, "level": "1min"},
            {"dataset": "stk_limit", "symbols": syms, "window": rng},
            {"dataset": "suspend_d", "symbols": syms, "window": rng},
            {"dataset": "stock_st", "symbols": syms, "window": rng},
        ]
        if price_mode == "qfq":
            # 固定锚需从 anchor_date 起的 adj（拿到锚 + 各 bar 当日因子）；否则只取窗口。
            adj_start = str(date_key(anchor_date)) if anchor_date is not None else start_s
            datasets.insert(1, {"dataset": "adj_factor", "symbols": syms, "window": {"range": {"start": adj_start, "end": end_s}}})
        result = self._query(as_of, datasets)

        minutes = normalize_columns(self._df(result, "stk_mins"))
        if minutes.empty:
            raise ValueError("minute_data_missing")
        minutes["symbol"] = minutes["symbol"].map(normalize_symbol)
        minutes["date"] = minutes["date"].map(int)
        minutes = minutes[minutes.get("freq", "1min") == "1min"].copy()
        minutes["trade_time"] = pd.to_datetime(minutes["trade_time"])

        limits = normalize_columns(self._df(result, "stk_limit"))
        if limits.empty:
            raise ValueError("market_rules_data_missing")
        limits["symbol"] = limits["symbol"].map(normalize_symbol)
        limits["date"] = limits["date"].map(int)
        limits = limits[["symbol", "date", "up_limit", "down_limit"]]

        # multiplier：raw → 恒 1（不复权）；qfq → adj_factor[bar] / 锚（PIT 锚点）。
        if price_mode == "qfq":
            adj = normalize_columns(self._df(result, "adj_factor"))
            if adj.empty:
                raise ValueError("adjustment_data_missing")
            adj["symbol"] = adj["symbol"].map(normalize_symbol)
            adj["date"] = adj["date"].map(int)
            adj = adj[["symbol", "date", "adj_factor"]]
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
        else:  # raw：不复权，撮合/估值口径 = 真实成交价（N8）
            enriched = minutes.copy()
            enriched["_mult"] = 1.0

        for col in ("open", "high", "low", "close"):
            if col in enriched.columns:
                enriched[f"{col}_qfq"] = round_series(enriched[col] * enriched["_mult"])

        enriched = enriched.merge(limits, on=["symbol", "date"], how="left", validate="many_to_one")
        enriched = enriched[enriched["up_limit"].notna() & enriched["down_limit"].notna()].copy()
        if enriched.empty:
            raise ValueError("market_rules_data_missing")
        enriched["limit_up_qfq"] = round_series(enriched["up_limit"] * enriched["_mult"])
        enriched["limit_down_qfq"] = round_series(enriched["down_limit"] * enriched["_mult"])
        enriched = enriched.drop(columns=[c for c in ("_latest_adj", "_mult") if c in enriched.columns])

        enriched["suspended"] = self._flag(enriched, normalize_columns(self._df(result, "suspend_d")), "suspend_type", "S")
        enriched["is_st"] = self._flag(enriched, normalize_columns(self._df(result, "stock_st")), None, None)

        enriched["board"] = enriched["symbol"].map(market_board)
        enriched["volume"] = enriched.get("volume", 0)
        enriched["volume"] = enriched["volume"].fillna(0).astype(int)
        enriched = enriched.sort_values(["trade_time", "symbol"]).reset_index(drop=True)
        calendar = sorted(int(x) for x in enriched["date"].unique())
        return TushareMinuteDataset(minutes=enriched, calendar=calendar)

    # ------------------------------------------------------------ dividends

    def load_dividends(self, *, symbols: set[str], as_of: str) -> list[dict]:
        """取持仓 symbol 的已实施分红（≤ as_of，网关按 ``effective_from`` 闸门=已公告）。

        只返回**有 ex_date 的实施分红**（预案/方案 ex_date 空 → 未除权、不入账，自然滤除）。
        会话据此在 (上一步 sim_time, 本步 sim_time] 内除权日入账现金/送转（N8 真实账户口径）。
        回 ``[{symbol, ex_date(int yyyymmdd), cash_div_tax, stk_div, stk_bo_rate, stk_co_rate}, ...]``。
        """
        syms = sorted({normalize_symbol(s) for s in symbols})
        if not syms:
            return []
        result = self._query(as_of, [{
            "dataset": "dividend", "symbols": syms,
            "fields": ["symbol", "ex_date", "cash_div_tax", "stk_div", "stk_bo_rate", "stk_co_rate"],
        }])
        df = normalize_columns(self._df(result, "dividend"))
        if df.empty or "ex_date" not in df.columns:
            return []
        df = df[df["ex_date"].notna()].copy()  # 仅实施（有除权日）
        if df.empty:
            return []
        df["symbol"] = df["symbol"].map(normalize_symbol)
        out: list[dict] = []
        for _, r in df.iterrows():
            try:
                ex = int(str(r["ex_date"])[:8].replace("-", ""))
            except (ValueError, TypeError):
                continue
            out.append({
                "symbol": str(r["symbol"]),
                "ex_date": ex,
                "cash_div_tax": _num(r.get("cash_div_tax")),
                "stk_div": _num(r.get("stk_div")),
                "stk_bo_rate": _num(r.get("stk_bo_rate")),
                "stk_co_rate": _num(r.get("stk_co_rate")),
            })
        # 去重：同 (symbol, ex_date) 一笔除权效果（多行进度状态合并）
        seen: dict[tuple[str, int], dict] = {}
        for d in out:
            seen[(d["symbol"], d["ex_date"])] = d
        return list(seen.values())

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
