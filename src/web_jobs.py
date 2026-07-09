import contextvars
import os
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final, NotRequired, TypedDict

from src import chapter_cache as chapter_cache_module
from src import epub as epub_module
from src.contracts import QueueSummaryRow
from src.runner import QueueOptions, parse_queue_lines, run_queue

MAX_STORED_JOBS: Final = 50
ACTIVE_JOB_STATUSES: Final = {"queued", "running"}

# Per-thread handle to the active job id. Lets each web job route tqdm progress
# into its own logs without monkey-patching module-level `tqdm` (which is
# process-global and corrupts concurrent jobs). The CLI never sets a sink, so
# calls fall through to the real tqdm.
_active_progress: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_progress", default=None
)


class JobState(TypedDict):
    id: str
    status: str
    created_at: str
    logs: list[str]
    rows: list[QueueSummaryRow]
    failures: list[tuple[int, str]]
    skipped_ids: list[int]
    error: str | None
    novel_ids: NotRequired[list[int]]
    started_at: NotRequired[str]
    finished_at: NotRequired[str]


class JobInputError(Exception):
    pass


class JobNotFoundError(Exception):
    pass


class ResultNotFoundError(Exception):
    pass


class DownloadUnavailableError(Exception):
    pass


class UnsafeDownloadPathError(Exception):
    pass


class JobProgress:
    """Mutable tqdm shim; emits progress into job logs."""

    def __init__(self, job_id: str, total: int = 0, desc: str = "", unit: str = "", **_kwargs) -> None:
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
        replace_or_append_log(self.job_id, message)


def _make_thread_local_tqdm(base_tqdm: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a module's real `tqdm` so web jobs route progress to their own sink.

    When a job id is active for the current thread, tqdm-style calls build a
    JobProgress bound to that job id (with the same args/kwargs tqdm would take,
    preserving desc/total/unit); otherwise they pass through to the real tqdm.
    This keeps progress per-job and avoids mutating shared module state.
    """

    def _tqdm(*args: Any, **kwargs: Any) -> Any:
        job_id = _active_progress.get()
        if job_id is not None:
            return JobProgress(job_id, *args, **kwargs)
        return base_tqdm(*args, **kwargs)

    return _tqdm


jobs: dict[str, JobState] = {}
jobs_lock = threading.Lock()


def append_log(job_id: str, message: str) -> None:
    with jobs_lock:
        jobs[job_id]["logs"].append(message)


def replace_or_append_log(job_id: str, message: str) -> None:
    with jobs_lock:
        logs = jobs[job_id]["logs"]
        if logs and logs[-1].startswith("[info] fetching chapters"):
            logs[-1] = message
        else:
            logs.append(message)


def prune_finished_jobs_locked() -> None:
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


# Install per-thread tqdm dispatchers once at import. These route progress to the
# active job's sink (set per-thread inside run_job) and fall through to the real
# tqdm otherwise. Replacing module-level `tqdm` here is safe: it happens a single
# time at import, never from a request path, so concurrent jobs don't clobber it.
epub_module.tqdm = _make_thread_local_tqdm(epub_module.tqdm)
chapter_cache_module.tqdm = _make_thread_local_tqdm(chapter_cache_module.tqdm)


def run_job(job_id: str, novel_ids: list[int], options: QueueOptions) -> None:
    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["started_at"] = datetime.now().isoformat(timespec="seconds")

    try:
        # Bind this job's id to the current thread. Because the tqdm wrappers
        # above read _active_progress per-call, concurrent jobs running on other
        # threads keep their own sinks (no shared module-state mutation).
        sink_token = _active_progress.set(job_id)
        try:
            result = run_queue(novel_ids, options, log=lambda msg: append_log(job_id, msg))
        finally:
            _active_progress.reset(sink_token)
        with jobs_lock:
            jobs[job_id]["status"] = "failed" if result["failures"] else "done"
            jobs[job_id]["rows"] = result["rows"]
            jobs[job_id]["failures"] = result["failures"]
            jobs[job_id]["skipped_ids"] = result["skipped_ids"]
            jobs[job_id]["finished_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001 - top-level job boundary records worker failure.
        with jobs_lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(exc)
            jobs[job_id]["logs"].append(f"[error] {exc}")
            jobs[job_id]["finished_at"] = datetime.now().isoformat(timespec="seconds")


def create_job(novel_text: str, options: QueueOptions, thread_factory: type[threading.Thread]) -> str:
    try:
        novel_ids = parse_queue_lines(novel_text.splitlines(), source="web")
    except ValueError as exc:
        raise JobInputError(str(exc)) from exc
    if not novel_ids:
        raise JobInputError("Enter at least one novel ID or Novelpia novel URL.")

    job_id = uuid.uuid4().hex
    requested_ids = tuple(novel_ids)
    with jobs_lock:
        for existing_id, job in jobs.items():
            if job.get("status") in ACTIVE_JOB_STATUSES and tuple(job.get("novel_ids", [])) == requested_ids:
                return existing_id
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "novel_ids": novel_ids,
            "logs": [],
            "rows": [],
            "failures": [],
            "skipped_ids": [],
            "error": None,
        }
        prune_finished_jobs_locked()

    thread = thread_factory(target=run_job, args=(job_id, novel_ids, options), daemon=True)
    thread.start()
    return job_id


def get_job(job_id: str) -> JobState:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise JobNotFoundError
        return job.copy()


def downloadable_path(job_id: str, row_index: int, project_root: str | None = None) -> str:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise JobNotFoundError
        rows = job.get("rows", [])
        if row_index < 0 or row_index >= len(rows):
            raise ResultNotFoundError
        path = rows[row_index].get("path")

    if not path or not os.path.isfile(path):
        raise DownloadUnavailableError

    base = os.path.realpath(project_root or os.getcwd())
    resolved = os.path.realpath(path)
    if os.path.commonpath([base, resolved]) != base:
        raise UnsafeDownloadPathError
    return resolved
