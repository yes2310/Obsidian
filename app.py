import json
import hashlib
import hmac
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse


BASE_DIR = Path(__file__).resolve().parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV in {"prod", "production"}
DEBUG_ERRORS = env_bool("DEBUG_ERRORS", default=not IS_PRODUCTION)
COOKIE_SECURE = env_bool("APP_COOKIE_SECURE", default=IS_PRODUCTION)
MAX_UPLOAD_BYTES = env_int("MAX_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024)
WHISPER_SCRIPT = Path(os.environ.get("WHISPER_SCRIPT", BASE_DIR / "whisper_server.py")).resolve()
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "uploads")).resolve()
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", BASE_DIR / "output")).resolve()
VAULT_DIR = Path(os.environ.get("VAULT_PATH", BASE_DIR / "vault")).resolve()
SYSTEM_RULES_PATH = Path(os.environ.get("SYSTEM_RULES_PATH", VAULT_DIR / "SystemRules.md")).resolve()
PROMPT_PATH = Path(os.environ.get("PROMPT_PATH", BASE_DIR / "prompt_system.txt")).resolve()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", os.environ.get("CHATMOCK_BASE_URL", "http://127.0.0.1:8000/v1")).rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", os.environ.get("CHATMOCK_API_KEY", "anything"))
LLM_MODEL = os.environ.get("LLM_MODEL", os.environ.get("CHATMOCK_MODEL", "gpt-5.4"))
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "480"))
DEFAULT_LLM_MODELS = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.2",
    "gpt-5.1",
    "gpt-5",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.2-codex",
    "gpt-5-codex",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "codex-mini",
]
DB_PATH = Path(os.environ.get("JOBS_DB_PATH", BASE_DIR / "jobs.db")).resolve()
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "float16")
WHISPER_GPU_IDS = os.environ.get("WHISPER_GPU_IDS", "auto")
STATIC_DIR = Path(os.environ.get("STATIC_DIR", BASE_DIR / "static")).resolve()
DASHBOARD_PATH = STATIC_DIR / "dashboard.html"
LOGIN_PATH = STATIC_DIR / "login.html"
SESSION_COOKIE_NAME = "notecraft_session"
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(60 * 60 * 24 * 7)))
APP_ADMIN_USERNAME = os.environ.get("APP_ADMIN_USERNAME", "yes2310")
APP_ADMIN_PASSWORD = os.environ.get("APP_ADMIN_PASSWORD")

logger = logging.getLogger("notecraft")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())

for d in (UPLOAD_DIR, OUTPUT_ROOT, VAULT_DIR, STATIC_DIR):
    d.mkdir(parents=True, exist_ok=True)


app = FastAPI(
    title="NoteCraft",
    version="0.1.0",
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()
auth_lock = threading.Lock()
db_lock = threading.RLock()
SUPPORTED_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav", ".m4a"}
db_conn: Optional[sqlite3.Connection] = None


def is_supported_upload(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in SUPPORTED_EXTS


def ensure_child_path(path: Path, parent: Path, error_detail: str = "access denied") -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail=error_detail)
    return resolved


def init_db():
    global db_conn
    db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    db_conn.row_factory = sqlite3.Row
    db_conn.execute("PRAGMA journal_mode=WAL")
    db_conn.execute("PRAGMA busy_timeout=5000")
    db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            filename TEXT,
            stored_path TEXT,
            status TEXT,
            stage TEXT,
            note_path TEXT,
            output_json TEXT,
            output_txt TEXT,
            output_srt TEXT,
            error TEXT,
            created_at REAL,
            updated_at REAL
        )
        """
    )
    cols = {row[1] for row in db_conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "llm_model" not in cols:
        db_conn.execute("ALTER TABLE jobs ADD COLUMN llm_model TEXT")
        db_conn.execute("UPDATE jobs SET llm_model = ? WHERE llm_model IS NULL OR TRIM(llm_model) = ''", (LLM_MODEL,))
    db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            approved_at REAL
        )
        """
    )
    db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            last_seen REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    ensure_admin_user()
    db_conn.commit()


def load_jobs_from_db():
    with jobs_lock:
        jobs.clear()
        cur = db_conn.execute("SELECT * FROM jobs")
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            rec = dict(zip(cols, row))
            jobs[rec["id"]] = {
                "id": rec["id"],
                "filename": rec["filename"],
                "stored_path": rec["stored_path"],
                "status": rec["status"],
                "stage": rec["stage"],
                "note_path": rec["note_path"],
                "llm_model": rec.get("llm_model") or LLM_MODEL,
                "output": {
                    "json": rec["output_json"],
                    "txt": rec["output_txt"],
                    "srt": rec["output_srt"],
                },
                "error": rec["error"],
                "created_at": rec["created_at"],
                "updated_at": rec["updated_at"],
            }


def save_job_to_db(job: Dict[str, Any]):
    with db_lock:
        db_conn.execute(
            """
            INSERT OR REPLACE INTO jobs
            (id, filename, stored_path, status, stage, note_path, llm_model, output_json, output_txt, output_srt, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.get("id"),
                job.get("filename"),
                job.get("stored_path"),
                job.get("status"),
                job.get("stage"),
                job.get("note_path"),
                job.get("llm_model") or LLM_MODEL,
                job.get("output", {}).get("json") if job.get("output") else None,
                job.get("output", {}).get("txt") if job.get("output") else None,
                job.get("output", {}).get("srt") if job.get("output") else None,
                job.get("error"),
                job.get("created_at"),
                job.get("updated_at"),
            ),
        )
        db_conn.commit()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, salt, expected = stored_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return hmac.compare_digest(digest.hex(), expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "display_name": user.get("display_name") or user.get("username"),
        "role": user.get("role"),
        "status": user.get("status"),
        "created_at": user.get("created_at"),
        "approved_at": user.get("approved_at"),
    }


def ensure_admin_user():
    if not APP_ADMIN_USERNAME:
        return
    configured_password = APP_ADMIN_PASSWORD
    if not configured_password:
        raise RuntimeError("APP_ADMIN_PASSWORD must be set before starting NoteCraft")
    existing = db_conn.execute("SELECT id FROM users WHERE username = ?", (APP_ADMIN_USERNAME,)).fetchone()
    if existing:
        password_update = ", password_hash = ?"
        params = [time.time(), hash_password(configured_password)]
        params.append(APP_ADMIN_USERNAME)
        db_conn.execute(
            f"UPDATE users SET role = 'admin', status = 'active', approved_at = COALESCE(approved_at, ?){password_update} WHERE username = ?",
            tuple(params),
        )
        return
    db_conn.execute(
        """
        INSERT INTO users (id, username, display_name, password_hash, role, status, created_at, approved_at)
        VALUES (?, ?, ?, ?, 'admin', 'active', ?, ?)
        """,
        (
            uuid.uuid4().hex,
            APP_ADMIN_USERNAME,
            APP_ADMIN_USERNAME,
            hash_password(configured_password),
            time.time(),
            time.time(),
        ),
    )


def get_user_by_session_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token or db_conn is None:
        return None
    now = time.time()
    token_hash = hash_token(token)
    with db_lock:
        db_conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        row = db_conn.execute(
            """
            SELECT users.*
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ?
              AND sessions.expires_at > ?
              AND users.status = 'active'
            """,
            (token_hash, now),
        ).fetchone()
        if not row:
            db_conn.commit()
            return None
        db_conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, token_hash))
        db_conn.commit()
    return dict(row)


def current_user(request: Request) -> Optional[Dict[str, Any]]:
    return get_user_by_session_token(request.cookies.get(SESSION_COOKIE_NAME))


def require_authenticated(request: Request) -> Dict[str, Any]:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    return user


def require_admin(request: Request) -> Dict[str, Any]:
    user = require_authenticated(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin required")
    return user


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db_lock:
        db_conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen) VALUES (?, ?, ?, ?, ?)",
            (hash_token(token), user_id, now, now + SESSION_TTL_SECONDS, now),
        )
        db_conn.commit()
    return token


def clear_session(token: Optional[str]) -> None:
    if token:
        with db_lock:
            db_conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
            db_conn.commit()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public_paths = {
        "/login",
        "/auth/login",
        "/auth/register",
        "/auth/logout",
        "/auth/me",
        "/health",
        "/favicon.ico",
    }
    if path in public_paths:
        return await call_next(request)
    if current_user(request):
        return await call_next(request)
    if path in {"/", "/ui"}:
        return RedirectResponse(url="/login")
    return JSONResponse(status_code=401, content={"detail": "login required"})


def read_system_rules() -> Optional[str]:
    """Read SystemRules if available."""
    if SYSTEM_RULES_PATH.exists():
        try:
            return SYSTEM_RULES_PATH.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


def read_system_prompt() -> str:
    """Load custom system prompt from PROMPT_PATH if present, else default."""
    default_prompt = (
        "너는 한국어 강의 전사문을 학습용 요점정리 노트로 재구성하는 전문 기록자다. "
        "전사문에 없는 개념, 예시, 결론, 수치, 인명, 용어, 과제, 교수자의 의도는 절대 새로 만들지 마라. "
        "불명확한 부분은 추측하지 말고 전사 불명확 또는 확인 필요라고 표시하라. "
        "단순 bullet 목록이 아니라 강의 흐름, 핵심 개념, 비교표, 복습 질문이 있는 실제 학습 노트로 구조화하라. "
        "전문용어, 영문 약어, 공식, 모델명, 논문명, 사람 이름, 날짜, 숫자는 원문 표기를 유지하라. "
        "JSON 객체 하나만 반환하고 JSON 밖 설명이나 Markdown 코드블록은 쓰지 마라."
    )
    if PROMPT_PATH.exists():
        try:
            return PROMPT_PATH.read_text(encoding="utf-8")
        except Exception:
            return default_prompt
    return default_prompt


def normalize_llm_model(model: Optional[str]) -> str:
    selected = str(model or "").strip()
    if not selected:
        return LLM_MODEL
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", selected):
        raise ValueError("LLM 모델명은 영문, 숫자, 점, 밑줄, 하이픈, 콜론만 사용할 수 있습니다.")
    return selected


def slugify_title(title: str) -> str:
    """Convert title to hyphenated filename without spaces."""
    cleaned = re.sub(r"[^\w\uAC00-\uD7A3-]+", "-", title.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if "-" not in cleaned:
        cleaned = f"{cleaned}-note"
    return cleaned or "note"


def sanitize_filename(name: str) -> str:
    """Remove risky characters while keeping Korean/ASCII/._-"""
    p = Path(name)
    stem = re.sub(r"[^\w\uAC00-\uD7A3._-]+", "_", p.stem).strip("._-") or "file"
    ext = p.suffix
    return f"{stem}{ext}"


def make_unique_path(base_dir: Path, name: str) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(name)
    candidate = base_dir / safe_name
    if not candidate.exists():
        return candidate
    stem = Path(safe_name).stem
    ext = Path(safe_name).suffix
    counter = 1
    while True:
        candidate = base_dir / f"{stem}-{counter}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def ensure_tags(tags: List[str], category: str) -> List[str]:
    base = [t for t in tags if t]
    base = [t.strip() for t in base if t.strip()]
    if category not in base:
        base.append(category)
    if not any(t.startswith("status/") for t in base):
        base.append("status/active")
    while len(base) < 3:
        base.append("tag/auto")
    return base


def ensure_related(related: List[str]) -> List[str]:
    rel = [r for r in related if r]
    rel = [r.strip() for r in rel if r.strip()]
    return rel[:5]


CATEGORY_FOLDER = {
    "daily": "Daily",
    "study": "Study",
    "ai": "AI",
    "research": "Research",
    "project": "Projects",
    "thesis": "Thesis",
    "resources": "Resources",
    "memo": "Memo",
}


def run_whisper(video_path: Path) -> Dict[str, Any]:
    """Call whisper_server.py and return parsed outputs."""
    if not WHISPER_SCRIPT.exists():
        raise FileNotFoundError(f"whisper script not found: {WHISPER_SCRIPT}")

    cmd = [
        sys.executable,
        str(WHISPER_SCRIPT),
        str(video_path),
        "-O",
        str(OUTPUT_ROOT),
        "-m",
        WHISPER_MODEL,
        "-c",
        WHISPER_COMPUTE,
    ]
    if WHISPER_GPU_IDS and WHISPER_GPU_IDS.lower() != "auto":
        cmd.extend(["--gpu-ids", WHISPER_GPU_IDS])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Whisper failed: {summarize_process_error(result.stderr, result.stdout)}")

    stem = video_path.stem
    output_dir = OUTPUT_ROOT / stem
    json_path = output_dir / f"{stem}.json"
    txt_path = output_dir / f"{stem}.txt"
    srt_path = output_dir / f"{stem}.srt"

    if not json_path.exists():
        raise FileNotFoundError(f"Expected JSON output not found: {json_path}")

    meta = json.loads(json_path.read_text(encoding="utf-8"))
    full_text = meta.get("text", "")

    return {
        "json_path": json_path,
        "txt_path": txt_path if txt_path.exists() else None,
        "srt_path": srt_path if srt_path.exists() else None,
        "full_text": full_text,
        "meta": meta,
    }


def summarize_process_error(stderr: str, stdout: str = "") -> str:
    lines = [line.strip() for line in str(stderr or "").splitlines() if line.strip()]
    if not lines:
        lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    if not lines:
        return "process exited with an error"
    for line in reversed(lines):
        if not line.startswith("File "):
            return line[:500]
    return lines[-1][:500]


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    cleaned = str(raw or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            cleaned = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(cleaned[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def _coerce_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")).strip())
                elif "text" in item:
                    parts.append(str(item["text"]).strip())
                elif "content" in item:
                    parts.append(str(item["content"]).strip())
        return "\n".join(part for part in parts if part).strip()
    return str(content or "").strip()


def coerce_text_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        parts = [p.strip() for p in value.splitlines() if p.strip()]
        if len(parts) <= 1:
            parts = re.split(r"(?<=[.!?])\s+", value)
        return [p.strip(" -•\t") for p in parts if p and p.strip()]
    return [str(value).strip()] if str(value).strip() else []


def coerce_dict_list(value: Any, keys: List[str]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    if value is None:
        return result
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, dict):
            result.append({key: item.get(key, "") for key in keys})
        else:
            text = str(item).strip()
            if text:
                first_key = keys[0] if keys else "value"
                result.append({first_key: text})
    return result


def coerce_paragraph_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("paragraph") or item.get("body")
                if isinstance(text, list):
                    parts.extend(coerce_paragraph_list(text))
                elif str(text or "").strip():
                    parts.append(str(text).strip())
            elif str(item or "").strip():
                parts.append(str(item).strip())
        return parts
    if isinstance(value, str):
        blocks = [block.strip() for block in re.split(r"\n\s*\n", value) if block.strip()]
        if len(blocks) > 1:
            return blocks
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return lines if lines else ([value.strip()] if value.strip() else [])
    text = str(value).strip()
    return [text] if text else []


def coerce_table_list(value: Any) -> List[Dict[str, Any]]:
    tables: List[Dict[str, Any]] = []
    if value is None:
        return tables
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, dict):
            tables.append({
                "title": item.get("title") or item.get("name") or "",
                "columns": item.get("columns") or item.get("headers") or [],
                "rows": item.get("rows") or item.get("data") or [],
            })
        elif str(item or "").strip():
            tables.append({"title": "", "columns": ["내용"], "rows": [[str(item).strip()]]})
    return tables


def coerce_section_list(value: Any) -> List[Dict[str, Any]]:
    sections: List[Dict[str, Any]] = []
    if value is None:
        return sections
    items = value if isinstance(value, list) else [value]
    for idx, item in enumerate(items, 1):
        if isinstance(item, dict):
            body = (
                item.get("body")
                or item.get("paragraphs")
                or item.get("content")
                or item.get("description")
                or []
            )
            sections.append({
                "title": str(item.get("title") or item.get("heading") or f"{idx}페이지. 강의 정리").strip(),
                "intro": str(item.get("intro") or item.get("summary") or "").strip(),
                "body": coerce_paragraph_list(body),
                "tables": coerce_table_list(item.get("tables") or item.get("table")),
                "formulas": coerce_dict_list(item.get("formulas") or item.get("equations"), ["formula", "explanation"]),
                "code_blocks": coerce_dict_list(item.get("code_blocks") or item.get("codes"), ["language", "code", "explanation"]),
                "examples": coerce_dict_list(item.get("examples"), ["title", "content", "explanation"]),
                "process": item.get("process") or item.get("steps") or [],
                "takeaways": coerce_text_list(item.get("takeaways") or item.get("must_remember")),
            })
        else:
            text = str(item).strip()
            if text:
                sections.append({
                    "title": f"{idx}페이지. 강의 정리",
                    "intro": "",
                    "body": [text],
                    "tables": [],
                    "formulas": [],
                    "code_blocks": [],
                    "examples": [],
                    "process": [],
                    "takeaways": [],
                })
    return sections


def call_chatmock_summarize(text: str, rules: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
    """Summarize transcript via a ChatMock/OpenAI-compatible endpoint."""
    system_prompt = read_system_prompt()
    schema_instruction = (
        "반드시 JSON 객체 하나만 반환하라. Markdown 코드블록, 설명 문장, 내부 추론은 출력하지 마라. "
        "전사문에 없는 내용을 보충하거나 일반 지식으로 확장하지 마라. "
        "내용이 불명확하면 추측하지 말고 전사 불명확 또는 확인 필요라고 명시하라. "
        "필드는 title, scope, overview, flow, sections, concept_tables, comparison_tables, final_summary, "
        "review_questions, unclear_parts, action_items, category, tags, related, context, importance 를 사용하라. "
        "overview는 강의 전체를 3~6개 문단으로 설명하고, flow는 강의 전개 순서를 번호 목록으로 만들 수 있게 배열로 작성하라. "
        "sections는 강의 단원별 객체 배열이며 title, intro, body, tables, formulas, code_blocks, examples, process, takeaways를 포함하라. "
        "각 section의 body는 가능한 한 2개 이상의 문단형 설명으로 작성하고 한 줄 bullet 나열을 피하라. "
        "tables는 용어, 기호, 모델 비교, 단계 구분처럼 표가 자연스러운 내용에 사용하라. "
        "formulas와 code_blocks는 전사문에 수식, 계산식, 코드, 모델 구조, 계층 흐름이 있을 때만 작성하라. "
        "examples는 전사문에 나온 예시만 사용하라. concept_tables와 comparison_tables는 title, columns, rows 구조로 작성하라. "
        "final_summary는 반드시 기억할 내용을 배열로, review_questions는 question/answer 객체 5~8개로 작성하라. "
        "unclear_parts는 없으면 빈 배열로 두고, action_items는 복습할 개념이나 확인할 전사 구간 중심으로 작성하라. "
        "category는 study를 기본값으로 사용하되 ai, research, project, thesis, resources, memo, daily 중 더 적절하면 선택하라."
    )
    system_prompt = f"{system_prompt}\n\n{schema_instruction}"
    if rules:
        system_prompt = (
            f"{system_prompt}\n\n"
            "아래 Obsidian 저장 규칙을 참고하되, JSON 출력 형식과 충돌하면 JSON 형식을 우선하라.\n"
            f"{rules.strip()}"
        )

    def _parse_markdown_sections(raw: str) -> Optional[Dict[str, Any]]:
        sections = {"summary": [], "outline": [], "action_items": [], "meta": {}}
        current = None
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if low.startswith("## summary"):
                current = "summary"
                continue
            if low.startswith("## outline"):
                current = "outline"
                continue
            if low.startswith("## action"):
                current = "action_items"
                continue
            if low.startswith("## meta"):
                current = "meta"
                continue
            if current in ("summary", "outline", "action_items"):
                if line.startswith("-") or line.startswith("*"):
                    sections[current].append(line.lstrip("-* ").strip())
                else:
                    sections[current].append(line)
            elif current == "meta":
                if ":" in line:
                    k, v = line.split(":", 1)
                    sections["meta"][k.strip().lower()] = v.strip()
        if any(sections.values()):
            return {
                "summary": sections["summary"],
                "outline": sections["outline"],
                "action_items": sections["action_items"],
                "category": sections["meta"].get("category"),
                "tags": [t.strip() for t in sections["meta"].get("tags", "").split(",") if t.strip()],
                "related": [r.strip() for r in sections["meta"].get("related", "").split(",") if r.strip()],
                "context": sections["meta"].get("context"),
                "importance": sections["meta"].get("importance"),
            }
        return None

    def _normalize(data: Dict[str, Any], raw: str) -> Dict[str, Any]:
        if not isinstance(data, dict):
            data = {}
        summary = data.get("summary") or []
        outline = data.get("outline") or []
        action_items = data.get("action_items") or data.get("actions") or []
        title = data.get("title") or data.get("lecture_title") or ""
        scope = data.get("scope") or {}
        overview = data.get("overview") or data.get("lecture_overview") or ""
        flow = data.get("flow") or data.get("overall_flow") or []
        sections = data.get("sections") or data.get("lecture_sections") or data.get("pages") or []
        concept_tables = data.get("concept_tables") or data.get("concept_table") or []
        final_summary = data.get("final_summary") or data.get("must_remember") or data.get("remember") or []
        key_points = data.get("key_points") or data.get("keypoints") or []
        concepts = data.get("concepts") or data.get("terms") or []
        comparison_tables = data.get("comparison_tables") or data.get("tables") or []
        process_flow = data.get("process_flow") or []
        if not process_flow and not sections and isinstance(data.get("flow"), list):
            flow_items = data.get("flow") or []
            if any(isinstance(item, dict) and ("step" in item or "name" in item or "description" in item) for item in flow_items):
                process_flow = flow_items
                flow = []
        must_remember = data.get("must_remember") or data.get("remember") or final_summary
        review_questions = data.get("review_questions") or data.get("questions") or []
        unclear_parts = data.get("unclear_parts") or data.get("unclear") or []
        category = data.get("category") or data.get("cat") or "ai"
        tags = data.get("tags") or []
        related = data.get("related") or []
        context = data.get("context") or "Auto-generated from transcript"
        importance = data.get("importance") or "normal"

        if isinstance(action_items, str):
            action_items = [
                line.strip(" -•\t")
                for line in action_items.splitlines()
                if line.strip()
            ]
        if not isinstance(action_items, list):
            action_items = [str(action_items)]
        summary_list = coerce_text_list(summary)
        outline_list = coerce_text_list(outline)
        action_items = action_items if isinstance(action_items, list) else coerce_text_list(action_items)

        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, list):
            tags = [str(tags)]
        if isinstance(related, str):
            related = [r.strip() for r in related.split(",") if r.strip()]
        if not isinstance(related, list):
            related = [str(related)]
        if not isinstance(scope, dict):
            scope = {"topic": str(scope)}
        return {
            "title": str(title).strip(),
            "scope": {
                "range": str(scope.get("range") or scope.get("source") or "").strip(),
                "topic": str(scope.get("topic") or scope.get("subject") or "").strip(),
                "format": str(scope.get("format") or "").strip(),
            },
            "overview": "\n\n".join(coerce_paragraph_list(overview)),
            "flow": coerce_text_list(flow),
            "sections": coerce_section_list(sections),
            "concept_tables": coerce_table_list(concept_tables),
            "key_points": coerce_dict_list(key_points, ["heading", "detail", "evidence"]),
            "concepts": coerce_dict_list(concepts, ["term", "definition", "explanation", "example", "caution"]),
            "comparison_tables": coerce_dict_list(comparison_tables, ["title", "columns", "rows"]),
            "process_flow": coerce_dict_list(process_flow, ["step", "name", "description"]),
            "must_remember": coerce_text_list(must_remember),
            "final_summary": coerce_text_list(final_summary),
            "review_questions": coerce_dict_list(review_questions, ["question", "answer"]),
            "unclear_parts": coerce_text_list(unclear_parts),
            "summary": summary_list,
            "outline": outline_list,
            "action_items": action_items,
            "category": category,
            "tags": tags,
            "related": related,
            "context": context,
            "importance": importance,
            "raw_md": data.get("raw_md"),
        }

    selected_model = normalize_llm_model(model)
    payload = {
        "model": selected_model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": (
                    "다음 전사문을 Notion에 붙여넣기 쉬운 교재형 강의 요점정리 노트로 아주 자세히 구조화하라. "
                    "전체 흐름, 단원별 문단 설명, 표, 수식/코드블록, 예시, 마지막 정리를 포함하되 "
                    "전사문에 없는 내용은 절대 추가하지 말고, 원문의 용어와 수치를 보존하라.\n\n"
                    f"{text}"
                ),
            },
        ],
        "temperature": 0.1,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    }
    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
            timeout=LLM_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            "ChatMock 분석 서버에 연결할 수 없습니다. "
            "`chatmock serve` 실행 상태와 서버의 LLM_BASE_URL 설정을 확인하세요."
        ) from exc
    if resp.status_code != 200:
        raise RuntimeError(f"ChatMock HTTP {resp.status_code}: {resp.text}")
    try:
        response_payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"ChatMock returned non-JSON response: {resp.text[:300]}") from exc

    choices = response_payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"ChatMock returned no choices: {response_payload}")
    content = _coerce_message_text(choices[0].get("message", {}).get("content", ""))
    if not content:
        raise RuntimeError(f"ChatMock returned empty content: {response_payload}")

    parsed_json = _extract_json_object(content)
    if parsed_json:
        result = _normalize(parsed_json, content)
        result["llm_model"] = selected_model
        return result
    parsed = _parse_markdown_sections(content)
    if parsed:
        result = _normalize(parsed, content)
        result["llm_model"] = selected_model
        return result
    # Fallback: keep raw markdown in summary
    result = _normalize({"raw_md": content, "summary": [content]}, content)
    result["llm_model"] = selected_model
    return result


def _as_bullets(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, list):
        lines = []
        for value in item:
            if isinstance(value, dict):
                text = " / ".join(str(v).strip() for v in value.values() if str(v).strip())
            else:
                text = str(value).strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)
    return "\n".join(f"- {line.strip()}" for line in str(item).splitlines() if line.strip())


def _md_text(value: Any) -> str:
    return str(value or "").strip()


def _table_cell(value: Any) -> str:
    return _md_text(value).replace("|", "\\|").replace("\n", "<br>") or "-"


def _markdown_table(headers: List[str], rows: List[List[Any]]) -> str:
    clean_headers = [_table_cell(header) for header in headers]
    clean_rows = []
    for row in rows:
        values = list(row)
        if len(values) < len(clean_headers):
            values.extend([""] * (len(clean_headers) - len(values)))
        clean_rows.append([_table_cell(value) for value in values[:len(clean_headers)]])
    if not clean_headers or not clean_rows:
        return ""
    lines = [
        "| " + " | ".join(clean_headers) + " |",
        "| " + " | ".join(["---"] * len(clean_headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in clean_rows)
    return "\n".join(lines)


def _normalize_table_rows(columns: List[str], rows: Any) -> List[List[Any]]:
    if not isinstance(rows, list):
        return []
    normalized = []
    for row in rows:
        if isinstance(row, dict):
            normalized.append([row.get(column, "") for column in columns])
        elif isinstance(row, list):
            normalized.append(row)
        else:
            normalized.append([row])
    return normalized


def _as_numbered(item: Any) -> str:
    if item is None:
        return ""
    values = item if isinstance(item, list) else coerce_text_list(item)
    lines = []
    for idx, value in enumerate(values, 1):
        if isinstance(value, dict):
            text = " - ".join(str(v).strip() for v in value.values() if str(v).strip())
        else:
            text = str(value).strip()
        if text:
            lines.append(f"{idx}. {text}")
    return "\n".join(lines)


def _markdown_code_block(language: Any, code: Any) -> str:
    safe_language = re.sub(r"[^A-Za-z0-9_+.-]", "", str(language or "text").strip()) or "text"
    safe_code = str(code or "").strip().replace("```", "'''")
    if not safe_code:
        return ""
    return f"```{safe_language}\n{safe_code}\n```"


def _append_table_blocks(body: List[str], tables: Any, default_title: str = "표") -> None:
    for idx, table_data in enumerate(coerce_table_list(tables), 1):
        columns = coerce_text_list(table_data.get("columns"))
        rows = table_data.get("rows") or []
        if not columns or not rows:
            continue
        table = _markdown_table(columns, _normalize_table_rows(columns, rows))
        if not table:
            continue
        title = _md_text(table_data.get("title")) or f"{default_title} {idx}"
        body.extend([f"### {title}", table, ""])


def _append_formula_blocks(body: List[str], formulas: Any) -> None:
    items = coerce_dict_list(formulas, ["formula", "explanation"])
    if not items:
        return
    emitted_heading = False
    for item in items:
        formula = _markdown_code_block("text", item.get("formula"))
        explanation = _md_text(item.get("explanation"))
        if not formula and not explanation:
            continue
        if not emitted_heading:
            body.extend(["### 수식/계산 흐름", ""])
            emitted_heading = True
        if formula:
            body.extend([formula, ""])
        if explanation:
            body.extend([explanation, ""])


def _append_code_blocks(body: List[str], code_blocks: Any) -> None:
    items = coerce_dict_list(code_blocks, ["language", "code", "explanation"])
    if not items:
        return
    emitted_heading = False
    for item in items:
        code = _markdown_code_block(item.get("language") or "text", item.get("code"))
        explanation = _md_text(item.get("explanation"))
        if not code and not explanation:
            continue
        if not emitted_heading:
            body.extend(["### 코드/구현 흐름", ""])
            emitted_heading = True
        if code:
            body.extend([code, ""])
        if explanation:
            body.extend([explanation, ""])


def _append_examples(body: List[str], examples: Any) -> None:
    items = coerce_dict_list(examples, ["title", "content", "explanation"])
    if not items:
        return
    emitted_heading = False
    for idx, item in enumerate(items, 1):
        content = _md_text(item.get("content"))
        explanation = _md_text(item.get("explanation"))
        if not content and not explanation:
            continue
        if not emitted_heading:
            body.extend(["### 강의 예시", ""])
            emitted_heading = True
        title = _md_text(item.get("title")) or f"예시 {idx}"
        body.extend([f"#### {title}"])
        if content:
            body.extend([content, ""])
        if explanation:
            body.extend([f"- 의미: {explanation}", ""])


def _section_heading(title: str, idx: int) -> str:
    clean_title = title.strip() or f"{idx}페이지. 강의 정리"
    if clean_title.startswith("#"):
        return clean_title
    if "페이지" in clean_title[:12]:
        return f"# {clean_title}"
    if re.match(r"^\d+[\).]\s*", clean_title):
        return f"## {clean_title}"
    return f"## {idx}. {clean_title}"


def _append_study_sections(body: List[str], sections: Any) -> None:
    for idx, section in enumerate(coerce_section_list(sections), 1):
        title = _md_text(section.get("title")) or f"{idx}페이지. 강의 정리"
        body.extend([_section_heading(title, idx), ""])
        intro = _md_text(section.get("intro"))
        if intro:
            body.extend([intro, ""])
        for paragraph in coerce_paragraph_list(section.get("body")):
            body.extend([paragraph, ""])
        _append_table_blocks(body, section.get("tables"), default_title="정리표")
        _append_formula_blocks(body, section.get("formulas"))
        _append_code_blocks(body, section.get("code_blocks"))
        _append_examples(body, section.get("examples"))
        process_block = _as_numbered(section.get("process"))
        if process_block:
            body.extend(["### 단계 흐름", process_block, ""])
        takeaways_block = _as_bullets(section.get("takeaways"))
        if takeaways_block:
            body.extend(["### 이 단원 핵심 정리", takeaways_block, ""])


def write_note(
    title: str,
    source_path: Path,
    transcript_meta: Dict[str, Any],
    llm_result: Dict[str, Any],
) -> Path:
    """Render and save Obsidian-friendly Markdown following SystemRules."""
    category_raw = str(llm_result.get("category") or "study").lower()
    category = category_raw if category_raw in CATEGORY_FOLDER else "study"
    tags = ensure_tags(llm_result.get("tags") or [], category)
    related = ensure_related(llm_result.get("related") or [])
    note_title = _md_text(llm_result.get("title")) or title
    scope = llm_result.get("scope") or {}
    overview = _md_text(llm_result.get("overview"))
    flow = llm_result.get("flow") or []
    sections = llm_result.get("sections") or []
    concept_tables = llm_result.get("concept_tables") or []
    final_summary = llm_result.get("final_summary") or []
    key_points = llm_result.get("key_points") or []
    concepts = llm_result.get("concepts") or []
    comparison_tables = llm_result.get("comparison_tables") or []
    process_flow = llm_result.get("process_flow") or []
    must_remember = llm_result.get("must_remember") or []
    review_questions = llm_result.get("review_questions") or []
    unclear_parts = llm_result.get("unclear_parts") or []
    summary_list = llm_result.get("summary") or []
    outline_list = llm_result.get("outline") or []
    action_list = llm_result.get("action_items") or []
    summary_block = _as_bullets(summary_list)
    outline_block = _as_bullets(outline_list)
    action_block = _as_bullets(action_list)
    context = llm_result.get("context") or f"Auto-generated from transcript {source_path.name}"
    importance = llm_result.get("importance") or "normal"
    llm_model = llm_result.get("llm_model") or LLM_MODEL

    txt_path = transcript_meta.get("txt_path")
    srt_path = transcript_meta.get("srt_path")
    json_path = transcript_meta.get("json_path")

    now = time.strftime("%Y-%m-%d %H:%M")
    file_slug = slugify_title(title)
    note_id = f"{category}-{file_slug}-{uuid.uuid4().hex[:6]}"
    summary_meta_src = [overview] if overview else (summary_list if isinstance(summary_list, list) else [])
    if not summary_meta_src and summary_block:
        summary_meta_src = [line.strip("- ").strip() for line in summary_block.splitlines() if line.strip()]
    summary_meta = " ".join(str(item) for item in summary_meta_src[:3]) if summary_meta_src else (summary_block or "- (empty)")
    summary_clean = summary_meta.replace("\"", "").replace("\n", " ")
    context_clean = str(context).replace("\"", "").replace("\n", " ")

    # YAML metadata
    meta_lines = [
        "---",
        f"category: {category}",
        f"llm_model: {llm_model}",
        f"related: [{', '.join(related)}]",
        f"summary: \"{summary_clean[:200]}\"",
        "---",
    ]

    raw_md = llm_result.get("raw_md")
    if raw_md:
        body = [f"# {note_title}", "", raw_md.strip(), "", "## 원문 파일", f"- txt: {txt_path}" if txt_path else "",
                f"- srt: {srt_path}" if srt_path else "", f"- json: {json_path}" if json_path else ""]
    else:
        body = [f"# {note_title}", ""]

        if isinstance(scope, dict):
            scope_lines = []
            if _md_text(scope.get("range")):
                scope_lines.append(f"> **범위**: {_md_text(scope.get('range'))}")
            if _md_text(scope.get("topic")):
                scope_lines.append(f"> **주제**: {_md_text(scope.get('topic'))}")
            if _md_text(scope.get("format")):
                scope_lines.append(f"> **형식**: {_md_text(scope.get('format'))}")
            if scope_lines:
                body.extend(scope_lines + ["", "---", ""])

        if overview or flow:
            body.append("## 전체 흐름 한눈에 보기")
            if overview:
                for paragraph in coerce_paragraph_list(overview):
                    body.extend([paragraph, ""])
            flow_block = _as_numbered(flow)
            if flow_block:
                body.extend(["### 강의 전개", flow_block, ""])
        elif summary_block:
            body.extend(["## 전체 흐름 한눈에 보기", summary_block, ""])

        if sections:
            _append_study_sections(body, sections)
        elif key_points:
            body.append("## 핵심 요점")
            for idx, point in enumerate(key_points, 1):
                if isinstance(point, dict):
                    heading = _md_text(point.get("heading")) or f"요점 {idx}"
                    detail = _md_text(point.get("detail"))
                    evidence = _md_text(point.get("evidence"))
                    body.extend([f"### {idx}. {heading}", detail or "-", f"- 근거: {evidence}" if evidence else "- 근거: 확인 필요", ""])
                else:
                    body.extend([f"### {idx}. 요점", _md_text(point), ""])
        elif outline_block:
            body.extend(["## 핵심 요점", outline_block, ""])

        if concept_tables:
            body.append("## 핵심 개념 표")
            _append_table_blocks(body, concept_tables, default_title="개념표")

        if concepts and not concept_tables:
            concept_rows = []
            for concept in concepts:
                if isinstance(concept, dict):
                    concept_rows.append([
                        concept.get("term", ""),
                        concept.get("definition", ""),
                        concept.get("explanation", ""),
                        concept.get("example", ""),
                        concept.get("caution", ""),
                    ])
                else:
                    concept_rows.append([concept, "", "", "", ""])
            table = _markdown_table(["개념", "정의", "설명", "예시", "주의점"], concept_rows)
            if table:
                body.extend(["## 핵심 개념 정리", table, ""])

        if comparison_tables:
            body.append("## 비교 정리")
            _append_table_blocks(body, comparison_tables, default_title="비교표")

        if process_flow:
            flow_rows = []
            for item in process_flow:
                if isinstance(item, dict):
                    flow_rows.append([item.get("step", ""), item.get("name", ""), item.get("description", "")])
                else:
                    flow_rows.append(["", item, ""])
            table = _markdown_table(["단계", "이름", "설명"], flow_rows)
            if table:
                body.extend(["## 강의 흐름", table, ""])

        remember_block = _as_bullets(final_summary or must_remember)
        if remember_block:
            body.extend(["## 마지막 핵심 정리", remember_block, ""])

        if review_questions:
            question_rows = []
            for item in review_questions:
                if isinstance(item, dict):
                    question_rows.append([item.get("question", ""), item.get("answer", "")])
                else:
                    question_rows.append([item, ""])
            table = _markdown_table(["질문", "답"], question_rows)
            if table:
                body.extend(["## 복습 질문", table, ""])

        unclear_block = _as_bullets(unclear_parts)
        if unclear_block:
            body.extend(["## 확인 필요", unclear_block, ""])

        action_block = _as_bullets(action_list)
        if action_block:
            body.extend(["## 복습 액션", action_block, ""])

        if not any([overview, flow, sections, key_points, concepts, concept_tables, comparison_tables, process_flow, must_remember, review_questions]) and summary_block:
            body.extend(["## Summary", summary_block, "", "## Outline", outline_block or "- (empty)", ""])

        body.extend([
            "## 원문 파일",
            f"- txt: {txt_path}" if txt_path else "",
            f"- srt: {srt_path}" if srt_path else "",
            f"- json: {json_path}" if json_path else "",
        ])

    content = "\n".join(meta_lines + [""] + body).rstrip() + "\n"

    # Determine storage path per SystemRules
    base_dir = VAULT_DIR / "지식창고" / CATEGORY_FOLDER.get(category, "AI")
    base_dir.mkdir(parents=True, exist_ok=True)
    note_path = base_dir / f"{file_slug}.md"
    note_path.write_text(content, encoding="utf-8")
    return note_path


def process_job(job_id: str, uploaded_path: Path, original_name: str, llm_model: str):
    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["stage"] = "transcribing"
        jobs[job_id]["updated_at"] = time.time()
        save_job_to_db(jobs[job_id])
    try:
        whisper_result = run_whisper(uploaded_path)
        with jobs_lock:
            jobs[job_id]["stage"] = "summarizing"
            jobs[job_id]["updated_at"] = time.time()
            save_job_to_db(jobs[job_id])
        rules_text = read_system_rules()
        llm_result = call_chatmock_summarize(whisper_result["full_text"], rules=rules_text, model=llm_model)
        with jobs_lock:
            jobs[job_id]["stage"] = "writing_note"
            jobs[job_id]["updated_at"] = time.time()
            save_job_to_db(jobs[job_id])
        note_path = write_note(
            title=Path(original_name).stem,
            source_path=uploaded_path,
            transcript_meta=whisper_result,
            llm_result=llm_result,
        )
        with jobs_lock:
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["stage"] = "done"
            jobs[job_id]["note_path"] = str(note_path)
            jobs[job_id]["output"] = {
                "json": str(whisper_result["json_path"]),
                "txt": str(whisper_result.get("txt_path")) if whisper_result.get("txt_path") else None,
                "srt": str(whisper_result.get("srt_path")) if whisper_result.get("srt_path") else None,
            }
            jobs[job_id]["updated_at"] = time.time()
            save_job_to_db(jobs[job_id])
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["stage"] = "error"
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["updated_at"] = time.time()
            save_job_to_db(jobs[job_id])


def enqueue_job(video_path: Path, original_name: str, llm_model: Optional[str] = None) -> str:
    job_id = uuid.uuid4().hex
    selected_model = normalize_llm_model(llm_model)
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "filename": original_name,
            "stored_path": str(video_path),
            "llm_model": selected_model,
            "status": "pending",
            "created_at": time.time(),
            "updated_at": time.time(),
            "stage": "pending",
            "note_path": None,
            "output": {},
            "error": None,
        }
        save_job_to_db(jobs[job_id])
    threading.Thread(
        target=process_job, args=(job_id, video_path, original_name, selected_model), daemon=True
    ).start()
    return job_id


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse(url="/ui")
    if LOGIN_PATH.exists():
        return HTMLResponse(LOGIN_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Login page missing</h1>", status_code=500)


@app.post("/auth/register")
def register(payload: Dict[str, str] = Body(...)):
    username = str(payload.get("username") or "").strip()
    display_name = str(payload.get("display_name") or username).strip() or username
    password = str(payload.get("password") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="아이디는 영문, 숫자, 점, 밑줄, 하이픈 3~32자로 입력하세요.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="비밀번호는 8자 이상이어야 합니다.")
    with auth_lock:
        with db_lock:
            existing = db_conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if existing:
                raise HTTPException(status_code=409, detail="이미 존재하는 아이디입니다.")
            db_conn.execute(
                """
                INSERT INTO users (id, username, display_name, password_hash, role, status, created_at, approved_at)
                VALUES (?, ?, ?, ?, 'user', 'pending', ?, NULL)
                """,
                (uuid.uuid4().hex, username, display_name, hash_password(password), time.time()),
            )
            db_conn.commit()
    return {"status": "pending", "message": "가입 요청이 접수되었습니다. 관리자 승인 후 로그인할 수 있습니다."}


@app.post("/auth/login")
def login(payload: Dict[str, str] = Body(...)):
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    with db_lock:
        row = db_conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다.")
    user = dict(row)
    if user.get("status") != "active":
        raise HTTPException(status_code=403, detail="관리자 승인 후 로그인할 수 있습니다.")
    token = create_session(user["id"])
    response = JSONResponse({"status": "ok", "user": public_user(user)})
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return response


@app.post("/auth/logout")
def logout(request: Request):
    clear_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(SESSION_COOKIE_NAME, secure=COOKIE_SECURE, samesite="lax")
    return response


@app.get("/auth/me")
def me(request: Request):
    user = current_user(request)
    return {"user": public_user(user) if user else None}


@app.get("/admin/users")
def admin_users(request: Request):
    require_admin(request)
    with db_lock:
        rows = db_conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return [public_user(dict(row)) for row in rows]


@app.post("/admin/users/{user_id}/approve")
def approve_user(user_id: str, request: Request):
    require_admin(request)
    with auth_lock:
        with db_lock:
            row = db_conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="user not found")
            db_conn.execute(
                "UPDATE users SET status = 'active', approved_at = ? WHERE id = ?",
                (time.time(), user_id),
            )
            db_conn.commit()
    return {"status": "approved", "user_id": user_id}


@app.post("/admin/users/{user_id}/reject")
def reject_user(user_id: str, request: Request):
    admin = require_admin(request)
    with auth_lock:
        with db_lock:
            row = db_conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="user not found")
            if row["id"] == admin["id"]:
                raise HTTPException(status_code=400, detail="관리자 본인은 거절할 수 없습니다.")
            db_conn.execute("UPDATE users SET status = 'rejected' WHERE id = ?", (user_id,))
            db_conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            db_conn.commit()
    return {"status": "rejected", "user_id": user_id}


@app.get("/system/gpu")
def system_gpu():
    """Return real-time GPU usage for the web dashboard."""
    if not shutil.which("nvidia-smi"):
        return {
            "available": False,
            "error": "nvidia-smi command not found",
            "checked_at": time.time(),
            "whisper": {
                "model": WHISPER_MODEL,
                "compute": WHISPER_COMPUTE,
                "gpu_ids": WHISPER_GPU_IDS,
            },
        }

    query = [
        "index",
        "name",
        "memory.total",
        "memory.used",
        "memory.free",
        "utilization.gpu",
        "temperature.gpu",
        "power.draw",
        "power.limit",
    ]
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(query)}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "checked_at": time.time(),
            "whisper": {
                "model": WHISPER_MODEL,
                "compute": WHISPER_COMPUTE,
                "gpu_ids": WHISPER_GPU_IDS,
            },
        }

    def as_float(value: str) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    gpus = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(query):
            continue
        index, name, total, used, free, util, temp, power, power_limit = parts
        try:
            gpu_index = int(index)
        except ValueError:
            continue
        gpus.append(
            {
                "index": gpu_index,
                "name": name,
                "memory_total_mb": as_float(total),
                "memory_used_mb": as_float(used),
                "memory_free_mb": as_float(free),
                "utilization_gpu": as_float(util),
                "temperature_c": as_float(temp),
                "power_w": as_float(power),
                "power_limit_w": as_float(power_limit),
            }
        )

    return {
        "available": bool(gpus),
        "gpus": gpus,
        "checked_at": time.time(),
        "whisper": {
            "model": WHISPER_MODEL,
            "compute": WHISPER_COMPUTE,
            "gpu_ids": WHISPER_GPU_IDS,
        },
    }


@app.get("/llm/models")
def llm_models():
    """Return ChatMock/OpenAI-compatible model names for the dashboard selector."""
    fallback = {
        "available": False,
        "models": DEFAULT_LLM_MODELS,
        "default_model": LLM_MODEL,
    }
    try:
        resp = requests.get(
            f"{LLM_BASE_URL}/models",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=3,
        )
    except requests.RequestException:
        return {**fallback, "error": "ChatMock model endpoint is unavailable"}
    if resp.status_code != 200:
        return {**fallback, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        payload = resp.json()
    except ValueError:
        return {**fallback, "error": "Model endpoint returned non-JSON response"}
    models = [
        str(item.get("id") or "").strip()
        for item in payload.get("data", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    return {
        "available": bool(models),
        "models": models or DEFAULT_LLM_MODELS,
        "default_model": LLM_MODEL,
    }


@app.get("/", include_in_schema=False)
def root():
    # 편의상 루트로 접근 시 대시보드로 이동
    return RedirectResponse(url="/ui")


@app.on_event("startup")
def on_startup():
    init_db()
    load_jobs_from_db()


@app.get("/ui", include_in_schema=False)
def ui():
    if DASHBOARD_PATH.exists():
        return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard file missing</h1>", status_code=500)


@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...), llm_model: str = Form(LLM_MODEL)):
    try:
        selected_model = normalize_llm_model(llm_model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    content_length = request.headers.get("content-length")
    if content_length and MAX_UPLOAD_BYTES > 0:
        try:
            if int(content_length) > MAX_UPLOAD_BYTES + 1024 * 1024:
                raise HTTPException(status_code=413, detail="uploaded file is too large")
        except ValueError:
            pass
    original_name = Path(file.filename or "").name
    if not original_name:
        raise HTTPException(status_code=400, detail="filename is required")
    if not is_supported_upload(original_name):
        raise HTTPException(status_code=400, detail="unsupported file type")
    dest = make_unique_path(UPLOAD_DIR, original_name)
    written = 0
    try:
        with dest.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if MAX_UPLOAD_BYTES > 0 and written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="uploaded file is too large")
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    job_id = enqueue_job(dest, original_name, selected_model)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job


@app.get("/jobs")
def list_jobs():
    with jobs_lock:
        return list(jobs.values())


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    """작업 삭제"""
    with jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="job not found")
        if jobs[job_id].get("status") in {"pending", "running"}:
            raise HTTPException(status_code=409, detail="running jobs cannot be deleted")
        del jobs[job_id]
        with db_lock:
            db_conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            db_conn.commit()
        return {"status": "deleted", "job_id": job_id}


@app.get("/note/{job_id}")
def get_note_content(job_id: str):
    """작업의 노트 파일 내용을 반환"""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")

        note_path = job.get("note_path")
        if not note_path:
            raise HTTPException(status_code=404, detail="note not found")

        note_file = ensure_child_path(Path(note_path), VAULT_DIR)
        if not note_file.is_file():
            raise HTTPException(status_code=404, detail="note file not found")

        try:
            content = note_file.read_text(encoding="utf-8")
            return {"content": content, "path": note_path}
        except Exception as e:
            logger.warning("Failed to read note %s: %s", note_file, e)
            raise HTTPException(status_code=500, detail="Failed to read note")


@app.get("/download")
def download_file(path: str):
    """파일 다운로드"""
    file_path = ensure_child_path(Path(path), OUTPUT_ROOT)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )


@app.get("/files")
def list_files():
    files = []
    for p in sorted(UPLOAD_DIR.glob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            stat = p.stat()
            files.append(
                {
                    "name": p.name,
                    "path": str(p),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
    return files


@app.post("/process_existing")
def process_existing(payload: Dict[str, str] = Body(...)):
    rel_path = payload.get("path") or payload.get("name")
    if not rel_path:
        raise HTTPException(status_code=400, detail="path is required")
    candidate = Path(rel_path)
    if not candidate.is_absolute():
        candidate = (UPLOAD_DIR / candidate).resolve()
    else:
        candidate = candidate.resolve()
    candidate = ensure_child_path(candidate, UPLOAD_DIR, "path must be inside uploads")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if not is_supported_upload(candidate.name):
        raise HTTPException(status_code=400, detail="unsupported file type")
    try:
        job_id = enqueue_job(candidate, candidate.name, payload.get("llm_model"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"job_id": job_id, "status": "queued", "filename": candidate.name}


@app.exception_handler(Exception)
async def exception_handler(request, exc):
    logger.error(
        "Unhandled error while serving %s",
        getattr(request, "url", "unknown"),
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    detail = str(exc) if DEBUG_ERRORS else "internal server error"
    return JSONResponse(status_code=500, content={"detail": detail})
