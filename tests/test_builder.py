import json

from src.api import AdRewardRequired, KnownApiBlockError, PremiumEpisodeBlocked
from src.builder import build_epub, build_txt
from src.chapter_cache import (
    fetch_with_cache,
    load_cache,
    load_failed_episode_nos,
    make_incremental_cache_writer,
    write_cache_item_if_absent,
)
from src.chapter_pipeline import (
    AccountChapterPolicy,
    ChapterFetchMode,
    ChapterSelection,
    fetch_chapters,
    select_episodes,
)
from src.contracts import ChapterResult, EpisodeItem, EpisodeListResponse, NovelResponse


class DummyClient:
    def __init__(self, fetched: list[ChapterResult]):
        self.fetched = fetched
        self.calls: list[str | tuple[object, ...]] = []
        self.s = __import__("requests").Session()
        self.timeout = 30

    def fetch_episodes_parallel(
        self,
        ep_list: list[EpisodeItem],
        max_workers: int = 1,
        progress_cb=None,
        on_result=None,
    ) -> list[ChapterResult]:
        self.calls.append((ep_list, max_workers))
        if progress_cb:
            for _ in ep_list:
                progress_cb()
        return self.fetched

    def fetch_episode(self, ep: EpisodeItem, idx: int = 0, ticket_data=None) -> ChapterResult:
        self.calls.append(([ep], 1, ticket_data))
        return self.fetched.pop(0)

    def probe_ad_reward_unlock(self, reward: AdRewardRequired) -> dict[str, str]:
        self.calls.append(([{"episode_no": reward.episode_no, "reward": True}], 1))
        return {"ticket": str(reward.episode_no)}


class FailingFetchClient(DummyClient):
    def fetch_episodes_parallel(
        self,
        ep_list: list[EpisodeItem],
        max_workers: int = 1,
        progress_cb=None,
        on_result=None,
    ) -> list[ChapterResult]:
        self.calls.append((ep_list, max_workers))
        return [
            {
                "idx": 1,
                "epi_no": 42,
                "epi_title": "Bad",
                "html": "",
                "error": "403 Client Error: ?_t=secret-token&x=1",
            }
        ]


class RaisingParallelClient(DummyClient):
    def fetch_episodes_parallel(
        self,
        ep_list: list[EpisodeItem],
        max_workers: int = 1,
        progress_cb=None,
        on_result=None,
    ) -> list[ChapterResult]:
        raise ValueError("fetch failed")


class RaisingFetchClient(DummyClient):
    def fetch_episode(self, ep: EpisodeItem, idx: int = 0, ticket_data=None) -> ChapterResult:
        raise ValueError("fetch failed")


class BuilderClient(DummyClient):
    def __init__(self, me_response: dict[str, object], fetched: list[ChapterResult]):
        super().__init__(fetched)
        self.me_response = me_response

    def me(self) -> dict[str, object]:
        self.calls.append("me")
        return self.me_response

    def novel(self, novel_id: int) -> "NovelResponse":
        self.calls.append(("novel", novel_id))
        return {
            "result": {
                "novel": {"novel_no": novel_id, "novel_name": "Paid Book", "count_epi": 1},
                "info": {"epi_cnt": 1},
            }
        }

    def episode_list(self, novel_id: int, rows: int) -> "EpisodeListResponse":
        self.calls.append(("episode_list", novel_id, rows))
        return {"result": {"list": [{"episode_no": 80, "epi_num": 1, "epi_title": "Paid"}]}}

class PartialBuilderClient(BuilderClient):
    def episode_list(self, novel_id: int, rows: int) -> "EpisodeListResponse":
        self.calls.append(("episode_list", novel_id, rows))
        return {
            "result": {
                "list": [
                    {"episode_no": 81, "epi_num": 1, "epi_title": "One"},
                    {"episode_no": 82, "epi_num": 2, "epi_title": "Premium"},
                    {"episode_no": 83, "epi_num": 3, "epi_title": "Later"},
                ]
            }
        }

class ProbePremiumClient(PartialBuilderClient):
    def probe_ad_reward_unlock(self, reward: AdRewardRequired) -> dict[str, str]:
        self.calls.append(([{"episode_no": reward.episode_no, "reward": True}], 1))
        if reward.episode_no == 82:
            raise KnownApiBlockError(PremiumEpisodeBlocked(novel_no=reward.novel_no, episode_no=reward.episode_no))
        return {"ok": "1"}


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


def test_make_incremental_cache_writer_persists_completed_chapters(tmp_path):
    book_dir = tmp_path / "book"
    episodes: list[EpisodeItem] = [
        {"episode_no": 20, "epi_num": 1, "epi_title": "A"},
        {"episode_no": 21, "epi_num": 2, "epi_title": "B"},
    ]
    writer = make_incremental_cache_writer(str(book_dir), episodes)

    # Simulate chapters finishing one-by-one (e.g. as the parallel fetch completes)
    writer(0, {"epi_no": 20, "html": "a", "epi_title": "A"})
    writer(1, {"epi_no": 21, "html": "b", "epi_title": "B"})

    assert (book_dir / ".cache" / "20.json").exists()
    assert (book_dir / ".cache" / "21.json").exists()
    assert load_cache(str(book_dir)) == {
        20: {"idx": 1, "epi_no": 20, "epi_title": "A", "html": "a"},
        21: {"idx": 2, "epi_no": 21, "epi_title": "B", "html": "b"},
    }


def test_make_incremental_cache_writer_skips_errors_and_runs_best_effort(tmp_path):
    book_dir = tmp_path / "book"
    episodes: list[EpisodeItem] = [{"episode_no": 20, "epi_num": 1, "epi_title": "A"}]
    writer = make_incremental_cache_writer(str(book_dir), episodes)

    # An error result must not be cached (it would be useless on resume).
    writer(0, {"error": "boom", "epi_no": 20, "epi_title": "A"})
    assert not (book_dir / ".cache").exists()


def test_fetch_with_cache_on_result_caches_incrementally_on_cancel(tmp_path):
    # Mirrors the real interrupt scenario: the parallel fetch is cancelled after
    # some chapters complete. Those chapters must already be on disk so a resume
    # (cache) picks them up instead of re-fetching from scratch.
    book_dir = tmp_path / "book"
    cache_dir = book_dir / ".cache"
    cache_dir.mkdir(parents=True)

    class SlowInterruptClient(DummyClient):
        def fetch_episodes_parallel(self, ep_list, max_workers=1, progress_cb=None, on_result=None):
            results = []
            for i, ep in enumerate(ep_list):
                # First chapter succeeds and should be cached immediately.
                res = {"epi_no": ep.get("episode_no"), "html": f"h{i}", "epi_title": ep.get("epi_title", "")}
                results.append(res)
                if on_result is not None:
                    on_result(i, res)
                if i == 0:
                    # Simulate user cancel / error after the first chapter.
                    raise KeyboardInterrupt
            return results

    episodes: list[EpisodeItem] = [
        {"episode_no": 20, "epi_num": 1, "epi_title": "A"},
        {"episode_no": 21, "epi_num": 2, "epi_title": "B"},
    ]
    client = SlowInterruptClient([{"epi_no": 20, "html": "h0", "epi_title": "A"},
                                  {"epi_no": 21, "html": "h1", "epi_title": "B"}])
    writer = make_incremental_cache_writer(str(book_dir), episodes)
    try:
        fetch_with_cache(client, episodes, str(book_dir), use_cache=True, on_result=writer)
    except KeyboardInterrupt:
        pass

    # Only the first chapter was fetched before cancel -> it must be cached.
    assert (cache_dir / "20.json").exists()
    assert not (cache_dir / "21.json").exists()
    assert load_cache(str(book_dir)) == {
        20: {"idx": 1, "epi_no": 20, "epi_title": "A", "html": "h0"}
    }


def test_fetch_with_cache_caches_even_when_use_cache_false(tmp_path):
    # Regression for the "save to .cache regardless of mode" requirement: a plain
    # fetch (no --update/--retry) must still persist every downloaded chapter so a
    # cancel/error mid-run keeps partial progress.
    book_dir = tmp_path / "book"
    client = DummyClient([
        {"epi_no": 20, "html": "a", "epi_title": "A"},
        {"epi_no": 21, "html": "b", "epi_title": "B"},
    ])
    episodes: list[EpisodeItem] = [
        {"episode_no": 20, "epi_num": 1, "epi_title": "A"},
        {"episode_no": 21, "epi_num": 2, "epi_title": "B"},
    ]

    fetch_with_cache(client, episodes, str(book_dir), use_cache=False)

    assert (book_dir / ".cache" / "20.json").exists()
    assert (book_dir / ".cache" / "21.json").exists()


def test_write_cache_item_if_absent_never_overwrites_existing(tmp_path):
    # A chapter already on disk must be left untouched, even if the fresh fetch
    # produced different (or stale) content. Re-running never regresses good cache.
    book_dir = tmp_path / "book"
    cache_dir = book_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "20.json").write_text(
        json.dumps({"idx": 1, "epi_no": 20, "epi_title": "Original", "html": "original"}),
        encoding="utf-8",
    )

    wrote = write_cache_item_if_absent(
        str(book_dir), {"idx": 9, "epi_no": 20, "epi_title": "Fresh", "html": "fresh"}
    )

    assert wrote is False
    row = json.loads((cache_dir / "20.json").read_text(encoding="utf-8"))
    assert row == {"idx": 1, "epi_no": 20, "epi_title": "Original", "html": "original"}


def test_make_incremental_cache_writer_never_overwrites_existing(tmp_path):
    book_dir = tmp_path / "book"
    cache_dir = book_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "20.json").write_text(
        json.dumps({"idx": 1, "epi_no": 20, "epi_title": "Original", "html": "original"}),
        encoding="utf-8",
    )
    episodes: list[EpisodeItem] = [{"episode_no": 20, "epi_num": 1, "epi_title": "A"}]
    writer = make_incremental_cache_writer(str(book_dir), episodes)

    writer(0, {"epi_no": 20, "html": "fresh", "epi_title": "Fresh"})

    row = json.loads((cache_dir / "20.json").read_text(encoding="utf-8"))
    assert row == {"idx": 1, "epi_no": 20, "epi_title": "Original", "html": "original"}


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


def test_load_cache_defaults_bad_idx_without_dropping_row(tmp_path):
    book_dir = tmp_path / "book"
    cache_dir = book_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "ok.json").write_text(
        json.dumps({"idx": "bad", "epi_no": 41, "html": "ok", "epi_title": "OK"}),
        encoding="utf-8",
    )

    cache = load_cache(str(book_dir))

    assert cache == {41: {"idx": 0, "epi_no": 41, "epi_title": "OK", "html": "ok"}}


def test_load_failed_episode_nos_skips_malformed_jsonl(tmp_path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / "failed_chapters.jsonl").write_text(
        '{\n{"idx": 1, "epi_no": 50, "title": "T", "url": "u", "error": "e"}\n',
        encoding="utf-8",
    )

    assert load_failed_episode_nos(str(book_dir)) == {50}


def test_load_failed_episode_nos_skips_bad_epi_no(tmp_path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / "failed_chapters.jsonl").write_text(
        '{"idx": 1, "epi_no": "bad", "title": "T", "url": "u", "error": "e"}\n'
        '{"idx": 2, "epi_no": 51, "title": "T", "url": "u", "error": "e"}\n',
        encoding="utf-8",
    )

    assert load_failed_episode_nos(str(book_dir)) == {51}


def test_fetch_with_cache_redacts_failed_chapter_tokens(tmp_path):
    book_dir = tmp_path / "book"
    client = FailingFetchClient([])
    episodes: list[EpisodeItem] = [{"episode_no": 42, "epi_num": 1, "epi_title": "Bad"}]

    fetch_with_cache(client, episodes, str(book_dir), use_cache=False)

    failed = (book_dir / "failed_chapters.jsonl").read_text(encoding="utf-8")
    assert "secret-token" not in failed
    assert "_t=<redacted>" in failed


def test_select_episodes_skips_malformed_epi_num():
    episodes: list[EpisodeItem] = [
        {"episode_no": 10, "epi_num": 1, "epi_title": "One"},
        {"episode_no": 11, "epi_num": "bad", "epi_title": "Two"},
        {"episode_no": 12, "epi_title": "Three"},
        {"episode_no": 13, "epi_num": 4, "epi_title": "Four"},
    ]
    selected = select_episodes(episodes, ChapterSelection(start_chapter=2))
    assert [row.get("episode_no") for row in selected] == [13]

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
    assert client.calls == [(episodes, 1, None)]


def test_fetch_chapters_retry_failed_with_empty_failed_list_fetches_nothing(tmp_path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / "failed_chapters.jsonl").write_text("", encoding="utf-8")
    client = DummyClient([{"epi_no": 20, "html": "unexpected", "epi_title": "Unexpected"}])
    episodes: list[EpisodeItem] = [
        {"episode_no": 20, "epi_num": 1, "epi_title": "One"},
        {"episode_no": 21, "epi_num": 2, "epi_title": "Two"},
    ]

    results, fetched_count = fetch_chapters(
        client,
        str(book_dir),
        episodes,
        ChapterFetchMode(retry_failed=True, max_workers=1, account_policy=AccountChapterPolicy.PAID),
    )

    assert results == []
    assert fetched_count == 0
    assert client.calls == []


def test_fetch_chapters_retry_failed_filters_non_failed_episodes(tmp_path):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    (book_dir / "failed_chapters.jsonl").write_text(
        json.dumps({"idx": 2, "epi_no": 21, "title": "Two", "url": "", "error": "old"}) + "\n",
        encoding="utf-8",
    )
    client = DummyClient([{"epi_no": 21, "html": "retried", "epi_title": "Two"}])
    episodes: list[EpisodeItem] = [
        {"episode_no": 20, "epi_num": 1, "epi_title": "One"},
        {"episode_no": 21, "epi_num": 2, "epi_title": "Two"},
    ]

    results, fetched_count = fetch_chapters(
        client,
        str(book_dir),
        episodes,
        ChapterFetchMode(retry_failed=True, max_workers=1, account_policy=AccountChapterPolicy.PAID),
    )

    assert fetched_count == 1
    assert [row.get("epi_no") for row in results] == [21]
    assert client.calls == [([episodes[1]], 1)]


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

def test_fetch_chapters_free_policy_unlocks_ads_then_stops_at_premium(tmp_path):
    book_dir = tmp_path / "book"
    client = DummyClient(
        [
            {"epi_no": 40, "html": "one", "epi_title": "One"},
            {"error": "ad reward required: novel_no=7 episode_no=41", "epi_no": 41, "epi_title": "Ad"},
            {"epi_no": 41, "html": "ad", "epi_title": "Ad"},
            {"error": "premium episode blocked: novel_no=7 episode_no=42", "epi_no": 42, "epi_title": "Premium"},
        ]
    )
    episodes: list[EpisodeItem] = [
        {"episode_no": 40, "epi_num": 1, "epi_title": "One"},
        {"episode_no": 41, "epi_num": 2, "epi_title": "Ad"},
        {"episode_no": 42, "epi_num": 3, "epi_title": "Premium"},
        {"episode_no": 43, "epi_num": 4, "epi_title": "Later"},
    ]

    results, fetched_count = fetch_chapters(
        client,
        str(book_dir),
        episodes,
        ChapterFetchMode(account_policy=AccountChapterPolicy.FREE),
    )

    assert fetched_count == 3
    assert [row.get("html") for row in results] == ["one", "ad"]
    assert not (book_dir / "failed_chapters.jsonl").exists()
    assert client.calls == [
        ([episodes[0]], 1, None),
        ([episodes[1]], 1, None),
        ([{"episode_no": 41, "reward": True}], 1),
        ([episodes[1]], 1, {"ticket": "41"}),
        ([{"episode_no": 42, "reward": True}], 1),
        ([episodes[2]], 1, {"ticket": "42"}),
    ]

def test_fetch_chapters_free_policy_logs_ad_gated_info_once(tmp_path, capsys):
    book_dir = tmp_path / "book"
    client = DummyClient(
        [
            {"error": "ad reward required: novel_no=7 episode_no=41", "epi_no": 41, "epi_title": "Ad"},
            {"epi_no": 41, "html": "ad", "epi_title": "Ad"},
            {"epi_no": 42, "html": "next", "epi_title": "Next"},
        ]
    )
    episodes: list[EpisodeItem] = [
        {"episode_no": 41, "epi_num": 1, "epi_title": "Ad"},
        {"episode_no": 42, "epi_num": 2, "epi_title": "Next"},
    ]

    fetch_chapters(
        client,
        str(book_dir),
        episodes,
        ChapterFetchMode(account_policy=AccountChapterPolicy.FREE),
    )

    out = capsys.readouterr().out
    assert out.count("ad-gated chapters detected") == 1

def test_fetch_chapters_paid_policy_keeps_parallel_path_without_reward(tmp_path):
    book_dir = tmp_path / "book"
    client = DummyClient([{"epi_no": 50, "html": "paid", "epi_title": "Paid"}])
    episodes: list[EpisodeItem] = [{"episode_no": 50, "epi_num": 1, "epi_title": "Paid"}]

    results, fetched_count = fetch_chapters(
        client,
        str(book_dir),
        episodes,
        ChapterFetchMode(account_policy=AccountChapterPolicy.PAID, max_workers=3),
    )

    assert fetched_count == 1
    assert results[0].get("html") == "paid"
    assert client.calls == [(episodes, 3)]

def test_fetch_chapters_free_policy_treats_malformed_block_as_normal_failure(tmp_path):
    book_dir = tmp_path / "book"
    client = DummyClient(
        [
            {"error": "ad reward required", "epi_no": 70, "epi_title": "Bad"},
            {"epi_no": 71, "html": "next", "epi_title": "Next"},
        ]
    )
    episodes: list[EpisodeItem] = [
        {"episode_no": 70, "epi_num": 1, "epi_title": "Bad"},
        {"episode_no": 71, "epi_num": 2, "epi_title": "Next"},
    ]

    results, fetched_count = fetch_chapters(
        client,
        str(book_dir),
        episodes,
        ChapterFetchMode(account_policy=AccountChapterPolicy.FREE),
    )

    failed_rows = [json.loads(line) for line in (book_dir / "failed_chapters.jsonl").read_text().splitlines()]
    assert fetched_count == 2
    assert results[0].get("error") == "ad reward required"
    assert results[1].get("html") == "next"
    assert failed_rows[0]["epi_no"] == 70
    assert client.calls == [([episodes[0]], 1, None), ([episodes[1]], 1, None)]

def test_build_txt_uses_paid_account_status_for_parallel_fetch_without_reward(tmp_path):
    client = BuilderClient(
        {"statusCode": "200", "result": {"login": {"mem_nick": "Paid", "mem_plus_type": "1"}}},
        [{"epi_no": 80, "html": "paid", "epi_title": "Paid"}],
    )

    book_dir, title, total = build_txt(client, 7, str(tmp_path), max_workers=4)

    assert total == 1
    assert title == "Paid Book"
    assert book_dir == str(tmp_path / "paid-book")
    assert ([{"episode_no": 80, "epi_num": 1, "epi_title": "Paid"}], 4) in client.calls
    assert ([{"episode_no": 80, "reward": True}], 1) not in client.calls

def test_build_txt_uses_free_account_status_for_reward_flow(tmp_path):
    client = BuilderClient(
        {"statusCode": "200", "result": {"login": {"mem_nick": "Free", "mem_plus_type": 0}}},
        [
            {"error": "ad reward required: novel_no=7 episode_no=80", "epi_no": 80, "epi_title": "Ad"},
            {"epi_no": 80, "html": "ad", "epi_title": "Ad"},
        ],
    )

    _book_dir, _title, total = build_txt(client, 7, str(tmp_path), max_workers=4)

    assert total == 1
    assert ([{"episode_no": 80, "reward": True}], 1) in client.calls

def test_build_txt_preserves_partial_output_after_premium_stop(tmp_path):
    client = PartialBuilderClient(
        {"statusCode": "200", "result": {"login": {"mem_nick": "Free", "mem_plus_type": 0}}},
        [
            {"epi_no": 81, "html": "<p>one</p>", "epi_title": "One"},
            {"error": "premium episode blocked: novel_no=7 episode_no=82", "epi_no": 82, "epi_title": "Premium"},
        ],
    )

    book_dir, _title, total = build_txt(client, 7, str(tmp_path))

    output_dir = tmp_path / "paid-book"
    assert total == 1
    assert book_dir == str(output_dir)
    assert (output_dir / "1_One.txt").read_text(encoding="utf-8") == "one"
    assert not (output_dir / "2_Premium.txt").exists()
    assert not (output_dir / "failed_chapters.jsonl").exists()
    chapters = [json.loads(line) for line in (output_dir / "chapters.jsonl").read_text(encoding="utf-8").splitlines()]
    assert chapters == [{"idx": 1, "title": "One", "url": "https://global.novelpia.com/viewer/81"}]
    assert json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))["chapter"] == 1
    assert json.loads((output_dir / ".cache" / "81.json").read_text(encoding="utf-8")) == {
        "idx": 1,
        "epi_no": 81,
        "epi_title": "One",
        "html": "<p>one</p>",
    }

def test_build_txt_preserves_partial_output_when_reward_probe_hits_premium(tmp_path):
    client = ProbePremiumClient(
        {"statusCode": "200", "result": {"login": {"mem_nick": "Free", "mem_plus_type": 0}}},
        [
            {"error": "ad reward required: novel_no=7 episode_no=81", "epi_no": 81, "epi_title": "One"},
            {"epi_no": 81, "html": "<p>one</p>", "epi_title": "One"},
        ],
    )

    book_dir, _title, total = build_txt(client, 7, str(tmp_path))

    output_dir = tmp_path / "paid-book"
    assert total == 1
    assert book_dir == str(output_dir)
    assert (output_dir / "1_One.txt").read_text(encoding="utf-8") == "one"
    assert not (output_dir / "2_Premium.txt").exists()
    assert not (output_dir / "failed_chapters.jsonl").exists()
    assert ([{"episode_no": 82, "reward": True}], 1) in client.calls

def test_build_epub_preserves_partial_output_after_premium_stop(monkeypatch, tmp_path):
    written = []
    client = PartialBuilderClient(
        {"statusCode": "200", "result": {"login": {"mem_nick": "Free", "mem_plus_type": 0}}},
        [
            {"epi_no": 81, "html": "<p>one</p>", "epi_title": "One"},
            {"error": "premium episode blocked: novel_no=7 episode_no=82", "epi_no": 82, "epi_title": "Premium"},
        ],
    )
    monkeypatch.setattr("src.epub.epub.write_epub", lambda path, _book, _opts: written.append(path))

    out_path, _title, total = build_epub(client, 7, str(tmp_path))

    output_dir = tmp_path / "paid-book"
    assert total == 1
    assert out_path == str(output_dir / "paid-book.epub")
    assert written == [str(output_dir / "paid-book.epub")]
    assert not (output_dir / "failed_chapters.jsonl").exists()
    chapters = [json.loads(line) for line in (output_dir / "chapters.jsonl").read_text(encoding="utf-8").splitlines()]
    assert chapters == [{"idx": 1, "title": "One", "url": "https://global.novelpia.com/viewer/81"}]
    assert json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))["chapter"] == 1
    assert json.loads((output_dir / ".cache" / "81.json").read_text(encoding="utf-8"))["html"] == "<p>one</p>"


def test_build_txt_retry_failed_rebuilds_from_cache_plus_retried(tmp_path):
    # Regression: --retry-failed only fetches the previously-failed chapter, but
    # the built book must still include the already-cached good chapters.
    output_dir = tmp_path / "paid-book"
    cache_dir = output_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "81.json").write_text(
        json.dumps({"idx": 1, "epi_no": 81, "epi_title": "One", "html": "<p>one</p>"}), encoding="utf-8"
    )
    (output_dir / "failed_chapters.jsonl").write_text(
        json.dumps({"idx": 2, "epi_no": 82, "title": "Two", "url": "", "error": "boom"}) + "\n",
        encoding="utf-8",
    )
    client = PartialBuilderClient(
        {"statusCode": "200", "result": {"login": {"mem_nick": "Free", "mem_plus_type": 0}}},
        [{"epi_no": 82, "html": "<p>two</p>", "epi_title": "Two"}],
    )

    book_dir, _title, total = build_txt(client, 7, str(tmp_path), retry_failed=True)

    assert total == 2
    assert book_dir == str(output_dir)
    assert (output_dir / "1_One.txt").read_text(encoding="utf-8") == "one"
    assert (output_dir / "2_Two.txt").read_text(encoding="utf-8") == "two"


def test_build_txt_update_merge_cache_with_new_fetched(tmp_path):
    # --update fetches only the missing chapter, but the output must contain both
    # the cached chapter and the freshly fetched one.
    output_dir = tmp_path / "paid-book"
    cache_dir = output_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "81.json").write_text(
        json.dumps({"idx": 1, "epi_no": 81, "epi_title": "One", "html": "<p>one</p>"}), encoding="utf-8"
    )
    client = PartialBuilderClient(
        {"statusCode": "200", "result": {"login": {"mem_nick": "Free", "mem_plus_type": 0}}},
        [
            {"epi_no": 82, "html": "<p>two</p>", "epi_title": "Premium"},
            {"epi_no": 83, "html": "<p>three</p>", "epi_title": "Later"},
        ],
    )

    book_dir, _title, total = build_txt(client, 7, str(tmp_path), update=True)

    assert total == 3
    assert book_dir == str(output_dir)
    assert (output_dir / "1_One.txt").read_text(encoding="utf-8") == "one"
    assert (output_dir / "3_Later.txt").read_text(encoding="utf-8") == "three"


def test_build_txt_update_noop_when_nothing_new(tmp_path):
    # --update is a no-op when the API fetched nothing new: it must return None
    # (no rebuild) and leave any existing output untouched, even though chapters
    # exist in .cache. --retry-failed differs by rebuilding from cache.
    output_dir = tmp_path / "paid-book"
    cache_dir = output_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "81.json").write_text(
        json.dumps({"idx": 1, "epi_no": 81, "epi_title": "One", "html": "<p>one</p>"}), encoding="utf-8"
    )
    (cache_dir / "82.json").write_text(
        json.dumps({"idx": 2, "epi_no": 82, "epi_title": "Premium", "html": "<p>two</p>"}), encoding="utf-8"
    )
    # Every episode is already cached, so the API returns nothing new.
    (cache_dir / "83.json").write_text(
        json.dumps({"idx": 3, "epi_no": 83, "epi_title": "Later", "html": "<p>three</p>"}), encoding="utf-8"
    )
    client = PartialBuilderClient(
        {"statusCode": "200", "result": {"login": {"mem_nick": "Free", "mem_plus_type": 0}}},
        [],
    )

    book_dir, _title, total = build_txt(client, 7, str(tmp_path), update=True)

    assert book_dir is None
    assert total == 0
    # No chapters rendered to the output dir.
    assert not (output_dir / "1_One.txt").exists()
    assert not (output_dir / "2_Premium.txt").exists()


def test_build_txt_retry_failed_noop_when_no_failed_chapters(tmp_path):
    # --retry-failed is a no-op when there are no failed chapters to retry
    # (no failed_chapters.jsonl), even if chapters exist in .cache.
    output_dir = tmp_path / "paid-book"
    cache_dir = output_dir / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "81.json").write_text(
        json.dumps({"idx": 1, "epi_no": 81, "epi_title": "One", "html": "<p>one</p>"}), encoding="utf-8"
    )
    client = PartialBuilderClient(
        {"statusCode": "200", "result": {"login": {"mem_nick": "Free", "mem_plus_type": 0}}},
        [],
    )

    book_dir, _title, total = build_txt(client, 7, str(tmp_path), retry_failed=True)

    assert book_dir is None
    assert total == 0
    assert not (output_dir / "1_One.txt").exists()

def test_fetch_with_cache_closes_progress_bar_when_parallel_fetch_raises(tmp_path, monkeypatch):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    client = RaisingParallelClient([])
    fake_pbar = None

    class FakeTqdm:
        def __init__(self, *args, **kwargs):
            nonlocal fake_pbar
            self.closed = False
            fake_pbar = self

        def update(self, *args, **kwargs):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr("src.chapter_cache.tqdm", FakeTqdm)

    try:
        fetch_with_cache(client, [{"episode_no": 1}, {"episode_no": 2}], str(book_dir))
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError")

    assert fake_pbar is not None
    assert fake_pbar.closed




def test_fetch_with_account_policy_closes_progress_bar_when_fetch_raises(tmp_path, monkeypatch):
    book_dir = tmp_path / "book"
    book_dir.mkdir()
    client = RaisingFetchClient([])

    fake_pbar = None

    class FakeTqdm:
        def __init__(self, *args, **kwargs):
            nonlocal fake_pbar
            self.closed = False
            fake_pbar = self

        def close(self):
            self.closed = True

        def update(self, *args, **kwargs):
            pass

    monkeypatch.setattr("src.chapter_cache.tqdm", FakeTqdm)

    try:
        from src.chapter_cache import fetch_with_account_policy
        fetch_with_account_policy(client, [{"episode_no": 1}, {"episode_no": 2}], str(book_dir))
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError")

    assert fake_pbar is not None
    assert fake_pbar.closed

