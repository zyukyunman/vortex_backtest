#!/usr/bin/env python3
"""在 qlib 镜像里用 QlibReplayEngine 对 vortex_data 导出的 qlib 数据出**日级回测报告**。

它直接调引擎（不起 HTTP 服务），证明：qlib 数据层 + 自研 A 股规则层 → 与自研引擎同款日级 summary。

容器内用法（挂载仓库源码 + qlib 数据）：
  docker run --rm --platform linux/amd64 \
    -v <repo>:/work -w /work -e PYTHONPATH=/work \
    -v <qlib_out>:/qlib:ro -e VORTEX_QLIB_PROVIDER_URI=/qlib \
    vortex-backtest-qlib \
    python spike/qlib_engine_demo.py --symbols 000001.SZ,600000.SH --start 2026-01-05 --end 2026-06-05
"""
from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--provider-uri", default=os.getenv("VORTEX_QLIB_PROVIDER_URI", "/qlib"))
    p.add_argument("--symbols", default="000001.SZ,600000.SH", help="逗号分隔（vortex 代码，如 600000.SH）")
    p.add_argument("--start", default="2026-01-05")
    p.add_argument("--end", default="2026-06-05")
    p.add_argument("--cash", type=float, default=1_000_000.0)
    p.add_argument("--report-dir", default="/tmp/qlib_engine_report")
    args = p.parse_args()

    from vortex_backtest.qlib_engine import QlibReplayEngine

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    account = {"account_id": "demo", "initial_cash": args.cash, "engine": "qlib"}
    orders = []
    for i, sym in enumerate(symbols):
        qty = 200 if sym.startswith("688") else 1000
        orders.append({
            "order_batch_id": "b1", "request_id": f"buy-{i}", "trade_date": args.start,
            "symbol": sym, "side": 1, "quantity": qty, "price_type": "close", "limit_price": None,
        })
    strategies = [{
        "strategy_id": "qlib-demo", "strategy_type": "order_replay", "initial_cash": args.cash,
        "symbols": symbols, "params": {"order_batch_id": "b1"},
    }]

    engine = QlibReplayEngine(provider_uri=args.provider_uri)
    summary = engine.run(
        job_id="demo", account=account, orders=orders, report_dir=Path(args.report_dir),
        start_date=date.fromisoformat(args.start), end_date=date.fromisoformat(args.end),
        order_batch_id="b1", market_data_set_id="qlib_smoke", frequency="1min",
        price_adjustment="qfq", order_price_adjustment="qfq", default_price_type="close",
        strategies=strategies, execution={},
    )

    print("=== QlibReplayEngine 日级回测报告 ===")
    print("status: completed")
    print("total_value:", summary["total_value"], "return:", summary["total_return"], "maxDD:", summary["max_drawdown"])
    print("#trades", len(summary["trades"]), "#rej", len(summary["rejections"]), "#daily", len(summary["daily"]))
    for t in summary["trades"]:
        print("  T", t["trade_date"], t["symbol"], t["side_name"], t["quantity"], "@", t["price"])
    for r in summary["rejections"][:10]:
        print("  R", r["trade_date"], r["symbol"], r["side_name"], r["reason"])
    for d in summary["daily"]:
        print("  D", d["trade_date"], "tot", d["total_value"], "pnl", d["daily_pnl"], "ret", d["total_return"])
    print("artifacts:", summary["artifacts"]["daily_equity"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
