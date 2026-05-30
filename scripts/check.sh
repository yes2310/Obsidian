#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Virtualenv not found. Run scripts/run.sh --init-only first." >&2
  exit 1
fi

"$VENV_DIR/bin/python" -m py_compile app.py whisper_server.py
sed -n '/<script>/,/<\/script>/p' static/dashboard.html | sed '1d;$d' | node --check -
sed -n '/<script>/,/<\/script>/p' static/login.html | sed '1d;$d' | node --check -
"$VENV_DIR/bin/python" -m pip check

tmpdir="$(mktemp -d)"
APP_ADMIN_PASSWORD='test-password-123' \
JOBS_DB_PATH="$tmpdir/jobs.db" \
UPLOAD_DIR="$tmpdir/uploads" \
OUTPUT_ROOT="$tmpdir/output" \
VAULT_PATH="$tmpdir/vault" \
STATIC_DIR="$ROOT_DIR/static" \
"$VENV_DIR/bin/python" - <<'PY'
from io import BytesIO

from fastapi.testclient import TestClient

import app


with TestClient(app.app) as client:
    assert client.get("/ui", follow_redirects=False).status_code in (307, 308)
    response = client.post(
        "/auth/login",
        json={"username": "yes2310", "password": "test-password-123"},
    )
    assert response.status_code == 200, response.text
    assert "httponly" in response.headers.get("set-cookie", "").lower()
    assert client.get("/ui").status_code == 200
    response = client.post(
        "/upload",
        files={"file": ("bad.txt", BytesIO(b"hello"), "text/plain")},
    )
    assert response.status_code == 400, response.text
    assert client.post("/process_existing", json={"path": "/etc/passwd"}).status_code == 403
    assert client.get("/download", params={"path": "/etc/passwd"}).status_code == 403
    assert client.get("/system/gpu").status_code == 200
    response = client.get("/llm/models")
    assert response.status_code == 200
    assert "base_url" not in response.json()

print("checks ok")
PY
