from dataclasses import dataclass
from enum import Enum

from src.api import assert_never
from src.chapter_cache import (
    ChapterFetchClient,
    episode_no,
    fetch_with_account_policy,
    fetch_with_cache,
    load_failed_episode_nos,
    make_incremental_cache_writer,
)
from src.contracts import ChapterResult, EpisodeItem
from src.logutil import get_logger

logger = get_logger(__name__)


class AccountChapterPolicy(str, Enum):
    PAID = "paid"
    FREE = "free"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ChapterSelection:
    start_chapter: int | None = None
    end_chapter: int | None = None
    max_chapters: int | None = None


@dataclass(frozen=True, slots=True)
class ChapterFetchMode:
    update: bool = False
    retry_failed: bool = False
    max_workers: int = 1
    account_policy: AccountChapterPolicy = AccountChapterPolicy.UNKNOWN


@dataclass(frozen=True, slots=True)
class ChapterFetchPlan:
    episodes: list[EpisodeItem]
    retry_episode_nos: set[int]
    use_cache: bool
    max_workers: int


def _episode_index(ep: EpisodeItem) -> int | None:
    raw_index = ep.get("epi_num")
    if raw_index is None:
        return None
    try:
        return int(raw_index)
    except (ValueError, TypeError):
        return None


def select_episodes(episodes: list[EpisodeItem], selection: ChapterSelection) -> list[EpisodeItem]:
    selected: list[EpisodeItem] = []
    for ep in episodes:
        index = _episode_index(ep)
        if index is None:
            continue
        if selection.start_chapter is not None and index < selection.start_chapter:
            continue
        if selection.end_chapter is not None and index > selection.end_chapter:
            continue
        selected.append(ep)
    if selection.max_chapters is not None:
        selected = selected[: selection.max_chapters]
    return selected


def plan_fetch(book_dir: str, episodes: list[EpisodeItem], mode: ChapterFetchMode) -> ChapterFetchPlan:
    retry_episode_nos: set[int] = load_failed_episode_nos(book_dir) if mode.retry_failed else set()
    planned_episodes = episodes
    if mode.retry_failed and not retry_episode_nos:
        logger.info("[info] no failed chapters to retry")
        planned_episodes = []
    elif mode.retry_failed:
        planned_episodes = [ep for ep in episodes if episode_no(ep) in retry_episode_nos]
    return ChapterFetchPlan(
        episodes=planned_episodes,
        retry_episode_nos=retry_episode_nos,
        use_cache=mode.update or mode.retry_failed,
        max_workers=max(1, int(mode.max_workers or 1)),
    )


def fetch_chapters(
    client: ChapterFetchClient,
    book_dir: str,
    episodes: list[EpisodeItem],
    mode: ChapterFetchMode,
) -> tuple[list[ChapterResult], int]:
    plan = plan_fetch(book_dir, episodes, mode)
    match mode.account_policy:
        case AccountChapterPolicy.PAID:
            # Always pass an incremental cache writer: it persists each fetched
            # chapter as it arrives (if-absent), so a cancel/error keeps partial
            # progress on every run, not only in --update/--retry mode.
            on_result = make_incremental_cache_writer(book_dir, plan.episodes)
            return fetch_with_cache(
                client,
                plan.episodes,
                book_dir,
                use_cache=plan.use_cache,
                force_episode_nos=plan.retry_episode_nos,
                max_workers=plan.max_workers,
                on_result=on_result,
            )
        case AccountChapterPolicy.FREE | AccountChapterPolicy.UNKNOWN:
            return fetch_with_account_policy(
                client,
                plan.episodes,
                book_dir,
                use_cache=plan.use_cache,
                force_episode_nos=plan.retry_episode_nos,
            )
        case unreachable:
            assert_never(unreachable)

