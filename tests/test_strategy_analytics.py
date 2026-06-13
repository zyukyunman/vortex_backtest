"""策略中心聚合纯函数金标单测（strategy_rollup / strategy_detail）。"""
from vortex_backtest import analytics


def _rec(strategy_id, session_id, created_at, total_return, *, account_id="acct",
         end_date="2026-03-01", status="closed"):
    """构造一条 per-run 记录（调用方在 app.py 从会话行 + summary + perf_stats 抽好的形状）。"""
    return {
        "strategy_id": strategy_id, "session_id": session_id, "account_id": account_id,
        "start_date": "2026-02-01", "end_date": end_date, "status": status,
        "created_at": created_at, "updated_at": created_at,
        "total_return": total_return, "annual_return": None, "sharpe": None,
        "volatility": None, "max_drawdown": -0.05, "n_days": 5, "low_confidence": True,
    }


def test_rollup_empty():
    assert analytics.strategy_rollup([]) == []


def test_rollup_single_run_latest_eq_best():
    rows = analytics.strategy_rollup([_rec("a", "s1", "2026-01-01T00:00:00", 0.10)])
    assert len(rows) == 1
    r = rows[0]
    assert r["strategy_id"] == "a" and r["n_runs"] == 1
    assert r["latest"]["session_id"] == "s1"
    assert r["best"]["session_id"] == "s1" and r["best"]["total_return"] == 0.10


def test_rollup_latest_by_created_at_best_by_total_return():
    # 同策略两次：s_old 收益高但更早；s_new 收益低但最新 → latest=s_new、best=s_old
    recs = [_rec("a", "s_old", "2026-01-01T00:00:00", 0.20),
            _rec("a", "s_new", "2026-02-01T00:00:00", 0.01)]
    r = analytics.strategy_rollup(recs)[0]
    assert r["n_runs"] == 2
    assert r["latest"]["session_id"] == "s_new"
    assert r["best"]["session_id"] == "s_old" and r["best"]["total_return"] == 0.20


def test_rollup_sorted_by_latest_total_return_desc():
    recs = [_rec("low", "s1", "2026-01-01T00:00:00", 0.01),
            _rec("high", "s2", "2026-01-01T00:00:00", 0.50),
            _rec("mid", "s3", "2026-01-01T00:00:00", 0.10)]
    assert [r["strategy_id"] for r in analytics.strategy_rollup(recs)] == ["high", "mid", "low"]


def test_rollup_created_at_tiebreak_by_session_id():
    # created_at 相同 → latest 取 session_id 字典序较大者
    recs = [_rec("a", "s_aaa", "2026-01-01T00:00:00", 0.30),
            _rec("a", "s_zzz", "2026-01-01T00:00:00", 0.05)]
    assert analytics.strategy_rollup(recs)[0]["latest"]["session_id"] == "s_zzz"


def test_rollup_accounts_dedup_sorted_and_window():
    recs = [_rec("a", "s1", "2026-01-01T00:00:00", 0.1, account_id="b", end_date="2026-03-01"),
            _rec("a", "s2", "2026-02-01T00:00:00", 0.2, account_id="a", end_date="2026-02-01"),
            _rec("a", "s3", "2026-03-01T00:00:00", 0.0, account_id="a", end_date="2026-04-01")]
    r = analytics.strategy_rollup(recs)[0]
    assert r["accounts"] == ["a", "b"]
    assert r["first_run"] == "2026-02-01" and r["last_run"] == "2026-04-01"


def test_rollup_window_all_end_dates_missing():
    recs = [_rec("a", "s1", "2026-01-01T00:00:00", 0.1, end_date=None)]
    r = analytics.strategy_rollup(recs)[0]
    assert r["first_run"] is None and r["last_run"] is None


def test_detail_none_when_absent():
    assert analytics.strategy_detail("nope", [_rec("a", "s1", "2026-01-01T00:00:00", 0.1)]) is None


def test_detail_runs_sorted_by_created_at_asc():
    recs = [_rec("a", "s2", "2026-02-01T00:00:00", 0.01),
            _rec("a", "s1", "2026-01-01T00:00:00", 0.20),
            _rec("b", "s9", "2026-01-01T00:00:00", 0.9)]
    d = analytics.strategy_detail("a", recs)
    assert d["strategy_id"] == "a" and d["n_runs"] == 2
    assert [run["session_id"] for run in d["runs"]] == ["s1", "s2"]   # created_at 升序
    assert d["latest"]["session_id"] == "s2" and d["best"]["session_id"] == "s1"
