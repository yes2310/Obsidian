#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
APP_HOST_DEFAULT="0.0.0.0"
APP_PORT_DEFAULT="9002"
CHATMOCK_HOST_DEFAULT="127.0.0.1"
CHATMOCK_PORT_DEFAULT="8000"
INIT_ONLY=0
START_CHATMOCK=1

usage() {
  cat <<EOF
Usage: scripts/run.sh [options]

Options:
  --init-only       Create .env and install dependencies, then exit.
  --no-chatmock     Do not auto-start ChatMock.
  -h, --help        Show this help.

Common environment overrides:
  APP_HOST=0.0.0.0 APP_PORT=9002 scripts/run.sh
  ENV_FILE=/path/to/.env scripts/run.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --init-only)
      INIT_ONLY=1
      shift
      ;;
    --no-chatmock)
      START_CHATMOCK=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

random_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 24 | tr -d '\n'
  else
    date +%s%N | sha256sum | awk '{print $1}'
  fi
}

write_default_env() {
  local password
  password="$(random_password)"
  umask 077
  cat > "$ENV_FILE" <<EOF
APP_ENV=development
APP_ADMIN_USERNAME=yes2310
APP_ADMIN_PASSWORD=$password
APP_COOKIE_SECURE=0
DEBUG_ERRORS=1
MAX_UPLOAD_BYTES=2147483648

APP_HOST=$APP_HOST_DEFAULT
APP_PORT=$APP_PORT_DEFAULT

CHATMOCK_HOST=$CHATMOCK_HOST_DEFAULT
CHATMOCK_PORT=$CHATMOCK_PORT_DEFAULT
AUTO_START_CHATMOCK=1

LLM_BASE_URL=http://$CHATMOCK_HOST_DEFAULT:$CHATMOCK_PORT_DEFAULT/v1
LLM_API_KEY=anything
LLM_MODEL=gpt-5.4
LLM_TIMEOUT=480

WHISPER_MODEL=large-v3
WHISPER_COMPUTE=float16
WHISPER_GPU_IDS=auto

VAULT_PATH=./vault
UPLOAD_DIR=./uploads
OUTPUT_ROOT=./output
JOBS_DB_PATH=./jobs.db
EOF
  echo "Created $ENV_FILE"
  echo "Initial admin login: yes2310 / $password"
}

ensure_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    write_default_env
  fi
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  if [[ -z "${APP_ADMIN_PASSWORD:-}" || "${APP_ADMIN_PASSWORD:-}" == "replace-with-a-strong-password" ]]; then
    echo "APP_ADMIN_PASSWORD is missing or still a placeholder in $ENV_FILE" >&2
    echo "Edit $ENV_FILE and set a real password." >&2
    exit 1
  fi
}

ensure_venv() {
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
  fi
  if [[ ! -x "$VENV_DIR/bin/uvicorn" || ! -x "$VENV_DIR/bin/chatmock" ]]; then
    "$VENV_DIR/bin/python" -m pip install --upgrade pip
    "$VENV_DIR/bin/python" -m pip install -r requirements.txt
  fi
}

port_is_open() {
  local host="$1"
  local port="$2"
  "$VENV_DIR/bin/python" - "$host" "$port" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.4)
    raise SystemExit(0 if sock.connect_ex((host, port)) == 0 else 1)
PY
}

CHATMOCK_PID=""

cleanup() {
  if [[ -n "$CHATMOCK_PID" ]] && kill -0 "$CHATMOCK_PID" >/dev/null 2>&1; then
    kill "$CHATMOCK_PID" >/dev/null 2>&1 || true
    wait "$CHATMOCK_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

start_chatmock_if_needed() {
  local chatmock_host="${CHATMOCK_HOST:-$CHATMOCK_HOST_DEFAULT}"
  local chatmock_port="${CHATMOCK_PORT:-$CHATMOCK_PORT_DEFAULT}"
  local auto_start="${AUTO_START_CHATMOCK:-1}"

  if [[ "$START_CHATMOCK" != "1" || "$auto_start" != "1" ]]; then
    return
  fi

  if port_is_open "$chatmock_host" "$chatmock_port"; then
    echo "ChatMock already running at http://$chatmock_host:$chatmock_port"
    return
  fi

  mkdir -p "$LOG_DIR"
  echo "Starting ChatMock at http://$chatmock_host:$chatmock_port ..."
  "$VENV_DIR/bin/chatmock" serve --host "$chatmock_host" --port "$chatmock_port" > "$LOG_DIR/chatmock.log" 2>&1 &
  CHATMOCK_PID="$!"

  for _ in {1..30}; do
    if port_is_open "$chatmock_host" "$chatmock_port"; then
      echo "ChatMock started. Log: $LOG_DIR/chatmock.log"
      return
    fi
    sleep 0.3
  done

  echo "ChatMock did not open port $chatmock_port. Check $LOG_DIR/chatmock.log" >&2
  exit 1
}

main() {
  ensure_env
  ensure_venv
  mkdir -p uploads output vault logs

  if [[ "$INIT_ONLY" == "1" ]]; then
    echo "Initialization complete."
    exit 0
  fi

  start_chatmock_if_needed

  local app_host="${APP_HOST:-$APP_HOST_DEFAULT}"
  local app_port="${APP_PORT:-$APP_PORT_DEFAULT}"
  echo "Starting NoteCraft at http://$app_host:$app_port"
  echo "Login user: ${APP_ADMIN_USERNAME:-yes2310}"
  "$VENV_DIR/bin/uvicorn" app:app --host "$app_host" --port "$app_port"
}

main
