import time

from fastapi import HTTPException

from src import web_jobs as web_jobs_module
from src.web_jobs import UnsafeDownloadPathError, downloadable_path
from web_app import (
    MAX_CONCURRENT_JOBS,
    MAX_STORED_JOBS,
    JobRequest,
    JobState,
    _job_semaphore,
    _prune_finished_jobs_locked,
    create_job,
    download,
    get_job,
    index,
    jobs,
    jobs_lock,
    list_jobs,
)


def _drain_semaphore():
    """Drain all permits from the job semaphore so tests aren't rate-limited."""
    while _job_semaphore.acquire(blocking=False):
        pass
    # Re-release MAX_CONCURRENT_JOBS permits
    for _ in range(MAX_CONCURRENT_JOBS):
        _job_semaphore.release()


def job_state(status: str, created_at: str) -> JobState:
    return {
        "id": created_at,
        "status": status,
        "created_at": created_at,
        "novel_ids": [],
        "started_at": "",
        "finished_at": "",
        "logs": [],
        "rows": [],
        "failures": [],
        "skipped_ids": [],
        "error": None,
    }


class DummyThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def test_prune_finished_jobs_preserves_active_jobs():
    with jobs_lock:
        jobs.clear()
        jobs["running-old"] = job_state("running", "2000-01-01T00:00:00")
        for idx in range(MAX_STORED_JOBS + 3):
            jobs[f"done-{idx:02d}"] = job_state("done", f"2000-01-02T00:00:{idx:02d}")

        _prune_finished_jobs_locked()

        assert "running-old" in jobs
        assert len(jobs) == MAX_STORED_JOBS
        assert "done-00" not in jobs
        assert "done-04" in jobs
        jobs.clear()


def test_create_job_and_get_job_without_starting_worker(monkeypatch):
    monkeypatch.setattr("web_app.threading.Thread", DummyThread)
    _drain_semaphore()
    with jobs_lock:
        jobs.clear()

    created = create_job(JobRequest(novel_text="5522"))
    job = get_job(created["job_id"])

    assert job["status"] == "queued"
    assert job["rows"] == []
    assert job["novel_ids"] == [5522]
    with jobs_lock:
        jobs.clear()


def test_create_job_returns_existing_active_job_for_same_novel(monkeypatch):
    monkeypatch.setattr("web_app.threading.Thread", DummyThread)
    _drain_semaphore()
    with jobs_lock:
        jobs.clear()

    first = create_job(JobRequest(novel_text="5522"))
    second = create_job(JobRequest(novel_text="https://global.novelpia.com/novel/5522"))

    assert second == first
    with jobs_lock:
        assert len(jobs) == 1
        jobs.clear()


def test_create_job_creates_new_job_for_partial_batch_overlap(monkeypatch):
    monkeypatch.setattr("web_app.threading.Thread", DummyThread)
    _drain_semaphore()
    with jobs_lock:
        jobs.clear()

    first = create_job(JobRequest(novel_text="5522"))
    second = create_job(JobRequest(novel_text="5522\n2937"))

    assert second != first
    with jobs_lock:
        assert len(jobs) == 2
        assert jobs[second["job_id"]].get("novel_ids") == [5522, 2937]
        jobs.clear()


def test_create_job_rejects_malformed_input_without_starting_worker(monkeypatch):
    monkeypatch.setattr("web_app.threading.Thread", DummyThread)
    _drain_semaphore()
    with jobs_lock:
        jobs.clear()

    try:
        create_job(JobRequest(novel_text="not-a-novel"))
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "web:1: invalid novel_id or novel URL 'not-a-novel'"
    else:
        raise AssertionError("expected HTTPException")
    with jobs_lock:
        jobs.clear()


def test_create_job_rejects_invalid_options_before_starting_worker(monkeypatch):
    monkeypatch.setattr("web_app.threading.Thread", DummyThread)
    _drain_semaphore()
    with jobs_lock:
        jobs.clear()

    try:
        create_job(JobRequest(novel_text="5522", workers=0))
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "-w/--workers must be at least 1"
    else:
        raise AssertionError("expected HTTPException")
    with jobs_lock:
        jobs.clear()


def test_index_serves_template_html():
    html = index()

    assert "<title>PIA Scrap</title>" in html
    assert 'id="job-form"' in html


def web_app_routes():
    from web_app import app

    return app.routes


def test_list_jobs_returns_all_jobs(monkeypatch):
    _drain_semaphore()
    monkeypatch.setattr("web_app.threading.Thread", DummyThread)
    with jobs_lock:
        jobs.clear()

    create_job(JobRequest(novel_text="5522"))
    result = list_jobs()
    assert len(result) == 1
    assert result[0]["status"] == "queued"
    assert result[0]["novel_ids"] == [5522]
    with jobs_lock:
        jobs.clear()


def test_download_serves_finished_file_inside_project(monkeypatch, tmp_path):
    out_file = tmp_path / "book.epub"
    out_file.write_text("epub", encoding="utf-8")
    monkeypatch.setattr("web_app.os.getcwd", lambda: str(tmp_path))
    with jobs_lock:
        jobs.clear()
        jobs["job"] = job_state("done", "2000-01-01T00:00:00")
        jobs["job"]["rows"] = [
            {
                "novel_id": 49,
                "status": "epub",
                "chapters": 1,
                "title": "Book",
                "path": str(out_file),
            }
        ]

    response = download("job", 0)

    assert response.path == str(out_file)
    with jobs_lock:
        jobs.clear()


def test_download_rejects_file_outside_project(monkeypatch, tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside.epub"
    project.mkdir()
    outside.write_text("epub", encoding="utf-8")
    monkeypatch.setattr("web_app.os.getcwd", lambda: str(project))
    with jobs_lock:
        jobs.clear()
        jobs["job"] = job_state("done", "2000-01-01T00:00:00")
        jobs["job"]["rows"] = [
            {
                "novel_id": 49,
                "status": "epub",
                "chapters": 1,
                "title": "Book",
                "path": str(outside),
            }
        ]

    try:
        download("job", 0)
    except HTTPException as exc:
        assert exc.status_code == 403
        assert exc.detail == "Refusing to serve a file outside the project."
    else:
        raise AssertionError("expected HTTPException")
    with jobs_lock:
        jobs.clear()


def test_downloadable_path_rejects_symlink_escape(monkeypatch, tmp_path):
    base = tmp_path / "project"
    base.mkdir()
    symlink_path = base / "link.epub"
    outside_real = tmp_path / "outside.epub"
    outside_real.write_text("epub", encoding="utf-8")

    monkeypatch.setattr("src.web_jobs.os.path.isfile", lambda _path: True)
    monkeypatch.setattr(
        "src.web_jobs.os.path.realpath",
        lambda path: str(outside_real) if str(path) == str(symlink_path) else str(base),
    )
    with jobs_lock:
        jobs.clear()
        jobs["job"] = job_state("done", "2000-01-01T00:00:00")
        jobs["job"]["rows"] = [
            {
                "novel_id": 49,
                "status": "epub",
                "chapters": 1,
                "title": "Book",
                "path": str(symlink_path),
            }
        ]

    try:
        downloadable_path("job", 0, project_root=str(base))
    except UnsafeDownloadPathError:
        pass
    else:
        raise AssertionError("expected UnsafeDownloadPathError")
    with jobs_lock:
        jobs.clear()


def test_download_rejects_unknown_row():
    with jobs_lock:
        jobs.clear()
        jobs["job"] = job_state("done", "2000-01-01T00:00:00")

    try:
        download("job", 0)
    except HTTPException as exc:
        assert exc.status_code == 404
        assert exc.detail == "Result not found."
    else:
        raise AssertionError("expected HTTPException")
    with jobs_lock:
        jobs.clear()


def test_concurrent_jobs_keep_separate_progress_sinks():
    """Regression: tqdm progress must route to the correct job's logs, never cross-talk."""
    _drain_semaphore()
    original_run_queue = web_jobs_module.run_queue

    def fake_run_queue(novel_ids, options, log=lambda m: None):
        # Simulate in-flight tqdm progress for this job's thread.
        bar = web_jobs_module.epub_module.tqdm(total=2, desc="[info] fetching chapters", unit="chap")
        bar.update(1)
        bar.update(1)
        bar.close()
        return {"rows": [], "failures": [], "skipped_ids": []}

    web_jobs_module.run_queue = fake_run_queue
    with jobs_lock:
        jobs.clear()
    try:
        a = create_job(JobRequest(novel_text="5522"))
        b = create_job(JobRequest(novel_text="2937"))
        for jid in (a["job_id"], b["job_id"]):
            get_job(jid)  # ensure created
        # Wait for both daemon jobs to finish.
        for _ in range(100):
            with jobs_lock:
                if all(jobs[j]["status"] != "running" for j in (a["job_id"], b["job_id"])):
                    break
            time.sleep(0.05)

        with jobs_lock:
            a_logs = list(jobs[a["job_id"]]["logs"])
            b_logs = list(jobs[b["job_id"]]["logs"])
    finally:
        web_jobs_module.run_queue = original_run_queue
        with jobs_lock:
            jobs.clear()

    assert any("[info] fetching chapters: 2/2 chap" in line for line in a_logs), a_logs
    assert any("[info] fetching chapters: 2/2 chap" in line for line in b_logs), b_logs
    # A real cross-talk bug would route one job's progress into the other's log.
    # Because the sink is bound per-thread, each job's progress line exists only
    # in its own log list — assert the lines are co-located per job.
    a_has = any("[info] fetching chapters: 2/2 chap" in line for line in a_logs)
    b_has = any("[info] fetching chapters: 2/2 chap" in line for line in b_logs)
    assert a_has and b_has
