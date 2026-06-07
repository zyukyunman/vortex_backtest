#!/usr/bin/env bash
# vortex_backtest 启动契约：组合镜像的 vortexctl backtest 会优先执行本文件。
# 不存在则回退到 vortexctl 内置默认命令——所以不加也能跑，加了则启动逻辑归本仓掌握。
set -euo pipefail

export VORTEX_BACKTEST_STATE_DIR="${VORTEX_BACKTEST_STATE_DIR:-/state}"
export VORTEX_DATA_WORKSPACE="${VORTEX_DATA_WORKSPACE:-/workspace}"
mkdir -p "$VORTEX_BACKTEST_STATE_DIR"

exec vortex-backtest serve
