"""serve 端口的环境变量覆盖语义（默认值 8766 的回归锁在 test_cli.py）。"""
from vortex_backtest.cli import build_parser


def test_serve_port_env_override(monkeypatch):
    monkeypatch.setenv("VORTEX_BACKTEST_PORT", "9999")
    args = build_parser().parse_args(["serve"])
    assert args.port == 9999
