#!/usr/bin/env python3
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PROJECT_PYTHON = ROOT / ".venv" / "bin" / "python"
if (
    PROJECT_PYTHON.exists()
    and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve()
    and not os.environ.get("NOTECRAFT_VISUAL_REEXEC")
):
    os.environ["NOTECRAFT_VISUAL_REEXEC"] = "1"
    os.execv(str(PROJECT_PYTHON), [str(PROJECT_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright is not installed. Run:", file=sys.stderr)
    print("  .venv/bin/python -m pip install -r requirements-dev.txt", file=sys.stderr)
    print("  .venv/bin/python -m playwright install chromium", file=sys.stderr)
    raise

PASSWORD = "visual-test-password"
ARTIFACT_DIR = ROOT / "artifacts" / "visual"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def first_existing(paths):
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def wait_for_server(base_url: str, process: subprocess.Popen) -> None:
    for _ in range(80):
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"server exited early\n{output}")
        try:
            with urlopen(base_url + "/health", timeout=0.2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    output = process.stdout.read() if process.stdout else ""
    raise RuntimeError(f"server did not start\n{output}")


def seed_db(db_path: Path, env: dict, note_path: Path) -> None:
    subprocess.run(
        [sys.executable, "-c", "import app; app.init_db(); app.db_conn.close()"],
        cwd=ROOT,
        env=env,
        check=True,
    )
    output_dir = ROOT / "output" / "26_Transformer"
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO jobs
            (id, filename, stored_path, status, stage, note_path, llm_model,
             output_json, output_txt, output_srt, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "visual-job-1",
                "26_Transformer.mp4",
                str(ROOT / "uploads" / "26_Transformer.mp4"),
                "completed",
                "done",
                str(note_path),
                "gpt-5.4",
                str(output_dir / "26_Transformer.json"),
                str(output_dir / "26_Transformer.txt"),
                str(output_dir / "26_Transformer.srt"),
                None,
                now,
                now,
            ),
        )


def collect_metrics(page):
    return page.evaluate(
        """
        () => {
          const doc = document.documentElement;
          const body = document.body;
          const note = document.querySelector('.note-content');
          const card = document.querySelector('.job-card');
          const panel = document.querySelector('.note-panel');
          const action = document.querySelector('.job-actions');
          const text = note ? note.innerText : '';
          const rect = (el) => {
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {left: r.left, right: r.right, width: r.width};
          };
          return {
            viewportWidth: window.innerWidth,
            docScrollWidth: doc.scrollWidth,
            bodyScrollWidth: body.scrollWidth,
            noteScrollWidth: note ? note.scrollWidth : 0,
            noteClientWidth: note ? note.clientWidth : 0,
            noteTextStart: text.slice(0, 140),
            hasFrontmatter: /(^|\\n)(category:|llm_model:|related: \\[|transcript_json:|transcript_txt:|transcript_srt:|source:)/m.test(text),
            hasLocalPath: /(^|\\s)\\/home\\//m.test(text),
            card: rect(card),
            panel: rect(panel),
            action: rect(action),
          };
        }
        """
    )


def assert_layout(name: str, metrics: dict) -> list[str]:
    failures = []
    viewport_width = metrics["viewportWidth"]
    if metrics["docScrollWidth"] > viewport_width + 1:
        failures.append(f"{name}: document overflow {metrics['docScrollWidth']} > {viewport_width}")
    if metrics["bodyScrollWidth"] > viewport_width + 1:
        failures.append(f"{name}: body overflow {metrics['bodyScrollWidth']} > {viewport_width}")
    if name == "mobile" and metrics["noteScrollWidth"] > metrics["noteClientWidth"] + 1:
        failures.append(
            f"{name}: note horizontal overflow {metrics['noteScrollWidth']} > {metrics['noteClientWidth']}"
        )
    if metrics["hasFrontmatter"]:
        failures.append(f"{name}: frontmatter is visible")
    if metrics["hasLocalPath"]:
        failures.append(f"{name}: local filesystem path is visible")
    for key in ("card", "panel", "action"):
        rect = metrics[key]
        if rect and rect["right"] > viewport_width + 1:
            failures.append(f"{name}: {key} exceeds viewport right edge")
    return failures


def main() -> None:
    note_path = first_existing(
        [
            ROOT / "vault" / "지식창고" / "AI" / "26_Transformer-1.md",
            ROOT / "vault" / "지식창고" / "Study" / "26_Transformer-1.md",
            *sorted((ROOT / "vault" / "지식창고").glob("**/*.md")),
        ]
    )
    if not note_path.exists():
        raise FileNotFoundError(f"No note file found for visual check: {note_path}")

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "jobs.db"
        env = os.environ.copy()
        env.update(
            {
                "APP_ADMIN_USERNAME": "yes2310",
                "APP_ADMIN_PASSWORD": PASSWORD,
                "APP_COOKIE_SECURE": "0",
                "DEBUG_ERRORS": "1",
                "JOBS_DB_PATH": str(db_path),
                "UPLOAD_DIR": str(ROOT / "uploads"),
                "OUTPUT_ROOT": str(ROOT / "output"),
                "VAULT_PATH": str(ROOT / "vault"),
                "STATIC_DIR": str(ROOT / "static"),
                "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            }
        )
        seed_db(db_path, env, note_path)

        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_for_server(base_url, server)
            failures = []
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                cases = [
                    ("desktop", {"width": 1440, "height": 1000}, False),
                    ("mobile", {"width": 390, "height": 844}, True),
                ]
                for name, viewport, mobile in cases:
                    context = browser.new_context(viewport=viewport, is_mobile=mobile, has_touch=mobile)
                    page = context.new_page()
                    page.goto(base_url + "/login", wait_until="networkidle")
                    page.fill("#login-username", "yes2310")
                    page.fill("#login-password", PASSWORD)
                    page.click("#login-form button[type='submit']")
                    page.wait_for_url("**/ui", timeout=8000)
                    page.wait_for_selector(".job-card", timeout=8000)
                    page.click("button[data-action='toggle-note']")
                    page.wait_for_selector(".note-content", timeout=8000)
                    page.wait_for_timeout(800)

                    metrics = collect_metrics(page)
                    screenshot = ARTIFACT_DIR / f"{name}-dashboard.png"
                    page.screenshot(path=str(screenshot), full_page=True)
                    print(f"[{name}] screenshot: {screenshot}")
                    print(f"[{name}] metrics: {metrics}")
                    failures.extend(assert_layout(name, metrics))
                    context.close()
                browser.close()

            if failures:
                raise AssertionError("\n".join(failures))
            print("visual checks ok")
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    main()
