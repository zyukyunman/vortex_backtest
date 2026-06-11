# vortex_backtest 可运行性验证 + 数据可用性核查 + 文档对齐 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 验证当前版本（会话式引擎 + data PIT 网关）基于 vortex_data 已落盘数据"明确可运行"；修掉端口默认值缺陷；把入口文档/脚本对齐到 sessions API 现实。

**Architecture:** 四阶段依赖序推进——Phase 0 数据实况核查（只读）→ Phase 1 端口缺陷修复（TDD）→ Phase 2 文档/脚本对齐（roundtrip 脚本重写为验收工具）→ Phase 3 端到端+pytest 验收。失败可按阶段干净归因。

**Tech Stack:** Python 3.12/3.13 venv、FastAPI/uvicorn、pyarrow/pandas、curl+bash、pytest。

**Spec:** `docs/superpowers/specs/2026-06-11-runnability-verification-design.md`

**约束:** 仅 vortex_backtest 仓可写；vortex_data / vortex_common / `~/vortex/workspace` 只读。`design/NN-*.md` 是历史记录，不改。

---

## 事实基线（计划编写时已用只读核查确认）

执行者无需重查，直接信任；与现实冲突时停下来报告：

- `app.py`(461 行) 端点全集：`GET /health`；`POST/GET /accounts`、`GET /accounts/{id}`；`GET /symbols/{symbol}`；
  `POST /sessions`、`GET /sessions`、`GET /sessions/{id}`、`POST /sessions/{id}/advance|close|data`、
  `GET /sessions/{id}/summary|daily|trades|rejections|minutes`；`/ui` 静态看板、`/guide` 静态 HTML、`/docs`、`/redoc`。
  **旧 A 面（`POST /backtests`、`POST /accounts/{id}/orders`、作业队列、worker）已删除。**
- `store.py` 只有 `accounts` + `sessions` 两张表。`models.py` 只有 `Side/EngineName/AccountCreate/AccountOut/SymbolCrosswalkOut`。
- `cli.py` 仅 `serve` 子命令；`cli.py:39` 与 `app.py:454` 默认端口 `"8767"`（缺陷；registry.yml 规范：backtest=8766，8767 属 vortex_qmt）。
- 取数双路：配 `VORTEX_DATA_URL`（+`VORTEX_DATA_DASHBOARD_TOKEN`）走网关 `POST :8765/api/v1/data`，
  RAW 价撮合 + `load_dividends` 除权日入账（N8）；不配则本地直读 `VORTEX_WORKSPACE`，qfq 前复权、不入分红（离线回退）。
- 会话委托语义（`session_engine.py::_resolve_target`）：带 `trade_date`+`exec_time` → 停泊到该日 at-or-after 该分钟的首个 bar；
  不带 `exec_time` 且 `fill_timing=next_bar`（默认）→ sim_time 后严格下一根 bar。`advance` 的 `to` 传日期(≤10字符) → 自动接 `T15:00:00`。
- `docs/usage-and-api.md` 旧实测：`stk_mins` 覆盖 20260506→20260605（23 交易日，5525 标的，`year/universe/symbol` 分区）；
  `adj_factor/stk_limit/bars/suspend_d` 同窗口。**以 Phase 0 重新实测为准。**
- `examples/run_30_day_http_sample.py` 不存在（README 引用失实）。
- `scripts/backtest_roundtrip.sh` 现版全部基于已删 A 面 + 默认 8767，需整体重写。

---

## 文件结构

| 动作 | 路径 | 职责 |
|---|---|---|
| Create | `tests/test_cli_defaults.py` | 锁定 serve 默认端口=8766 的回归测试 |
| Modify | `vortex_backtest/cli.py:39` | 默认端口 8767→8766 |
| Modify | `vortex_backtest/app.py:454` | 默认端口 8767→8766 |
| Rewrite | `scripts/backtest_roundtrip.sh` | sessions API 开闭环脚本（兼 Phase 3 验收工具） |
| Rewrite | `README.md` | 入口文档对齐 sessions 现实（含双路口径差异） |
| Modify | `CLAUDE.md` | 模块图 + 关键约定对齐 |
| Modify | `docs/usage-and-api.md`、`docs/operations.md` | 活文档对齐 |
| Create | `docs/superpowers/reports/2026-06-11-runnability-verification.md` | Phase 0 数据结论表 + Phase 3 验收结果 + 跨仓行动项 |

---

### Task 0: 提交此前已写好的 spec 与本计划

**Files:** 无新文件（spec/plan 已存在于工作区）

- [ ] **Step 0.1: 确认工作区状态**

Run: `git status --short`
Expected: 未跟踪的 `docs/superpowers/specs/...design.md`、`docs/superpowers/plans/...md`、`.claude/`（`.claude/` 不提交）

- [ ] **Step 0.2: 提交**

```bash
git add docs/superpowers/specs/2026-06-11-runnability-verification-design.md \
        docs/superpowers/plans/2026-06-11-runnability-verification.md
git commit -m "docs(backtest): 可运行性验证 spec + 实施计划"
```

---

### Task 1: Phase 0a · workspace 数据清点（只读）

**Files:** 无修改（产出记录在 Task 3 的报告里）

- [ ] **Step 1.1: 列数据集目录**

Run: `ls ~/vortex/workspace/data/`
Expected: 看到至少 `stk_mins adj_factor stk_limit suspend_d stock_st`；记录 `dividend` 与分钟 by-date 镜像（形如 `stk_mins_date`/`stk_mins_by_date`，名字以实际为准）是否存在。

- [ ] **Step 1.2: 6 依赖集 schema 与覆盖探针**

```bash
cd /Users/zyukyunman/Documents/vortex/vortex_backtest
.venv/bin/python - <<'PY'
from pathlib import Path
import pyarrow.parquet as pq
import pandas as pd

root = Path.home() / "vortex/workspace/data"
need = ["stk_mins", "stk_limit", "adj_factor", "suspend_d", "stock_st", "dividend"]
for name in need:
    p = root / name
    if not p.exists():
        print(f"{name:10s} | MISSING"); continue
    files = sorted(p.rglob("*.parquet"))
    if not files:
        print(f"{name:10s} | EMPTY(无 parquet)"); continue
    cols = pq.read_schema(files[0]).names
    if name == "stk_mins":
        sym_dirs = {q.name for q in p.rglob("symbol=*") if q.is_dir()}
        sample = pd.read_parquet(files[0])
        dcol = "trade_date" if "trade_date" in sample.columns else "date"
        print(f"{name:10s} | files={len(files)} symbols={len(sym_dirs)} cols={cols}")
        print(f"{'':10s} | 样本文件 {files[0].parent.name}: {dcol}∈[{sample[dcol].min()},{sample[dcol].max()}] rows={len(sample)}")
    else:
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        dcol = next((c for c in ("date","trade_date","end_date","ex_date") if c in df.columns), None)
        rng = f"{dcol}∈[{df[dcol].min()},{df[dcol].max()}]" if dcol else "无日期列"
        print(f"{name:10s} | files={len(files)} rows={len(df)} {rng} cols={cols}")
        if name == "dividend":
            has = {"ex_date","effective_from"} & set(df.columns)
            n_ex = int(df["ex_date"].notna().sum()) if "ex_date" in df.columns else 0
            print(f"{'':10s} | N8 关键列: 含 {sorted(has) or '无'}; 非空 ex_date 行数={n_ex}")
PY
```

Expected: 每个数据集一行结论。**关键判定**：
(a) `stk_mins` 与 `stk_limit` 覆盖窗口重叠（端到端窗口取重叠区间）；
(b) `dividend` 是否同时含 `ex_date` 与 `effective_from`（缺 → 分红入账失效，记行动项"vortex_data 重抓 dividend"，验收窗口避开除权日，不阻塞）；
(c) by-date 镜像缺失只记录（性能项，不阻塞）。
若 (a) 完全不重叠或 `stk_mins` 缺失 → **阻塞**：停止后续 Task，按报告模板记录并向用户汇报。

- [ ] **Step 1.3: 选定端到端验收参数**

从 Step 1.2 输出选定：`SYMBOL`（默认 `000001.SZ`，须在 stk_mins 分区中存在）、`BUY_DATE`（覆盖窗口首个交易日）、`SELL_DATE`（≥BUY_DATE+3 个交易日）、`START/END`（覆盖窗口）。记入报告草稿。

---

### Task 2: Phase 0b · 网关连通性探测（只读）

- [ ] **Step 2.1: 健康检查**

Run: `curl -sS http://127.0.0.1:8765/api/health`
Expected: HTTP 200 + JSON。失败 → 数据服务未起，向用户确认后再继续。

- [ ] **Step 2.2: 找 token**

Run: `grep -h "VORTEX_DATA_DASHBOARD_TOKEN" /Users/zyukyunman/Documents/vortex/vortex_data/.env 2>/dev/null; env | grep VORTEX_DATA`
Expected: 拿到 token 值，导出 `export TOKEN=<值>`。找不到 → 用 AskUserQuestion 向用户索取（阻塞项）。

- [ ] **Step 2.3: 最小取数（正路径）**

```bash
curl -sS -X POST http://127.0.0.1:8765/api/v1/data \
  -H 'Content-Type: application/json' -H "X-API-Token: ${TOKEN}" \
  -d '{"as_of":"<BUY_DATE>T10:00:00","datasets":[{"dataset":"stk_mins","symbols":["<SYMBOL>"],"window":{"count":3},"level":"1min"}]}'
```
Expected: 200，`results.stk_mins.rows` 非空，所有行 `trade_time ≤ as_of`。

- [ ] **Step 2.4: PIT 闸门负验证**

同上但 `"as_of":"<BUY_DATE>T09:00:00"`（开盘前）。
Expected: 当日 rows 为空或只含更早日期——证明闸门生效。若返回当日盘中 bar → **重大缺陷**，停下报告。

- [ ] **Step 2.5: dividend 经网关验证（仅当 Step 1.2 确认含 ex_date 列）**

```bash
curl -sS -X POST http://127.0.0.1:8765/api/v1/data \
  -H 'Content-Type: application/json' -H "X-API-Token: ${TOKEN}" \
  -d '{"as_of":"<END>T15:00:00","datasets":[{"dataset":"dividend","symbols":["<SYMBOL>"]}]}'
```
Expected: 200；缺列时 storage 行为应是优雅降级或明确错误（记录实际行为）。

---

### Task 3: Phase 0c · 数据可用性结论入报告并提交

**Files:** Create: `docs/superpowers/reports/2026-06-11-runnability-verification.md`

- [ ] **Step 3.1: 写报告骨架（数据部分先填，验收部分留待 Phase 3 回填）**

```markdown
# 2026-06-11 · 可运行性验证报告

## 1. 数据可用性结论（Phase 0 实测）
| 数据集 | 存在 | 覆盖 [min,max] | 规模 | 关键列 | 结论/缺口 |
|---|---|---|---|---|---|
（按 Task 1/2 实测填写：6 依赖集 + by-date 镜像 + 网关连通/PIT 正负验证结果）

## 2. 端到端验收参数
SYMBOL=… BUY_DATE=… SELL_DATE=… START=… END=…（选定依据：覆盖窗口）

## 3. 修复清单（Phase 1/2 完成后回填 commit hash）
## 4. 验收结果（Phase 3 回填：pytest / 网关主路 / 直读回退）
## 5. 跨仓行动项（vortex_data 侧，本 session 只读不改）
（如 dividend 缺 ex_date 需重抓、by-date 镜像未生成、macro/ths_member fail-closed 待补抓等）
```

- [ ] **Step 3.2: 提交**

```bash
git add docs/superpowers/reports/2026-06-11-runnability-verification.md
git commit -m "docs(backtest): Phase0 数据可用性实测结论"
```

---

### Task 4: Phase 1 · 默认端口 8767→8766（TDD）

**Files:**
- Test: `tests/test_cli_defaults.py`（新建）
- Modify: `vortex_backtest/cli.py:39`、`vortex_backtest/app.py:454`

- [ ] **Step 4.1: 写失败测试**

```python
"""默认端口回归锁：registry.yml 规范 backtest=8766（8767 属 vortex_qmt 实盘）。"""
from vortex_backtest.cli import build_parser


def test_serve_default_port_is_8766(monkeypatch):
    monkeypatch.delenv("VORTEX_BACKTEST_PORT", raising=False)
    args = build_parser().parse_args(["serve"])
    assert args.port == 8766


def test_serve_port_env_override(monkeypatch):
    monkeypatch.setenv("VORTEX_BACKTEST_PORT", "9999")
    args = build_parser().parse_args(["serve"])
    assert args.port == 9999
```

- [ ] **Step 4.2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_cli_defaults.py -v`
Expected: `test_serve_default_port_is_8766` FAIL（assert 8767 == 8766）

- [ ] **Step 4.3: 修两处默认值**

`vortex_backtest/cli.py:39`：`default=int(os.getenv("VORTEX_BACKTEST_PORT", "8767"))` → `"8766"`。
`vortex_backtest/app.py:454`：`port=int(os.getenv("VORTEX_BACKTEST_PORT", "8767")),` → `"8766"`。

- [ ] **Step 4.4: 跑测试确认通过 + 全仓无 8767 残留**

Run: `.venv/bin/python -m pytest tests/test_cli_defaults.py -v`
Expected: 2 PASS

Run: `grep -rn "8767" vortex_backtest/ tests/ scripts/ README.md CLAUDE.md docs/operations.md docs/usage-and-api.md | grep -v qmt`
Expected: 仅剩 `scripts/backtest_roundtrip.sh`（Task 5 重写时消掉）；design/ 不查（历史记录）。

- [ ] **Step 4.5: 提交**

```bash
git add tests/test_cli_defaults.py vortex_backtest/cli.py vortex_backtest/app.py
git commit -m "fix(backtest): serve 默认端口 8767→8766 对齐 registry.yml（8767 属 qmt），加回归测试"
```

---

### Task 5: Phase 2a · 重写 scripts/backtest_roundtrip.sh 为 sessions 流程

**Files:** Rewrite: `scripts/backtest_roundtrip.sh`（整文件替换为下面内容）

- [ ] **Step 5.1: 整文件替换**

```bash
#!/usr/bin/env bash
# =============================================================================
# vortex_backtest 会话式回测开闭环示例(纯 HTTP,仅依赖 curl + python3)
#
# 流程「建账户 → 开会话 → 买 → 卖 → 推进到期末 → 关闭 → 输出报告」:
#   ① 建账户        POST /accounts
#   ② 开会话        POST /sessions
#   ③ 买入+推进     POST /sessions/{id}/advance   (orders=[买], to=买入日)
#   ④ 卖出+推进     POST /sessions/{id}/advance   (orders=[卖], to=卖出日; T+1)
#   ⑤ 推进到期末    POST /sessions/{id}/advance   (to=end)
#   ⑥ 关闭          POST /sessions/{id}/close
#   ⑦ 输出报告      GET  /sessions/{id}/summary
#
# 用法: scripts/backtest_roundtrip.sh [选项]   (同名大写环境变量亦可)
#   --base-url URL       服务地址(默认 http://127.0.0.1:8766)
#   --token TOKEN        写接口鉴权(非回环部署必填)
#   --account-id ID      账户 id(默认 roundtrip-<时间戳>)
#   --initial-cash N     初始资金(默认 1000000)
#   --symbol CODE        标的(默认 000001.SZ)
#   --quantity N         买卖股数(默认 1000)
#   --buy-date D         买入交易日(默认 2026-05-06)
#   --sell-date D        卖出交易日(默认 2026-05-13, 须晚于买入日满足 T+1)
#   --exec-time HH:MM    盘中执行分钟(默认 09:31)
#   --start D / --end D  会话区间(默认 2026-05-06 ~ 2026-06-05)
#   -h, --help           显示本帮助
#
# 前置: 服务已起(vortex-backtest serve 或 vortex run up backtest)。
#   网关路(推荐): 服务侧配 VORTEX_DATA_URL + VORTEX_DATA_DASHBOARD_TOKEN(RAW+分红入账);
#   离线回退:     服务侧配 VORTEX_WORKSPACE 本地直读(qfq, 不入分红)。
# =============================================================================
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8766}"
TOKEN="${TOKEN:-}"
ACCOUNT_ID="${ACCOUNT_ID:-}"
INITIAL_CASH="${INITIAL_CASH:-1000000}"
SYMBOL="${SYMBOL:-000001.SZ}"
QUANTITY="${QUANTITY:-1000}"
BUY_DATE="${BUY_DATE:-2026-05-06}"
SELL_DATE="${SELL_DATE:-2026-05-13}"
START="${START:-2026-05-06}"
END="${END:-2026-06-05}"
EXEC_TIME="${EXEC_TIME:-09:31}"

usage() { awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "$0"; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)     BASE_URL="$2"; shift 2;;
    --token)        TOKEN="$2"; shift 2;;
    --account-id)   ACCOUNT_ID="$2"; shift 2;;
    --initial-cash) INITIAL_CASH="$2"; shift 2;;
    --symbol)       SYMBOL="$2"; shift 2;;
    --quantity)     QUANTITY="$2"; shift 2;;
    --buy-date)     BUY_DATE="$2"; shift 2;;
    --sell-date)    SELL_DATE="$2"; shift 2;;
    --exec-time)    EXEC_TIME="$2"; shift 2;;
    --start)        START="$2"; shift 2;;
    --end)          END="$2"; shift 2;;
    -h|--help)      usage;;
    *) echo "未知选项: $1(用 --help 看用法)" >&2; exit 2;;
  esac
done

[[ -z "$ACCOUNT_ID" ]] && ACCOUNT_ID="roundtrip-$(date +%s)"
command -v curl    >/dev/null || { echo "需要 curl"    >&2; exit 1; }
command -v python3 >/dev/null || { echo "需要 python3" >&2; exit 1; }

api() { # method path [json-body]
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "$method" "${BASE_URL}${path}" -H 'Content-Type: application/json' -w $'\n%{http_code}')
  [[ -n "$TOKEN" ]] && args+=(-H "Authorization: Bearer ${TOKEN}")
  [[ -n "$body" ]] && args+=(-d "$body")
  curl "${args[@]}"
}
http_code() { tail -n1 <<<"$1"; }
http_body() { sed '$d' <<<"$1"; }
jget() { # 顶层标量键
  python3 -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: print(""); sys.exit(0)
v = d.get(sys.argv[1]) if isinstance(d, dict) else None
print("" if v is None else v)' "$1"
}
jlen() { # 顶层数组键的长度
  python3 -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: print(0); sys.exit(0)
v = d.get(sys.argv[1]) if isinstance(d, dict) else None
print(len(v) if isinstance(v, list) else 0)' "$1"
}
die() { echo "✗ $1" >&2; exit 1; }

echo "▶ 服务: ${BASE_URL}   账户: ${ACCOUNT_ID}   标的: ${SYMBOL}   区间: ${START}~${END}"

# ---- 0. 健康检查 ----
resp="$(api GET /health || true)"
[[ "$(http_code "$resp")" == "200" ]] || die "服务不可达——先起服务(vortex-backtest serve / vortex run up backtest)"
echo "✓ 0) 健康检查通过"

# ---- ① 建账户 ----
resp="$(api POST /accounts "{\"account_id\":\"${ACCOUNT_ID}\",\"initial_cash\":${INITIAL_CASH},\"name\":\"开闭环示例\"}")"
case "$(http_code "$resp")" in
  201)     echo "✓ 1) 账户已建(初始资金 ${INITIAL_CASH})";;
  409)     echo "• 1) 账户已存在,复用 ${ACCOUNT_ID}";;
  401|403) die "建账户被拒——写接口需 --token,或服务绑了非回环地址";;
  *)       die "建账户失败(HTTP $(http_code "$resp")): $(http_body "$resp")";;
esac

# ---- ② 开会话 ----
resp="$(api POST /sessions "{\"account_id\":\"${ACCOUNT_ID}\",\"level\":\"1min\",\"start_date\":\"${START}\",\"end_date\":\"${END}\",\"universe\":[\"${SYMBOL}\"],\"strategy_id\":\"roundtrip\"}")"
[[ "$(http_code "$resp")" == "201" ]] || die "开会话失败(HTTP $(http_code "$resp")): $(http_body "$resp")"
SESSION_ID="$(http_body "$resp" | jget session_id)"
[[ -n "$SESSION_ID" ]] || die "未取到 session_id"
echo "✓ 2) 会话已开 session_id=${SESSION_ID}"

# ---- advance helper ----
adv() { # request_id to [orders_json]
  local rid="$1" to="$2" orders="${3:-[]}"
  local r; r="$(api POST "/sessions/${SESSION_ID}/advance" \
    "{\"request_id\":\"${rid}\",\"to\":\"${to}\",\"orders\":${orders}}")"
  [[ "$(http_code "$r")" == "200" ]] || die "advance(${rid}) 失败(HTTP $(http_code "$r")): $(http_body "$r")"
  http_body "$r"
}

# ---- ③ 买入 + 推进到买入日收盘 ----
orders="[{\"request_id\":\"rt-buy\",\"symbol\":\"${SYMBOL}\",\"side\":1,\"quantity\":${QUANTITY},\"trade_date\":\"${BUY_DATE}\",\"exec_time\":\"${EXEC_TIME}\"}]"
body="$(adv rt-step-buy "${BUY_DATE}" "$orders")"
n_fill="$(echo "$body" | jlen filled)"; n_rej="$(echo "$body" | jlen rejected)"
echo "✓ 3) 买入步 ${BUY_DATE}: 成交 ${n_fill} 笔 / 拒单 ${n_rej} 笔"
if [[ "$n_fill" == "0" ]]; then
  echo "  拒单详情: $(echo "$body" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin).get("rejected",[]),ensure_ascii=False))')"
  die "买入未成交——检查数据覆盖(分钟 bar/涨跌停表)与窗口"
fi

# ---- ④ 卖出 + 推进到卖出日收盘(T+1) ----
orders="[{\"request_id\":\"rt-sell\",\"symbol\":\"${SYMBOL}\",\"side\":2,\"quantity\":${QUANTITY},\"trade_date\":\"${SELL_DATE}\",\"exec_time\":\"${EXEC_TIME}\"}]"
body="$(adv rt-step-sell "${SELL_DATE}" "$orders")"
echo "✓ 4) 卖出步 ${SELL_DATE}: 成交 $(echo "$body" | jlen filled) 笔 / 拒单 $(echo "$body" | jlen rejected) 笔"

# ---- ⑤ 推进到期末 ----
body="$(adv rt-step-end end)"
echo "✓ 5) 已推进到期末 sim_time=$(echo "$body" | jget sim_time)"

# ---- ⑥ 关闭 ----
resp="$(api POST "/sessions/${SESSION_ID}/close")"
[[ "$(http_code "$resp")" == "200" ]] || die "close 失败(HTTP $(http_code "$resp")): $(http_body "$resp")"
echo "✓ 6) 会话已关闭"

# ---- ⑦ 输出报告 ----
resp="$(api GET "/sessions/${SESSION_ID}/summary")"
[[ "$(http_code "$resp")" == "200" ]] || die "取报告失败(HTTP $(http_code "$resp"))"
echo "════════════════════ 回测报告 ════════════════════"
http_body "$resp" | python3 -c '
import sys, json
s = json.load(sys.stdin)
def money(x):
    try: return format(float(x), ",.2f")
    except Exception: return str(x)
def pct(x): return "%.2f%%" % ((x or 0.0) * 100)
print("  策略        :", s.get("strategy_id"))
print("  初始资金    :", money(s.get("initial_cash")))
print("  现金        :", money(s.get("cash")))
print("  持仓市值    :", money(s.get("market_value")))
print("  总资产      :", money(s.get("total_value")))
print("  总收益率    :", pct(s.get("total_return")))
print("  最大回撤    :", pct(s.get("max_drawdown")))
print("  已实现盈亏  :", money(s.get("realized_pnl")))
print("  期末持仓数  :", len(s.get("positions") or []))
'
echo "═══════════════════════════════════════════════════"
echo "更多: ${BASE_URL}/sessions/${SESSION_ID}/daily | /trades | /rejections | /minutes"
echo "看板: ${BASE_URL}/ui/"
```

- [ ] **Step 5.2: 语法检查**

Run: `bash -n scripts/backtest_roundtrip.sh && echo SYNTAX-OK`
Expected: `SYNTAX-OK`

- [ ] **Step 5.3: 提交**

```bash
git add scripts/backtest_roundtrip.sh
git commit -m "feat(backtest): roundtrip 开闭环脚本重写为 sessions API（旧 A 面已删）"
```

---

### Task 6: Phase 2b · 重写 README.md

**Files:** Rewrite: `README.md`

- [ ] **Step 6.1: 整文件替换**

新 README 按以下结构与内容写（数据覆盖数字用 Task 1 实测值替换尖括号占位）：

```markdown
# vortex_backtest

独立 HTTP **会话式回测/账户回放**服务（A 股分钟级）。策略与服务的交互模型是
「**建会话 → 按模拟时钟逐步 advance（提交委托+推进）→ close 出报告**」，
服务端控 `sim_time`、按 `as_of` 强制 point-in-time 取数（防未来函数）。

```text
HTTP 协议层(sessions) + A 股分钟撮合/规则内核 + data 取数网关(PIT) / 本地 Parquet 回退
```

第一阶段只支持 A 股现金账户、`1min` 分钟回测、多策略独立账户。
架构/引擎选型/会话协议见 `design/18-session-backtest-engine.md`；部署见
[docs/operations.md](docs/operations.md)；交互式 API 文档见服务自带 `/docs`(Swagger)。

## 当前能力

- `POST /accounts` 建账户；`GET /accounts`、`GET /accounts/{id}` 查询
- `POST /sessions` 开会话（账户、区间、股池、撮合配置）
- `POST /sessions/{id}/advance` 提交本步委托 + 推进模拟时钟（`request_id` 幂等去重，重试不双成交）
- `POST /sessions/{id}/data` 策略取数（透传 data 网关，服务端用会话 `sim_time` 当 `as_of`）
- `POST /sessions/{id}/close` 关闭会话出最终报告
- `GET /sessions/{id}/summary|daily|trades|rejections|minutes` 报告（会话期间即可读当前累积态）
- `GET /symbols/{symbol}` Tushare/MiniQMT/Vortex 统一代码与板块规则
- `/ui` 看板、`/guide` 文档站、`/docs` Swagger

## 数据：两条取数路（口径不同，须知悉）

| 路 | 触发条件 | 撮合/估值口径 | 分红处理 | 用途 |
|---|---|---|---|---|
| **data 网关**（推荐/部署） | 配 `VORTEX_DATA_URL`（+`VORTEX_DATA_DASHBOARD_TOKEN`） | RAW 不复权真实价 | 除权日显式入账现金/送转（真实账户口径） | 生产；服务端强制 PIT |
| 本地直读（回退） | 不配 `VORTEX_DATA_URL`，配 `VORTEX_WORKSPACE` | qfq 前复权 | 不入账（已吸进前复权价） | 离线开发/调试 |

两条路的总收益近似一致（纯拆股精确等价），但现金流/持仓估值数值不同，不要混用对账。

本地直读需要 workspace 下数据集（缺关键表 → 明确报 `*_data_missing`，不伪装成功）：

| 数据集 | 用途 | 缺失行为 |
| --- | --- | --- |
| `data/stk_mins` | 1min 主行情 | `minute_data_missing` |
| `data/adj_factor` | qfq 前复权 | `adjustment_data_missing` |
| `data/stk_limit` | 涨跌停价 | `market_rules_data_missing` |
| `data/suspend_d` | 停复牌 | 缺表按无停牌处理 |
| `data/stock_st` | 历史 ST | 缺表按非 ST 处理 |

网关路另需 data 服务落盘 `dividend`（含 `ex_date` 列）供除权日入账。
当前 workspace 实测覆盖：`stk_mins` <实测窗口>（<N> 标的）。

## 安装和启动

建议 Python 3.12 或 3.13：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'

export VORTEX_WORKSPACE=$WS               # 本地直读回退用
export VORTEX_DATA_URL=http://127.0.0.1:8765        # 网关路(推荐)
export VORTEX_DATA_DASHBOARD_TOKEN=<token>
export VORTEX_STATE=$REPO/state
.venv/bin/vortex-backtest serve            # 默认 127.0.0.1:8766
curl http://127.0.0.1:8766/health
```

容器部署：`vortex run up backtest`（端口 8766）；全栈 `vortex run deploy`。
端口/变量规范以 vortex_common `config/registry.yml` + ADR-003 为准。

## 基本调用（会话式）

```bash
# 建账户
curl -X POST http://127.0.0.1:8766/accounts -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","initial_cash":1000000}'

# 开会话
curl -X POST http://127.0.0.1:8766/sessions -H 'Content-Type: application/json' \
  -d '{"account_id":"demo","level":"1min","start_date":"<START>","end_date":"<END>","universe":["000001.SZ"]}'
# → {"session_id":"...","status":"open",...}

# 买入并推进到当日收盘（to 传日期 = 推进到该日 15:00）
curl -X POST http://127.0.0.1:8766/sessions/<id>/advance -H 'Content-Type: application/json' \
  -d '{"request_id":"step1","to":"<BUY_DATE>","orders":[{"request_id":"buy-1","symbol":"000001.SZ","side":1,"quantity":1000,"trade_date":"<BUY_DATE>","exec_time":"09:31"}]}'

# 关闭出报告
curl -X POST http://127.0.0.1:8766/sessions/<id>/close
curl http://127.0.0.1:8766/sessions/<id>/summary
```

或一条命令跑完开闭环（建账户→会话→买卖→close→报告）：

```bash
scripts/backtest_roundtrip.sh --symbol 000001.SZ \
  --buy-date <BUY_DATE> --sell-date <SELL_DATE> --start <START> --end <END>
```

委托语义：带 `trade_date`+`exec_time` → 停泊到该日 at-or-after 该分钟首个 bar 成交；
不带 `exec_time` → 默认下一根 bar（`fill_timing=next_bar`，防未来）。
T+1、涨跌停、分板手数、费用由规则内核强制。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q vortex_backtest tests
```
```

注意：删除原 README 的「本地样例」章节（`examples/run_30_day_http_sample.py` 不存在）；
`compileall` 参数里去掉 `examples`（目录已无该示例；若 examples/ 目录仍存在其他文件则保留参数，以实际为准）。

- [ ] **Step 6.2: 验证无旧面残留**

Run: `grep -nE "backtests|/orders|job_id|202|order_batch_id|run_30_day" README.md; echo "exit=$?"`
Expected: `exit=1`（无匹配）

- [ ] **Step 6.3: 提交**

```bash
git add README.md
git commit -m "docs(backtest): README 对齐 sessions API 现实（旧 A 面叙述移除，写明双路口径差异）"
```

---

### Task 7: Phase 2c · CLAUDE.md 对齐

**Files:** Modify: `CLAUDE.md`

- [ ] **Step 7.1: 模块地图三行替换（Edit，old→new 精确匹配）**

1. `| \`vortex_backtest/app.py\` | FastAPI 应用：REST/JSON 端点、写接口鉴权、异步作业队列、托管 /ui 与 /guide |`
   → `| \`vortex_backtest/app.py\` | FastAPI 应用：accounts/sessions 端点、写接口鉴权、会话产物 JSONL 落盘、托管 /ui 与 /guide |`
2. `| \`vortex_backtest/models.py\` | 请求/响应/资源模型(account / order / job / report / strategy) |`
   → `| \`vortex_backtest/models.py\` | 请求/响应模型(Side / EngineName / account / symbol crosswalk) |`
3. `| \`vortex_backtest/store.py\` | SQLite 持久层(账户 / 订单 / 作业 / 报告 / strategy_meta) |`
   → `| \`vortex_backtest/store.py\` | SQLite 持久层(accounts / sessions 两表) |`

- [ ] **Step 7.2: 关键约定「回测异步」bullet 替换**

old:
```
- **回测异步**：`POST /backtests` 入队即回 `202 + job_id`，轮询 `GET /backtests/{job_id}`
  到终态(`completed/failed/cancelled/interrupted`)再取报告；崩溃重启自动重排残留作业。
```
new:
```
- **会话式回测**：`POST /sessions` 建会话 → `POST /sessions/{id}/advance` 提交委托并推进
  `sim_time`（`request_id` 幂等去重，重试不双成交）→ `POST /sessions/{id}/close` 出报告；
  产物追加 JSONL，先更新会话行再写日志，崩溃落在中间也不双推进。
```

- [ ] **Step 7.3: 项目定位段微调**

old: `接收\n**账户 + 一批外部委托(订单) + 策略配置**，基于本地分钟行情按真实 A 股规则回放这些\n委托，产出可对账的成交 / 拒单 / 持仓 / 日净值 / 汇总报告。`（以文件实际换行为准）
new: 描述会话步进——`以会话步进方式接收**账户 + 逐步提交的委托**，服务端控模拟时钟、按 as_of 强制 PIT 取数，按真实 A 股规则撮合，产出可对账的成交 / 拒单 / 持仓 / 日净值 / 汇总报告（批量订单回放 = 一次 advance 到期末的特例）。`

- [ ] **Step 7.4: 验证 + 提交**

Run: `grep -nE "backtests|job_id|202" CLAUDE.md; echo "exit=$?"`
Expected: `exit=1`

```bash
git add CLAUDE.md
git commit -m "docs(backtest): CLAUDE.md 模块图/关键约定对齐会话式现实"
```

---

### Task 8: Phase 2d · docs/usage-and-api.md 与 docs/operations.md 对齐

**Files:** Modify: `docs/usage-and-api.md`、`docs/operations.md`

- [ ] **Step 8.1: 先核实 `VORTEX_INDEX_DATA_DIR` 是否仍被代码引用**

Run: `grep -rn "VORTEX_INDEX_DATA_DIR" vortex_backtest/ tests/`
Expected: 无匹配 → 文档中该变量一并删除；有匹配 → 保留并如实描述。

- [ ] **Step 8.2: 通读两文件全文，按下列替换规则逐节改写**

替换规则（真值以 Task 6 新 README 为准）：
| 旧概念（删/换） | 新概念 |
|---|---|
| `POST /backtests` + `202+job_id` + 轮询 + worker | sessions 三步（create/advance/close）+ 同步响应 |
| `POST /accounts/{id}/orders` + `order_batch_id` 批次 | advance 的 `orders` 数组（`request_id` 幂等） |
| CLI 子命令 `account create / order add / backtest run --wait / report`（operations.md §2.1） | 命令行只剩 `serve`；操作全走 HTTP（roundtrip 脚本示例） |
| `--port 8767` / `http://127.0.0.1:8767` | `8766` |
| 「作业 job / 策略中心从作业派生 / strategy_meta」概念段 | 会话 session 概念（status open/closed、sim_time、JSONL 产物） |
| `VORTEX_DATA_WORKSPACE`（错名） | `VORTEX_WORKSPACE` |
| 数据范围实测表（usage §1） | 用 Task 1 实测值更新，并补 `dividend`（网关路用）一行 |
| usage §0 启动段 | 补网关路 env（`VORTEX_DATA_URL`/`VORTEX_DATA_DASHBOARD_TOKEN`），写明双路口径差异（一句话+指向 README 表） |

- [ ] **Step 8.3: 残留扫描**

Run: `grep -rnE "POST /backtests|/accounts/[a-zA-Z{][^ ]*/orders|job_id|backtest run|order add|account create --|8767|VORTEX_DATA_WORKSPACE" docs/usage-and-api.md docs/operations.md; echo "exit=$?"`
Expected: `exit=1`

- [ ] **Step 8.4: 提交**

```bash
git add docs/usage-and-api.md docs/operations.md
git commit -m "docs(backtest): usage/operations 活文档对齐 sessions API 与 8766 端口"
```

---

### Task 9: Phase 3a · 全量测试套件

- [ ] **Step 9.1: pytest 全量**

Run: `.venv/bin/python -m pytest -q`
Expected: 全绿（含金标 `test_golden_a_equals_b`、`test_golden_raw_vs_qfq`、对抗测试；对抗测试读 `VORTEX_WORKSPACE` env，未设则自动 skip——如需全跑先 `export VORTEX_WORKSPACE=~/vortex/workspace`）。
红 → 按 superpowers:systematic-debugging 定位；本仓问题修复后重跑；跨仓问题记入报告 §5。

- [ ] **Step 9.2: compileall**

Run: `.venv/bin/python -m compileall -q vortex_backtest tests && echo COMPILE-OK`
Expected: `COMPILE-OK`

---

### Task 10: Phase 3b · 端到端验收（网关主路）

- [ ] **Step 10.1: 起服务（独立 state，临时端口防冲突）**

```bash
cd /Users/zyukyunman/Documents/vortex/vortex_backtest
export VORTEX_WORKSPACE=~/vortex/workspace
export VORTEX_STATE=$(mktemp -d /tmp/vbt-e2e-XXXXXX)
export VORTEX_DATA_URL=http://127.0.0.1:8765
export VORTEX_DATA_DASHBOARD_TOKEN=<Task2 拿到的 token>
.venv/bin/vortex-backtest serve --port 18766 &   # 后台；规范端口 8766 可能被容器占用
sleep 2 && curl -sS http://127.0.0.1:18766/health
```
Expected: `{"status":"ok"}`

- [ ] **Step 10.2: 跑 roundtrip（参数=Task 1 选定值）**

Run: `scripts/backtest_roundtrip.sh --base-url http://127.0.0.1:18766 --symbol <SYMBOL> --buy-date <BUY_DATE> --sell-date <SELL_DATE> --start <START> --end <END>`
Expected: 7 步全 ✓；买入步成交 ≥1 笔；报告打印成功。

- [ ] **Step 10.3: 数值合理性校验**

```bash
curl -sS "http://127.0.0.1:18766/sessions/<SESSION_ID>/summary" | .venv/bin/python -c '
import sys, json
s = json.load(sys.stdin)
assert abs(s["cash"] + s["market_value"] - s["total_value"]) < 0.01, "资产恒等式破"
assert s["total_value"] > 0 and abs(s["total_return"]) < 0.5, "数值不合理"
print("SANITY-OK", {k: s[k] for k in ("cash","market_value","total_value","total_return")})'
curl -sS "http://127.0.0.1:18766/sessions/<SESSION_ID>/trades" | .venv/bin/python -c '
import sys, json
t = json.load(sys.stdin)
assert t, "无成交"
assert all(row.get("fee", 0) >= 0 for row in t), "费用为负"
print("TRADES-OK", len(t), "笔, 首笔:", {k: t[0].get(k) for k in ("symbol","side","quantity","price","fee")})'
```
Expected: `SANITY-OK` + `TRADES-OK`

- [ ] **Step 10.4: 负路径一次（数据缺失显式失败）**

Run: roundtrip 用覆盖窗口**之外**的日期（如 `--start 2025-01-02 --end 2025-01-10 --buy-date 2025-01-02 --sell-date 2025-01-06`）
Expected: 买入步 0 成交、脚本以"买入未成交"明确失败退出（非伪装成功）。

- [ ] **Step 10.5: 停服务**

Run: `kill %1`

---

### Task 11: Phase 3c · 回退路冒烟（本地直读）

- [ ] **Step 11.1: 不配网关起服务并跑最小会话**

```bash
unset VORTEX_DATA_URL VORTEX_DATA_DASHBOARD_TOKEN
export VORTEX_STATE=$(mktemp -d /tmp/vbt-fallback-XXXXXX)
.venv/bin/vortex-backtest serve --port 18768 &
sleep 2
scripts/backtest_roundtrip.sh --base-url http://127.0.0.1:18768 --symbol <SYMBOL> \
  --buy-date <BUY_DATE> --sell-date <SELL_DATE> --start <START> --end <END>
kill %1
```
Expected: 全流程 ✓（qfq 口径，数值与主路不必相等，但形状合理）。

---

### Task 12: 报告回填 + 收尾提交

**Files:** Modify: `docs/superpowers/reports/2026-06-11-runnability-verification.md`

- [ ] **Step 12.1: 回填报告 §3 修复清单（commit hash）、§4 验收结果（pytest 计数、主路/回退路输出摘要、负路径行为）、§5 跨仓行动项**

- [ ] **Step 12.2: 最终提交 + 全量回归确认**

```bash
.venv/bin/python -m pytest -q   # 最后一遍全绿确认
git add docs/superpowers/reports/2026-06-11-runnability-verification.md
git commit -m "docs(backtest): 可运行性验证报告（数据实测+端到端验收结论）"
git log --oneline -8
```

- [ ] **Step 12.3: 向用户汇报**：数据可用性结论表、修复点、验收结果、跨仓行动项（如 dividend 重抓、by-date 镜像）。

---

## 自检记录（plan self-review）

- Spec 覆盖：验收标准 1→Task 1-3；2→Task 9；3→Task 10（含负路径）+11；4→Task 5-8。阻塞处置/错误处理→Task 1.2/2.2/2.4/9.1 内嵌。范围边界（vortex_data 只读）→所有任务无 data 仓写操作。✓
- 占位符：`<SYMBOL>/<BUY_DATE>...` 为 Task 1.3 实测选定值的显式传参点（计划内定义了来源与默认值），非 TBD。✓
- 类型/命名一致性：roundtrip 脚本字段与 `app.py`/`session_engine.py` 契约逐一核对（`request_id/to/orders/side/quantity/trade_date/exec_time`、summary 键名）。✓
