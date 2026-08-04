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
- The job detail page polls a small JSON endpoint (/jobs/<id>/log) via
  JavaScript and patches just the log/status DOM in place, instead of a
  <meta http-equiv="refresh"> full-page reload -- that used to yank the
  scroll position back to the top every few seconds. Polling stops once
  the job reaches a terminal status (done/error).
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
_LOG_TAIL_FOR_UI = 500  # lines sent to the browser per poll -- plenty for a live tail


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
        dev_mode = job["dev_mode"]

    real_stdout = sys.stdout
    sys.stdout = _JobLogWriter(job_id, also_forward=real_stdout)
    try:
        # dev_mode_override=True caps this run to 20 items (including how
        # many images get downloaded) and switches to the cheaper dev AI
        # model, same as running `python main.py --dev` from a terminal.
        # False forces dev mode off for this run even if core.config.DEV_MODE
        # is on. See main.py's _apply_dev_mode_override for the mechanics.
        report_path = pipeline.main(url_override=url, non_interactive=True, dev_mode_override=dev_mode)
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
  form { display: flex; flex-direction: column; gap: .3rem; }
  input[type=url] { flex: 1; padding: .5rem; font-size: 1rem; }
  button { padding: .5rem 1rem; font-size: 1rem; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; margin-top: .5rem; }
  th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #8884; font-size: .9rem; vertical-align: top; }
  .status { font-weight: 600; padding: .1rem .5rem; border-radius: .3rem; font-size: .8rem; transition: background-color .3s ease; }
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
  .dev-toggle { display: flex; align-items: center; gap: .35rem; font-size: .85rem; margin-top: .5rem; cursor: pointer; user-select: none; }
  .dev-badge { font-weight: 600; padding: .1rem .4rem; border-radius: .3rem; font-size: .7rem; background: #9b59b655; margin-left: .4rem; }
</style>
</head>
<body>
  <h1>AISaleAnalyst</h1>
  <p class="muted">eBay scraping runs headless on this server — captchas / sign-in walls can't be solved interactively here. Run main.py directly from a terminal if you hit persistent blocks.</p>

  <form method="post" action="{{ url_for('add_job') }}">
    <div style="display:flex; gap:.5rem;">
      <input type="url" name="url" placeholder="Paste an EstateSales.net / .org / MaxSold listing URL" required style="flex:1; padding:.5rem; font-size:1rem;">
      <button type="submit">Queue it</button>
    </div>
    <label class="dev-toggle">
      <input type="checkbox" name="dev_mode" value="1">
      Dev mode (caps to 20 items, downloads only 20 images, uses the cheaper AI model)
    </label>
  </form>

  <h2>Jobs</h2>
  <div id="jobs-container">{{ jobs_html | safe }}</div>

  <h2>Reports</h2>
  <div id="reports-container">{{ reports_html | safe }}</div>

<script>
(function () {
  const jobsBox = document.getElementById("jobs-container");
  const reportsBox = document.getElementById("reports-container");
  let lastJobsHtml = jobsBox.innerHTML;
  let lastReportsHtml = reportsBox.innerHTML;
  let pollHandle = null;

  async function fetchPartial(url) {
    try {
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) return null;
      return await res.text();
    } catch (e) {
      return null; // transient network hiccup -- just try again next tick
    }
  }

  async function poll() {
    const [jobsHtml, reportsHtml] = await Promise.all([
      fetchPartial("{{ url_for('partial_jobs') }}"),
      fetchPartial("{{ url_for('partial_reports') }}"),
    ]);
    // Only touch the DOM when the rendered HTML actually differs --
    // avoids re-rendering (and losing e.g. a mid-click) on every tick
    // when nothing on the dashboard has actually changed.
    if (jobsHtml !== null && jobsHtml !== lastJobsHtml) {
      jobsBox.innerHTML = jobsHtml;
      lastJobsHtml = jobsHtml;
    }
    if (reportsHtml !== null && reportsHtml !== lastReportsHtml) {
      reportsBox.innerHTML = reportsHtml;
      lastReportsHtml = reportsHtml;
    }
  }

  function startPolling() {
    if (pollHandle) return;
    poll();
    pollHandle = setInterval(poll, 2500);
  }

  function stopPolling() {
    if (pollHandle) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
  }

  // Pause polling while the tab isn't visible (saves requests when the
  // dashboard is just sitting in a background tab), and catch up
  // immediately when it becomes visible again.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopPolling();
    } else {
      startPolling();
    }
  });

  startPolling();
})();
</script>
</body>
</html>
"""

_JOBS_SECTION = """
{% if jobs %}
<table>
  <tr><th>URL</th><th>Status</th><th>Queued</th><th>Report</th></tr>
  {% for job in jobs %}
  <tr>
    <td><a href="{{ job.url }}" target="_blank" rel="noopener">{{ job.url }}</a>{% if job.dev_mode %}<span class="dev-badge">DEV</span>{% endif %}</td>
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
"""

_REPORTS_SECTION = """
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
"""

_JOB_PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Job {{ job.id[:8] }}</title>
<style>
  /* Matches the main page's :root rule -- without it this page ignores
     the OS/browser dark-mode preference and always renders light, while
     the main page (which has this rule) follows the system setting. That
     mismatch is what made the two pages look like different themes. */
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  .status { font-weight: 600; padding: .1rem .5rem; border-radius: .3rem; font-size: .85rem; transition: background-color .3s ease; }
  .status-queued { background: #8884; }
  .status-running { background: #f0ad4e55; }
  .status-done { background: #5cb85c55; }
  .status-error { background: #d9534f55; }
  .muted { opacity: .65; font-size: .85rem; }
  .log {
    background: #1118; color: #ddd; padding: .8rem; font-family: ui-monospace, monospace;
    font-size: .8rem; white-space: pre-wrap; border-radius: .4rem; max-height: 70vh;
    overflow-y: auto; scroll-behavior: smooth;
  }
  a { color: #2a7fdb; }
  .dev-badge { font-weight: 600; padding: .1rem .4rem; border-radius: .3rem; font-size: .7rem; background: #9b59b655; margin-left: .5rem; vertical-align: middle; }
</style>
</head>
<body>
  <p><a href="{{ url_for('index') }}">&larr; back</a></p>
  <h1>{{ job.url }}{% if job.dev_mode %}<span class="dev-badge">DEV</span>{% endif %}</h1>
  <p>Status: <span id="status-badge" class="status status-{{ job.status }}">{{ job.status }}</span>
     <span id="error-text" class="muted">{% if job.error %} — {{ job.error }}{% endif %}</span></p>
  <p id="report-link">{% if job.report_path %}<a href="{{ url_for('view_report', filename=job.report_path) }}" target="_blank">Open generated report</a>{% endif %}</p>
  <p class="muted" id="poll-note">Live log — updates automatically while the job runs.</p>
  <div class="log" id="log-box">{{ log_text }}</div>

<script>
(function () {
  const jobId = {{ job.id | tojson }};
  const logBox = document.getElementById("log-box");
  const statusBadge = document.getElementById("status-badge");
  const errorText = document.getElementById("error-text");
  const reportLink = document.getElementById("report-link");
  const pollNote = document.getElementById("poll-note");
  const TERMINAL = new Set(["done", "error"]);
  let pollHandle = null;
  let lastLog = logBox.textContent;   // seed with the server-rendered log so the first poll doesn't re-render it unnecessarily
  let lastStatus = statusBadge.textContent.trim();

  function nearBottom() {
    // Treat "within 40px of the bottom" as still following the tail --
    // this is what lets us auto-scroll on new lines without yanking the
    // view away from someone who's scrolled up to read earlier output.
    return logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 40;
  }

  async function poll() {
    let data;
    try {
      const res = await fetch(`/jobs/${jobId}/log`, { cache: "no-store" });
      if (!res.ok) return;
      data = await res.json();
    } catch (e) {
      return; // transient network hiccup -- just try again next tick
    }

    const newLog = data.log || "(no output yet)";
    // Only touch the DOM when the content actually changed -- writing the
    // same textContent every 2s still forces a reflow/repaint for no
    // visible benefit, which is most of what made this feel choppy.
    if (newLog !== lastLog) {
      const wasNearBottom = nearBottom();
      logBox.textContent = newLog;
      lastLog = newLog;
      if (wasNearBottom) {
        // Animated scroll instead of an instant jump -- smoother when new
        // lines land while you're already watching the tail.
        logBox.scrollTo({ top: logBox.scrollHeight, behavior: "smooth" });
      }
    }

    if (data.status !== lastStatus) {
      statusBadge.textContent = data.status;
      statusBadge.className = "status status-" + data.status;
      lastStatus = data.status;
    }
    errorText.textContent = data.error ? " — " + data.error : "";

    if (data.report_path && !reportLink.querySelector("a")) {
      const a = document.createElement("a");
      a.href = "/reports/" + encodeURIComponent(data.report_path);
      a.target = "_blank";
      a.textContent = "Open generated report";
      reportLink.appendChild(a);
    }

    if (TERMINAL.has(data.status)) {
      pollNote.textContent = "Job finished.";
      if (pollHandle) clearInterval(pollHandle);
    }
  }

  // Land the view at the tail on first load, then poll until done. 1.5s
  // feels noticeably more "live" than 2s without adding meaningful load,
  // since a poll that finds nothing new now skips all DOM work anyway.
  logBox.scrollTop = logBox.scrollHeight;
  poll();
  pollHandle = setInterval(poll, 1500);
})();
</script>
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


def _sorted_jobs() -> list[dict]:
    with _jobs_lock:
        return sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)


@app.route("/")
def index():
    jobs_html = render_template_string(_JOBS_SECTION, jobs=_sorted_jobs())
    reports_html = render_template_string(_REPORTS_SECTION, reports=_report_file_list())
    return render_template_string(_PAGE, jobs_html=jobs_html, reports_html=reports_html)


@app.route("/partial/jobs")
def partial_jobs():
    """Renders just the Jobs table (or its empty state) -- polled by the
    dashboard's JavaScript every 2.5s and swapped into #jobs-container in
    place, so a job flipping from running -> done shows up without a full
    page reload."""
    return render_template_string(_JOBS_SECTION, jobs=_sorted_jobs())


@app.route("/partial/reports")
def partial_reports():
    """Renders just the Reports table (or its empty state) -- polled
    alongside partial_jobs so a newly finished report appears here too
    without a manual refresh."""
    return render_template_string(_REPORTS_SECTION, reports=_report_file_list())


@app.route("/jobs", methods=["POST"])
def add_job():
    url = (request.form.get("url") or "").strip()
    if not url:
        return redirect(url_for("index"))

    # Checked -> force dev mode on for this run (True). Unchecked -> leave
    # as None so main.py falls back to whatever core.config.DEV_MODE
    # already says, same as the CLI's default (--dev omitted) behavior --
    # this deliberately isn't False, so a .env-level DEV_MODE=True isn't
    # silently overridden just because the box was left unticked.
    dev_mode = True if request.form.get("dev_mode") else None

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "url": url,
            "dev_mode": dev_mode,
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
        log_text = "\n".join(job["log"][-_LOG_TAIL_FOR_UI:]) or "(no output yet)"
    return render_template_string(_JOB_PAGE, job=job, log_text=log_text)


@app.route("/jobs/<job_id>/status")
def job_status(job_id: str):
    """JSON status endpoint (no log), for polling from your own tooling if
    you'd rather not scrape the HTML page."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({k: v for k, v in job.items() if k != "log"})


@app.route("/jobs/<job_id>/log")
def job_log(job_id: str):
    """JSON status + a tail of the log, polled by the job detail page's
    JavaScript every 2s to patch the DOM in place (see _JOB_PAGE's
    <script>) instead of doing a full page reload."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "status": job["status"],
            "error": job["error"],
            "report_path": job["report_path"],
            "log": "\n".join(job["log"][-_LOG_TAIL_FOR_UI:]),
        })


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