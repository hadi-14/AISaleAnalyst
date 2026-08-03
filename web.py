"""
webapp.py
=========
Small web front-end for AISaleAnalyst.

Lets you:
- paste an estate-sale listing URL and queue it for processing
- watch each job's status (queued / running / done / error) and a live
  tail of its console log
- browse and open/download every HTML report (and the duplicates .xlsx,
  if enabled) that's ever been generated

Run
---
    pip install flask
    python webapp.py

Then open http://127.0.0.1:5000/

Design notes
------------
- Jobs run **one at a time**, in a single background worker thread. The
  eBay scraper (core/ebay.py) keeps exactly one shared browser/session
  state for the whole process, so running two pipeline jobs concurrently
  would fight over that state. If you need throughput, run several
  separate processes/machines instead of raising worker concurrency here.
- Jobs and their logs are kept in memory only (a plain dict). Restarting
  webapp.py clears job history -- generated reports on disk are
  unaffected, they'll still show up under "Reports".
- Headless eBay scraping: this file sets ``EBAY_HEADLESS=1`` before
  running any job, since a server has no display for a human to solve a
  captcha/sign-in on anyway. See core/ebay.py's module docstring for what
  that trades away (captchas/sign-in walls can't be solved -- those items
  just come back with 0 comps). If you need the visible/interactive
  fallback, run main.py directly from a terminal instead of through this
  app.
"""

import os

# Must be set before core.ebay's Selenium fallback ever launches a
# browser -- _is_headless() reads this env var at launch time, not import
# time, so setting it here (before any job runs) is sufficient even
# though core.ebay may already be imported by the time this module loads.
os.environ.setdefault("EBAY_HEADLESS", "1")

import queue
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template_string, request, send_from_directory, url_for

import main as pipeline  # your main.py, modified to accept url_override / non_interactive
from core.config import OUTPUT_FOLDER, REPORT_OUTPUT_DIR

app = Flask(__name__)

REPORTS_DIR = Path(REPORT_OUTPUT_DIR) if REPORT_OUTPUT_DIR else Path(OUTPUT_FOLDER)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# In-memory job store
# ---------------------------------------------------------------------------
# job = {
#   id, url, status ("queued"|"running"|"done"|"error"),
#   created_at, started_at, finished_at,
#   report_path (str | None), error (str | None),
#   log (list[str]),
# }
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_job_queue: "queue.Queue[str]" = queue.Queue()

_MAX_LOG_LINES = 2000  # per job, to keep memory bounded on long runs


class _JobLogWriter:
    """A file-like object that appends everything written to it into a
    job's in-memory log (trimmed to _MAX_LOG_LINES) instead of, or in
    addition to, stdout. Swapped in as sys.stdout while a job runs, since
    main.py communicates progress via plain print() calls."""

    def __init__(self, job_id: str, also_forward=None):
        self.job_id = job_id
        self._buffer = ""
        self._also_forward = also_forward  # e.g. the real sys.stdout, for server-console visibility

    def write(self, text: str) -> int:
        if self._also_forward is not None:
            try:
                self._also_forward.write(text)
            except Exception:
                pass
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            with _jobs_lock:
                job = _jobs.get(self.job_id)
                if job is not None:
                    job["log"].append(line)
                    if len(job["log"]) > _MAX_LOG_LINES:
                        job["log"] = job["log"][-_MAX_LOG_LINES:]
        return len(text)

    def flush(self) -> None:
        if self._also_forward is not None:
            try:
                self._also_forward.flush()
            except Exception:
                pass


def _worker_loop() -> None:
    """Single background worker: pulls one job id at a time and runs the
    pipeline for it. Sequential on purpose -- see module docstring."""
    while True:
        job_id = _job_queue.get()
        try:
            _run_job(job_id)
        except Exception:
            pass  # _run_job already records any error onto the job itself
        finally:
            _job_queue.task_done()


def _run_job(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "running"
        job["started_at"] = datetime.now().isoformat(timespec="seconds")
        url = job["url"]

    real_stdout = sys.stdout
    sys.stdout = _JobLogWriter(job_id, also_forward=real_stdout)
    try:
        report_path = pipeline.main(url_override=url, non_interactive=True)
        with _jobs_lock:
            job = _jobs[job_id]
            job["report_path"] = report_path
            job["status"] = "done" if report_path else "error"
            if not report_path:
                job["error"] = "Pipeline finished without producing a report (see log)."
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:
        with _jobs_lock:
            job = _jobs[job_id]
            job["status"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
    finally:
        sys.stdout = real_stdout


_worker_thread = threading.Thread(target=_worker_loop, daemon=True)
_worker_thread.start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AISaleAnalyst</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; line-height: 1.4; }
  h1 { font-size: 1.4rem; }
  h2 { font-size: 1.1rem; margin-top: 2.5rem; border-bottom: 1px solid #8888; padding-bottom: .3rem;}
  form { display: flex; gap: .5rem; }
  input[type=url] { flex: 1; padding: .5rem; font-size: 1rem; }
  button { padding: .5rem 1rem; font-size: 1rem; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; margin-top: .5rem; }
  th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #8884; font-size: .9rem; vertical-align: top; }
  .status { font-weight: 600; padding: .1rem .5rem; border-radius: .3rem; font-size: .8rem; }
  .status-queued { background: #8884; }
  .status-running { background: #f0ad4e55; }
  .status-done { background: #5cb85c55; }
  .status-error { background: #d9534f55; }
  .muted { opacity: .65; font-size: .85rem; }
  a { color: #2a7fdb; }
  .log { background: #1118; color: #ddd; padding: .6rem; font-family: ui-monospace, monospace; font-size: .75rem; max-height: 200px; overflow-y: auto; white-space: pre-wrap; border-radius: .3rem; }
  .empty { opacity: .6; font-style: italic; }
  button.danger { background: #d9534f; color: #fff; border: none; border-radius: .3rem; padding: .3rem .7rem; font-size: .8rem; }
  button.danger:hover { background: #c9302c; }
</style>
</head>
<body>
  <h1>AISaleAnalyst</h1>
  <p class="muted">eBay scraping runs headless on this server — captchas / sign-in walls can't be solved interactively here. Run main.py directly from a terminal if you hit persistent blocks.</p>

  <form method="post" action="{{ url_for('add_job') }}">
    <input type="url" name="url" placeholder="Paste an EstateSales.net / .org / MaxSold listing URL" required>
    <button type="submit">Queue it</button>
  </form>

  <h2>Jobs</h2>
  {% if jobs %}
  <table>
    <tr><th>URL</th><th>Status</th><th>Queued</th><th>Report</th></tr>
    {% for job in jobs %}
    <tr>
      <td><a href="{{ job.url }}" target="_blank" rel="noopener">{{ job.url }}</a></td>
      <td><span class="status status-{{ job.status }}">{{ job.status }}</span>
          {% if job.status == 'error' %}<div class="muted">{{ job.error }}</div>{% endif %}</td>
      <td class="muted">{{ job.created_at }}</td>
      <td>
        {% if job.report_path %}<a href="{{ url_for('view_report', filename=job.report_path) }}" target="_blank">Open report</a>{% endif %}
        <div><a href="{{ url_for('job_detail', job_id=job.id) }}">details / log</a></div>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="empty">No jobs yet — paste a URL above.</p>
  {% endif %}

  <h2>Reports</h2>
  {% if reports %}
  <table>
    <tr><th>File</th><th>Generated</th><th></th><th></th></tr>
    {% for r in reports %}
    <tr>
      <td>{{ r.name }}</td>
      <td class="muted">{{ r.modified }}</td>
      <td><a href="{{ url_for('view_report', filename=r.name) }}" target="_blank">View</a></td>
      <td>
        <form method="post" action="{{ url_for('delete_report', filename=r.name) }}"
              onsubmit="return confirm('Delete {{ r.name }}? This cannot be undone.');" style="display:inline">
          <button type="submit" class="danger">Delete</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="empty">No reports generated yet.</p>
  {% endif %}
</body>
</html>
"""

_JOB_PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Job {{ job.id[:8] }}</title>
<meta http-equiv="refresh" content="4">
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  .log { background: #1118; color: #ddd; padding: .8rem; font-family: ui-monospace, monospace; font-size: .8rem; white-space: pre-wrap; border-radius: .4rem; max-height: 70vh; overflow-y: auto; }
  a { color: #2a7fdb; }
</style>
</head>
<body>
  <p><a href="{{ url_for('index') }}">&larr; back</a></p>
  <h1>{{ job.url }}</h1>
  <p>Status: <b>{{ job.status }}</b>{% if job.error %} — {{ job.error }}{% endif %}</p>
  {% if job.report_path %}<p><a href="{{ url_for('view_report', filename=job.report_path) }}" target="_blank">Open generated report</a></p>{% endif %}
  <p class="muted">This page auto-refreshes every 4s while the job runs.</p>
  <div class="log">{{ log_text }}</div>
</body>
</html>
"""


def _report_file_list():
    files = []
    if REPORTS_DIR.exists():
        for f in sorted(REPORTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix.lower() in (".html", ".xlsx"):
                files.append({
                    "name": f.name,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
    return files


@app.route("/")
def index():
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
    return render_template_string(_PAGE, jobs=jobs, reports=_report_file_list())


@app.route("/jobs", methods=["POST"])
def add_job():
    url = (request.form.get("url") or "").strip()
    if not url:
        return redirect(url_for("index"))

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "url": url,
            "status": "queued",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": None,
            "finished_at": None,
            "report_path": None,
            "error": None,
            "log": [],
        }
    _job_queue.put(job_id)
    return redirect(url_for("index"))


@app.route("/jobs/<job_id>")
def job_detail(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return "Job not found", 404
        job = dict(job)  # shallow copy for rendering outside the lock
        log_text = "\n".join(job["log"][-500:]) or "(no output yet)"
    return render_template_string(_JOB_PAGE, job=job, log_text=log_text)


@app.route("/jobs/<job_id>/status")
def job_status(job_id: str):
    """JSON status endpoint, for polling from your own tooling if you'd
    rather not scrape the HTML page."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({k: v for k, v in job.items() if k != "log"})


@app.route("/reports/<path:filename>")
def view_report(filename: str):
    # send_from_directory guards against path traversal outside REPORTS_DIR
    return send_from_directory(REPORTS_DIR, filename)


def _resolve_report_path(filename: str) -> Path | None:
    """Resolve *filename* to a path strictly inside REPORTS_DIR, or None if
    it doesn't exist / would escape that directory (e.g. via '..' or an
    absolute path slipped into the URL). Path.name strips any directory
    components first, so this only ever targets a file directly inside
    REPORTS_DIR -- never a parent or sibling directory."""
    candidate = REPORTS_DIR / Path(filename).name
    try:
        resolved = candidate.resolve()
        resolved.relative_to(REPORTS_DIR.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


@app.route("/reports/<path:filename>/delete", methods=["POST"])
def delete_report(filename: str):
    target = _resolve_report_path(filename)
    if target is None:
        return "File not found", 404

    # Don't let a report get deleted while a job might still be writing it.
    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] == "running":
                return "A job is currently running -- try deleting after it finishes.", 409

    try:
        target.unlink()
    except OSError as exc:
        return f"Could not delete {target.name}: {exc}", 500

    # Also clear the report_path off any job record that pointed at this
    # file, so the "Open report" link on the Jobs table doesn't dangle.
    with _jobs_lock:
        for job in _jobs.values():
            if job.get("report_path") and Path(job["report_path"]).name == target.name:
                job["report_path"] = None

    return redirect(url_for("index"))


if __name__ == "__main__":
    print(f"Reports directory: {REPORTS_DIR.resolve()}")
    print("EBAY_HEADLESS =", os.environ.get("EBAY_HEADLESS"))
    app.run(host="0.0.0.0", port=5000, debug=False)