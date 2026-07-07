import os
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from src import web_jobs
from src.runner import QueueOptions
from src.web_jobs import (
    DownloadUnavailableError,
    JobInputError,
    JobNotFoundError,
    ResultNotFoundError,
    UnsafeDownloadPathError,
    downloadable_path,
)
from src.web_jobs import (
    create_job as create_web_job,
)
from src.web_jobs import (
    get_job as get_web_job,
)

app = FastAPI(title="PIA Scrap")
MAX_STORED_JOBS = web_jobs.MAX_STORED_JOBS
JobState = web_jobs.JobState
jobs = web_jobs.jobs
jobs_lock = web_jobs.jobs_lock
_prune_finished_jobs_locked = web_jobs.prune_finished_jobs_locked


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


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.post("/api/jobs")
def create_job(request: JobRequest) -> dict[str, str]:
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
    try:
        return {"job_id": create_web_job(request.novel_text, options, threading.Thread)}
    except JobInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, object]:
    try:
        return dict(get_web_job(job_id))
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found.") from exc


@app.get("/download/{job_id}/{row_index}")
def download(job_id: str, row_index: int) -> FileResponse:
    try:
        resolved = downloadable_path(job_id, row_index)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found.") from exc
    except ResultNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Result not found.") from exc
    except DownloadUnavailableError as exc:
        raise HTTPException(status_code=404, detail="No downloadable file for this result.") from exc
    except UnsafeDownloadPathError as exc:
        raise HTTPException(status_code=403, detail="Refusing to serve a file outside the project.") from exc

    return FileResponse(resolved, filename=os.path.basename(resolved))


def _load_index_html() -> str:
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(template_path, encoding="utf-8") as f:
        return f.read()


HTML = _load_index_html()
