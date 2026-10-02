#!/usr/bin/env bash
# Остановка сервиса пульта, поднятого scripts/run.sh.
#
# Идемпотентно: если сервис не запущен — просто сообщает об этом.
# Сначала SIGTERM, при отказе завершиться за ~5 с — SIGKILL. Чужие процессы
# не трогаются: pid сверяется с командной строкой сервера. Без pkill -f.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

PID_FILE="$ROOT/run/panel.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "пульт не запущен (нет run/panel.pid)"
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
  echo "процесса нет — чищу pid-файл"
  rm -f "$PID_FILE"
  exit 0
fi

# Убедиться, что это наш сервер, а не переиспользованный pid.
cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
if ! printf '%s' "$cmdline" | grep -q -- '-m server'; then
  echo "pid $pid не похож на сервер пульта — не трогаю" >&2
  exit 1
fi

echo "останавливаю пульт (pid $pid)"
kill -TERM "$pid"
for _ in $(seq 1 50); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.1
done

if kill -0 "$pid" 2>/dev/null; then
  echo "не завершился по SIGTERM — SIGKILL"
  kill -KILL "$pid" 2>/dev/null || true
  sleep 0.5
fi

rm -f "$PID_FILE"
echo "пульт остановлен"
