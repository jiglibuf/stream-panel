#!/usr/bin/env bash
# Запуск сервиса пульта в фоне.
#
#   scripts/run.sh          поднять сервис (если уже поднят — ничего не делать)
#   scripts/run.sh --stop   остановить (то же, что scripts/stop.sh)
#
# Идемпотентно: повторный запуск при живом процессе просто сообщает об этом.
# Если порт занят чужим процессом — отказ без запуска. Секреты (токен) в вывод
# не попадают: из config.json читаются только host/port.
#
# Журнал: panel.log (дописывается). PID: run/panel.pid.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "нет окружения .venv — сначала соберите его (scripts/test.sh создаёт .venv)" >&2
  exit 1
fi

RUN_DIR="$ROOT/run"
PID_FILE="$RUN_DIR/panel.pid"
LOG_FILE="$ROOT/panel.log"

if [ "${1:-}" = "--stop" ]; then
  exec "$ROOT/scripts/stop.sh"
fi

# host/port из config.json, иначе значения по умолчанию (как в server/config.py).
read -r HOST PORT < <("$PY" -c '
import json, pathlib
host, port = "0.0.0.0", 8090
try:
    cfg = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
    host = cfg.get("host") or host
    port = int(cfg.get("port") or port)
except Exception:
    pass
print(host, port)
')
[ -n "$HOST" ] || HOST="0.0.0.0"
[ -n "$PORT" ] || PORT="8090"

ours() {  # процесс с таким pid — наш сервер (а не переиспользованный pid)
  local pid="$1"
  kill -0 "$pid" 2>/dev/null || return 1
  [ -r "/proc/$pid/cmdline" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- '-m server'
}

# Уже запущен?
if [ -f "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ] && ours "$pid"; then
    echo "пульт уже запущен: pid $pid, журнал $(basename "$LOG_FILE")"
    exit 0
  fi
  echo "pid-файл устарел (процесса нет) — чищу"
  rm -f "$PID_FILE"
fi

# Порт занят посторонним процессом?
if ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; then
  echo "порт $PORT уже занят — запуск отменён" >&2
  ss -ltnp "sport = :$PORT" 2>/dev/null >&2 || true
  exit 1
fi

mkdir -p "$RUN_DIR"
nohup "$PY" -m server >>"$LOG_FILE" 2>&1 &
pid=$!
echo "$pid" > "$PID_FILE"

# Дождаться готовности порта (или падения процесса).
ready=""
for _ in $(seq 1 50); do
  if ! kill -0 "$pid" 2>/dev/null; then
    break
  fi
  if ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; then
    ready=1
    break
  fi
  sleep 0.1
done

if [ -z "$ready" ]; then
  echo "сервис не поднялся — смотри $(basename "$LOG_FILE"):" >&2
  tail -n 20 "$LOG_FILE" >&2 || true
  rm -f "$PID_FILE"
  exit 1
fi

where="$HOST"
[ "$HOST" = "0.0.0.0" ] && where="127.0.0.1"
echo "пульт поднят: http://$where:$PORT (pid $pid, журнал $(basename "$LOG_FILE"))"
