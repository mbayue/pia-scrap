from web_app import MAX_STORED_JOBS, JobRequest, _prune_finished_jobs_locked, create_job, get_job, jobs, jobs_lock


class DummyThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def test_prune_finished_jobs_preserves_active_jobs():
    with jobs_lock:
        jobs.clear()
        jobs["running-old"] = {"status": "running", "created_at": "2000-01-01T00:00:00"}
        for idx in range(MAX_STORED_JOBS + 3):
            jobs[f"done-{idx:02d}"] = {"status": "done", "created_at": f"2000-01-02T00:00:{idx:02d}"}

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
    with jobs_lock:
        jobs.clear()
