import json
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

import requests

from src.api import AdRewardRequired
from src.chapter_cache import episode_no as _episode_no
from src.chapter_cache import load_cache, load_failed_episode_nos
from src.chapter_pipeline import (
    AccountChapterPolicy,
    ChapterFetchMode,
    ChapterSelection,
    fetch_chapters,
    select_episodes,
)
from src.contracts import ChapterResult, EpisodeItem, EpisodeListResponse, NovelResponse
from src.epub import EpubBuilder
from src.export import write_txt_chapters
from src.helper import ensure_dir, kebab, normalize_description
from src.novel import fetch_novel_and_episodes


class BuilderClient(Protocol):
    s: requests.Session
    timeout: int

    def me(self) -> Mapping[str, object]: ...

    def novel(self, novel_id: int) -> NovelResponse: ...

    def episode_list(self, novel_id: int, rows: int) -> EpisodeListResponse: ...

    def fetch_episodes_parallel(
        self,
        ep_list: list[EpisodeItem],
        max_workers: int = 1,
        progress_cb: Callable[[], None] | None = None,
        on_result: Callable[[int, ChapterResult], None] | None = None,
    ) -> list[ChapterResult]: ...

    def fetch_episode(
        self,
        ep: EpisodeItem,
        idx: int = 0,
        ticket_data: Mapping[str, object] | None = None,
    ) -> ChapterResult: ...

    def probe_ad_reward_unlock(self, reward: AdRewardRequired) -> Mapping[str, str]: ...


def build_epub(
    client: BuilderClient,
    novel_id: int,
    out_dir: str,
    start_chapter: int | None = None,
    end_chapter: int | None = None,
    max_chapters: int | None = None,
    language: str = "en",
    debug_dump: bool = False,
    update: bool = False,
    retry_failed: bool = False,
    max_workers: int = 1,
) -> tuple[str | None, str, int]:
    data_novel, ep_list, title, account_status = fetch_novel_and_episodes(client, novel_id)
    ep_list = select_episodes(ep_list, ChapterSelection(start_chapter, end_chapter, max_chapters))

    builder = EpubBuilder(out_dir, debug_dump=debug_dump)

    base = kebab(title)
    book_dir = os.path.join(out_dir, base)
    ensure_dir(book_dir)

    # --retry-failed is a no-op when there are no failed chapters at all. This
    # must run BEFORE fetching, because a successful retry clears the failure
    # list, so by the time fetch returns the file would already be gone.
    if retry_failed and not load_failed_episode_nos(book_dir):
        return None, title, 0

    fetched_results, fetched_count = fetch_chapters(
        client,
        book_dir,
        ep_list,
        ChapterFetchMode(
            update=update,
            retry_failed=retry_failed,
            max_workers=max_workers,
            account_policy=AccountChapterPolicy(account_status),
        ),
    )
    # --update is a no-op when the API returned no new chapters: leave any existing
    # build untouched and let the caller report "no updates".
    if update and fetched_count == 0:
        return None, title, 0

    # Render from on-disk .cache merged with this run's fresh fetches, so a
    # --retry-failed run rebuilds the book from cached + retried chapters (and an
    # --update run picks up any progress that was cached before a cancel).
    build_results = merge_cache_with_fetched(ep_list, fetched_results, book_dir)
    if not build_results:
        return None, title, 0

    completed_ep_list = completed_episodes(ep_list, build_results)
    build_metadata(book_dir, data_novel, novel_id, completed_ep_list)

    return builder.build(
        client=client,
        novel=data_novel,
        episodes=completed_ep_list,
        filename_hint=title,
        language=language,
        novel_id=novel_id,
        fetched_results=build_results,
        max_workers=max_workers,
    )


def build_txt(
    client: BuilderClient,
    novel_id: int,
    out_dir: str,
    start_chapter: int | None = None,
    end_chapter: int | None = None,
    max_chapters: int | None = None,
    language: str = "en",
    debug_dump: bool = False,
    update: bool = False,
    retry_failed: bool = False,
    max_workers: int = 1,
) -> tuple[str | None, str, int]:
    data_novel, ep_list, title, account_status = fetch_novel_and_episodes(client, novel_id)
    ep_list = select_episodes(ep_list, ChapterSelection(start_chapter, end_chapter, max_chapters))

    base = kebab(title)
    book_dir = os.path.join(out_dir, base)
    ensure_dir(book_dir)

    # --retry-failed is a no-op when there are no failed chapters at all (see build_epub).
    if retry_failed and not load_failed_episode_nos(book_dir):
        return None, title, 0

    fetched_results, fetched_count = fetch_chapters(
        client,
        book_dir,
        ep_list,
        ChapterFetchMode(
            update=update,
            retry_failed=retry_failed,
            max_workers=max_workers,
            account_policy=AccountChapterPolicy(account_status),
        ),
    )
    # --update is a no-op when the API returned no new chapters (see build_epub);
    # --retry-failed then rebuilds from .cache below.
    if update and fetched_count == 0:
        return None, title, 0

    # Render from on-disk .cache merged with this run's fresh fetches (see build_epub).
    build_results = merge_cache_with_fetched(ep_list, fetched_results, book_dir)
    if not build_results:
        return None, title, 0

    total = write_txt_chapters(book_dir, build_results)

    build_metadata(book_dir, data_novel, novel_id, completed_episodes(ep_list, build_results))

    return book_dir, title, total


def merge_cache_with_fetched(
    ep_list: list[EpisodeItem],
    fetched_results: Sequence[ChapterResult],
    book_dir: str,
) -> list[ChapterResult]:
    """Build the ordered chapter list the builder should render.

    Combines chapters freshly fetched this run (``fetched_results``) with any
    chapters already on disk in ``.cache`` (``book_dir``). This matters for
    ``--retry-failed`` mode, where ``fetch_chapters`` only returns the retried
    subset -- without the merge, every previously-cached good chapter would be
    dropped from the output. Cache entries are only used to fill gaps the fresh
    fetch didn't cover, so a newly-fetched chapter always wins over its cache copy.

    Order follows ``ep_list``; episodes with neither a fresh result nor a cache
    entry are skipped.
    """
    # Index fresh results by episode number so we can overlay them on the cache
    # regardless of list position (retry-failed returns a filtered subset).
    fresh_by_epi: dict[int, ChapterResult] = {}
    for res in fetched_results:
        if not res or "error" in res:
            continue
        epi_no_raw = res.get("epi_no")
        if isinstance(epi_no_raw, int):
            fresh_by_epi[epi_no_raw] = res

    cache = load_cache(book_dir) if os.path.isdir(os.path.join(book_dir, ".cache")) else {}

    merged: list[ChapterResult] = []
    for ep in ep_list:
        epi_no = _episode_no(ep)
        if epi_no is None:
            continue
        fresh = fresh_by_epi.get(epi_no)
        if fresh is not None and fresh.get("html"):
            merged.append(fresh)
            continue
        cached = cache.get(epi_no)
        if cached is not None and cached.get("html"):
            merged.append(cached)
            continue
        # Neither available; nothing to render for this episode.
    return merged


def completed_episodes(ep_list: list[EpisodeItem], fetched_results: Sequence[ChapterResult]) -> list[EpisodeItem]:
    completed_nos: set[int] = set()
    for row in fetched_results:
        row_epi_no = row.get("epi_no")
        if row and "error" not in row and row_epi_no is not None and row.get("html"):
            completed_nos.add(int(row_epi_no))
    if not completed_nos:
        return []
    return [ep for ep in ep_list if (epi_no := _episode_no(ep)) is not None and epi_no in completed_nos]


def build_metadata(
    book_dir: str,
    data_novel: NovelResponse,
    novel_id: int,
    ep_list: list[EpisodeItem],
    max_chapters: int | None = None,
) -> None:
    result = data_novel["result"]
    nv = result["novel"]
    title = nv.get("novel_name", f"novel_{nv.get('novel_no', '')}")
    writers = result.get("writer_list") or []
    author = writers[0].get("writer_name") if writers and writers[0].get("writer_name") else "Unknown Author"
    status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
    description = normalize_description(nv.get("novel_story") or "")

    tag_items = result.get("tag_list") or nv.get("tag_list") or []
    tags: list[str] = []
    for t in tag_items:
        if isinstance(t, str):
            tags.append(t)
        elif isinstance(t, dict):
            val = t.get("tag_name") or t.get("name") or t.get("title")
            if isinstance(val, str):
                tags.append(val)
    uniq_tags = list(dict.fromkeys(tags))

    meta = {
        "url": f"https://global.novelpia.com/novel/{novel_id}",
        "title": nv.get("novel_name") or title,
        "author": author,
        "tags": uniq_tags,
        "chapter": len(ep_list),
        "status": status,
        "description": description,
    }

    meta_path = os.path.join(book_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    chapters_path = os.path.join(book_dir, "chapters.jsonl")
    with open(chapters_path, "w", encoding="utf-8") as f:
        for idx, ep in enumerate(ep_list, 1):
            epi_no = _episode_no(ep)
            if epi_no is None:
                continue
            epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"
            rec = {"idx": idx, "title": epi_title, "url": f"https://global.novelpia.com/viewer/{epi_no}"}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
