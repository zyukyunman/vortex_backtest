#!/usr/bin/env python3
"""vortex_backtest 快速上手样例（端到端，自包含可跑）。

演示完整链路：建账户 → 提交外部订单 → 跑回测 → 取成交/拒单/持仓/日净值/汇总。
用**进程内 HTTP API**（FastAPI TestClient）+ 同步 `drain_jobs` 跑完，无需另起服务、无需 Docker。

运行::

    # 指向 vortex_data 的行情 workspace（loader 自动接 /data）
    export VORTEX_DATA_WORKSPACE=/Users/zyukyunman/Documents/vortex/vortex_data/workspace
    ./.venv/bin/python examples/quickstart.py

可用环境变量（都有默认值）：
    VORTEX_DATA_WORKSPACE  行情根目录（必需，默认指向本机 vortex_data/workspace）
    QS_SYMBOL              标的（默认 600000.SH）
    QS_BUY_DATE/QS_SELL_DATE  买/卖交易日（默认 2026-05-06 / 2026-05-13，须在数据窗口内）
    QS_START/QS_END        回测区间（默认 2026-05-06 ~ 2026-06-05）

生产形态（HTTP 服务 + CLI）见 docs/quickstart.md。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# ── 0. 环境：行情数据目录（必须在导入引擎前设好；loader 读 $VORTEX_DATA_WORKSPACE/data）──
DEFAULT_WS = "/Users/zyukyunman/Documents/vortex/vortex_data/workspace"
os.environ.setdefault("VORTEX_DATA_WORKSPACE", DEFAULT_WS)
os.environ.setdefault("VORTEX_INDEX_DATA_DIR", os.environ["VORTEX_DATA_WORKSPACE"] + "/data/index_daily")

from fastapi.testclient import TestClient  # noqa: E402

from vortex_backtest.app import create_app  # noqa: E402
from vortex_backtest.worker import drain_jobs  # noqa: E402

SYMBOL = os.getenv("QS_SYMBOL", "600000.SH")
BUY_DATE = os.getenv("QS_BUY_DATE", "2026-05-06")
SELL_DATE = os.getenv("QS_SELL_DATE", "2026-05-13")
START = os.getenv("QS_START", "2026-05-06")
END = os.getenv("QS_END", "2026-06-05")


def _p(title: str, obj: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    state_dir = Path(tempfile.mkdtemp(prefix="vbt_quickstart_"))
    # run_worker=False：不起后台线程，回测改用 drain_jobs 同步跑完，便于脚本里确定性拿结果
    client = TestClient(create_app(state_dir, run_worker=False))

    # ── 1. 建账户（默认引擎 replay；A 股现金账户）──────────────────────────
    r = client.post("/accounts", json={"account_id": "demo", "initial_cash": 1_000_000, "name": "快速上手"})
    assert r.status_code == 201, r.text
    _p("1) 账户已建", r.json())

    # ── 2. 提交外部订单（同一 order_batch_id 下；side 1=买 2=卖）──────────────
    orders = [
        {"request_id": "buy-1", "order_batch_id": "default", "trade_date": BUY_DATE,
         "symbol": SYMBOL, "side": 1, "quantity": 1000},                       # 按当日收盘买入 1000 股
        {"request_id": "sell-1", "order_batch_id": "default", "trade_date": SELL_DATE,
         "symbol": SYMBOL, "side": 2, "quantity": 1000},                       # 数日后卖出（T+1 满足）
    ]
    for o in orders:
        rr = client.post("/accounts/demo/orders", json=o)
        assert rr.status_code == 201, rr.text
    print(f"\n2) 已提交 {len(orders)} 笔订单（{SYMBOL}：{BUY_DATE} 买 / {SELL_DATE} 卖）")

    # ── 3. 提交回测（异步：返回 202 + job_id）然后同步跑完 ──────────────────
    r = client.post("/backtests", json={
        "account_id": "demo", "order_batch_id": "default",
        "frequency": "1min", "price_adjustment": "qfq",
        "start_date": START, "end_date": END,
    })
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    print(f"\n3) 回测已入队 job_id={job_id}，同步执行中…")
    drain_jobs(client.app.state.store)   # 生产中由后台 worker 执行；此处脚本里同步排空

    # ── 4. 取报告（汇总 / 成交 / 拒单 / 日净值）──────────────────────────────
    summary = client.get(f"/backtests/{job_id}/summary").json()
    _p("4a) 汇总（账户级）", {k: summary[k] for k in (
        "cash", "market_value", "total_value", "total_return", "max_drawdown", "realized_pnl")})
    _p("4b) 成交（含 realized_pnl / requested_quantity）", summary["trades"])
    _p("4c) 拒单（reason 为英文码；看板展示层中文化）", summary["rejections"])
    daily = summary["daily"]
    _p(f"4d) 日净值（共 {len(daily)} 个交易日，仅示首尾）", [daily[0], daily[-1]] if daily else [])

    print(f"\n✅ 完成。落盘报告在：{state_dir}/reports/{job_id}/")
    print("   - account_summary.json / trades.csv / rejections.csv / positions.csv / daily_equity.csv")
    print("   提示：与券商对账见 scripts/reconcile_statement.py（docs/quickstart.md 第 5 节）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
