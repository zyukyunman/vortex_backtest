from __future__ import annotations

from vortex_backtest.cli import build_parser, _side_to_int


def test_cli_parser_builds_known_commands() -> None:
    parser = build_parser()

    args = parser.parse_args(["serve", "--port", "9000"])
    assert args.command == "serve" and args.port == 9000

    args = parser.parse_args(["account", "create", "--id", "demo", "--cash", "100000"])
    assert args.id == "demo" and args.cash == 100000.0 and callable(args.func)

    args = parser.parse_args(
        [
            "order", "add", "--account", "demo", "--request-id", "buy-1",
            "--date", "2026-01-02", "--symbol", "000001.SZ", "--side", "buy", "--qty", "100",
        ]
    )
    assert args.account == "demo" and args.qty == 100 and args.side == "buy"

    args = parser.parse_args(
        ["backtest", "run", "--account", "demo", "--start", "2026-01-02", "--end", "2026-01-05", "--wait"]
    )
    assert args.account == "demo" and args.wait is True

    args = parser.parse_args(["report", "JOB-1", "--what", "daily"])
    assert args.job_id == "JOB-1" and args.what == "daily"

    args = parser.parse_args(["symbol", "000001.SZ"])
    assert args.symbol == "000001.SZ"


def test_cli_side_mapping() -> None:
    assert _side_to_int("buy") == 1
    assert _side_to_int("sell") == 2
    assert _side_to_int("1") == 1
    assert _side_to_int("2") == 2
