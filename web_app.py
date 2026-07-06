import os
import threading
import uuid
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from src import builder as builder_module
from src import epub as epub_module
from src.runner import QueueOptions, parse_queue_lines, run_queue

app = FastAPI(title="PIA Scrap")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
MAX_STORED_JOBS = 50
ACTIVE_JOB_STATUSES = {"queued", "running"}


class JobRequest(BaseModel):
    novel_text: str
    out: str = "output"
    start_chapter: int | None = None
    end_chapter: int | None = None
    max_chapters: int = 0
    lang: str = "en"
    proxy: str | None = None
    debug: bool = False
    throttle: float = 1.25
    workers: int = 1
    update: bool = False
    retry_failed: bool = False
    txt: bool = False
    email: str | None = None
    password: str | None = None
    cookie_file: str | None = None
    cookie_text: str | None = None


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

def _prune_finished_jobs_locked() -> None:
    overflow = len(jobs) - MAX_STORED_JOBS
    if overflow <= 0:
        return
    finished_ids = [
        job_id
        for job_id, job in sorted(jobs.items(), key=lambda item: item[1].get("created_at", ""))
        if job.get("status") not in ACTIVE_JOB_STATUSES
    ]
    for job_id in finished_ids[:overflow]:
        jobs.pop(job_id, None)

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
        raise HTTPException(status_code=400, detail=str(e)) from e
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
        _prune_finished_jobs_locked()

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


def _load_index_html() -> str:
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(template_path, encoding="utf-8") as f:
        return f.read()


HTML = _load_index_html()
