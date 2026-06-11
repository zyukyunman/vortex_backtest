from __future__ import annotations

import pytest

from vortex_backtest.cli import build_parser


def test_cli_parser_serve() -> None:
    parser = build_parser()
    args = parser.parse_args(["serve", "--host", "0.0.0.0", "--port", "9000"])
    assert args.command == "serve"
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert callable(args.func)


def test_cli_serve_default_port_is_8766(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VORTEX_BACKTEST_PORT", raising=False)
    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert args.port == 8766  # registry.yml 规范端口（8767 属 vortex_qmt 实盘）


@pytest.mark.parametrize(
    "removed",
    [
        ["account", "create", "--id", "demo", "--cash", "1"],
        ["order", "add", "--account", "demo"],
        ["backtest", "run", "--account", "demo"],
        ["report", "JOB-1"],
        ["symbol", "000001.SZ"],
    ],
)
def test_cli_protocol_client_subcommands_removed(removed: list[str]) -> None:
    """协议客户端子命令已下线——所有回测操作改走 HTTP 接口。"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(removed)
