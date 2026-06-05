from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pandas as pd


DEFAULT_WORKSPACE = Path("/Users/zyukyunman/Documents/vortex_workspace")
DEFAULT_SYMBOLS = ("000001.SZ", "688809.SH")


def main() -> int:
    args = parse_args()
    workspace = args.workspace.expanduser().resolve()
    check_workspace(workspace)
    dates_by_symbol = load_available_dates(workspace, args.symbols)
    common_dates = sorted(set.intersection(*(set(values) for values in dates_by_symbol.values())))
    if len(common_dates) < 2:
        raise SystemExit("分钟样例至少需要 2 个共同交易日")
    start_date = common_dates[0]
    end_date = common_dates[min(len(common_dates) - 1, args.days - 1)]

    client = httpx.Client(
        base_url=args.base_url,
        timeout=httpx.Timeout(30.0),
        trust_env=False,
    )
    timestamp = int(time.time())
    account_id = args.account_id or f"minute-sample-{timestamp}"

    print(f"base_url={args.base_url}")
    print(f"workspace={workspace}")
    print(f"account_id={account_id}")
    print(f"date_range={start_date}..{end_date}")
    print(f"symbols={','.join(args.symbols)}")

    post(client, "/accounts", {"account_id": account_id, "initial_cash": args.initial_cash})
    strategies = []
    for symbol in args.symbols:
        batch_id = f"{account_id}-{symbol.replace('.', '-')}"
        strategies.append(
            {
                "strategy_id": f"replay-{symbol.replace('.', '-')}",
                "strategy_type": "order_replay",
                "initial_cash": args.initial_cash / len(args.symbols),
                "symbols": [symbol],
                "params": {"order_batch_id": batch_id},
            }
        )
        for order in sample_orders(symbol=symbol, order_batch_id=batch_id, dates=common_dates):
            post(client, f"/accounts/{account_id}/orders", order)

    job = post(
        client,
        "/backtests",
        {
            "account_id": account_id,
            "frequency": "1min",
            "price_adjustment": "qfq",
            "start_date": start_date,
            "end_date": end_date,
            "strategies": strategies,
        },
    )
    job_id = job["job_id"]
    print(f"job_id={job_id} status={job['status']}")

    summary = get(client, f"/backtests/{job_id}/summary")
    daily = get(client, f"/backtests/{job_id}/daily")
    minutes = get(client, f"/backtests/{job_id}/minutes")

    print("\nFinal summary")
    print_jsonish(
        {
            "cash": summary["cash"],
            "market_value": summary["market_value"],
            "total_value": summary["total_value"],
            "total_return": summary["total_return"],
            "strategies": [
                {
                    "strategy_id": item["strategy_id"],
                    "total_value": item["total_value"],
                    "trades": len(item["trades"]),
                    "rejections": len(item["rejections"]),
                }
                for item in summary["strategies"]
            ],
            "daily_rows": len(daily),
            "minute_rows": len(minutes),
            "artifacts": summary["artifacts"],
        }
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a minute-level Vortex backtest HTTP sample from local Tushare workspace."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--days", type=int, default=25)
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--account-id")
    args = parser.parse_args()
    args.symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    if not args.symbols:
        parser.error("--symbols cannot be empty")
    if args.days < 2:
        parser.error("--days must be >= 2")
    return args


def check_workspace(workspace: Path) -> None:
    missing = [
        dataset
        for dataset in ("stk_mins", "adj_factor", "stk_limit")
        if not (workspace / "data" / dataset).exists()
    ]
    if missing:
        raise SystemExit(
            "workspace 缺少分钟回测必需数据集: "
            + ",".join(missing)
            + f"\n请先在 vortex_data 中补齐，然后再运行样例。workspace={workspace}"
        )


def load_available_dates(workspace: Path, symbols: tuple[str, ...]) -> dict[str, list[str]]:
    root = workspace / "data" / "stk_mins"
    frames = [pd.read_parquet(path) for path in sorted(root.rglob("*.parquet"))]
    if not frames:
        raise SystemExit(f"stk_mins 没有 parquet 文件: {root}")
    minutes = pd.concat(frames, ignore_index=True)
    if "ts_code" in minutes.columns and "symbol" not in minutes.columns:
        minutes = minutes.rename(columns={"ts_code": "symbol"})
    result: dict[str, list[str]] = {}
    minutes["symbol"] = minutes["symbol"].astype(str).str.upper()
    for symbol in symbols:
        dates = sorted(
            {
                normalize_date(value)
                for value in minutes.loc[minutes["symbol"] == symbol, "date"].tolist()
            }
        )
        if len(dates) < 2:
            raise SystemExit(f"{symbol} 可用分钟交易日不足 2 天")
        result[symbol] = dates
    return result


def sample_orders(symbol: str, order_batch_id: str, dates: list[str]) -> list[dict[str, Any]]:
    buy_quantity = 200 if symbol.startswith("688") else 100
    return [
        {
            "order_batch_id": order_batch_id,
            "request_id": f"{symbol}-buy-001",
            "trade_date": dates[0],
            "symbol": symbol,
            "side": 1,
            "quantity": buy_quantity,
        },
        {
            "order_batch_id": order_batch_id,
            "request_id": f"{symbol}-sell-001",
            "trade_date": dates[1],
            "symbol": symbol,
            "side": 2,
            "quantity": buy_quantity,
        },
    ]


def post(client: httpx.Client, path: str, payload: Any) -> Any:
    response = client.post(path, json=payload)
    if response.status_code >= 400:
        raise SystemExit(f"POST {path} failed: {response.status_code} {response.text}")
    return response.json()


def get(client: httpx.Client, path: str) -> Any:
    response = client.get(path)
    if response.status_code >= 400:
        raise SystemExit(f"GET {path} failed: {response.status_code} {response.text}")
    return response.json()


def normalize_date(value: Any) -> str:
    text = str(int(value)) if isinstance(value, (int, float)) else str(value)
    text = text.replace("-", "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def print_jsonish(payload: Any) -> None:
    import json

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
