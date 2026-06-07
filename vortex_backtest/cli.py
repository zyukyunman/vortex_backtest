"""vortex-backtest 命令行。

两类子命令：
- `serve`：在本机起 HTTP 服务（等价于以前的 `vortex-backtest`）。
- 其余子命令是**协议客户端**：通过 HTTP 与运行中的服务交互（用 stdlib urllib，无额外依赖）。
  默认 base-url = $VORTEX_BACKTEST_BASE_URL 或 http://127.0.0.1:8767。

示例：
  vortex-backtest serve --host 127.0.0.1 --port 8767
  vortex-backtest account create --id demo --cash 100000
  vortex-backtest account list
  vortex-backtest order add --account demo --request-id buy-1 --date 2026-01-02 \
      --symbol 000001.SZ --side buy --qty 100 --batch batch-main --limit-price 10.50
  vortex-backtest order add --account demo --file orders.json        # 批量(JSON 数组)
  vortex-backtest backtest run --account demo --start 2026-01-02 --end 2026-01-05 \
      --batch batch-main --wait
  vortex-backtest backtest run --account demo --start 2026-01-02 --end 2026-01-05 \
      --strategies-file strategies.json --wait
  vortex-backtest backtest status <job_id>
  vortex-backtest report <job_id> --what summary|daily|trades|rejections
  vortex-backtest symbol 000001.SZ
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = os.getenv("VORTEX_BACKTEST_BASE_URL", "http://127.0.0.1:8767")
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


# ---------- HTTP 协议客户端 ----------

def request(
    method: str,
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    url = base_url.rstrip("/") + path
    if params:
        from urllib.parse import urlencode

        query = urlencode({k: v for k, v in params.items() if v is not None})
        if query:
            url = f"{url}?{query}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body
    except urllib.error.URLError as exc:
        print(f"[error] 无法连接服务 {base_url}: {exc.reason}", file=sys.stderr)
        print("提示：先 `vortex-backtest serve` 起服务，或用 --base-url 指向正确地址。", file=sys.stderr)
        raise SystemExit(2)


def emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _side_to_int(side: str) -> int:
    mapping = {"buy": 1, "sell": 2, "1": 1, "2": 2}
    if side.lower() not in mapping:
        raise SystemExit(f"side 必须是 buy/sell 或 1/2，收到 {side!r}")
    return mapping[side.lower()]


# ---------- 子命令实现 ----------

def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "vortex_backtest.app:app",
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
    )
    return 0


def cmd_account_create(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"account_id": args.id, "initial_cash": args.cash}
    if args.name:
        payload["name"] = args.name
    status, body = request("POST", args.base_url, "/accounts", payload)
    emit(body)
    return 0 if status < 400 else 1


def cmd_account_list(args: argparse.Namespace) -> int:
    status, body = request("GET", args.base_url, "/accounts")
    emit(body)
    return 0 if status < 400 else 1


def cmd_account_get(args: argparse.Namespace) -> int:
    status, body = request("GET", args.base_url, f"/accounts/{args.id}")
    emit(body)
    return 0 if status < 400 else 1


def cmd_order_add(args: argparse.Namespace) -> int:
    if args.file:
        orders = json.loads(open(args.file, encoding="utf-8").read())
        if not isinstance(orders, list):
            raise SystemExit("--file 必须是订单对象的 JSON 数组")
    else:
        required = [args.request_id, args.date, args.symbol, args.side, args.qty]
        if any(value is None for value in required):
            raise SystemExit("单条下单需要 --request-id --date --symbol --side --qty")
        order: dict[str, Any] = {
            "order_batch_id": args.batch,
            "request_id": args.request_id,
            "trade_date": args.date,
            "symbol": args.symbol,
            "side": _side_to_int(args.side),
            "quantity": args.qty,
        }
        if args.limit_price is not None:
            order["limit_price"] = args.limit_price
        orders = [order]

    failures = 0
    for order in orders:
        if "side" in order and isinstance(order["side"], str):
            order["side"] = _side_to_int(order["side"])
        status, body = request("POST", args.base_url, f"/accounts/{args.account}/orders", order)
        emit({"status": status, "result": body})
        if status >= 400:
            failures += 1
    return 0 if failures == 0 else 1


def cmd_backtest_run(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "account_id": args.account,
        "order_batch_id": args.batch,
        "frequency": args.frequency,
        "price_adjustment": args.price_adjustment,
        "start_date": args.start,
        "end_date": args.end,
    }
    if args.strategies_file:
        payload["strategies"] = json.loads(open(args.strategies_file, encoding="utf-8").read())
    status, body = request("POST", args.base_url, "/backtests", payload)
    if status >= 400:
        emit({"status": status, "error": body})
        return 1
    job_id = body["job_id"]
    if not args.wait:
        emit(body)
        return 0
    # 轮询作业直到终态（异步协议）
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        s, job = request("GET", args.base_url, f"/backtests/{job_id}")
        state = (job or {}).get("status")
        if state in TERMINAL_STATUSES:
            emit(job)
            return 0 if state == "completed" else 1
        time.sleep(args.poll_interval)
    print(f"[error] 等待作业 {job_id} 超时（{args.timeout}s）", file=sys.stderr)
    return 1


def cmd_backtest_status(args: argparse.Namespace) -> int:
    status, body = request("GET", args.base_url, f"/backtests/{args.job_id}")
    emit(body)
    return 0 if status < 400 else 1


def cmd_report(args: argparse.Namespace) -> int:
    path = {
        "summary": f"/backtests/{args.job_id}/summary",
        "daily": f"/backtests/{args.job_id}/daily",
        "trades": f"/backtests/{args.job_id}/trades",
        "rejections": f"/backtests/{args.job_id}/rejections",
    }[args.what]
    status, body = request("GET", args.base_url, path)
    emit(body)
    return 0 if status < 400 else 1


def cmd_symbol(args: argparse.Namespace) -> int:
    status, body = request("GET", args.base_url, f"/symbols/{args.symbol}")
    emit(body)
    return 0 if status < 400 else 1


# ---------- 解析器 ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vortex-backtest", description="Vortex 回测服务命令行")
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help=f"服务地址（默认 {DEFAULT_BASE_URL}）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="在本机起 HTTP 服务")
    p_serve.add_argument("--host", default=os.getenv("VORTEX_BACKTEST_HOST", "127.0.0.1"))
    p_serve.add_argument("--port", type=int, default=int(os.getenv("VORTEX_BACKTEST_PORT", "8767")))
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_account = sub.add_parser("account", help="账户")
    account_sub = p_account.add_subparsers(dest="account_command", required=True)
    a_create = account_sub.add_parser("create", help="建账户")
    a_create.add_argument("--id", required=True)
    a_create.add_argument("--cash", type=float, required=True)
    a_create.add_argument("--name")
    a_create.set_defaults(func=cmd_account_create)
    a_list = account_sub.add_parser("list", help="列账户")
    a_list.set_defaults(func=cmd_account_list)
    a_get = account_sub.add_parser("get", help="查账户")
    a_get.add_argument("--id", required=True)
    a_get.set_defaults(func=cmd_account_get)

    p_order = sub.add_parser("order", help="订单")
    order_sub = p_order.add_subparsers(dest="order_command", required=True)
    o_add = order_sub.add_parser("add", help="下单（单条用 flag，批量用 --file）")
    o_add.add_argument("--account", required=True)
    o_add.add_argument("--file", help="订单 JSON 数组文件（批量）")
    o_add.add_argument("--batch", default="default")
    o_add.add_argument("--request-id")
    o_add.add_argument("--date", help="交易日 YYYY-MM-DD")
    o_add.add_argument("--symbol")
    o_add.add_argument("--side", help="buy/sell 或 1/2")
    o_add.add_argument("--qty", type=int)
    o_add.add_argument("--limit-price", type=float, default=None)
    o_add.set_defaults(func=cmd_order_add)

    p_bt = sub.add_parser("backtest", help="回测")
    bt_sub = p_bt.add_subparsers(dest="backtest_command", required=True)
    b_run = bt_sub.add_parser("run", help="提交回测（异步；--wait 轮询到完成）")
    b_run.add_argument("--account", required=True)
    b_run.add_argument("--start", help="开始日 YYYY-MM-DD")
    b_run.add_argument("--end", help="结束日 YYYY-MM-DD")
    b_run.add_argument("--batch", default="default")
    b_run.add_argument("--frequency", default="1min")
    b_run.add_argument("--price-adjustment", default="qfq", dest="price_adjustment")
    b_run.add_argument("--strategies-file", dest="strategies_file", help="策略列表 JSON 文件")
    b_run.add_argument("--wait", action="store_true", help="轮询到作业终态")
    b_run.add_argument("--poll-interval", type=float, default=1.0, dest="poll_interval")
    b_run.add_argument("--timeout", type=float, default=600.0)
    b_run.set_defaults(func=cmd_backtest_run)
    b_status = bt_sub.add_parser("status", help="查作业状态/进度")
    b_status.add_argument("job_id")
    b_status.set_defaults(func=cmd_backtest_status)

    p_report = sub.add_parser("report", help="取回测报告（日级）")
    p_report.add_argument("job_id")
    p_report.add_argument(
        "--what", choices=["summary", "daily", "trades", "rejections"], default="summary"
    )
    p_report.set_defaults(func=cmd_report)

    p_symbol = sub.add_parser("symbol", help="代码与板块规则")
    p_symbol.add_argument("symbol")
    p_symbol.set_defaults(func=cmd_symbol)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
