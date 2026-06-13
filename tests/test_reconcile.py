"""reconcile_statement.py 会话产物适配的最小回归：从 trades.jsonl 对账券商单。"""
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_statement.py"
_spec = importlib.util.spec_from_file_location("reconcile_statement", _PATH)
recon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recon)


def _session_dir(tmp_path, trades):
    sdir = tmp_path / "sessions" / "sid-1"
    sdir.mkdir(parents=True)
    with open(sdir / "trades.jsonl", "w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t) + "\n")
    return sdir


_TRADE = {"trade_date": "2026-02-03", "symbol": "000001.SZ", "side": 1, "quantity": 1000,
          "amount": 10880.0, "commission": 5.0, "stamp_tax": 0.0, "transfer_fee": 0.1088}


def test_reconcile_session_dir_match(tmp_path):
    """会话 trades.jsonl 与对账单完全一致 → 全 OK，退出码 0。"""
    sdir = _session_dir(tmp_path, [_TRADE])
    stmt = tmp_path / "stmt.csv"
    stmt.write_text("date,symbol,side,quantity,amount,fee\n2026-02-03,000001.SZ,buy,1000,10880.0,5.1088\n",
                    encoding="utf-8")
    rc = recon.main(["--session-dir", str(sdir), "--statement", str(stmt)])
    assert rc == 0


def test_reconcile_session_dir_over_tolerance(tmp_path):
    """数量对不上 → 超差，退出码 1（CI 卡口）。"""
    sdir = _session_dir(tmp_path, [_TRADE])
    stmt = tmp_path / "stmt.csv"
    stmt.write_text("date,symbol,side,quantity,amount,fee\n2026-02-03,000001.SZ,buy,2000,21760.0,10.0\n",
                    encoding="utf-8")
    rc = recon.main(["--session-dir", str(sdir), "--statement", str(stmt)])
    assert rc == 1


def test_reconcile_missing_trades_jsonl(tmp_path):
    """会话目录缺 trades.jsonl → 明确报错（SystemExit）。"""
    stmt = tmp_path / "stmt.csv"
    stmt.write_text("date,symbol,side,quantity\n2026-02-03,000001.SZ,buy,1000\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        recon.main(["--session-dir", str(tmp_path / "nope"), "--statement", str(stmt)])
