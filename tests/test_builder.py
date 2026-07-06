import json

from src.builder import _fetch_with_cache, _load_failed_episode_nos


class DummyClient:
    def __init__(self, fetched):
        self.fetched = fetched
        self.calls = []

    def fetch_episodes_parallel(self, ep_list, max_workers=1, progress_cb=None):
        self.calls.append((ep_list, max_workers))
        if progress_cb:
            for _ in ep_list:
                progress_cb()
        return self.fetched


def test_fetch_with_cache_uses_cache_and_fetches_missing(tmp_path):
    book_dir = tmp_path / "book"
    cache_dir = book_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "10.json").write_text(
        json.dumps({"epi_no": 10, "html": "cached", "epi_title": "Cached"}), encoding="utf-8"
    )
    client = DummyClient([{"epi_no": 11, "html": "fresh", "epi_title": "Fresh"}])
    episodes = [
        {"episode_no": 10, "epi_num": 1, "epi_title": "Cached"},
        {"episode_no": 11, "epi_num": 2, "epi_title": "Fresh"},
    ]

    results, fetched_count = _fetch_with_cache(client, episodes, str(book_dir), use_cache=True, max_workers=2)

    assert fetched_count == 1
    assert results[0]["html"] == "cached"
    assert results[0]["idx"] == 1
    assert results[1]["html"] == "fresh"
    assert client.calls[0][1] == 2


def test_fetch_with_cache_writes_failed_chapters_and_loads_retry_ids(tmp_path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    client = DummyClient([{"error": "Too many requests", "epi_no": 12, "epi_title": "Failed"}])
    episodes = [{"episode_no": 12, "epi_num": 3, "epi_title": "Failed"}]

    results, fetched_count = _fetch_with_cache(client, episodes, str(book_dir), use_cache=False)

    failed_path = book_dir / "failed_chapters.jsonl"
    failed_rows = [json.loads(line) for line in failed_path.read_text(encoding="utf-8").splitlines()]
    assert fetched_count == 1
    assert results[0]["error"] == "Too many requests"
    assert failed_rows == [{
        "idx": 3,
        "epi_no": 12,
        "title": "Failed",
        "url": "https://global.novelpia.com/viewer/12",
        "error": "Too many requests",
    }]
    assert _load_failed_episode_nos(str(book_dir)) == {12}
