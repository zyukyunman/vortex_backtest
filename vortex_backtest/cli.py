"""vortex-backtest 命令行入口。

**仅保留 `serve`**：在本机前台起 HTTP 服务。这是容器/k8s 的启动契约——
组合镜像里 common 的 `vortexctl backtest` 与本仓 `deploy/run.sh` 都调用
`vortex-backtest serve` 把服务拉起来（一进程一容器，见 vortex_common ADR-001）。

回测的全部操作（建账户 / 下单 / 提交回测 / 轮询 / 取报告）统一走 **HTTP 接口**，
不再提供命令行协议客户端。接口契约见 `design/10-api-protocol.md`，
上手见 `docs/usage-and-api.md`，端到端开闭环示例见 `scripts/backtest_roundtrip.sh`。
"""
from __future__ import annotations

import argparse
import os


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "vortex_backtest.app:app",
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vortex-backtest",
        description="Vortex 回测服务（仅起 HTTP 服务；回测操作统一走 HTTP 接口）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="在本机前台起 HTTP 服务")
    p_serve.add_argument("--host", default=os.getenv("VORTEX_BACKTEST_HOST", "127.0.0.1"))
    p_serve.add_argument(
        "--port", type=int, default=int(os.getenv("VORTEX_BACKTEST_PORT", "8767"))
    )
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
