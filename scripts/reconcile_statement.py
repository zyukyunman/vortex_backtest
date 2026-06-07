#!/usr/bin/env python3
"""券商对账单 ↔ 回测结果 对照脚手架（design/15 Phase 4）。

口径：回测走 **qfq 前复权、不建模现金分红**（见 design/15）。因此与券商真实账本对账时，
**按容差判定合理**，并对"窗口内除权(ex_date)的标的"标注为**预期 qfq 分红差**——区分
"已知口径差"与"真 bug"。

用法::

    python scripts/reconcile_statement.py \
        --summary  <回测产物 account_summary.json> \
        --statement <券商对账单.csv> \
        [--events-dir <vortex_data/workspace/data/events>] \
        [--tolerance 0.005] [--out diff.csv]

对账单 CSV 期望列（大小写不敏感，常见别名自动映射；缺列则跳过该项对比）：
    date(交易日) symbol(代码,如600000.SH) side(buy/sell 或 1/2) quantity(成交量) price(成交均价)
    可选：amount(成交额) commission/stamp_tax/transfer_fee 或 fee(合计费用) request_id

对比口径：按 (date, symbol, side) **聚合**两侧成交（券商可能多笔分单、回测为日级单笔），
比较聚合后的 数量 / 成交额(或 量×均价) / 费用；相对误差 > tolerance 记为超差。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# 列名别名 → 规范名
_ALIASES = {
    "date": {"date", "trade_date", "交易日", "日期", "成交日期"},
    "symbol": {"symbol", "ts_code", "code", "证券代码", "代码", "股票代码"},
    "side": {"side", "direction", "买卖", "方向", "买卖方向"},
    "quantity": {"quantity", "qty", "vol", "volume", "成交数量", "成交量", "数量"},
    "price": {"price", "成交均价", "成交价格", "均价", "成交价"},
    "amount": {"amount", "turnover", "成交金额", "成交额", "金额"},
    "commission": {"commission", "佣金", "手续费"},
    "stamp_tax": {"stamp_tax", "印花税"},
    "transfer_fee": {"transfer_fee", "过户费"},
    "fee": {"fee", "total_fee", "费用合计", "合计费用", "总费用"},
    "request_id": {"request_id", "委托编号", "合同编号"},
}


def _canon_columns(df: pd.DataFrame) -> pd.DataFrame:
    lower = {c.lower().strip(): c for c in df.columns}
    rename: dict[str, str] = {}
    for canon, names in _ALIASES.items():
        for n in names:
            if n.lower() in lower:
                rename[lower[n.lower()]] = canon
                break
    return df.rename(columns=rename)


def _norm_side(value) -> str:
    s = str(value).strip().lower()
    if s in {"1", "b", "buy", "买", "买入", "证券买入"}:
        return "BUY"
    if s in {"2", "s", "sell", "卖", "卖出", "证券卖出"}:
        return "SELL"
    return s.upper()


def _norm_date(value) -> str:
    s = str(value).strip().replace("/", "-")
    digits = s.replace("-", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return s[:10]


def _norm_symbol(value) -> str:
    return str(value).strip().upper()


def _aggregate(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """规范化 + 按 (date,symbol,side) 聚合成交（数量求和、成交额求和、费用求和、均价=额/量）。"""
    if df.empty:
        return pd.DataFrame(columns=["date", "symbol", "side", "qty", "notional", "fee"])
    df = _canon_columns(df.copy())
    for col in ("date", "symbol", "side", "quantity"):
        if col not in df.columns:
            raise SystemExit(f"[{source}] 缺少必需列：{col}（现有列：{list(df.columns)}）")
    df["date"] = df["date"].map(_norm_date)
    df["symbol"] = df["symbol"].map(_norm_symbol)
    df["side"] = df["side"].map(_norm_side)
    df["qty"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0.0)
    if "amount" in df.columns:
        df["notional"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0).abs()
    elif "price" in df.columns:
        df["notional"] = df["qty"] * pd.to_numeric(df["price"], errors="coerce").fillna(0.0)
    else:
        df["notional"] = 0.0
    if "fee" in df.columns:
        df["fee"] = pd.to_numeric(df["fee"], errors="coerce").fillna(0.0)
    else:
        df["fee"] = sum(
            pd.to_numeric(df[c], errors="coerce").fillna(0.0)
            for c in ("commission", "stamp_tax", "transfer_fee")
            if c in df.columns
        ) if any(c in df.columns for c in ("commission", "stamp_tax", "transfer_fee")) else 0.0
    return df.groupby(["date", "symbol", "side"], as_index=False)[["qty", "notional", "fee"]].sum()


def _trades_from_summary(summary: dict) -> pd.DataFrame:
    rows = [
        {
            "date": _norm_date(t.get("trade_date")),
            "symbol": _norm_symbol(t.get("symbol")),
            "side": "BUY" if int(t.get("side", 0)) == 1 else "SELL",
            "quantity": t.get("quantity", 0),
            "amount": t.get("amount", 0.0),
            "fee": float(t.get("commission", 0.0)) + float(t.get("stamp_tax", 0.0)) + float(t.get("transfer_fee", 0.0)),
        }
        for t in summary.get("trades", [])
    ]
    return _aggregate(pd.DataFrame(rows), source="backtest")


def _ex_div_symbols(events_dir: Path, start: str, end: str) -> set[str]:
    """events(分红)数据集中、在 [start,end] 窗口内 ex_date 的标的集合（用于标注预期 qfq 差）。"""
    if not events_dir or not events_dir.exists():
        return set()
    s, e = start.replace("-", ""), end.replace("-", "")
    out: set[str] = set()
    for p in events_dir.rglob("*.parquet"):
        try:
            df = pd.read_parquet(p)
        except Exception:
            continue
        df = _canon_columns(df)
        col = "ex_date" if "ex_date" in df.columns else ("date" if "date" in df.columns else None)
        sym = "symbol" if "symbol" in df.columns else None
        if not col or not sym:
            continue
        d = df[[sym, col]].dropna()
        d["k"] = d[col].astype(str).str.replace("-", "", regex=False).str.slice(0, 8)
        hit = d[(d["k"] >= s) & (d["k"] <= e)]
        out |= {_norm_symbol(x) for x in hit[sym].tolist()}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="券商对账单 ↔ 回测对照（容差，qfq 口径）")
    ap.add_argument("--summary", required=True, type=Path, help="回测 account_summary.json")
    ap.add_argument("--statement", required=True, type=Path, help="券商对账单 CSV")
    ap.add_argument("--events-dir", type=Path, default=None, help="vortex_data events 目录（标注分红差）")
    ap.add_argument("--tolerance", type=float, default=0.005, help="相对误差容差（默认 0.5%）")
    ap.add_argument("--out", type=Path, default=None, help="差异明细输出 CSV")
    args = ap.parse_args(argv)

    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    bt = _trades_from_summary(summary)
    stmt = _aggregate(pd.read_csv(args.statement), source="statement")

    merged = bt.merge(stmt, on=["date", "symbol", "side"], how="outer", suffixes=("_bt", "_stmt"), indicator=True)
    merged = merged.fillna(0.0)

    def _rel(a: float, b: float) -> float:
        base = max(abs(a), abs(b))
        return 0.0 if base == 0 else abs(a - b) / base

    merged["qty_rel"] = merged.apply(lambda r: _rel(r["qty_bt"], r["qty_stmt"]), axis=1)
    merged["notional_rel"] = merged.apply(lambda r: _rel(r["notional_bt"], r["notional_stmt"]), axis=1)
    merged["fee_rel"] = merged.apply(lambda r: _rel(r["fee_bt"], r["fee_stmt"]), axis=1)

    dates = [d for d in merged["date"].tolist() if d]
    ex_div = _ex_div_symbols(args.events_dir, min(dates) if dates else "", max(dates) if dates else "") if dates else set()
    merged["expected_div_gap"] = merged["symbol"].isin(ex_div)

    tol = args.tolerance
    merged["status"] = merged.apply(
        lambda r: (
            "UNMATCHED" if r["_merge"] != "both"
            else "OK" if max(r["qty_rel"], r["notional_rel"], r["fee_rel"]) <= tol
            else "EXPECTED_DIV_GAP" if r["expected_div_gap"]
            else "OVER_TOLERANCE"
        ),
        axis=1,
    )

    counts = merged["status"].value_counts().to_dict()
    print("=== 对账汇总（容差 {:.2%}）==========================".format(tol))
    print(f"  成交聚合行(date×symbol×side)：{len(merged)}")
    for k in ("OK", "EXPECTED_DIV_GAP", "OVER_TOLERANCE", "UNMATCHED"):
        print(f"  {k:18s}: {counts.get(k, 0)}")
    if ex_div:
        print(f"  窗口内除权标的(预期 qfq 分红差)：{sorted(ex_div)}")
    problems = merged[merged["status"].isin(["OVER_TOLERANCE", "UNMATCHED"])]
    if not problems.empty:
        print("\n=== 需排查（超差/未匹配）=========================")
        cols = ["date", "symbol", "side", "qty_bt", "qty_stmt", "notional_bt", "notional_stmt",
                "fee_bt", "fee_stmt", "qty_rel", "notional_rel", "fee_rel", "_merge", "status"]
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(problems[cols].to_string(index=False))
    else:
        print("\n✅ 全部在容差内（或属预期分红差）。")

    if args.out:
        merged.to_csv(args.out, index=False)
        print(f"\n差异明细已写入：{args.out}")
    # 退出码：有需排查项 → 1，便于 CI 卡口
    return 1 if not problems.empty else 0


if __name__ == "__main__":
    sys.exit(main())
