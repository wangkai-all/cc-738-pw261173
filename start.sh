#!/usr/bin/env bash
# 一键本地启动（无需安装第三方依赖，Python >= 3.10；在 3.11 验证）
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
echo "启动河网卷积分析工具： http://127.0.0.1:${PORT}"
exec python3 run.py --host "${HOST}" --port "${PORT}"
