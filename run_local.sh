#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

SOURCES_ENV_FILE="${TC_SOURCES_ENV_FILE:-$ROOT_DIR/.tc_live_sources.env}"
if [[ -f "$SOURCES_ENV_FILE" ]]; then
  echo "Lade Datenquellen aus ${SOURCES_ENV_FILE} ..."
  set -a
  # shellcheck source=/dev/null
  . "$SOURCES_ENV_FILE"
  set +a
fi

HOST="${TC_HOST:-0.0.0.0}"
PORT="${TC_PORT:-${PORT:-8080}}"
HOST_CANDIDATES=("$HOST")
if [[ "$HOST" == "0.0.0.0" ]]; then
  HOST_CANDIDATES+=("127.0.0.1")
fi

if BIND_TARGET="$(python3 - "$PORT" "${HOST_CANDIDATES[@]}" <<'PY'
import socket
import sys

start_port = int(sys.argv[1])
hosts = sys.argv[2:] or ["0.0.0.0"]
max_scan = 50

for host in hosts:
    for offset in range(max_scan + 1):
        candidate = start_port + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, candidate))
            print(f"{host}:{candidate}")
            raise SystemExit(0)
        except OSError:
            continue

raise SystemExit(1)
PY
)"
then
  SELECTED_HOST="${BIND_TARGET%%:*}"
  SELECTED_PORT="${BIND_TARGET##*:}"
  if [[ "$SELECTED_HOST" != "$HOST" ]]; then
    echo "Host ${HOST} nicht bindbar, nutze stattdessen ${SELECTED_HOST}."
  fi
  if [[ "$SELECTED_PORT" != "$PORT" ]]; then
    echo "Port ${PORT} ist belegt, nutze stattdessen freien Port ${SELECTED_PORT}."
  else
    echo "Lokaler Port verfügbar, Server startet auf ${SELECTED_HOST}:${SELECTED_PORT} ..."
  fi
  exec env TC_HOST="$SELECTED_HOST" TC_PORT="$SELECTED_PORT" python3 src/server.py
fi

echo "Kein freier Port im Bereich ${PORT}..$((PORT + 50)). Nutze serverlosen Fallback."
python3 src/static_preview.py
echo "Im Browser öffnen: file:///tmp/tc_artikelliste_preview.html"
