import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


BASE_DIR = Path(__file__).resolve().parent
WHISPER_SCRIPT = Path(os.environ.get("WHISPER_SCRIPT", BASE_DIR / "whisper_server.py")).resolve()
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "uploads")).resolve()
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", BASE_DIR / "output")).resolve()
VAULT_DIR = Path(os.environ.get("VAULT_PATH", BASE_DIR / "vault")).resolve()
SYSTEM_RULES_PATH = Path(os.environ.get("SYSTEM_RULES_PATH", VAULT_DIR / "SystemRules.md")).resolve()
PROMPT_PATH = Path(os.environ.get("PROMPT_PATH", BASE_DIR / "prompt_system.txt")).resolve()
OLLAMA_ENDPOINT = os.environ.get("OLLAMA_ENDPOINT", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
DB_PATH = Path(os.environ.get("JOBS_DB_PATH", BASE_DIR / "jobs.db")).resolve()

for d in (UPLOAD_DIR, OUTPUT_ROOT, VAULT_DIR):
    d.mkdir(parents=True, exist_ok=True)


app = FastAPI(title="STT to Note Automation", version="0.1.0")

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()
SUPPORTED_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav", ".m4a"}
db_conn: Optional[sqlite3.Connection] = None


def init_db():
    global db_conn
    db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
    db_conn.execute(
        """
        INSERT OR REPLACE INTO jobs
        (id, filename, stored_path, status, stage, note_path, output_json, output_txt, output_srt, error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.get("id"),
            job.get("filename"),
            job.get("stored_path"),
            job.get("status"),
            job.get("stage"),
            job.get("note_path"),
            job.get("output", {}).get("json") if job.get("output") else None,
            job.get("output", {}).get("txt") if job.get("output") else None,
            job.get("output", {}).get("srt") if job.get("output") else None,
            job.get("error"),
            job.get("created_at"),
            job.get("updated_at"),
        ),
    )
    db_conn.commit()


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
        "너는 한국어로 자막 전체를 아주 자세히 정리하는 요약자다. "
        "반드시 한국어로만 답하고, 영어를 사용하지 마라. "
        "JSON 객체 하나만 반환하고, JSON 밖에 어떤 문구도 넣지 마라. "
        "요구 필드와 최소 길이: "
        "summary (배열, 12~18개 bullet, 각 bullet은 주어+동사 있는 2~3문장, 최소 60자 이상, 세부 내용/숫자/사례/인용/결론 포함, 키워드 나열과 반복 금지), "
        "outline (배열, 8~12개 bullet, 각 bullet은 1~2문장, 흐름을 단계별로 자세히, 키워드 나열과 반복 금지), "
        "action_items (배열, 5~8개, 주어+동사+기한/담당자/조건 포함 시 명시), "
        "category, tags, related, context, importance. "
        "한국어만 사용하고, 반복이나 중복을 피하며, 가능한 한 길고 문장형으로 자세히 작성하라."
    )
    if PROMPT_PATH.exists():
        try:
            return PROMPT_PATH.read_text(encoding="utf-8")
        except Exception:
            return default_prompt
    return default_prompt


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
    while len(rel) < 2:
        rel.append(f"related/placeholder-{len(rel)+1}")
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
        "python",
        str(WHISPER_SCRIPT),
        str(video_path),
        "-O",
        str(OUTPUT_ROOT),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Whisper failed: {result.stderr.strip()}")

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


def call_ollama_summarize(text: str, rules: Optional[str] = None) -> Dict[str, Any]:
    """Summarize transcript via Ollama. Prefer Markdown sections; fallback to raw."""
    system_prompt = read_system_prompt()

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
        category = data.get("category") or data.get("cat") or "ai"
        tags = data.get("tags") or []
        related = data.get("related") or []
        context = data.get("context") or "Auto-generated from transcript"
        importance = data.get("importance") or "normal"

        def _coerce_list(value):
            if value is None:
                return []
            if isinstance(value, list):
                return [str(x).strip() for x in value if str(x).strip()]
            if isinstance(value, str):
                parts = [p.strip() for p in value.splitlines() if p.strip()]
                if len(parts) <= 1:
                    parts = re.split(r"(?<=[.!?])\s+", value)
                return [p.strip(" -•\t") for p in parts if p and p.strip()]
            return [str(value).strip()]

        if isinstance(action_items, str):
            action_items = [
                line.strip(" -•\t")
                for line in action_items.splitlines()
                if line.strip()
            ]
        if not isinstance(action_items, list):
            action_items = [str(action_items)]
        # Normalize to lists and expand if too short
        summary_list = _coerce_list(summary)
        outline_list = _coerce_list(outline)
        action_items = action_items if isinstance(action_items, list) else _coerce_list(action_items)

        summary_list = _coerce_list(summary)
        outline_list = _coerce_list(outline)
        action_items = action_items if isinstance(action_items, list) else _coerce_list(action_items)

        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, list):
            tags = [str(tags)]
        if isinstance(related, str):
            related = [r.strip() for r in related.split(",") if r.strip()]
        if not isinstance(related, list):
            related = [str(related)]
        return {
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

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }
    resp = requests.post(f"{OLLAMA_ENDPOINT}/api/chat", json=payload, timeout=240)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text}")
    content = resp.json().get("message", {}).get("content", "")
    parsed = _parse_markdown_sections(content)
    if parsed:
        return _normalize(parsed, content)
    # Fallback: keep raw markdown in summary
    return _normalize({"raw_md": content, "summary": [content]}, content)


def _as_bullets(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, list):
        return "\n".join(f"- {str(x).strip()}" for x in item)
    return "\n".join(f"- {line.strip()}" for line in str(item).splitlines() if line.strip())


def write_note(
    title: str,
    source_path: Path,
    transcript_meta: Dict[str, Any],
    llm_result: Dict[str, Any],
) -> Path:
    """Render and save Obsidian-friendly Markdown following SystemRules."""
    category_raw = str(llm_result.get("category") or "ai").lower()
    category = category_raw if category_raw in CATEGORY_FOLDER else "ai"
    tags = ensure_tags(llm_result.get("tags") or [], category)
    related = ensure_related(llm_result.get("related") or [])
    summary_list = llm_result.get("summary") or []
    outline_list = llm_result.get("outline") or []
    action_list = llm_result.get("action_items") or []
    summary_block = _as_bullets(summary_list)
    outline_block = _as_bullets(outline_list)
    action_block = _as_bullets(action_list)
    context = llm_result.get("context") or f"Auto-generated from transcript {source_path.name}"
    importance = llm_result.get("importance") or "normal"

    txt_path = transcript_meta.get("txt_path")
    srt_path = transcript_meta.get("srt_path")
    json_path = transcript_meta.get("json_path")

    now = time.strftime("%Y-%m-%d %H:%M")
    file_slug = slugify_title(title)
    note_id = f"{category}-{file_slug}-{uuid.uuid4().hex[:6]}"
    summary_meta_src = summary_list if isinstance(summary_list, list) else []
    if not summary_meta_src and summary_block:
        summary_meta_src = [line.strip("- ").strip() for line in summary_block.splitlines() if line.strip()]
    summary_meta = " ".join(summary_meta_src[:3]) if summary_meta_src else (summary_block or "- (empty)")
    summary_clean = summary_meta.replace("\"", "").replace("\n", " ")
    context_clean = str(context).replace("\"", "").replace("\n", " ")

    # YAML metadata
    meta_lines = [
        "---",
        f"category: {category}",
        f"related: [{', '.join(related)}]",
        f"summary: \"{summary_clean[:200]}\"",
        "---",
    ]

    raw_md = llm_result.get("raw_md")
    if raw_md:
        body = [raw_md.strip(), "", "## Transcript", f"- txt: {txt_path}" if txt_path else "",
                f"- srt: {srt_path}" if srt_path else "", f"- json: {json_path}" if json_path else ""]
    else:
        body = [
            "## Summary",
            summary_block or "- (empty)",
            "",
            "## Outline",
            outline_block or "- (empty)",
            "",
            "## Action Items",
            action_block or "- (empty)",
            "",
            "## Transcript",
            f"- txt: {txt_path}" if txt_path else "",
            f"- srt: {srt_path}" if srt_path else "",
            f"- json: {json_path}" if json_path else "",
        ]

    content = "\n".join([line for line in body + meta_lines if line != ""])

    # Determine storage path per SystemRules
    base_dir = VAULT_DIR / "지식창고" / CATEGORY_FOLDER.get(category, "AI")
    base_dir.mkdir(parents=True, exist_ok=True)
    note_path = base_dir / f"{file_slug}.md"
    note_path.write_text(content, encoding="utf-8")
    return note_path


def process_job(job_id: str, uploaded_path: Path, original_name: str):
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
        llm_result = call_ollama_summarize(whisper_result["full_text"], rules=rules_text)
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


def enqueue_job(video_path: Path, original_name: str) -> str:
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "filename": original_name,
            "stored_path": str(video_path),
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
        target=process_job, args=(job_id, video_path, original_name), daemon=True
    ).start()
    return job_id


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root():
    # 편의상 루트로 접근 시 대시보드로 이동
    return RedirectResponse(url="/ui")


@app.on_event("startup")
def on_startup():
    init_db()
    load_jobs_from_db()


DASHBOARD_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>NoteCraft - 노트 제작소</title>
  <script src="https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js"></script>
  <style>
    * { box-sizing: border-box; }

    :root {
      --bg-primary: #fafbfc;
      --bg-card: #ffffff;
      --bg-hover: #f3f4f6;
      --border-color: #e1e4e8;
      --border-hover: #d1d5da;
      --text-primary: #24292e;
      --text-secondary: #586069;
      --text-muted: #6a737d;
      --accent: #0969da;
      --accent-hover: #0860ca;
      --accent-light: #ddf4ff;
      --success: #1a7f37;
      --success-light: #d1f4e0;
      --warning: #9a6700;
      --warning-light: #fff8c5;
      --error: #cf222e;
      --error-light: #ffebe9;
    }

    body {
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
      background: var(--bg-primary);
      color: var(--text-primary);
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
    }

    .wrap {
      max-width: 1200px;
      margin: 0 auto;
      padding: 40px 24px;
    }

    /* Header */
    .header {
      margin-bottom: 40px;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
    }

    .header-content h1 {
      margin: 0 0 8px;
      font-size: 32px;
      font-weight: 600;
      color: var(--text-primary);
      letter-spacing: -0.5px;
    }

    .header-subtitle {
      color: var(--text-secondary);
      font-size: 15px;
    }

    .header-stats {
      display: flex;
      gap: 16px;
      font-size: 13px;
      color: var(--text-muted);
    }

    .stat-item {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
    }

    .stat-value {
      font-size: 20px;
      font-weight: 600;
      color: var(--text-primary);
    }

    .stat-label {
      color: var(--text-muted);
    }

    /* Grid Layout */
    .grid {
      display: grid;
      gap: 20px;
      grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
      margin-bottom: 24px;
    }

    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr; }
      .header { flex-direction: column; align-items: flex-start; }
    }

    /* Cards */
    .card {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 6px;
      padding: 24px;
      box-shadow: 0 1px 2px rgba(0,0,0,0.05);
      transition: box-shadow 0.2s, border-color 0.2s;
    }

    .card:hover {
      border-color: var(--border-hover);
      box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    }

    .section-title {
      font-weight: 600;
      color: var(--text-primary);
      margin: 0 0 18px;
      font-size: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border-color);
    }

    /* Form Elements */
    label {
      display: block;
      margin-bottom: 8px;
      font-weight: 600;
      color: var(--text-primary);
      font-size: 14px;
    }

    input[type="file"] {
      width: 100%;
      padding: 12px;
      border-radius: 6px;
      border: 2px dashed var(--border-color);
      background: var(--bg-primary);
      color: var(--text-primary);
      cursor: pointer;
      font-size: 14px;
      transition: border-color 0.2s, background 0.2s;
    }

    input[type="file"]:hover {
      border-color: var(--accent);
      background: var(--bg-card);
    }

    select {
      width: 100%;
      padding: 10px 12px;
      border-radius: 6px;
      border: 1px solid var(--border-color);
      background: var(--bg-card);
      color: var(--text-primary);
      cursor: pointer;
      font-size: 14px;
      transition: border-color 0.2s;
    }

    select:hover {
      border-color: var(--border-hover);
    }

    select:focus, input:focus {
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-light);
    }

    /* Buttons */
    button {
      padding: 10px 18px;
      border: none;
      border-radius: 6px;
      background: var(--accent);
      color: white;
      font-weight: 500;
      cursor: pointer;
      font-size: 14px;
      white-space: nowrap;
      transition: background 0.2s, transform 0.1s;
    }

    button:hover:not(:disabled) {
      background: var(--accent-hover);
    }

    button:active:not(:disabled) {
      transform: scale(0.98);
    }

    button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .btn-secondary {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      color: var(--text-primary);
    }

    .btn-secondary:hover:not(:disabled) {
      background: var(--bg-hover);
      border-color: var(--border-hover);
    }

    /* Status & Messages */
    .status {
      margin-top: 14px;
      padding: 12px 14px;
      border-radius: 6px;
      background: var(--accent-light);
      border: 1px solid var(--accent);
      font-size: 14px;
      color: var(--text-primary);
      min-height: 44px;
      display: flex;
      align-items: center;
    }

    .status:empty {
      display: none;
    }

    .muted {
      color: var(--text-muted);
      font-size: 13px;
    }

    /* Job List */
    .jobs {
      margin-top: 16px;
      font-size: 14px;
    }

    .job-row {
      padding: 16px;
      border: 1px solid var(--border-color);
      border-radius: 6px;
      margin-bottom: 12px;
      background: var(--bg-card);
      transition: border-color 0.2s, box-shadow 0.2s;
    }

    .job-row:hover {
      border-color: var(--border-hover);
      box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }

    .job-row:last-child {
      margin-bottom: 0;
    }

    .job-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      font-size: 15px;
      margin-bottom: 10px;
      flex-wrap: wrap;
    }

    .job-filename {
      font-weight: 600;
      color: var(--text-primary);
    }

    .job-stage {
      color: var(--text-secondary);
      font-size: 13px;
      font-weight: 500;
    }

    .job-details {
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid var(--border-color);
      font-size: 13px;
      line-height: 1.8;
    }

    .job-details a {
      color: var(--accent);
      text-decoration: none;
      font-weight: 500;
    }

    .job-details a:hover {
      text-decoration: underline;
    }

    /* Note Preview Container */
    .note-preview-container {
      margin-top: 12px;
      padding: 16px;
      background: var(--bg-primary);
      border: 1px solid var(--border-color);
      border-radius: 6px;
      max-height: 500px;
      overflow-y: auto;
    }

    .note-preview-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border-color);
    }

    .note-preview-title {
      font-weight: 600;
      color: var(--text-primary);
      font-size: 14px;
    }

    .toggle-btn {
      background: none;
      border: none;
      color: var(--accent);
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      padding: 4px 8px;
    }

    .toggle-btn:hover {
      text-decoration: underline;
    }

    .note-content {
      font-size: 14px;
    }

    .note-content.collapsed {
      display: none;
    }


    /* Badges */
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 5px 12px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 600;
      background: var(--bg-primary);
      color: var(--text-secondary);
      border: 1px solid var(--border-color);
    }

    .badge.done {
      background: var(--success-light);
      color: var(--success);
      border-color: var(--success);
    }

    .badge.fail {
      background: var(--error-light);
      color: var(--error);
      border-color: var(--error);
    }

    .badge.run {
      background: var(--warning-light);
      color: var(--warning);
      border-color: var(--warning);
      animation: pulse-badge 2s ease-in-out infinite;
    }

    @keyframes pulse-badge {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.6; }
    }

    /* Step Indicator */
    .step-indicator {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      padding: 12px;
      background: var(--bg-primary);
      border-radius: 6px;
      border: 1px solid var(--border-color);
    }

    .step {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
      color: var(--text-muted);
      position: relative;
    }

    .step-icon {
      width: 24px;
      height: 24px;
      border-radius: 50%;
      background: var(--bg-card);
      border: 2px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 11px;
      font-weight: 600;
      flex-shrink: 0;
    }

    .step.active .step-icon {
      border-color: var(--accent);
      background: var(--accent-light);
      color: var(--accent);
      animation: pulse-step 1.5s ease-in-out infinite;
    }

    .step.completed .step-icon {
      border-color: var(--success);
      background: var(--success);
      color: white;
    }

    .step.active {
      color: var(--text-primary);
      font-weight: 600;
    }

    .step.completed {
      color: var(--text-secondary);
    }

    .step-divider {
      width: 20px;
      height: 2px;
      background: var(--border-color);
      margin: 0 4px;
    }

    .step.completed ~ .step-divider {
      background: var(--success);
    }

    @keyframes pulse-step {
      0%, 100% { transform: scale(1); }
      50% { transform: scale(1.1); }
    }

    /* Loading dots animation */
    .loading-dots::after {
      content: '';
      animation: loading-dots 1.5s steps(4, end) infinite;
    }

    @keyframes loading-dots {
      0%, 20% { content: ''; }
      40% { content: '.'; }
      60% { content: '..'; }
      80%, 100% { content: '...'; }
    }

    /* Spinner */
    .spinner {
      display: block;
      width: 14px;
      height: 14px;
      border: 2px solid var(--border-color);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin: 0 auto;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    /* Dark Mode */
    html.dark-mode {
      --bg-primary: #0b1120;
      --bg-card: #0f172a;
      --bg-hover: #1e293b;
      --border-color: #1e293b;
      --border-hover: #334155;
      --text-primary: #e2e8f0;
      --text-secondary: #cbd5e1;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-hover: #22d3ee;
      --accent-light: #075985;
      --success: #34d399;
      --success-light: #064e3b;
      --warning: #fbbf24;
      --warning-light: #78350f;
      --error: #f87171;
      --error-light: #7f1d1d;
    }

    /* Utility Classes */
    .flex {
      display: flex;
    }

    .flex-between {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    .gap-10 {
      gap: 10px;
    }

    .mt-16 {
      margin-top: 16px;
    }

    /* Markdown Preview Styles (Obsidian-like) */
    .markdown-preview {
      color: var(--text-primary);
      line-height: 1.7;
      font-size: 15px;
    }

    .markdown-preview h1 {
      font-size: 28px;
      font-weight: 600;
      margin: 24px 0 16px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border-color);
      color: var(--text-primary);
    }

    .markdown-preview h2 {
      font-size: 22px;
      font-weight: 600;
      margin: 20px 0 12px;
      color: var(--text-primary);
    }

    .markdown-preview h3 {
      font-size: 18px;
      font-weight: 600;
      margin: 16px 0 10px;
      color: var(--text-primary);
    }

    .markdown-preview p {
      margin: 12px 0;
    }

    .markdown-preview ul, .markdown-preview ol {
      margin: 12px 0;
      padding-left: 24px;
    }

    .markdown-preview li {
      margin: 6px 0;
    }

    .markdown-preview code {
      background: var(--bg-primary);
      padding: 2px 6px;
      border-radius: 3px;
      font-family: 'Monaco', 'Menlo', 'Consolas', monospace;
      font-size: 14px;
      color: var(--error);
    }

    .markdown-preview pre {
      background: var(--bg-primary);
      padding: 16px;
      border-radius: 6px;
      overflow-x: auto;
      border: 1px solid var(--border-color);
    }

    .markdown-preview pre code {
      background: none;
      padding: 0;
      color: var(--text-primary);
    }

    .markdown-preview blockquote {
      border-left: 4px solid var(--accent);
      padding-left: 16px;
      margin: 16px 0;
      color: var(--text-secondary);
      font-style: italic;
    }

    .markdown-preview a {
      color: var(--accent);
      text-decoration: none;
      font-weight: 500;
    }

    .markdown-preview a:hover {
      text-decoration: underline;
    }

    .markdown-preview table {
      border-collapse: collapse;
      width: 100%;
      margin: 16px 0;
    }

    .markdown-preview th, .markdown-preview td {
      border: 1px solid var(--border-color);
      padding: 10px;
      text-align: left;
    }

    .markdown-preview th {
      background: var(--bg-primary);
      font-weight: 600;
    }

    .markdown-preview hr {
      border: none;
      border-top: 1px solid var(--border-color);
      margin: 24px 0;
    }

    .markdown-preview strong {
      font-weight: 600;
      color: var(--text-primary);
    }

    .markdown-preview em {
      font-style: italic;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div class="header-content">
        <h1>NoteCraft</h1>
        <div class="header-subtitle">
          음성을 지식으로 변환하는 노트 제작소 • Whisper STT + AI 요약
        </div>
      </div>
      <div style="display: flex; align-items: center; gap: 20px;">
        <button id="dark-mode-toggle" type="button" class="btn-secondary" style="padding: 10px 14px; font-size: 18px;">🌙</button>
        <div class="header-stats">
          <div class="stat-item">
            <div class="stat-value" id="total-jobs">-</div>
            <div class="stat-label">총 작업</div>
          </div>
          <div class="stat-item">
            <div class="stat-value" id="completed-jobs">-</div>
            <div class="stat-label">완료</div>
          </div>
        </div>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="section-title">업로드 & 처리</div>
        <form id="upload-form">
          <label for="file">파일 업로드</label>
          <input id="file" name="file" type="file" accept="audio/*,video/*" required />
          <div class="flex gap-10 mt-16">
            <button id="upload-btn" type="submit">업로드 & 처리 시작</button>
            <div id="current-job" class="muted"></div>
          </div>
        </form>
        <div id="status" class="status"></div>
      </div>

      <div class="card">
        <div class="section-title">기존 업로드 파일</div>
        <div class="flex gap-10 mt-16">
          <select id="file-select"></select>
          <button id="process-existing" type="button">선택 처리</button>
        </div>
        <div class="flex-between mt-16">
          <div id="files-status" class="muted"></div>
          <button id="refresh-files" type="button" class="btn-secondary">새로고침</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="flex-between">
        <div class="section-title">작업 목록</div>
        <button id="refresh-btn" type="button" class="btn-secondary">새로고침</button>
      </div>

      <!-- 검색 및 필터 -->
      <div style="display: flex; gap: 12px; margin-top: 16px; flex-wrap: wrap;">
        <input
          type="text"
          id="search-input"
          placeholder="파일명 검색..."
          style="flex: 1; min-width: 200px; padding: 10px; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-card);"
        />
        <select id="status-filter" style="padding: 10px; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-card);">
          <option value="all">전체 상태</option>
          <option value="completed">완료</option>
          <option value="running">처리중</option>
          <option value="failed">실패</option>
          <option value="pending">대기</option>
        </select>
        <button id="clear-filters" type="button" class="btn-secondary">필터 초기화</button>
      </div>

      <div id="jobs" class="jobs"></div>
    </div>
  </div>

  <script>
    const uploadForm = document.getElementById("upload-form");
    const uploadBtn = document.getElementById("upload-btn");
    const statusEl = document.getElementById("status");
    const jobsEl = document.getElementById("jobs");
    const currentJobEl = document.getElementById("current-job");
    const refreshBtn = document.getElementById("refresh-btn");
    const fileSelect = document.getElementById("file-select");
    const processExistingBtn = document.getElementById("process-existing");
    const refreshFilesBtn = document.getElementById("refresh-files");
    const filesStatus = document.getElementById("files-status");
    let pollTimer = null;
    let autoRefreshTimer = null;

    function statusBadge(status) {
      const map = {
        completed: { cls: "badge done", label: "완료" },
        failed: { cls: "badge fail", label: "실패" },
        running: { cls: "badge run", label: "처리중" },
        pending: { cls: "badge", label: "대기" },
      };
      const info = map[status] || map.pending;
      return `<span class="${info.cls}">${info.label}</span>`;
    }

    function setStatus(msg) {
      statusEl.textContent = msg;
    }

    // 현재 렌더링된 작업들의 상태를 저장
    let currentJobs = new Map();

    async function renderJobs(list) {
      if (!Array.isArray(list) || list.length === 0) {
        jobsEl.innerHTML = '<div style="text-align: center; padding: 40px; color: var(--text-muted);">작업이 없습니다</div>';
        document.getElementById('total-jobs').textContent = '0';
        document.getElementById('completed-jobs').textContent = '0';
        currentJobs.clear();
        return;
      }

      // 통계 업데이트
      const completedCount = list.filter(job => job.status === 'completed').length;
      document.getElementById('total-jobs').textContent = list.length;
      document.getElementById('completed-jobs').textContent = completedCount;

      // 정렬된 작업 리스트
      const sortedList = list.sort((a,b)=> (b.created_at||0) - (a.created_at||0));

      // 새로운 작업 ID 세트
      const newJobIds = new Set(sortedList.map(j => j.id));

      // 삭제된 작업 제거
      for (const [jobId, element] of currentJobs.entries()) {
        if (!newJobIds.has(jobId)) {
          element.remove();
          currentJobs.delete(jobId);
          loadedNotes.delete(jobId);
        }
      }

      // 각 작업 처리
      for (let i = 0; i < sortedList.length; i++) {
        const job = sortedList[i];
        const existingElement = currentJobs.get(job.id);

        // 작업의 현재 상태를 JSON으로 직렬화하여 비교
        const jobState = JSON.stringify({
          status: job.status,
          stage: job.stage,
          filename: job.filename,
          note_path: job.note_path,
          error: job.error,
          created_at: job.created_at,
          completed_at: job.completed_at
        });

        // 완료된 작업의 노트가 이미 로드되어 있는지 확인
        let hasLoadedNote = false;
        if (existingElement && job.status === 'completed' && job.note_path) {
          const contentEl = existingElement.querySelector(`#note-content-${job.id}`);
          if (contentEl && !contentEl.innerHTML.includes('로딩 중')) {
            hasLoadedNote = true;
          }
        }

        // 이미 존재하고 변경사항이 없으면 스킵 (단, 순서 확인 및 노트 로드)
        // 또는 노트가 이미 로드된 완료 작업이면 절대 DOM 교체하지 않음
        if (existingElement && (existingElement.dataset.state === jobState || hasLoadedNote)) {
          // 순서가 맞는지 확인
          const currentIndex = Array.from(jobsEl.children).indexOf(existingElement);
          if (currentIndex !== i) {
            // 순서가 다르면 재배치
            if (i === 0) {
              jobsEl.prepend(existingElement);
            } else {
              jobsEl.children[i - 1].after(existingElement);
            }
          }

          // 노트가 아직 로드되지 않았다면 로드
          if (job.status === 'completed' && job.note_path) {
            const contentEl = existingElement.querySelector(`#note-content-${job.id}`);
            if (contentEl && contentEl.innerHTML.includes('로딩 중')) {
              loadNoteContent(job.id);
            }
          }

          continue;
        }

        // 작업 카드 HTML 생성
        const note = job.note_path ? `<div class="job-details">노트: <a href="file://${job.note_path}" target="_blank">${job.note_path}</a></div>` : "";

        // 자막 파일 다운로드 버튼
        let files = '';
        if (job.output && (job.output.json || job.output.txt || job.output.srt)) {
          files = `<div class="job-details" style="margin-top: 8px; display: flex; gap: 6px; align-items: center;">
            <span style="color: var(--text-muted); font-size: 12px;">자막 파일:</span>
            ${job.output.json ? `<button onclick="downloadFile('${job.output.json}', '${job.filename}.json')" class="btn-secondary" style="padding: 4px 8px; font-size: 11px;">JSON ⬇</button>` : ""}
            ${job.output.txt ? `<button onclick="downloadFile('${job.output.txt}', '${job.filename}.txt')" class="btn-secondary" style="padding: 4px 8px; font-size: 11px;">TXT ⬇</button>` : ""}
            ${job.output.srt ? `<button onclick="downloadFile('${job.output.srt}', '${job.filename}.srt')" class="btn-secondary" style="padding: 4px 8px; font-size: 11px;">SRT ⬇</button>` : ""}
          </div>`;
        }

        // 완료된 작업의 노트 미리보기 (기존 노트 콘텐츠 보존)
        let notePreview = '';
        let preservedNoteContent = null;
        let shouldLoadNote = false;

        if (job.status === 'completed' && job.note_path) {
          // 기존 노트 콘텐츠가 있으면 보존
          if (existingElement) {
            const oldContent = existingElement.querySelector(`#note-content-${job.id}`);
            if (oldContent) {
              const oldHtml = oldContent.innerHTML.trim();
              // "로딩 중..." 텍스트가 아니고, 실제 콘텐츠가 있으면 보존
              if (!oldHtml.includes('로딩 중') && !oldHtml.includes('color: var(--text-muted)')) {
                preservedNoteContent = oldContent.innerHTML;
                console.log('노트 콘텐츠 보존:', job.id);
              } else {
                shouldLoadNote = true;
              }
            }
          } else {
            shouldLoadNote = true;
          }

          // 접기/펼치기 상태 결정
          let collapsedClass = '';
          let toggleBtnText = '접기';

          if (existingElement) {
            // 기존 요소가 있으면 현재 상태 유지
            const wasCollapsed = existingElement.querySelector('.note-content.collapsed');
            if (wasCollapsed) {
              collapsedClass = 'collapsed';
              toggleBtnText = '펼치기';
            }
          } else {
            // 새 노트인 경우, 최신 것(i === 0)만 펼치고 나머지는 접기
            const isLatest = i === 0;
            if (!isLatest) {
              collapsedClass = 'collapsed';
              toggleBtnText = '펼치기';
            }
          }

          notePreview = `<div class="note-preview-container" id="note-${job.id}">
            <div class="note-preview-header">
              <div class="note-preview-title">생성된 노트</div>
              <button class="toggle-btn" onclick="toggleNote('${job.id}')">${toggleBtnText}</button>
            </div>
            <div class="note-content markdown-preview ${collapsedClass}" id="note-content-${job.id}">
              ${preservedNoteContent || ''}
            </div>
          </div>`;
        }

        // 스텝 인디케이터 생성
        let stepIndicator = '';
        if (job.status === 'running' || job.status === 'pending') {
          const stage = (job.stage || "").toLowerCase();
          const steps = [
            { key: 'transcribing', label: '음성 인식', icon: '1' },
            { key: 'summarizing', label: 'AI 요약', icon: '2' },
            { key: 'writing_note', label: '노트 생성', icon: '3' }
          ];

          stepIndicator = `<div class="step-indicator">
            ${steps.map((step, idx) => {
              let stepClass = '';
              let iconContent = step.icon;

              if (stage === step.key) {
                stepClass = 'active';
                iconContent = `<span class="spinner"></span>`;
              } else if (
                (step.key === 'transcribing' && (stage === 'summarizing' || stage === 'writing_note')) ||
                (step.key === 'summarizing' && stage === 'writing_note')
              ) {
                stepClass = 'completed';
                iconContent = '✓';
              }

              const divider = idx < steps.length - 1 ? '<div class="step-divider"></div>' : '';

              return `
                <div class="step ${stepClass}">
                  <div class="step-icon">${iconContent}</div>
                  <span>${step.label}</span>
                </div>
                ${divider}
              `;
            }).join('')}
          </div>`;
        }

        // 작업 액션 버튼들
        const actions = `
          <div style="display: flex; gap: 6px; margin-left: auto;">
            ${job.status === 'completed' && job.note_path ? `
              <button onclick="downloadNote('${job.id}', '${job.filename}')" class="btn-secondary" style="padding: 6px 10px; font-size: 12px;" title="노트 다운로드">💾</button>
              <button onclick="copyNote('${job.id}')" class="btn-secondary" style="padding: 6px 10px; font-size: 12px;" title="노트 복사">📋</button>
            ` : ''}
            ${job.status === 'failed' ? `
              <button onclick="retryJob('${job.id}')" class="btn-secondary" style="padding: 6px 10px; font-size: 12px;" title="재시도">🔄</button>
            ` : ''}
            <button onclick="deleteJob('${job.id}')" class="btn-secondary" style="padding: 6px 10px; font-size: 12px;" title="삭제">🗑️</button>
          </div>
        `;

        // 에러 메시지 표시
        const errorMsg = job.status === 'failed' && job.error ? `
          <div style="margin-top: 10px; padding: 10px; background: var(--error-light); border: 1px solid var(--error); border-radius: 6px; font-size: 13px; color: var(--error);">
            ❌ ${job.error}
          </div>
        ` : '';

        // 파일 크기 및 시간 정보
        const timeInfo = `
          <div class="muted" style="margin-top: 8px; font-size: 11px; display: flex; gap: 12px; flex-wrap: wrap;">
            <span>ID: ${job.id}</span>
            ${job.created_at ? `<span>생성: ${new Date(job.created_at * 1000).toLocaleString('ko-KR')}</span>` : ''}
            ${job.completed_at ? `<span>완료: ${new Date(job.completed_at * 1000).toLocaleString('ko-KR')}</span>` : ''}
          </div>
        `;

        const jobHtml = `<div class="job-row" data-job-id="${job.id}" data-state='${jobState}'>
          <div class="job-header">
            <div class="job-filename">${job.filename || "(no name)"}</div>
            <div style="display: flex; align-items: center; gap: 10px;">
              ${statusBadge(job.status || "pending")}
              ${actions}
            </div>
          </div>
          <div class="job-stage">
            ${(() => {
              const stage = (job.stage || "").toLowerCase();
              if (stage === "transcribing") return '<span class="loading-dots">자막 추출/인식 중</span>';
              if (stage === "summarizing") return '<span class="loading-dots">LLM 요약 중</span>';
              if (stage === "writing_note") return '<span class="loading-dots">노트 저장 중</span>';
              if (stage === "done") return "완료";
              if (stage === "error") return "오류";
              return stage || "대기";
            })()}
          </div>
          ${stepIndicator}
          ${errorMsg}
          ${note}
          ${files}
          ${notePreview}
          ${timeInfo}
        </div>`;

        // 노트를 먼저 로드 (DOM 교체 전에)
        if (shouldLoadNote && existingElement) {
          const oldContentEl = existingElement.querySelector(`#note-content-${job.id}`);
          if (oldContentEl) {
            // 기존 요소에서 직접 로드
            await loadNoteContentDirect(oldContentEl, job.id);
            // 로드된 내용을 preservedNoteContent에 저장
            preservedNoteContent = oldContentEl.innerHTML;
            // notePreview 다시 생성
            notePreview = `<div class="note-preview-container" id="note-${job.id}">
              <div class="note-preview-header">
                <div class="note-preview-title">생성된 노트</div>
                <button class="toggle-btn" onclick="toggleNote('${job.id}')">접기</button>
              </div>
              <div class="note-content markdown-preview" id="note-content-${job.id}">
                ${preservedNoteContent}
              </div>
            </div>`;

            // jobHtml 재생성
            const jobHtmlUpdated = `<div class="job-row" data-job-id="${job.id}" data-state='${jobState}'>
              <div class="job-header">
                <div class="job-filename">${job.filename || "(no name)"}</div>
                <div style="display: flex; align-items: center; gap: 10px;">
                  ${statusBadge(job.status || "pending")}
                  ${actions}
                </div>
              </div>
              <div class="job-stage">
                ${(() => {
                  const stage = (job.stage || "").toLowerCase();
                  if (stage === "transcribing") return '<span class="loading-dots">자막 추출/인식 중</span>';
                  if (stage === "summarizing") return '<span class="loading-dots">LLM 요약 중</span>';
                  if (stage === "writing_note") return '<span class="loading-dots">노트 저장 중</span>';
                  if (stage === "done") return "완료";
                  if (stage === "error") return "오류";
                  return stage || "대기";
                })()}
              </div>
              ${stepIndicator}
              ${errorMsg}
              ${note}
              ${files}
              ${notePreview}
              ${timeInfo}
            </div>`;

            // 스크롤 위치 보존
            const noteContainer = existingElement.querySelector('.note-preview-container');
            const scrollTop = noteContainer ? noteContainer.scrollTop : 0;
            const isCollapsed = existingElement.querySelector('.note-content.collapsed');

            existingElement.outerHTML = jobHtmlUpdated;
            const newElement = jobsEl.querySelector(`[data-job-id="${job.id}"]`);
            currentJobs.set(job.id, newElement);

            // 스크롤 위치 복원
            if (scrollTop > 0) {
              setTimeout(() => {
                const newContainer = newElement.querySelector('.note-preview-container');
                if (newContainer) newContainer.scrollTop = scrollTop;
              }, 0);
            }

            // 접기 상태 복원
            if (isCollapsed) {
              setTimeout(() => {
                const content = newElement.querySelector('.note-content');
                const btn = newElement.querySelector('.toggle-btn');
                if (content) content.classList.add('collapsed');
                if (btn) btn.textContent = '펼치기';
              }, 0);
            }
          }
        } else if (existingElement) {
          // 스크롤 위치 보존
          const noteContainer = existingElement.querySelector('.note-preview-container');
          const scrollTop = noteContainer ? noteContainer.scrollTop : 0;
          const isCollapsed = existingElement.querySelector('.note-content.collapsed');

          existingElement.outerHTML = jobHtml;
          const newElement = jobsEl.querySelector(`[data-job-id="${job.id}"]`);
          currentJobs.set(job.id, newElement);

          // 스크롤 위치 복원
          if (scrollTop > 0) {
            setTimeout(() => {
              const newContainer = newElement.querySelector('.note-preview-container');
              if (newContainer) newContainer.scrollTop = scrollTop;
            }, 0);
          }

          // 접기 상태 복원
          if (isCollapsed) {
            setTimeout(() => {
              const content = newElement.querySelector('.note-content');
              const btn = newElement.querySelector('.toggle-btn');
              if (content) content.classList.add('collapsed');
              if (btn) btn.textContent = '펼치기';
            }, 0);
          }
        } else {
          // 새 작업 추가
          const tempDiv = document.createElement('div');
          tempDiv.innerHTML = jobHtml;
          const newElement = tempDiv.firstElementChild;

          if (i === 0) {
            jobsEl.prepend(newElement);
          } else if (jobsEl.children[i - 1]) {
            jobsEl.children[i - 1].after(newElement);
          } else {
            jobsEl.appendChild(newElement);
          }

          currentJobs.set(job.id, newElement);

          // 새 작업이면 노트 로드
          if (shouldLoadNote) {
            loadNoteContent(job.id);
          }
        }
      }
    }

    // 이미 로드된 노트 내용 캐시
    const loadedNotes = new Set();

    // 특정 요소에 직접 노트 로드 (await 가능)
    async function loadNoteContentDirect(contentEl, jobId) {
      if (!contentEl) return;

      console.log('loadNoteContentDirect 호출:', jobId);
      try {
        const res = await fetch(`/note/${jobId}`);
        console.log('fetch 응답:', res.status, res.ok);
        if (!res.ok) throw new Error(`Failed to load note: ${res.status}`);
        const data = await res.json();
        console.log('데이터 수신:', data);

        const htmlContent = marked.parse(data.content);
        console.log('HTML 변환 완료');
        contentEl.innerHTML = htmlContent;
        loadedNotes.add(jobId);
        console.log('노트 렌더링 완료:', jobId);
      } catch (err) {
        console.error('노트 로드 실패:', jobId, err);
        contentEl.innerHTML = `<div style="text-align: center; padding: 20px; color: var(--error);">노트를 불러올 수 없습니다.<br><small>${err.message}</small></div>`;
      }
    }

    // 노트 내용 로드 (ID로)
    async function loadNoteContent(jobId) {
      console.log('loadNoteContent 호출:', jobId);
      const contentEl = document.getElementById(`note-content-${jobId}`);
      if (!contentEl) {
        console.log('엘리먼트 없음:', jobId);
        return;
      }

      // 이미 로드된 경우 스킵
      if (loadedNotes.has(jobId) && !contentEl.innerHTML.includes('로딩 중')) {
        console.log('이미 로드됨:', jobId);
        return;
      }

      console.log('노트 fetch 시작:', jobId);
      try {
        const res = await fetch(`/note/${jobId}`);
        console.log('fetch 응답:', res.status, res.ok);
        if (!res.ok) throw new Error(`Failed to load note: ${res.status}`);
        const data = await res.json();
        console.log('데이터 수신:', data);

        const htmlContent = marked.parse(data.content);
        console.log('HTML 변환 완료');
        contentEl.innerHTML = htmlContent;
        loadedNotes.add(jobId);
        console.log('노트 렌더링 완료:', jobId);
      } catch (err) {
        console.error('노트 로드 실패:', jobId, err);
        contentEl.innerHTML = `<div style="text-align: center; padding: 20px; color: var(--error);">노트를 불러올 수 없습니다.<br><small>${err.message}</small></div>`;
      }
    }

    // 노트 접기/펼치기
    function toggleNote(jobId) {
      const contentEl = document.getElementById(`note-content-${jobId}`);
      const btn = event.target;

      if (contentEl.classList.contains('collapsed')) {
        contentEl.classList.remove('collapsed');
        btn.textContent = '접기';
      } else {
        contentEl.classList.add('collapsed');
        btn.textContent = '펼치기';
      }
    }

    async function fetchJobs() {
      try {
        const res = await fetch("/jobs");
        if (!res.ok) throw new Error("failed to load jobs");
        const data = await res.json();
        renderJobs(data);
      } catch (err) {
        jobsEl.textContent = "목록 불러오기 실패";
      }
    }

    async function fetchFiles() {
      filesStatus.textContent = "목록 불러오는 중...";
      fileSelect.innerHTML = "";
      try {
        const res = await fetch("/files");
        if (!res.ok) throw new Error("failed");
        const data = await res.json();
        if (!Array.isArray(data) || data.length === 0) {
          filesStatus.textContent = "업로드된 파일이 없습니다.";
          return;
        }
        data.sort((a,b)=> (b.mtime||0) - (a.mtime||0));
        data.forEach(f => {
          const opt = document.createElement("option");
          opt.value = f.path;
          const sizeMB = (f.size / (1024*1024)).toFixed(1);
          opt.textContent = `${f.name} (${sizeMB} MB)`;
          fileSelect.appendChild(opt);
        });
        filesStatus.textContent = `${data.length}개 파일`;
      } catch (e) {
        filesStatus.textContent = "파일 목록 불러오기 실패";
      }
    }

    // 자동 새로고침 시작
    function startAutoRefresh() {
      if (autoRefreshTimer) clearInterval(autoRefreshTimer);
      autoRefreshTimer = setInterval(() => {
        fetchJobs();
      }, 3000); // 3초마다 새로고침
    }

    // 자동 새로고침 중지
    function stopAutoRefresh() {
      if (autoRefreshTimer) {
        clearInterval(autoRefreshTimer);
        autoRefreshTimer = null;
      }
    }

    async function pollJob(id) {
      if (pollTimer) clearInterval(pollTimer);

      // 작업 시작하면 자동 새로고침 시작
      startAutoRefresh();

      pollTimer = setInterval(async () => {
        try {
          const res = await fetch(`/jobs/${id}`);
          if (!res.ok) throw new Error("not found");
          const job = await res.json();
          currentJobEl.textContent = `진행 중: ${job.filename || job.id}`;
          setStatus(`상태: ${job.status}`);
          if (job.status === "completed") {
            setStatus(`완료! 노트: ${job.note_path}`);
            fetchJobs();
            clearInterval(pollTimer);
            currentJobEl.textContent = "";
          } else if (job.status === "failed") {
            setStatus(`실패: ${job.error || "unknown error"}`);
            clearInterval(pollTimer);
            currentJobEl.textContent = "";
          }
        } catch (e) {
          setStatus("상태 조회 실패");
        }
      }, 1500);
    }

    uploadForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fileInput = document.getElementById("file");
      if (!fileInput.files.length) return;
      const formData = new FormData();
      formData.append("file", fileInput.files[0]);
      uploadBtn.disabled = true;
      setStatus("업로드 중...");
      try {
        const res = await fetch("/upload", { method: "POST", body: formData });
        if (!res.ok) throw new Error("업로드 실패");
        const data = await res.json();
        setStatus("처리 대기 중...");

        // 즉시 작업 목록 새로고침
        await fetchJobs();

        pollJob(data.job_id);
      } catch (err) {
        setStatus("업로드/처리 시작 실패");
      } finally {
        uploadBtn.disabled = false;
      }
    });

    refreshBtn.addEventListener("click", fetchJobs);
    refreshFilesBtn.addEventListener("click", fetchFiles);
    processExistingBtn.addEventListener("click", async () => {
      const selected = fileSelect.value;
      if (!selected) {
        setStatus("선택된 파일이 없습니다.");
        return;
      }
      setStatus("기존 파일 처리 시작...");
      try {
        const res = await fetch("/process_existing", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: selected }),
        });
        if (!res.ok) throw new Error("failed");
        const data = await res.json();

        // 즉시 작업 목록 새로고침
        await fetchJobs();

        pollJob(data.job_id);
      } catch (err) {
        setStatus("기존 파일 처리 실패");
      }
    });

    // 검색 및 필터 기능
    let allJobsData = [];
    const searchInput = document.getElementById('search-input');
    const statusFilterSelect = document.getElementById('status-filter');
    const clearFiltersBtn = document.getElementById('clear-filters');

    searchInput?.addEventListener('input', (e) => {
      filterAndRenderJobs();
    });

    statusFilterSelect?.addEventListener('change', () => {
      filterAndRenderJobs();
    });

    clearFiltersBtn?.addEventListener('click', () => {
      searchInput.value = '';
      statusFilterSelect.value = 'all';
      filterAndRenderJobs();
    });

    function filterAndRenderJobs() {
      let filtered = allJobsData;
      const query = searchInput?.value.toLowerCase() || '';
      const status = statusFilterSelect?.value || 'all';

      if (query) {
        filtered = filtered.filter(job =>
          (job.filename || '').toLowerCase().includes(query)
        );
      }

      if (status !== 'all') {
        filtered = filtered.filter(job => job.status === status);
      }

      renderJobs(filtered);
    }

    // fetchJobs 수정: 전역 변수에 저장
    const originalFetchJobs = fetchJobs;
    fetchJobs = async function() {
      try {
        const res = await fetch("/jobs");
        if (!res.ok) throw new Error("failed to load jobs");
        const data = await res.json();
        allJobsData = data;
        filterAndRenderJobs();
      } catch (err) {
        jobsEl.textContent = "목록 불러오기 실패";
      }
    };

    // 다크 모드 토글
    const darkModeBtn = document.getElementById('dark-mode-toggle');
    darkModeBtn?.addEventListener('click', () => {
      document.documentElement.classList.toggle('dark-mode');
      const isDark = document.documentElement.classList.contains('dark-mode');
      darkModeBtn.textContent = isDark ? '☀️' : '🌙';
      localStorage.setItem('darkMode', isDark);
    });

    // 다크 모드 초기화
    if (localStorage.getItem('darkMode') === 'true') {
      document.documentElement.classList.add('dark-mode');
      if (darkModeBtn) darkModeBtn.textContent = '☀️';
    }

    // 작업 삭제
    async function deleteJob(jobId) {
      if (!confirm('이 작업을 삭제하시겠습니까?')) return;
      try {
        const res = await fetch(`/jobs/${jobId}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('삭제 실패');
        await fetchJobs();
      } catch (err) {
        alert('작업 삭제 실패: ' + err.message);
      }
    }

    // 노트 다운로드
    async function downloadNote(jobId, filename) {
      try {
        const res = await fetch(`/note/${jobId}`);
        if (!res.ok) throw new Error('노트 로드 실패');
        const data = await res.json();
        const blob = new Blob([data.content], { type: 'text/markdown' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${filename || 'note'}.md`;
        a.click();
        URL.revokeObjectURL(url);
      } catch (err) {
        alert('노트 다운로드 실패: ' + err.message);
      }
    }

    // 자막 파일 다운로드
    async function downloadFile(filePath, downloadName) {
      try {
        const res = await fetch(`/download?path=${encodeURIComponent(filePath)}`);
        if (!res.ok) throw new Error('파일 다운로드 실패');
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = downloadName;
        a.click();
        URL.revokeObjectURL(url);
      } catch (err) {
        alert('파일 다운로드 실패: ' + err.message);
      }
    }

    // 노트 복사
    async function copyNote(jobId) {
      try {
        const res = await fetch(`/note/${jobId}`);
        if (!res.ok) throw new Error('노트 로드 실패');
        const data = await res.json();
        await navigator.clipboard.writeText(data.content);
        alert('노트가 클립보드에 복사되었습니다!');
      } catch (err) {
        alert('노트 복사 실패: ' + err.message);
      }
    }

    // 재시도
    async function retryJob(jobId) {
      // 구현 필요: 실패한 작업의 파일 경로를 가져와서 다시 처리
      alert('재시도 기능은 준비 중입니다.');
    }

    // 키보드 단축키
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        searchInput?.focus();
      }
      if (e.key === 'Escape' && document.activeElement === searchInput) {
        searchInput.value = '';
        filterAndRenderJobs();
      }
    });

    // 초기화
    fetchJobs();
    fetchFiles();

    // 페이지 로드 시 자동 새로고침 시작
    startAutoRefresh();
  </script>
</body>
</html>
"""


@app.get("/ui", include_in_schema=False)
def ui():
    return HTMLResponse(DASHBOARD_HTML)


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    original_name = Path(file.filename).name
    dest = make_unique_path(UPLOAD_DIR, original_name)
    try:
        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        file.file.close()

    job_id = enqueue_job(dest, original_name)
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
        del jobs[job_id]
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

        note_file = Path(note_path)
        if not note_file.exists():
            raise HTTPException(status_code=404, detail="note file not found")

        try:
            content = note_file.read_text(encoding="utf-8")
            return {"content": content, "path": note_path}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read note: {str(e)}")


@app.get("/download")
def download_file(path: str):
    """파일 다운로드"""
    from fastapi.responses import FileResponse
    import traceback

    try:
        print(f"[DEBUG] 다운로드 요청: {path}")
        file_path = Path(path).resolve()
        print(f"[DEBUG] 절대 경로: {file_path}")
        print(f"[DEBUG] 파일 존재: {file_path.exists()}")

        if not file_path.exists():
            raise HTTPException(status_code=404, detail="file not found")

        # 보안: 허용된 디렉토리 내의 파일만 다운로드 가능
        output_dir = OUTPUT_ROOT.resolve()

        print(f"[DEBUG] OUTPUT_ROOT: {output_dir}")

        try:
            file_path.relative_to(output_dir)
            allowed = True
            print("[DEBUG] OUTPUT_ROOT 내 파일 확인됨")
        except ValueError:
            allowed = False
            print("[DEBUG] 허용되지 않은 경로")

        if not allowed:
            raise HTTPException(status_code=403, detail="access denied")

        print(f"[DEBUG] FileResponse 생성 중: {file_path}")
        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type='application/octet-stream'
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] 다운로드 실패: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")


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
    # security: must be under UPLOAD_DIR
    try:
        candidate.relative_to(UPLOAD_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="path must be inside uploads")
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="file not found")
    if candidate.suffix.lower() not in SUPPORTED_EXTS:
        raise HTTPException(status_code=400, detail="unsupported file type")
    job_id = enqueue_job(candidate, candidate.name)
    return {"job_id": job_id, "status": "queued", "filename": candidate.name}


@app.exception_handler(Exception)
async def exception_handler(request, exc):
    return JSONResponse(status_code=500, content={"detail": str(exc)})
