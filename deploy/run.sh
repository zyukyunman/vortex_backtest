#!/usr/bin/env bash
# vortex_backtest 启动契约：组合镜像的 vortexctl backtest 会优先执行本文件。
# 不存在则回退到 vortexctl 内置默认命令——所以不加也能跑，加了则启动逻辑归本仓掌握。
set -euo pipefail

export VORTEX_STATE="${VORTEX_STATE:-/state}"
export VORTEX_WORKSPACE="${VORTEX_WORKSPACE:-/workspace}"
mkdir -p "$VORTEX_STATE"

exec vortex-backtest serve
