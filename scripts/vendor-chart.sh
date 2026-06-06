#!/usr/bin/env bash
# 下载 Chart.js + zoom 插件到 web/static/vendor/，让看板不依赖 CDN（离线/代理环境也能出交互图）。
# 在**能联网**的机器上运行一次即可；之后 index.html 从本地加载，SVG 回退仅作兜底。
#   用法: bash scripts/vendor-chart.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)/vortex_backtest/web/static/vendor"
mkdir -p "$DIR"

fetch() {  # $1=输出文件名  $2.. = 候选源(按序回退)
  local out="$1"; shift
  for u in "$@"; do
    if curl -fsSL --max-time 30 "$u" -o "$DIR/$out" && [ -s "$DIR/$out" ]; then
      echo "  $out  <-  $u  ($(wc -c < "$DIR/$out") bytes)"; return 0
    fi
  done
  echo "  失败: $out（所有源都不可达，检查网络/代理）" >&2; return 1
}

echo "下载到 $DIR"
fetch chart.umd.min.js \
  "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js" \
  "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.3/chart.umd.min.js" \
  "https://unpkg.com/chart.js@4.4.3/dist/chart.umd.min.js"
fetch chartjs-plugin-zoom.min.js \
  "https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-zoom/2.0.1/chartjs-plugin-zoom.min.js" \
  "https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js" \
  "https://unpkg.com/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"
echo "完成。"
