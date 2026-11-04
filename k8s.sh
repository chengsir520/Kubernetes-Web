#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
COMMAND="${1:-start}"
if [[ "$COMMAND" == "install" ]]; then
  if [[ -e .venv ]] && ! .venv/bin/python -c 'import sys' >/dev/null 2>&1; then
    BACKUP=".venv.incompatible.$(date +%Y%m%d%H%M%S)"
    mv .venv "$BACKUP"
    echo "已将不兼容的旧虚拟环境保留为 $BACKUP"
  fi
  if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  echo "依赖安装完成。运行 ./k8s.sh start 启动面板。"
  exit 0
fi
if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import sys' >/dev/null 2>&1; then
  echo "还没有安装运行依赖。先执行：./k8s.sh install" >&2
  exit 1
fi
exec .venv/bin/python server.py "$COMMAND"
