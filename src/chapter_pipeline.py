from dataclasses import dataclass
from enum import Enum

from src.chapter_cache import (
    ChapterFetchClient,
    fetch_with_account_policy,
    fetch_with_cache,
    load_failed_episode_nos,
)
from src.contracts import ChapterResult, EpisodeItem


def assert_never(value: object) -> None:
    raise AssertionError(f"unreachable value: {value!r}")


def episode_no(ep: EpisodeItem) -> int | None:
    raw_episode_no = ep.get("episode_no")
    if raw_episode_no is None:
        return None
    try:
        return int(raw_episode_no)
    except (TypeError, ValueError):
        return None


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


def select_episodes(episodes: list[EpisodeItem], selection: ChapterSelection) -> list[EpisodeItem]:
    selected = episodes
    if selection.start_chapter:
        selected = [ep for ep in selected if int(ep.get("epi_num", 0)) >= selection.start_chapter]
    if selection.end_chapter:
        selected = [ep for ep in selected if int(ep.get("epi_num", 0)) <= selection.end_chapter]
    if selection.max_chapters:
        selected = selected[: selection.max_chapters]
    return selected


def plan_fetch(book_dir: str, episodes: list[EpisodeItem], mode: ChapterFetchMode) -> ChapterFetchPlan:
    retry_episode_nos: set[int] = load_failed_episode_nos(book_dir) if mode.retry_failed else set()
    planned_episodes = episodes
    if mode.retry_failed and not retry_episode_nos:
        print("[info] no failed chapters to retry")
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
            return fetch_with_cache(
                client,
                plan.episodes,
                book_dir,
                use_cache=plan.use_cache,
                force_episode_nos=plan.retry_episode_nos,
                max_workers=plan.max_workers,
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
