#!/usr/bin/env bash
# =============================================================================
# vortex_backtest 回测开闭环示例(纯 HTTP,仅依赖 curl + python3)
#
# 演示「开始 → 提交买卖 → 结束回测 → 关闭(轮询) → 输出报告」全流程:
#   ① 建回测账户       POST /accounts
#   ② 提交买卖订单     POST /accounts/{id}/orders     (一买一卖)
#   ③ 结束/提交回测    POST /backtests                (异步,返回 job_id)
#   ④ 关闭(轮询到终态) GET  /backtests/{job_id}
#   ⑤ 输出回测报告     GET  /backtests/{job_id}/summary
#
# 用法:
#   scripts/backtest_roundtrip.sh [选项]
# 选项(也可用同名大写环境变量,如 BASE_URL / TOKEN):
#   --base-url URL       服务地址(默认 http://127.0.0.1:8767)
#   --token TOKEN        写接口鉴权(非回环部署必填;默认空=本机回环放行)
#   --account-id ID      账户 id(默认 roundtrip-<时间戳>,避免重复)
#   --initial-cash N     初始资金(默认 1000000)
#   --symbol CODE        标的(默认 600000.SH)
#   --quantity N         买卖股数(默认 1000)
#   --buy-date D         买入交易日(默认 2026-05-06)
#   --sell-date D        卖出交易日(默认 2026-05-13)
#   --exec-time HH:MM    盘中执行分钟(分钟级;默认空=按 open/close 日级成交)
#   --start D / --end D  回测区间(默认 2026-05-06 ~ 2026-06-05)
#   --poll-timeout S     轮询超时秒(默认 600)
#   -h, --help           显示本帮助
#
# 前置:服务已起(本机 `vortex-backtest serve`,或 `docker compose up -d`),
#       且服务能读到 vortex_data 导出的分钟数据(VORTEX_DATA_WORKSPACE)。
# =============================================================================
set -euo pipefail

# ---- 默认值(环境变量可覆盖,命令行优先级最高)----
BASE_URL="${BASE_URL:-http://127.0.0.1:8767}"
TOKEN="${TOKEN:-}"
ACCOUNT_ID="${ACCOUNT_ID:-}"
INITIAL_CASH="${INITIAL_CASH:-1000000}"
SYMBOL="${SYMBOL:-600000.SH}"
QUANTITY="${QUANTITY:-1000}"
BUY_DATE="${BUY_DATE:-2026-05-06}"
SELL_DATE="${SELL_DATE:-2026-05-13}"
START="${START:-2026-05-06}"
END="${END:-2026-06-05}"
BATCH="${BATCH:-roundtrip}"
POLL_TIMEOUT="${POLL_TIMEOUT:-600}"
POLL_INTERVAL="${POLL_INTERVAL:-1}"
EXEC_TIME="${EXEC_TIME:-}"

usage() { awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "$0"; exit 0; }

# ---- 参数解析 ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)      BASE_URL="$2"; shift 2;;
    --token)         TOKEN="$2"; shift 2;;
    --account-id)    ACCOUNT_ID="$2"; shift 2;;
    --initial-cash)  INITIAL_CASH="$2"; shift 2;;
    --symbol)        SYMBOL="$2"; shift 2;;
    --quantity)      QUANTITY="$2"; shift 2;;
    --buy-date)      BUY_DATE="$2"; shift 2;;
    --sell-date)     SELL_DATE="$2"; shift 2;;
    --exec-time)     EXEC_TIME="$2"; shift 2;;
    --start)         START="$2"; shift 2;;
    --end)           END="$2"; shift 2;;
    --poll-timeout)  POLL_TIMEOUT="$2"; shift 2;;
    -h|--help)       usage;;
    *) echo "未知选项: $1(用 --help 看用法)" >&2; exit 2;;
  esac
done

[[ -z "$ACCOUNT_ID" ]] && ACCOUNT_ID="roundtrip-$(date +%s)"

# ---- 依赖检查 ----
command -v curl    >/dev/null || { echo "需要 curl"    >&2; exit 1; }
command -v python3 >/dev/null || { echo "需要 python3" >&2; exit 1; }

# ---- HTTP helper:curl 输出 = 正文 + 末行 HTTP 状态码 ----
api() { # method path [json-body]
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "$method" "${BASE_URL}${path}" -H 'Content-Type: application/json' -w $'\n%{http_code}')
  [[ -n "$TOKEN" ]] && args+=(-H "Authorization: Bearer ${TOKEN}")
  [[ -n "$body" ]] && args+=(-d "$body")
  curl "${args[@]}"
}
http_code() { tail -n1 <<<"$1"; }
http_body() { sed '$d' <<<"$1"; }

# 从 stdin 的 JSON 取顶层键(标量)。用法: echo "$json" | jget status
jget() {
  python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
v = d.get(sys.argv[1]) if isinstance(d, dict) else None
print("" if v is None else v)
' "$1"
}

die() { echo "✗ $1" >&2; exit 1; }

echo "▶ 服务: ${BASE_URL}   账户: ${ACCOUNT_ID}   标的: ${SYMBOL}"

# ---- 0. 健康检查 ----
resp="$(api GET /health || true)"
[[ "$(http_code "$resp")" == "200" ]] \
  || die "服务不可达——先起服务(本机 vortex-backtest serve,或 docker compose up -d)"
echo "✓ 0) 健康检查通过"

# ---- ① 建账户 ----
resp="$(api POST /accounts \
  "{\"account_id\":\"${ACCOUNT_ID}\",\"initial_cash\":${INITIAL_CASH},\"name\":\"开闭环示例\"}")"
case "$(http_code "$resp")" in
  201)     echo "✓ 1) 账户已建(初始资金 ${INITIAL_CASH})";;
  409)     echo "• 1) 账户已存在,复用 ${ACCOUNT_ID}";;
  401|403) die "建账户被拒——写接口需 --token,或服务绑了非回环地址(见接口协议 §7)";;
  *)       die "建账户失败(HTTP $(http_code "$resp")): $(http_body "$resp")";;
esac

# ---- ② 提交买卖(side: 1=买 2=卖)----
post_order() { # request_id trade_date side label
  local rid="$1" d="$2" side="$3" label="$4"
  local extra=""; [[ -n "$EXEC_TIME" ]] && extra=",\"exec_time\":\"${EXEC_TIME}\""
  local body="{\"order_batch_id\":\"${BATCH}\",\"request_id\":\"${rid}\",\"trade_date\":\"${d}\",\"symbol\":\"${SYMBOL}\",\"side\":${side},\"quantity\":${QUANTITY}${extra}}"
  local r; r="$(api POST "/accounts/${ACCOUNT_ID}/orders" "$body")"
  case "$(http_code "$r")" in
    201) echo "  ✓ ${d} ${label} ${SYMBOL} x${QUANTITY}";;
    409) echo "  • ${rid} 已存在(幂等键命中,跳过)";;
    *)   die "下单失败(HTTP $(http_code "$r")): $(http_body "$r")";;
  esac
}
echo "▶ 2) 提交买卖订单(批次 ${BATCH})"
post_order "${BATCH}-buy"  "$BUY_DATE"  1 "买入"
post_order "${BATCH}-sell" "$SELL_DATE" 2 "卖出"

# ---- ③ 结束 = 提交回测(异步,拿 job_id)----
resp="$(api POST /backtests \
  "{\"account_id\":\"${ACCOUNT_ID}\",\"order_batch_id\":\"${BATCH}\",\"start_date\":\"${START}\",\"end_date\":\"${END}\",\"frequency\":\"1min\",\"price_adjustment\":\"qfq\"}")"
[[ "$(http_code "$resp")" == "202" ]] \
  || die "提交回测失败(HTTP $(http_code "$resp")): $(http_body "$resp")"
JOB_ID="$(http_body "$resp" | jget job_id)"
[[ -n "$JOB_ID" ]] || die "未取到 job_id"
echo "✓ 3) 回测已提交(结束并入队)job_id=${JOB_ID}"

# ---- ④ 关闭 = 轮询到终态 ----
echo -n "▶ 4) 轮询作业到终态"
deadline=$(( $(date +%s) + POLL_TIMEOUT ))
status=""
while :; do
  resp="$(api GET "/backtests/${JOB_ID}" || true)"
  status="$(http_body "$resp" | jget status)"
  case "$status" in completed|failed|cancelled|interrupted) break;; esac
  [[ $(date +%s) -ge $deadline ]] && { echo; die "轮询超时(${POLL_TIMEOUT}s),最后状态: ${status:-未知}"; }
  echo -n "."
  sleep "$POLL_INTERVAL"
done
echo " → ${status}"
if [[ "$status" != "completed" ]]; then
  err="$(http_body "$resp" | jget error)"
  die "回测未成功(status=${status}${err:+, error=${err}})"
fi
echo "✓ 4) 回测已关闭(completed)"
report_dir="$(http_body "$resp" | jget report_dir)"

# ---- ⑤ 输出报告 ----
resp="$(api GET "/backtests/${JOB_ID}/summary")"
[[ "$(http_code "$resp")" == "200" ]] || die "取报告失败(HTTP $(http_code "$resp"))"
echo "════════════════════ 回测报告 ════════════════════"
http_body "$resp" | python3 -c '
import sys, json
s = json.load(sys.stdin)
def money(x):
    try: return format(float(x), ",.2f")
    except Exception: return str(x)
def pct(x): return "%.2f%%" % ((x or 0.0) * 100)
print("  账户        :", s.get("account_id"))
print("  现金        :", money(s.get("cash")))
print("  持仓市值    :", money(s.get("market_value")))
print("  总资产      :", money(s.get("total_value")))
print("  总收益率    :", pct(s.get("total_return")))
print("  最大回撤    :", pct(s.get("max_drawdown")))
print("  已实现盈亏  :", money(s.get("realized_pnl")))
print("  成交 / 拒单 : %d / %d" % (len(s.get("trades", [])), len(s.get("rejections", []))))
print("  交易日数    :", len(s.get("daily", [])))
'
echo "═══════════════════════════════════════════════════"
[[ -n "$report_dir" ]] && echo "落盘报告目录: ${report_dir}"
echo "更多: ${BASE_URL}/backtests/${JOB_ID}/daily | /minutes | /trades | /rejections"
echo "看板: ${BASE_URL}/ui/"
