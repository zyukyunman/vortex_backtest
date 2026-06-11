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
