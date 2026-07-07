from dataclasses import dataclass

from src.chapter_cache import ChapterFetchClient, fetch_with_cache, load_failed_episode_nos
from src.contracts import ChapterResult, EpisodeItem


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
    if mode.retry_failed and not retry_episode_nos:
        print("[info] no failed chapters to retry")
    return ChapterFetchPlan(
        episodes=episodes,
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
    return fetch_with_cache(
        client,
        plan.episodes,
        book_dir,
        use_cache=plan.use_cache,
        force_episode_nos=plan.retry_episode_nos,
        max_workers=plan.max_workers,
    )
