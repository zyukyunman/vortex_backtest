#!/usr/bin/env python3
"""
Qlib 订单回放 Spike —— 在你的环境验证 design/06 的 4 项端到端结论。

为什么需要你来跑：开发沙箱的代理封了 PyPI，装不了 qlib，所以源码级结论已在
design/06 给出，但"真实数据下的数值/性能"这一步必须在有 qlib + 数据的机器上跑。

前置：
    pip install pyqlib
    # 数据二选一：
    #   (a) 冒烟用 Qlib 自带 CN 样例：
    #       python -m qlib.run.get_data qlib_data --target_dir ~/.qlib/qlib_data/cn_data --region cn
    #   (b) 真数据：用 vortex_data 的 qlib_view 导出到某目录，--provider-uri 指过去

运行：
    python spike/qlib_replay_spike.py \
        --provider-uri ~/.qlib/qlib_data/cn_data \
        --symbol SH600000 --symbol2 SZ000001 \
        --start 2020-01-02 --end 2020-02-28 --freq day

输出：每项 [PASS]/[FAIL]/[INFO]，最后给一句"能否锁定 Qlib"的判定提示。
这是 spike 探针，不是生产代码。
"""
from __future__ import annotations

import argparse
import sys


def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--provider-uri", required=True, help="qlib 数据目录")
    p.add_argument("--symbol", default="SH600000", help="主板标的")
    p.add_argument("--symbol2", default="SZ000001", help="第二标的")
    p.add_argument("--start", default="2020-01-02")
    p.add_argument("--end", default="2020-02-28")
    p.add_argument("--freq", default="day", choices=["day", "1min"])
    p.add_argument("--cash", type=float, default=1_000_000.0)
    args = p.parse_args()

    try:
        import pandas as pd  # noqa: F401
    except ModuleNotFoundError:
        log("FAIL", "缺少 pandas —— 多半是用了系统 python，而不是项目 venv。")
        log("HINT", "用项目环境：python3.12 -m venv .venv && .venv/bin/pip install -e '.[spike]'，"
                    "然后用 .venv/bin/python 跑本脚本。")
        return 2
    try:
        import qlib
        from qlib.constant import REG_CN
        from qlib.backtest.exchange import Exchange
        from qlib.backtest.decision import Order, OrderDir
        from qlib.backtest.position import Position
    except ModuleNotFoundError as exc:
        log("FAIL", f"缺少 qlib（{getattr(exc, 'name', exc)}）。")
        log("HINT", ".venv/bin/pip install pyqlib（或 pip install -e '.[spike]'）；需 Python ≥3.11。")
        return 2
    import pandas as pd

    qlib.init(provider_uri=args.provider_uri, region=REG_CN)
    log("INFO", f"qlib initialized @ {args.provider_uri}, region=cn")

    # —— 机制检查（确定性，不依赖具体数据）——————————————————————————
    ex = Exchange(
        freq=args.freq,
        start_time=args.start,
        end_time=args.end,
        codes=[args.symbol, args.symbol2],
        deal_price="$close",
        limit_threshold=0.095,          # 真用时改为 (buy_expr, sell_expr) 或预计算 limit_buy/limit_sell
        volume_threshold=("current", "1.0*$volume"),
        open_cost=0.0003,
        close_cost=0.0008,              # 含印花税 0.0005 + 佣金 0.0003（聚合口径）
        min_cost=5.0,
        trade_unit=100,
    )

    # 手数取整：150 -> 100（factor=1 时）
    rounded = ex.round_amount_by_trade_unit(150.0, factor=1.0)
    ok = abs(rounded - 100.0) < 1e-6
    log("PASS" if ok else "FAIL", f"手数取整 150 -> {rounded} (期望 100)")
    log("INFO", "科创板 200 起+1、北交所手数：Qlib 单一 trade_unit 不覆盖 -> 需规则层预校验")

    # 费用公式：trade_cost = max(val*ratio, min_cost)
    val = 100 * 10.0
    expect_cost = max(val * 0.0003, 5.0)
    log("INFO", f"费用模型自检：100股*10元 买入成本应为 max({val*0.0003:.2f}, 5.0) = {expect_cost:.2f}")

    # —— 行为检查（依赖真实数据）——————————————————————————————————
    sym = args.symbol
    # 用区间内第一个交易时刻；day 频率下 start/end 用同一天
    t0 = pd.Timestamp(args.start)
    try:
        # 1) 买入应成交
        pos = Position(cash=args.cash)
        buy = Order(stock_id=sym, amount=1000, direction=Order.BUY, start_time=t0, end_time=t0)
        tv, tc, tp = ex.deal_order(buy, position=pos)
        filled = buy.deal_amount > 0
        log("PASS" if filled else "INFO",
            f"买入 {sym} 1000股 @ {t0.date()}: deal_amount={buy.deal_amount}, price={tp}, cost={tc}")
        if not filled:
            log("INFO", "该日不可成交（可能停牌/涨停/无数据）。换个 --start 再试。")

        # 2) T+1：同日卖出 —— 预期 Qlib 放行（证明 T+1 必须我们锁）
        if filled:
            sell = Order(stock_id=sym, amount=buy.deal_amount, direction=Order.SELL,
                         start_time=t0, end_time=t0)
            ex.deal_order(sell, position=pos)
            qlib_allows_same_day = sell.deal_amount > 0
            log("PASS" if qlib_allows_same_day else "INFO",
                f"同日卖出 deal_amount={sell.deal_amount} -> "
                f"{'Qlib 不强制 T+1（符合预期，需我们规则层冻结）' if qlib_allows_same_day else 'Qlib 拒绝（少见）'}")

        # 3) 涨跌停：扫区间找一个 limit 日
        cal = ex.quote.get_all_stock() if hasattr(ex, "quote") else []
        limited_found = False
        for d in pd.date_range(args.start, args.end, freq="D"):
            try:
                if ex.check_stock_limit(sym, d, d, direction=Order.BUY):
                    limited_found = True
                    log("PASS", f"{sym} 在 {d.date()} 触发涨停限制 -> 买入会被 deal_order 拒（deal_amount=0）")
                    break
            except Exception:  # noqa: BLE001
                continue
        if not limited_found:
            log("INFO", "区间内未发现涨停日（机制已在源码确认：check_order->deal_amount=0）。可换含涨停的区间复现。")

        # 4) NAV
        nav = pos.calculate_value()
        log("INFO", f"当前账户 NAV（持仓市值+现金）= {nav:,.2f}")
        log("INFO", "除权除息日 NAV 不假跳空：请取一个含除权日的区间，逐日打印 NAV 人工核对。")

    except Exception as exc:  # noqa: BLE001
        log("FAIL", f"行为检查异常（多半是 symbol/日期不在数据里）：{exc}")
        log("HINT", "用你数据里真实存在的 --symbol 与 --start/--end。")
        return 1

    print("\n=== 判定提示 ===")
    print("若上面 手数取整=PASS、买入成交、同日卖出被 Qlib 放行、涨停日(若有)被拒，")
    print("则 design/06 的源码级结论得到真机印证 -> 可锁定 Qlib + 薄规则层（T+1/科创手数/分项费用）。")
    print("最后人工确认：含除权日区间的逐日 NAV 无假跳空。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
