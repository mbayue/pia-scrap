import os
import threading
import uuid
from datetime import datetime
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from src import builder as builder_module
from src import epub as epub_module
from src.runner import QueueOptions, parse_queue_lines, run_queue


app = FastAPI(title="PIA Scrap")
jobs: Dict[str, Dict] = {}
jobs_lock = threading.Lock()


class JobRequest(BaseModel):
    novel_text: str
    out: str = "output"
    start_chapter: Optional[int] = None
    end_chapter: Optional[int] = None
    max_chapters: int = 0
    lang: str = "en"
    proxy: Optional[str] = None
    debug: bool = False
    throttle: float = 1.25
    workers: int = 1
    update: bool = False
    retry_failed: bool = False
    txt: bool = False
    email: Optional[str] = None
    password: Optional[str] = None
    cookie_file: Optional[str] = None
    cookie_text: Optional[str] = None


def _append_log(job_id: str, message: str) -> None:
    with jobs_lock:
        jobs[job_id]["logs"].append(message)

def _replace_or_append_log(job_id: str, message: str) -> None:
    with jobs_lock:
        logs = jobs[job_id]["logs"]
        if logs and logs[-1].startswith("[info] fetching chapters"):
            logs[-1] = message
        else:
            logs.append(message)

class _JobProgress:
    def __init__(self, job_id: str, total: int = 0, desc: str = "", unit: str = "", **_kwargs):
        self.job_id = job_id
        self.total = int(total or 0)
        self.desc = desc or "[info] progress"
        self.unit = unit or "item"
        self.count = 0
        self._emit()

    def update(self, amount: int = 1) -> None:
        self.count += amount
        self._emit()

    def close(self) -> None:
        self._emit()

    def _emit(self) -> None:
        if self.total:
            message = f"{self.desc}: {self.count}/{self.total} {self.unit}"
        else:
            message = f"{self.desc}: {self.count} {self.unit}"
        _replace_or_append_log(self.job_id, message)

def _run_job(job_id: str, novel_ids: list[int], options: QueueOptions) -> None:
    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["started_at"] = datetime.now().isoformat(timespec="seconds")

    try:
        original_builder_tqdm = builder_module.tqdm
        original_epub_tqdm = epub_module.tqdm
        builder_module.tqdm = lambda *args, **kwargs: _JobProgress(job_id, *args, **kwargs)
        epub_module.tqdm = lambda *args, **kwargs: _JobProgress(job_id, *args, **kwargs)
        try:
            result = run_queue(novel_ids, options, log=lambda msg: _append_log(job_id, msg))
        finally:
            builder_module.tqdm = original_builder_tqdm
            epub_module.tqdm = original_epub_tqdm
        with jobs_lock:
            jobs[job_id]["status"] = "failed" if result["failures"] else "done"
            jobs[job_id]["rows"] = result["rows"]
            jobs[job_id]["failures"] = result["failures"]
            jobs[job_id]["skipped_ids"] = result["skipped_ids"]
            jobs[job_id]["finished_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["logs"].append(f"[error] {e}")
            jobs[job_id]["finished_at"] = datetime.now().isoformat(timespec="seconds")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


@app.post("/api/jobs")
def create_job(request: JobRequest):
    try:
        novel_ids = parse_queue_lines(request.novel_text.splitlines(), source="web")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not novel_ids:
        raise HTTPException(status_code=400, detail="Enter at least one novel ID or Novelpia novel URL.")

    options = QueueOptions(
        out=request.out,
        start_chapter=request.start_chapter,
        end_chapter=request.end_chapter,
        max_chapters=request.max_chapters,
        lang=request.lang,
        proxy=request.proxy,
        debug=request.debug,
        throttle=request.throttle,
        workers=request.workers,
        update=request.update,
        retry_failed=request.retry_failed,
        txt=request.txt,
        email=request.email,
        password=request.password,
        cookie_file=request.cookie_file,
        cookie_text=request.cookie_text,
    )

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "logs": [],
            "rows": [],
            "failures": [],
            "skipped_ids": [],
            "error": None,
        }

    thread = threading.Thread(target=_run_job, args=(job_id, novel_ids, options), daemon=True)
    thread.start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return dict(job)


@app.get("/download/{job_id}/{row_index}")
def download(job_id: str, row_index: int):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        rows = job.get("rows", [])
        if row_index < 0 or row_index >= len(rows):
            raise HTTPException(status_code=404, detail="Result not found.")
        path = rows[row_index].get("path")

    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="No downloadable file for this result.")

    base = os.path.abspath(os.getcwd())
    resolved = os.path.abspath(path)
    if not resolved.startswith(base + os.sep):
        raise HTTPException(status_code=403, detail="Refusing to serve a file outside the project.")

    return FileResponse(resolved, filename=os.path.basename(resolved))


HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PIA Scrap</title>
  <style>
    :root {
      color-scheme: light;
      --background: #f6f6f4;
      --foreground: #18181b;
      --card: #ffffff;
      --card-soft: #fafafa;
      --muted: #71717a;
      --muted-foreground: #52525b;
      --border: #e4e4e7;
      --input: #d4d4d8;
      --primary: #18181b;
      --primary-foreground: #fafafa;
      --secondary: #f4f4f5;
      --secondary-foreground: #27272a;
      --destructive: #dc2626;
      --destructive-soft: #fef2f2;
      --success: #15803d;
      --success-soft: #f0fdf4;
      --info: #2563eb;
      --info-soft: #eff6ff;
      --warning-soft: #fffbeb;
      --terminal: #09090b;
      --terminal-text: #e4e4e7;
      --radius: 14px;
      --radius-sm: 10px;
      --shadow: 0 1px 2px rgba(24, 24, 27, .06), 0 12px 32px rgba(24, 24, 27, .06);
      --space: 16px;
    }
    * { box-sizing: border-box; }
    html { min-height: 100%; }
    body {
      min-height: 100%;
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--background);
      color: var(--foreground);
      line-height: 1.5;
    }
    button, input, textarea, select { font: inherit; }
    a { color: var(--info); font-weight: 650; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .page {
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 24px 0 40px;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
      padding: 10px 0;
    }
    .brand {
      display: flex;
      align-items: baseline;
      gap: 12px;
      min-width: 0;
    }
    .eyebrow {
      margin: 0;
      color: var(--muted);
      font-size: 14px;
      font-weight: 600;
      white-space: nowrap;
    }
    h1, h2, h3, p { margin-top: 0; }
    h1 { margin-bottom: 0; font-size: clamp(22px, 4vw, 28px); letter-spacing: -.035em; line-height: 1.05; white-space: nowrap; }
    h2 { margin-bottom: 4px; font-size: 18px; letter-spacing: -.02em; }
    h3 { margin-bottom: 10px; font-size: 14px; letter-spacing: -.01em; }
    .badge, .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 28px;
      padding: 4px 10px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--card);
      color: var(--muted-foreground);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.06fr) minmax(360px, .94fr);
      gap: 16px;
      align-items: start;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }
    .card-header { padding: 14px 16px 0; }
    .card-body { padding: 14px 16px 16px; }
    .helper { margin: 0; color: var(--muted); font-size: 13px; }
    .form-section {
      padding: 10px 0;
      border-top: 1px solid var(--border);
    }
    .form-section:first-child { padding-top: 0; border-top: 0; }
    .section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    label, .field-label {
      display: grid;
      gap: 7px;
      color: var(--foreground);
      font-size: 13px;
      font-weight: 650;
    }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--input);
      border-radius: var(--radius-sm);
      padding: 9px 11px;
      background: #fff;
      color: var(--foreground);
      outline: none;
      transition: border-color .15s ease, box-shadow .15s ease;
    }
    textarea { min-height: 165px; resize: vertical; line-height: 1.5; }
    .cookie-text { min-height: 120px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; font-size: 12px; }
    input:focus, textarea:focus, select:focus, summary:focus-visible, button:focus-visible {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(24, 24, 27, .12);
    }
    .check-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .check, .toggle-option {
      display: flex;
      align-items: center;
      gap: 9px;
      min-height: 38px;
      padding: 9px 11px;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--card-soft);
      color: var(--foreground);
      font-weight: 600;
    }
    .check input, .toggle-option input { width: 16px; height: 16px; flex: 0 0 auto; }
    .output-toggle { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .format-row {
      display: inline-flex;
      align-items: center;
      width: fit-content;
      min-height: 30px;
      margin: 0 0 8px;
      padding: 4px 10px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--card-soft);
      color: var(--muted-foreground);
      font-size: 12px;
      font-weight: 750;
    }
    .auth-box {
      border-color: var(--border);
      background: var(--card-soft);
    }
    .compact-details { padding-top: 2px; }
    details summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .auth-box summary::after,
    details:not(.auth-box) summary::after {
      content: "+";
      color: var(--muted);
      font-weight: 800;
    }
    .auth-box[open] summary::after,
    details[open]:not(.auth-box) summary::after {
      content: "−";
    }
    details {
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--card-soft);
    }
    summary {
      cursor: pointer;
      list-style: none;
      padding: 11px 12px;
      font-weight: 750;
      border-radius: var(--radius);
      outline: none;
    }
    summary::-webkit-details-marker { display: none; }
    .details-body { padding: 0 12px 12px; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; padding: 8px 0 4px; }
    button {
      min-height: 38px;
      border: 1px solid transparent;
      border-radius: var(--radius-sm);
      padding: 0 16px;
      cursor: pointer;
      font-weight: 750;
      transition: transform .12s ease, background .15s ease, border-color .15s ease, opacity .15s ease;
    }
    button:hover { transform: translateY(-1px); }
    button:disabled { cursor: not-allowed; opacity: .62; transform: none; }
    .primary { background: var(--primary); color: var(--primary-foreground); }
    .secondary { background: var(--secondary); color: var(--secondary-foreground); border-color: var(--border); }
    .error-banner {
      display: none;
      margin-bottom: 14px;
      padding: 11px 12px;
      border: 1px solid #fecaca;
      border-radius: var(--radius-sm);
      background: var(--destructive-soft);
      color: #991b1b;
      font-size: 13px;
      font-weight: 650;
    }
    .error-banner.show { display: block; }
    .status-card { position: sticky; top: 18px; }
    .status-head { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
    .status.idle { background: var(--secondary); color: var(--muted-foreground); }
    .status.queued { background: #f1f5f9; color: #334155; border-color: #cbd5e1; }
    .status.running { background: var(--info-soft); color: #1d4ed8; border-color: #bfdbfe; }
    .status.done { background: var(--success-soft); color: var(--success); border-color: #bbf7d0; }
    .status.failed { background: var(--destructive-soft); color: var(--destructive); border-color: #fecaca; }
    .meta { display: grid; gap: 7px; margin: 12px 0; }
    .meta-row { display: flex; justify-content: space-between; gap: 16px; color: var(--muted); font-size: 13px; }
    .meta-row strong { color: var(--foreground); font-weight: 650; }
    .recent-section { margin-bottom: 12px; }
    .recent-section.hidden { display: none; }
    .recent-jobs {
      display: grid;
      gap: 8px;
      margin: 0;
    }
    .recent-job {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      width: 100%;
      min-height: 38px;
      padding: 8px 10px;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--card-soft);
      color: var(--foreground);
      text-align: left;
      cursor: pointer;
    }
    .recent-job:hover { transform: none; border-color: var(--input); }
    .recent-job.active { border-color: #bfdbfe; background: var(--info-soft); }
    .recent-job-id { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 750; }
    .recent-job-status { color: var(--muted); font-size: 12px; font-weight: 750; }
    .empty-state {
      padding: 14px;
      border: 1px dashed var(--border);
      border-radius: var(--radius);
      background: var(--card-soft);
      color: var(--muted);
      text-align: center;
      font-size: 14px;
    }
    .terminal {
      min-height: 150px;
      max-height: 280px;
      overflow: auto;
      margin: 12px 0 18px;
      padding: 11px 12px;
      border-radius: var(--radius-sm);
      background: var(--terminal);
      color: var(--terminal-text);
      white-space: pre-wrap;
      font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }
    .terminal.empty { color: #a1a1aa; }
    .table-wrap { overflow-x: auto; }
    .table-wrap.hidden { display: none; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 10px 8px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; }
    td:first-child, th:first-child { padding-left: 0; }
    td:last-child, th:last-child { padding-right: 0; }
    .result-cards { display: none; gap: 10px; }
    .result-card { padding: 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--card-soft); }
    .result-card dl { display: grid; gap: 7px; margin: 0; }
    .result-card div { display: flex; justify-content: space-between; gap: 12px; }
    .result-card dt { color: var(--muted); font-size: 12px; font-weight: 750; }
    .result-card dd { margin: 0; text-align: right; font-size: 13px; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    @media (max-width: 900px) {
      .layout { grid-template-columns: 1fr; }
      .status-card { position: static; }
    }
    @media (max-width: 640px) {
      .page { width: min(100% - 24px, 1180px); padding-top: 18px; }
      .topbar { align-items: flex-start; }
      .brand { flex-direction: column; align-items: flex-start; gap: 4px; }
      .eyebrow { white-space: normal; }
      .badge { width: fit-content; }
      .grid, .grid.three, .check-grid, .output-toggle { grid-template-columns: 1fr; }
      .card-header, .card-body { padding-left: 16px; padding-right: 16px; }
      .table-wrap { display: none; }
      .result-cards { display: grid; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; animation: none !important; }
    }
  </style>
</head>
<body>
  <main class="page">
    <header class="topbar">
      <div class="brand">
        <h1>PIA Scrap</h1>
        <p class="eyebrow">Novelpia → EPUB queue runner</p>
      </div>
      <span class="badge">Local FastAPI App</span>
    </header>

    <div class="layout">
      <section class="card" aria-labelledby="new-job-title">
        <div class="card-header">
          <h2 id="new-job-title">New Job</h2>
          <p class="helper">Create EPUB exports from one or more Novelpia novels.</p>
        </div>
        <div class="card-body">
          <div id="error-banner" class="error-banner" role="alert"></div>
          <form id="job-form" novalidate>
            <div class="form-section">
              <div class="section-title"><h3>Novel Source</h3></div>
              <label>
                Novel IDs or URLs
                <textarea name="novel_text" placeholder="https://global.novelpia.com/novel/4874&#10;1234" aria-describedby="novel-help"></textarea>
              </label>
              <p id="novel-help" class="helper">Paste one novel ID or Novelpia URL per line.</p>
            </div>

            <input name="out" type="hidden" value="output">
            <input name="lang" type="hidden" value="en">

            <div class="format-row" aria-label="Output format">Format: EPUB</div>

            <div class="actions">
              <button id="submit-btn" class="primary" type="submit">Start Job</button>
              <button id="clear-btn" class="secondary" type="button">Clear Form</button>
            </div>

            <div class="form-section">
              <details>
                <summary>Chapter Range</summary>
                <div class="details-body">
                  <div class="grid three">
                    <label>Start chapter <input name="start_chapter" type="number" min="1"></label>
                    <label>End chapter <input name="end_chapter" type="number" min="1"></label>
                    <label>Max chapters <input name="max_chapters" type="number" min="0" value="0"></label>
                  </div>
                  <p class="helper" style="margin-top: 8px;">Start/end choose chapter bounds. Max chapters limits total fetches; 0 means all.</p>
                </div>
              </details>
            </div>

            <div class="form-section">
              <details>
                <summary>Download Options</summary>
                <div class="details-body">
                  <input name="workers" type="hidden" value="1">
                  <input name="throttle" type="hidden" value="1.25">
                  <div class="check-grid">
                    <label class="check"><input name="update" type="checkbox"> Update existing cache</label>
                    <label class="check"><input name="retry_failed" type="checkbox"> Retry failed chapters</label>
                  </div>
                  <p class="helper" style="margin-top: 8px;">Update reuses cached chapters and fetches missing/new ones. Retry fetches chapters that failed before.</p>
                </div>
              </details>
            </div>

            <div class="form-section">
              <details class="auth-box">
                <summary>Authentication <span class="helper">Ads / Premium</span></summary>
                <div class="details-body">
                  <label>Paste Netscape cookies
                    <textarea class="cookie-text" name="cookie_text" placeholder="# Netscape HTTP Cookie File&#10;.novelpia.com&#9;TRUE&#9;/&#9;FALSE&#9;0&#9;USERKEY&#9;..."></textarea>
                  </label>
                  <p class="helper" style="margin-top: 8px;">Use cookies to bypass ad-gated chapters or download premium content from your account. Export Netscape cookies with <a href="https://github.com/kairi003/Get-cookies.txt-Locally" target="_blank" rel="noreferrer">Get-cookies.txt-Locally</a>.</p>
                  <input name="cookie_file" type="hidden">
                  <input name="email" type="hidden">
                  <input name="password" type="hidden">
                </div>
              </details>
            </div>

            <div class="form-section">
              <details class="advanced-box">
                <summary>Advanced Settings</summary>
                <div class="details-body compact-details">
                  <input name="debug" type="checkbox" hidden>
                  <label>Proxy <input name="proxy" placeholder="http://host:port"></label>
                  <p class="helper" style="margin-top: 8px;">HTTP/HTTPS/SOCKS proxy, for example http://host:port or socks5h://host:port.</p>
                </div>
              </details>
            </div>
          </form>
        </div>
      </section>

      <section class="card status-card" aria-labelledby="current-job-title">
        <div class="card-header status-head">
          <div>
            <h2 id="current-job-title">Current Job</h2>
            <p class="helper">Progress, logs, and downloadable output.</p>
          </div>
          <span id="status" class="pill status idle">idle</span>
        </div>
        <div class="card-body">
          <div id="job-empty" class="empty-state">Start a job to see progress, logs, and downloads here.</div>
          <div class="meta" aria-label="Job metadata">
            <div class="meta-row"><span>Created at</span><strong id="created-at">—</strong></div>
            <div class="meta-row"><span>Started at</span><strong id="started-at">—</strong></div>
            <div class="meta-row"><span>Finished at</span><strong id="finished-at">—</strong></div>
          </div>
          <div id="recent-section" class="recent-section hidden">
            <div class="section-title"><h3>Recent Jobs</h3></div>
            <div id="recent-empty" class="empty-state">No recent jobs yet.</div>
            <div id="recent-jobs" class="recent-jobs" aria-label="Recent jobs"></div>
          </div>
          <h3>Logs</h3>
          <pre id="logs" class="terminal empty">No logs yet.</pre>
          <div class="section-title">
            <h3>Results</h3>
          </div>
          <div id="results-empty" class="empty-state">No results yet.</div>
          <div class="table-wrap hidden" aria-label="Result table">
            <table>
              <thead><tr><th>Novel ID</th><th>Status</th><th>Chapters</th><th>Title</th><th>File</th></tr></thead>
              <tbody id="rows"></tbody>
            </table>
          </div>
          <div id="result-cards" class="result-cards" aria-label="Result cards"></div>
        </div>
      </section>
    </div>
  </main>
<script>
const form = document.querySelector("#job-form");
const button = document.querySelector("#submit-btn");
const clearButton = document.querySelector("#clear-btn");
const statusEl = document.querySelector("#status");
const logsEl = document.querySelector("#logs");
const rowsEl = document.querySelector("#rows");
const resultCardsEl = document.querySelector("#result-cards");
const errorBanner = document.querySelector("#error-banner");
const jobEmpty = document.querySelector("#job-empty");
const resultsEmpty = document.querySelector("#results-empty");
const recentSection = document.querySelector("#recent-section");
const recentJobsEl = document.querySelector("#recent-jobs");
const recentEmpty = document.querySelector("#recent-empty");
const tableWrap = document.querySelector(".table-wrap");
const createdAtEl = document.querySelector("#created-at");
const startedAtEl = document.querySelector("#started-at");
const finishedAtEl = document.querySelector("#finished-at");
let pollTimer = null;
const activeJobKey = "pia-scrap.activeJobId";
const recentJobsKey = "pia-scrap.recentJobs";
const downloadedJobsKey = "pia-scrap.downloadedJobs";
let activeJobId = localStorage.getItem(activeJobKey);

function value(name) {
  const input = form.elements[name];
  if (!input) return null;
  if (input.type === "checkbox") return input.checked;
  if (input.type === "number") return input.value === "" ? null : Number(input.value);
  return input.value.trim() || null;
}

function setStatus(status) {
  const safeStatus = status || "idle";
  statusEl.textContent = safeStatus;
  statusEl.className = "pill status " + safeStatus;
}

function setError(message) {
  errorBanner.textContent = message || "";
  errorBanner.classList.toggle("show", Boolean(message));
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function rememberJob(jobId) {
  activeJobId = jobId;
  if (jobId) localStorage.setItem(activeJobKey, jobId);
}

function forgetJob() {
  activeJobId = null;
  localStorage.removeItem(activeJobKey);
}

function loadRecentJobs() {
  try {
    const jobs = JSON.parse(localStorage.getItem(recentJobsKey) || "[]");
    return Array.isArray(jobs) ? jobs : [];
  } catch (error) {
    return [];
  }
}

function saveRecentJobs(jobs) {
  localStorage.setItem(recentJobsKey, JSON.stringify(jobs.slice(0, 8)));
}

function loadDownloadedJobs() {
  try {
    const jobs = JSON.parse(localStorage.getItem(downloadedJobsKey) || "[]");
    return Array.isArray(jobs) ? jobs : [];
  } catch (error) {
    return [];
  }
}

function rememberDownloadedJob(jobId) {
  const jobs = loadDownloadedJobs().filter((id) => id !== jobId);
  jobs.unshift(jobId);
  localStorage.setItem(downloadedJobsKey, JSON.stringify(jobs.slice(0, 20)));
}

function autoDownloadResults(job) {
  if (!job.id || job.status !== "done" || loadDownloadedJobs().includes(job.id)) return;
  const epubRows = (job.rows || [])
    .map((row, index) => ({row, index}))
    .filter(({row}) => row.path && row.status === "epub");
  if (!epubRows.length) return;
  rememberDownloadedJob(job.id);
  epubRows.forEach(({index}, offset) => {
    window.setTimeout(() => {
      const link = document.createElement("a");
      link.href = `/download/${job.id}/${index}`;
      link.download = "";
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      link.remove();
    }, offset * 350);
  });
}

function rememberRecentJob(job) {
  const jobId = job.id || activeJobId;
  if (!jobId) return;
  const jobs = loadRecentJobs().filter((item) => item.id !== jobId);
  jobs.unshift({
    id: jobId,
    status: job.status || "queued",
    created_at: job.created_at || new Date().toISOString().slice(0, 19)
  });
  saveRecentJobs(jobs);
  renderRecentJobs();
}

function renderRecentJobs() {
  const jobs = loadRecentJobs();
  recentJobsEl.innerHTML = "";
  recentSection.classList.toggle("hidden", !jobs.length);
  recentEmpty.style.display = jobs.length ? "none" : "block";
  jobs.forEach((job) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "recent-job" + (job.id === activeJobId ? " active" : "");
    item.innerHTML = `<span class="recent-job-id">${escapeHtml(job.id)}</span><span class="recent-job-status">${escapeHtml(job.status || "saved")}</span>`;
    item.addEventListener("click", async () => {
      stopPolling();
      rememberJob(job.id);
      setError("");
      await poll(job.id);
      if (activeJobId && (statusEl.textContent === "queued" || statusEl.textContent === "running")) {
        pollTimer = setInterval(() => poll(activeJobId), 1200);
      }
      renderRecentJobs();
    });
    recentJobsEl.appendChild(item);
  });
}

function setRunning(isRunning) {
  button.disabled = isRunning;
  button.textContent = isRunning ? "Running..." : "Start Job";
}

function render(job) {
  if (job.id) rememberJob(job.id);
  setStatus(job.status);
  jobEmpty.style.display = "none";
  createdAtEl.textContent = job.created_at || "—";
  startedAtEl.textContent = job.started_at || "—";
  finishedAtEl.textContent = job.finished_at || "—";
  rememberRecentJob(job);

  const logs = job.logs || [];
  logsEl.textContent = logs.length ? logs.join("\n") : "No logs yet.";
  logsEl.classList.toggle("empty", !logs.length);
  logsEl.scrollTop = logsEl.scrollHeight;

  rowsEl.innerHTML = "";
  resultCardsEl.innerHTML = "";
  const rows = job.rows || [];
  resultsEmpty.style.display = rows.length ? "none" : "block";
  tableWrap.classList.toggle("hidden", !rows.length);

  rows.forEach((row, index) => {
    const fileCell = row.path && row.status === "epub"
      ? `<a href="/download/${job.id}/${index}">Download</a>`
      : escapeHtml(row.path || "");
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(row.novel_id)}</td><td>${escapeHtml(row.status)}</td><td>${escapeHtml(row.chapters ?? "")}</td><td>${escapeHtml(row.title)}</td><td>${fileCell}</td>`;
    rowsEl.appendChild(tr);

    const card = document.createElement("article");
    card.className = "result-card";
    card.innerHTML = `<dl>
      <div><dt>Novel ID</dt><dd>${escapeHtml(row.novel_id)}</dd></div>
      <div><dt>Status</dt><dd>${escapeHtml(row.status)}</dd></div>
      <div><dt>Chapters</dt><dd>${escapeHtml(row.chapters ?? "")}</dd></div>
      <div><dt>Title</dt><dd>${escapeHtml(row.title)}</dd></div>
      <div><dt>File</dt><dd>${fileCell}</dd></div>
    </dl>`;
    resultCardsEl.appendChild(card);
  });

  if (job.status === "done" || job.status === "failed") {
    setRunning(false);
    stopPolling();
    autoDownloadResults(job);
  } else if (job.status === "queued" || job.status === "running") {
    setRunning(true);
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function readError(response, fallback) {
  try {
    const data = await response.json();
    return data.detail || fallback;
  } catch (error) {
    return fallback;
  }
}

async function poll(jobId) {
  try {
    const res = await fetch(`/api/jobs/${jobId}`);
    if (res.status === 404) {
      const missingJobId = jobId;
      forgetJob();
      saveRecentJobs(loadRecentJobs().filter((item) => item.id !== missingJobId));
      renderRecentJobs();
      throw new Error("Saved job is no longer available. Server reloads clear in-memory jobs.");
    }
    if (!res.ok) throw new Error(await readError(res, "Could not load job status."));
    render(await res.json());
  } catch (error) {
    setError(error.message || "Connection error while polling job status.");
    setRunning(false);
    stopPolling();
  }
}

function validatePayload(payload) {
  if (!payload.novel_text.trim()) return "Paste at least one novel ID or Novelpia URL.";
  if (payload.start_chapter !== null && payload.end_chapter !== null && payload.start_chapter > payload.end_chapter) {
    return "Start chapter cannot be greater than end chapter.";
  }
  return null;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  stopPolling();

  const payload = {
    novel_text: value("novel_text") || "",
    out: value("out") || "output",
    start_chapter: value("start_chapter"),
    end_chapter: value("end_chapter"),
    max_chapters: value("max_chapters") ?? 0,
    lang: value("lang") || "en",
    proxy: value("proxy"),
    debug: value("debug"),
    throttle: value("throttle") ?? 1.25,
    workers: value("workers") ?? 1,
    update: value("update"),
    retry_failed: value("retry_failed"),
    txt: false,
    email: value("email"),
    password: value("password"),
    cookie_file: value("cookie_file"),
    cookie_text: value("cookie_text")
  };

  const validationError = validatePayload(payload);
  if (validationError) {
    setError(validationError);
    return;
  }

  setRunning(true);
  setStatus("queued");
  jobEmpty.style.display = "none";
  logsEl.textContent = "No logs yet.";
  logsEl.classList.add("empty");
  rowsEl.innerHTML = "";
  resultCardsEl.innerHTML = "";
  resultsEmpty.style.display = "block";
  tableWrap.classList.add("hidden");
  createdAtEl.textContent = "—";
  startedAtEl.textContent = "—";
  finishedAtEl.textContent = "—";

  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    if (!res.ok) throw new Error(await readError(res, "Failed to create job."));
    const data = await res.json();
    rememberJob(data.job_id);
    await poll(activeJobId);
    stopPolling();
    pollTimer = setInterval(() => poll(activeJobId), 1200);
  } catch (error) {
    setStatus("failed");
    setError(error.message || "Network error while creating job.");
    setRunning(false);
    stopPolling();
  }
});

clearButton.addEventListener("click", () => {
  form.reset();
  form.elements.out.value = "output";
  form.elements.lang.value = "en";
  form.elements.max_chapters.value = "0";
  form.elements.workers.value = "1";
  form.elements.throttle.value = "1.25";
  setError("");
});

window.addEventListener("DOMContentLoaded", async () => {
  renderRecentJobs();
  if (!activeJobId) return;
  setError("");
  await poll(activeJobId);
  if (activeJobId && (statusEl.textContent === "queued" || statusEl.textContent === "running")) {
    stopPolling();
    pollTimer = setInterval(() => poll(activeJobId), 1200);
  }
});
</script>
</body>
</html>
"""
