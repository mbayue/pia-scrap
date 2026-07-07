from fastapi import HTTPException

from web_app import (
    MAX_STORED_JOBS,
    JobRequest,
    JobState,
    _prune_finished_jobs_locked,
    create_job,
    download,
    get_job,
    index,
    jobs,
    jobs_lock,
)


def job_state(status: str, created_at: str) -> JobState:
    return {
        "id": created_at,
        "status": status,
        "created_at": created_at,
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
    with jobs_lock:
        jobs.clear()

    first = create_job(JobRequest(novel_text="5522"))
    second = create_job(JobRequest(novel_text="https://global.novelpia.com/novel/5522"))

    assert second == first
    with jobs_lock:
        assert len(jobs) == 1
        jobs.clear()


def test_create_job_rejects_malformed_input_without_starting_worker(monkeypatch):
    monkeypatch.setattr("web_app.threading.Thread", DummyThread)
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

def test_index_serves_template_html():
    html = index()

    assert "<title>PIA Scrap</title>" in html
    assert 'id="job-form"' in html

def test_download_serves_finished_file_inside_project(monkeypatch, tmp_path):
    out_file = tmp_path / "book.epub"
    out_file.write_text("epub", encoding="utf-8")
    monkeypatch.setattr("web_app.os.getcwd", lambda: str(tmp_path))
    with jobs_lock:
        jobs.clear()
        jobs["job"] = job_state("done", "2000-01-01T00:00:00")
        jobs["job"]["rows"] = [{
            "novel_id": 49,
            "status": "epub",
            "chapters": 1,
            "title": "Book",
            "path": str(out_file),
        }]

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
        jobs["job"]["rows"] = [{
            "novel_id": 49,
            "status": "epub",
            "chapters": 1,
            "title": "Book",
            "path": str(outside),
        }]

    try:
        download("job", 0)
    except HTTPException as exc:
        assert exc.status_code == 403
        assert exc.detail == "Refusing to serve a file outside the project."
    else:
        raise AssertionError("expected HTTPException")
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
