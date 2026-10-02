#!/usr/bin/env bash
# Быстрый прогон тестов (без живого OBS). Живой E2E: scripts/test.sh --live
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "== создаю окружение .venv =="
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.14 .venv
    uv pip install --python "$PY" aiohttp websockets pytest pytest-asyncio
  else
    python3 -m venv .venv
    "$PY" -m pip install -q --upgrade pip
    "$PY" -m pip install -q aiohttp websockets pytest pytest-asyncio
  fi
fi

if [ "${1:-}" = "--live" ]; then
  shift
  exec "$PY" -m pytest -o addopts= -m e2e_live "$@"
fi

"$PY" -m pytest "$@"
