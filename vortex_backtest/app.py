from __future__ import annotations

import json
import os
import secrets
import sqlite3
import uuid
from datetime import date
from pathlib import Path

import pandas as pd
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .models import AccountCreate, AccountOut, SymbolCrosswalkOut
from .store import DataStore, normalize_account
from .symbols import crosswalk
from .market_rules import AShareRuleEngine
from .data_adapter import DEFAULT_WORKSPACE, TushareMinuteDataLoader
from .session_engine import SessionRuntime
from .session_engine import advance as session_advance
from .session_engine import finalize as session_finalize


def _workspace_dir() -> Path:
    return Path(os.getenv("VORTEX_WORKSPACE", str(DEFAULT_WORKSPACE)))


def _load_session_bars(symbols: list[str], start_d: date, end_d: date, as_of: str | None = None,
                       anchor_d: date | None = None):
    """加载会话所需富 bar。

    配了 ``VORTEX_DATA_URL`` → 走 data 取数网关（服务端按 as_of 强制 PIT，前复权固定锚 anchor_d=会话起始）；
    否则回退本地直读 ``data_adapter``（开发/离线用）。缺数据 → 返回空帧（优雅降级）。
    """
    if not symbols:
        return pd.DataFrame(), []
    if os.getenv("VORTEX_DATA_URL") and as_of:
        from .gateway_adapter import GatewayDataAdapter, GatewayDataError

        try:
            ds = GatewayDataAdapter().load(
                symbols=set(symbols), start_date=start_d, end_date=end_d,
                as_of=as_of, anchor_date=anchor_d,
            )
            return ds.minutes, ds.calendar
        except (ValueError, GatewayDataError):
            return pd.DataFrame(), []
    loader = TushareMinuteDataLoader(_workspace_dir())
    try:
        ds = loader.load(symbols=set(symbols), start_date=start_d, end_date=end_d)
    except ValueError:
        return pd.DataFrame(), []
    return ds.minutes, ds.calendar


def _append_jsonl(path: Path, rows: list) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _resolve_to(to: str | None, session_row: dict, sim_time) -> str:
    """把 advance 的 to 解析成时间戳字符串：显式时间戳 / 日期(@15:00) / 'end' / 'next_day'。"""
    end_date = session_row.get("end_date")
    if not to or to == "end":
        return f"{end_date}T15:00:00" if end_date else (sim_time.isoformat() if sim_time is not None else "")
    if to == "next_day":
        base = sim_time.date() if sim_time is not None else date.fromisoformat(str(session_row["start_date"]))
        return f"{base.isoformat()}T15:00:00"
    if len(str(to)) <= 10:  # 仅日期
        return f"{to}T15:00:00"
    return str(to)


def default_state_dir() -> Path:
    env_value = os.getenv("VORTEX_STATE") or os.getenv("VORTEX_BACKTEST_STATE_DIR")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return (Path.cwd() / ".vortex_backtest").resolve()


def require_write_auth(
    authorization: str | None = Header(default=None),
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
) -> None:
    """写接口鉴权：
    - 配了 `VORTEX_BACKTEST_TOKEN`：必须带匹配 token（`Authorization: Bearer <t>` 或 `X-Auth-Token`）。
    - 没配 token：仅本机回环放行；绑到非回环 host 时拒绝（fail-closed，避免裸暴露写接口）。
    """
    configured = os.getenv("VORTEX_BACKTEST_TOKEN")
    host = os.getenv("VORTEX_BACKTEST_HOST", "127.0.0.1")
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    if not configured:
        if is_loopback:
            return
        raise HTTPException(
            status_code=403,
            detail={
                "error": "write_disabled",
                "hint": "set VORTEX_BACKTEST_TOKEN to enable write endpoints on a non-loopback host",
            },
        )
    presented: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    presented = presented or x_auth_token
    if not presented or not secrets.compare_digest(presented, configured):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


def create_app(state_dir: Path | None = None, *, run_worker: bool = True) -> FastAPI:
    store = DataStore(state_dir or default_state_dir())
    app = FastAPI(
        title="Vortex Backtest Service",
        version="0.1.0",
        description=(
            "A 股账户级订单回放回测服务。浏览器文档："
            "设计与使用指南见 /guide，交互式 API 见 /docs(Swagger) 与 /redoc，看板见 /ui/。"
        ),
    )
    app.state.store = store
    def get_store() -> DataStore:
        return store

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/accounts", response_model=AccountOut, status_code=201)
    def create_account(
        payload: AccountCreate,
        data_store: DataStore = Depends(get_store),
        _auth: None = Depends(require_write_auth),
    ) -> dict:
        try:
            return normalize_account(data_store.create_account(payload))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="account_id already exists") from exc

    @app.get("/symbols/{symbol}", response_model=SymbolCrosswalkOut)
    def get_symbol_crosswalk(symbol: str) -> dict:
        try:
            return crosswalk(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/accounts", response_model=list[AccountOut])
    def list_accounts(data_store: DataStore = Depends(get_store)) -> list[dict]:
        return [normalize_account(row) for row in data_store.list_accounts()]

    @app.get("/accounts/{account_id}", response_model=AccountOut)
    def get_account(
        account_id: str,
        data_store: DataStore = Depends(get_store),
    ) -> dict:
        return normalize_account(_get_account_or_404(data_store, account_id))

    # ------------------------------------------------------------------
    # 会话式回测（design/18：sessions/data/advance/close）
    # ------------------------------------------------------------------

    def _session_or_404(data_store: DataStore, session_id: str) -> dict:
        try:
            return data_store.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc

    def _session_dir(data_store: DataStore, session_id: str) -> Path:
        return data_store.report_root / "sessions" / session_id

    @app.post("/sessions", status_code=201)
    def create_session(
        payload: dict = Body(...),
        data_store: DataStore = Depends(get_store),
        _auth: None = Depends(require_write_auth),
    ) -> dict:
        account_id = str(payload.get("account_id") or "")
        account = _get_account_or_404(data_store, account_id)
        level = str(payload.get("level") or "1min")
        if level not in ("daily", "1min"):
            raise HTTPException(status_code=400, detail={"error": "unsupported_level"})
        universe = [str(s) for s in (payload.get("universe") or [])]
        cfg = {
            "strategy_id": str(payload.get("strategy_id") or "session"),
            "fill_timing": str(payload.get("fill_timing") or "next_bar"),
            "default_price_type": str(payload.get("default_price_type") or "close"),
            "slippage_bps": float((payload.get("execution") or {}).get("slippage_bps", 0.0)),
        }
        session_id = str(uuid.uuid4())
        row = data_store.create_session(
            session_id=session_id, account_id=account_id, level=level,
            start_date=payload.get("start_date"), end_date=payload.get("end_date"),
            sim_time=None,  # 首个 advance 建立时钟
            initial_cash=float(account["initial_cash"]),
            universe=universe, config=cfg,
        )
        rt = SessionRuntime.hydrate(row)
        return {"session_id": session_id, "status": "open", "level": level, **rt.account_context()}

    @app.get("/sessions")
    def list_sessions(
        account_id: str | None = Query(default=None),
        data_store: DataStore = Depends(get_store),
    ) -> list[dict]:
        return data_store.list_sessions(account_id)

    @app.get("/sessions/{session_id}")
    def get_session(
        session_id: str, data_store: DataStore = Depends(get_store)
    ) -> dict:
        row = _session_or_404(data_store, session_id)
        rt = SessionRuntime.hydrate(row)
        return {"session_id": session_id, "status": row["status"], "level": row["level"], **rt.account_context()}

    @app.post("/sessions/{session_id}/advance")
    def advance_session(
        session_id: str,
        payload: dict = Body(default=None),
        data_store: DataStore = Depends(get_store),
        _auth: None = Depends(require_write_auth),
    ) -> dict:
        payload = payload or {}
        row = _session_or_404(data_store, session_id)
        if row["status"] != "open":
            raise HTTPException(status_code=409, detail={"error": "session_closed"})
        rt = SessionRuntime.hydrate(row)
        req_id = payload.get("request_id")
        if req_id and req_id in rt.processed_advances:
            # 幂等：同 request_id 已处理 → no-op，回当前状态（重试/网络重发安全，不双成交/双推进）
            return {**rt.account_context(), "filled": [], "rejected": [], "cancelled": [], "duplicate": True}
        to_ts = _resolve_to(payload.get("to"), row, rt.sim_time)
        if not to_ts:
            raise HTTPException(status_code=400, detail={"error": "missing_to_or_end_date"})
        from_d = rt.sim_time.date() if rt.sim_time is not None else date.fromisoformat(str(row["start_date"]))
        to_d = pd.Timestamp(to_ts).date()
        symbols = payload.get("set_universe") or rt.universe
        anchor_d = date.fromisoformat(str(row["start_date"])) if row.get("start_date") else from_d
        bars, _cal = _load_session_bars([str(s) for s in symbols], from_d, to_d, as_of=to_ts, anchor_d=anchor_d)
        rules = AShareRuleEngine()
        try:
            ctx = session_advance(
                rt, bars, rules=rules,
                orders=payload.get("orders") or [],
                set_universe=payload.get("set_universe"), to=to_ts,
                cancel=payload.get("cancel"),
            )
        except ValueError as exc:  # 时钟不单调
            raise HTTPException(status_code=409, detail={"error": "non_monotonic_clock", "detail": str(exc)}) from exc
        if req_id:
            rt.processed_advances.append(req_id)
        # 崩溃恢复：先更新会话行（状态权威，含 request_id 去重指纹），再追加 JSONL（日志）。
        # 崩溃落在二者之间 → 状态已正确、日志略少；request_id 已记 → 重试被去重，绝不双成交/双推进。
        data_store.update_session(session_id, **rt.dump())
        sdir = _session_dir(data_store, session_id)
        _append_jsonl(sdir / "trades.jsonl", rt.trades)
        _append_jsonl(sdir / "rejections.jsonl", rt.rejections)
        _append_jsonl(sdir / "snapshots.jsonl", rt.snapshots)
        return {**ctx, "filled": rt.trades, "rejected": rt.rejections, "cancelled": rt.last_cancelled}

    @app.post("/sessions/{session_id}/close")
    def close_session(
        session_id: str,
        data_store: DataStore = Depends(get_store),
        _auth: None = Depends(require_write_auth),
    ) -> dict:
        row = _session_or_404(data_store, session_id)
        rt = SessionRuntime.hydrate(row)
        sdir = _session_dir(data_store, session_id)
        rt.trades = _read_jsonl(sdir / "trades.jsonl")
        rt.rejections = _read_jsonl(sdir / "rejections.jsonl")
        rt.snapshots = _read_jsonl(sdir / "snapshots.jsonl")
        calendar = sorted({int(s["timestamp"][:10].replace("-", "")) for s in rt.snapshots})
        summary = session_finalize(rt, calendar)
        (sdir).mkdir(parents=True, exist_ok=True)
        (sdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, default=str), encoding="utf-8")
        data_store.update_session(session_id, status="closed")
        return {"session_id": session_id, "status": "closed", "summary": {
            k: summary[k] for k in ("total_return", "max_drawdown", "realized_pnl", "cash", "total_value")
        }}

    def _session_summary(data_store: DataStore, session_id: str) -> dict:
        """会话汇总：已 close 读 summary.json；否则按当前累积产物即时归约（读到当前 sim_time）。"""
        row = _session_or_404(data_store, session_id)
        sdir = _session_dir(data_store, session_id)
        cached = sdir / "summary.json"
        if row["status"] == "closed" and cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))
        rt = SessionRuntime.hydrate(row)
        rt.trades = _read_jsonl(sdir / "trades.jsonl")
        rt.rejections = _read_jsonl(sdir / "rejections.jsonl")
        rt.snapshots = _read_jsonl(sdir / "snapshots.jsonl")
        calendar = sorted({int(s["timestamp"][:10].replace("-", "")) for s in rt.snapshots})
        return session_finalize(rt, calendar)

    @app.get("/sessions/{session_id}/summary")
    def session_summary(session_id: str, data_store: DataStore = Depends(get_store)) -> dict:
        s = _session_summary(data_store, session_id)
        return {k: s.get(k) for k in (
            "strategy_id", "initial_cash", "cash", "market_value", "total_value",
            "total_return", "max_drawdown", "realized_pnl", "positions")}

    @app.get("/sessions/{session_id}/daily")
    def session_daily(session_id: str, data_store: DataStore = Depends(get_store)) -> list[dict]:
        return _session_summary(data_store, session_id).get("daily", [])

    @app.get("/sessions/{session_id}/trades")
    def session_trades(
        session_id: str, data_store: DataStore = Depends(get_store),
        symbol: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=5000), offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        _session_or_404(data_store, session_id)
        trades = _read_jsonl(_session_dir(data_store, session_id) / "trades.jsonl")
        if symbol:
            trades = [t for t in trades if t.get("symbol") == symbol]
        return trades[offset:offset + limit]

    @app.get("/sessions/{session_id}/rejections")
    def session_rejections(
        session_id: str, data_store: DataStore = Depends(get_store),
        limit: int = Query(default=500, ge=1, le=5000), offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        _session_or_404(data_store, session_id)
        rej = _read_jsonl(_session_dir(data_store, session_id) / "rejections.jsonl")
        return rej[offset:offset + limit]

    @app.get("/sessions/{session_id}/minutes")
    def session_minutes(
        session_id: str, data_store: DataStore = Depends(get_store),
        limit: int = Query(default=1000, ge=1, le=10000), offset: int = Query(default=0, ge=0),
    ) -> list[dict]:
        _session_or_404(data_store, session_id)
        snaps = _read_jsonl(_session_dir(data_store, session_id) / "snapshots.jsonl")
        slim = [{"timestamp": s["timestamp"], "cash": s["cash"],
                 "market_value": s["market_value"], "total_value": s["total_value"]} for s in snaps]
        return slim[offset:offset + limit]

    @app.post("/sessions/{session_id}/data")
    def session_data(
        session_id: str,
        payload: dict = Body(...),
        data_store: DataStore = Depends(get_store),
        _auth: None = Depends(require_write_auth),
    ) -> dict:
        """策略取数（design/18 §3.2）：透传到 data 网关，**服务端用会话 sim_time 当 as_of**（不信客户端时间）。

        `symbols:"universe"` 由本服务展开成显式列表再下发（data 无会话状态）。需配 `VORTEX_DATA_URL`。
        """
        row = _session_or_404(data_store, session_id)
        rt = SessionRuntime.hydrate(row)
        as_of = rt.sim_time.isoformat() if rt.sim_time is not None else f"{row['start_date']}T09:30:00"
        datasets = []
        for d in (payload.get("datasets") or []):
            d = dict(d)
            if d.get("symbols") == "universe":
                d["symbols"] = list(rt.universe)
            datasets.append(d)
        if not os.getenv("VORTEX_DATA_URL"):
            raise HTTPException(status_code=503, detail={"error": "gateway_not_configured", "hint": "set VORTEX_DATA_URL"})
        from .gateway_adapter import GatewayDataAdapter, GatewayDataError
        try:
            return GatewayDataAdapter()._query(as_of, datasets)
        except GatewayDataError as exc:
            raise HTTPException(status_code=502, detail={"error": "gateway_error", "detail": str(exc)}) from exc

    # 托管只读看板（P5 壳）：/ui/ 提供静态 SPA，/ 重定向到看板。
    web_dir = Path(__file__).resolve().parent / "web"
    if web_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(web_dir), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def dashboard_root() -> RedirectResponse:
            return RedirectResponse(url="/ui/")

    # 技术文档站：/guide 返回精修的静态 HTML（系统设计 / HTTP 接口协议 / 环境部署）。
    # 不再走 Markdown 现场渲染；交互式 API 见 FastAPI 自带的 /docs(Swagger) 与 /redoc。
    guide_html = web_dir / "guide.html"

    @app.get("/guide", include_in_schema=False)
    def guide_home() -> HTMLResponse:
        try:
            return HTMLResponse(guide_html.read_text(encoding="utf-8"))
        except OSError as exc:
            raise HTTPException(status_code=404, detail="guide not found") from exc

    return app


def _get_account_or_404(data_store: DataStore, account_id: str) -> dict:
    try:
        return data_store.get_account(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="account not found") from exc


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "vortex_backtest.app:app",
        host=os.getenv("VORTEX_BACKTEST_HOST", "127.0.0.1"),
        port=int(os.getenv("VORTEX_BACKTEST_PORT", "8767")),
        reload=os.getenv("VORTEX_BACKTEST_RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    main()
