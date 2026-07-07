import json

from src.chapter_cache import fetch_with_cache, load_cache, load_failed_episode_nos
from src.chapter_pipeline import ChapterFetchMode, ChapterSelection, fetch_chapters, select_episodes
from src.contracts import ChapterResult, EpisodeItem


class DummyClient:
    def __init__(self, fetched: list[ChapterResult]):
        self.fetched = fetched
        self.calls = []

    def fetch_episodes_parallel(
        self,
        ep_list: list[EpisodeItem],
        max_workers: int = 1,
        progress_cb=None,
    ) -> list[ChapterResult]:
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
    episodes: list[EpisodeItem] = [
        {"episode_no": 10, "epi_num": 1, "epi_title": "Cached"},
        {"episode_no": 11, "epi_num": 2, "epi_title": "Fresh"},
    ]

    results, fetched_count = fetch_with_cache(client, episodes, str(book_dir), use_cache=True, max_workers=2)

    assert fetched_count == 1
    assert results[0].get("html") == "cached"
    assert results[0].get("idx") == 1
    assert results[1].get("html") == "fresh"
    assert client.calls[0][1] == 2


def test_fetch_with_cache_writes_failed_chapters_and_loads_retry_ids(tmp_path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    client = DummyClient([{"error": "Too many requests", "epi_no": 12, "epi_title": "Failed"}])
    episodes: list[EpisodeItem] = [{"episode_no": 12, "epi_num": 3, "epi_title": "Failed"}]

    results, fetched_count = fetch_with_cache(client, episodes, str(book_dir), use_cache=False)

    failed_path = book_dir / "failed_chapters.jsonl"
    failed_rows = [json.loads(line) for line in failed_path.read_text(encoding="utf-8").splitlines()]
    assert fetched_count == 1
    assert results[0].get("error") == "Too many requests"
    assert failed_rows == [
        {
            "idx": 3,
            "epi_no": 12,
            "title": "Failed",
            "url": "https://global.novelpia.com/viewer/12",
            "error": "Too many requests",
        }
    ]
    assert load_failed_episode_nos(str(book_dir)) == {12}


def test_fetch_with_cache_updates_fresh_cache_rows(tmp_path):
    book_dir = tmp_path / "book"
    client = DummyClient([{"epi_no": 20, "html": "fresh", "epi_title": "Fresh"}])
    episodes: list[EpisodeItem] = [{"episode_no": 20, "epi_num": 1, "epi_title": "Fresh"}]

    results, fetched_count = fetch_with_cache(client, episodes, str(book_dir), use_cache=True)

    cache_row = json.loads((book_dir / ".cache" / "20.json").read_text(encoding="utf-8"))
    assert fetched_count == 1
    assert results[0].get("html") == "fresh"
    assert cache_row == {"idx": 1, "epi_no": 20, "epi_title": "Fresh", "html": "fresh"}


def test_fetch_with_cache_retry_failed_refetches_cached_failed_episode(tmp_path):
    book_dir = tmp_path / "book"
    cache_dir = book_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "30.json").write_text(
        json.dumps({"epi_no": 30, "html": "cached", "epi_title": "Cached"}),
        encoding="utf-8",
    )
    client = DummyClient([{"epi_no": 30, "html": "retried", "epi_title": "Retried"}])
    episodes: list[EpisodeItem] = [{"episode_no": 30, "epi_num": 1, "epi_title": "Retried"}]

    results, fetched_count = fetch_with_cache(
        client,
        episodes,
        str(book_dir),
        use_cache=True,
        force_episode_nos={30},
    )

    assert fetched_count == 1
    assert results[0].get("html") == "retried"
    assert client.calls[0][0] == episodes


def test_load_cache_skips_malformed_cache_rows(tmp_path):
    book_dir = tmp_path / "book"
    cache_dir = book_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "bad.json").write_text("{", encoding="utf-8")
    (cache_dir / "missing_html.json").write_text(json.dumps({"epi_no": 40}), encoding="utf-8")
    (cache_dir / "ok.json").write_text(
        json.dumps({"idx": 4, "epi_no": 41, "html": "ok", "epi_title": "OK"}),
        encoding="utf-8",
    )

    cache = load_cache(str(book_dir))

    assert cache == {41: {"idx": 4, "epi_no": 41, "epi_title": "OK", "html": "ok"}}


def test_load_failed_episode_nos_skips_malformed_jsonl(tmp_path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / "failed_chapters.jsonl").write_text(
        '{\n{"idx": 1, "epi_no": 50, "title": "T", "url": "u", "error": "e"}\n',
        encoding="utf-8",
    )

    assert load_failed_episode_nos(str(book_dir)) == {50}


def test_select_episodes_applies_start_end_then_max():
    episodes: list[EpisodeItem] = [
        {"episode_no": 10, "epi_num": 1, "epi_title": "One"},
        {"episode_no": 11, "epi_num": 2, "epi_title": "Two"},
        {"episode_no": 12, "epi_num": 3, "epi_title": "Three"},
        {"episode_no": 13, "epi_num": 4, "epi_title": "Four"},
    ]

    selected = select_episodes(episodes, ChapterSelection(start_chapter=2, end_chapter=4, max_chapters=2))

    assert [row.get("episode_no") for row in selected] == [11, 12]


def test_fetch_chapters_retry_failed_refetches_only_failed_cache_row(tmp_path):
    book_dir = tmp_path / "book"
    cache_dir = book_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "20.json").write_text(
        json.dumps({"idx": 1, "epi_no": 20, "epi_title": "Cached", "html": "cached"}),
        encoding="utf-8",
    )
    (book_dir / "failed_chapters.jsonl").write_text(
        json.dumps(
            {
                "idx": 1,
                "epi_no": 20,
                "title": "Cached",
                "url": "https://global.novelpia.com/viewer/20",
                "error": "old",
            },
        )
        + "\n",
        encoding="utf-8",
    )
    client = DummyClient([{"epi_no": 20, "html": "retried", "epi_title": "Retried"}])
    episodes: list[EpisodeItem] = [{"episode_no": 20, "epi_num": 1, "epi_title": "Retried"}]

    results, fetched_count = fetch_chapters(
        client,
        str(book_dir),
        episodes,
        ChapterFetchMode(retry_failed=True, max_workers=0),
    )

    assert fetched_count == 1
    assert results[0].get("html") == "retried"
    assert client.calls == [(episodes, 1)]


def test_fetch_chapters_update_no_op_when_all_requested_chapters_are_cached(tmp_path):
    book_dir = tmp_path / "book"
    cache_dir = book_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "30.json").write_text(
        json.dumps({"idx": 1, "epi_no": 30, "epi_title": "Cached", "html": "cached"}),
        encoding="utf-8",
    )
    client = DummyClient([])
    episodes: list[EpisodeItem] = [{"episode_no": 30, "epi_num": 1, "epi_title": "Cached"}]

    results, fetched_count = fetch_chapters(
        client,
        str(book_dir),
        episodes,
        ChapterFetchMode(update=True, max_workers=2),
    )

    assert fetched_count == 0
    assert results[0].get("html") == "cached"
    assert client.calls == []
