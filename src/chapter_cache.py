import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Protocol

from tqdm import tqdm

from src.api import AdRewardRequired, KnownApiBlockError, PremiumEpisodeBlocked
from src.contracts import BlockKind, ChapterResult, EpisodeItem, FailedChapter, parse_block_label
from src.helper import ensure_dir
from src.logutil import get_logger

logger = get_logger(__name__)

TOKEN_RE = re.compile(r"([?&]_t=)[^&\s]+")


class ChapterFetchClient(Protocol):
    def fetch_episodes_parallel(
        self,
        ep_list: list[EpisodeItem],
        max_workers: int = 1,
        progress_cb: Callable[[], None] | None = None,
        on_result: Callable[[int, ChapterResult], None] | None = None,
    ) -> list[ChapterResult]: ...

    def fetch_episode(
        self, ep: EpisodeItem, idx: int = 0, ticket_data: Mapping[str, object] | None = None
    ) -> ChapterResult: ...

    def probe_ad_reward_unlock(self, reward: AdRewardRequired) -> Mapping[str, str]: ...


def episode_no(ep: EpisodeItem) -> int | None:
    episode_no_value = ep.get("episode_no")
    if episode_no_value is None:
        return None
    try:
        return int(episode_no_value)
    except (TypeError, ValueError):
        return None


def chapter_idx(ep: EpisodeItem, fallback: int) -> int:
    try:
        return int(ep.get("epi_num") or fallback)
    except (TypeError, ValueError):
        return fallback


def chapter_title(ep: EpisodeItem) -> str:
    return ep.get("epi_title") or f"Episode {ep.get('epi_num')}"


def cache_dir(book_dir: str) -> str:
    return os.path.join(book_dir, ".cache/")


def cache_file_path(book_dir: str, epi_no: int) -> str:
    return os.path.join(cache_dir(book_dir), f"{epi_no}.json")


def failed_path(book_dir: str) -> str:
    return os.path.join(book_dir, "failed_chapters.jsonl")


def load_jsonl(path: str) -> list[FailedChapter]:
    rows: list[FailedChapter] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(
                        {
                            "idx": int(row.get("idx") or 0),
                            "epi_no": int(row["epi_no"]) if row.get("epi_no") is not None else None,
                            "title": str(row.get("title") or ""),
                            "url": str(row.get("url") or ""),
                            "error": str(row.get("error") or ""),
                        }
                    )
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return rows


def load_cache(book_dir: str) -> dict[int, ChapterResult]:
    cache: dict[int, ChapterResult] = {}
    cache_path = cache_dir(book_dir)
    if os.path.isdir(cache_path):
        for name in os.listdir(cache_path):
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(cache_path, name)
            try:
                with open(path, encoding="utf-8") as f:
                    row = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"[warn] Ignoring unreadable cache file {path}: {e}")
                continue
            if not isinstance(row, dict):
                continue
            raw_epi_no = row.get("epi_no")
            if raw_epi_no is None:
                continue
            try:
                epi_no = int(raw_epi_no)
            except (TypeError, ValueError) as e:
                logger.warning(f"[warn] Ignoring cache row with invalid epi_no in {path}: {e}")
                continue
            html_text = row.get("html")
            if isinstance(html_text, str) and html_text:
                try:
                    idx = int(row.get("idx") or 0)
                except (TypeError, ValueError):
                    idx = 0
                cache[epi_no] = {
                    "idx": idx,
                    "epi_no": epi_no,
                    "epi_title": str(row.get("epi_title") or ""),
                    "html": html_text,
                }
    return cache


def load_failed_episode_nos(book_dir: str) -> set[int]:
    failed: set[int] = set()
    for row in load_jsonl(failed_path(book_dir)):
        epi_no = row.get("epi_no")
        if isinstance(epi_no, int):
            failed.add(epi_no)
    return failed


def write_jsonl(path: str, rows: Iterable[FailedChapter]) -> None:
    parent = os.path.dirname(path)
    if parent:
        ensure_dir(parent)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            safe_row = dict(row)
            safe_row["error"] = TOKEN_RE.sub(r"\1<redacted>", str(safe_row.get("error") or ""))
            f.write(json.dumps(safe_row, ensure_ascii=False) + "\n")


def write_cache_item(book_dir: str, row: ChapterResult) -> None:
    row_epi_no = row.get("epi_no")
    if row_epi_no is None:
        raise ValueError("cache row missing epi_no")
    epi_no = int(row_epi_no)
    ensure_dir(cache_dir(book_dir))
    with open(cache_file_path(book_dir, epi_no), "w", encoding="utf-8") as f:
        json.dump(row, f, ensure_ascii=False, indent=2)


def write_cache_item_if_absent(book_dir: str, row: ChapterResult) -> bool:
    row_epi_no = row.get("epi_no")
    if row_epi_no is None:
        raise ValueError("cache row missing epi_no")
    path = cache_file_path(book_dir, int(row_epi_no))
    if os.path.exists(path):
        return False
    write_cache_item(book_dir, row)
    return True


def normalize_failed_chapter(ep: EpisodeItem, res: ChapterResult, pos: int) -> FailedChapter:
    epi_no = episode_no(ep)
    return {
        "idx": chapter_idx(ep, pos),
        "epi_no": epi_no,
        "title": chapter_title(ep),
        "url": f"https://global.novelpia.com/viewer/{epi_no}" if epi_no else "",
        "error": TOKEN_RE.sub(r"\1<redacted>", str(res.get("error", "Unknown error"))),
    }


def normalize_cache_row(ep: EpisodeItem, res: ChapterResult, pos: int) -> ChapterResult | None:
    epi_no = episode_no(ep)
    cache_epi_no = res.get("epi_no") or epi_no
    if cache_epi_no is None:
        return None
    return {
        "idx": chapter_idx(ep, pos),
        "epi_no": int(cache_epi_no),
        "epi_title": res.get("epi_title") or chapter_title(ep),
        "html": res.get("html") or "",
    }


def make_incremental_cache_writer(
    book_dir: str,
    episodes: list[EpisodeItem],
) -> Callable[[int, ChapterResult], None]:
    def write_result(index: int, result: ChapterResult) -> None:
        if not result or "error" in result:
            return
        if index < 0 or index >= len(episodes):
            return
        cache_row = normalize_cache_row(episodes[index], result, index + 1)
        if cache_row is not None:
            write_cache_item_if_absent(book_dir, cache_row)

    return write_result

def known_block_from_result(res: ChapterResult) -> tuple[BlockKind, int, int] | None:
    error = res.get("error")
    if error is None:
        return None
    return parse_block_label(error)

def premium_block_from_error(error: KnownApiBlockError) -> PremiumEpisodeBlocked | None:
    if isinstance(error.block, PremiumEpisodeBlocked):
        return error.block
    return None

def fetch_with_account_policy(
    client: ChapterFetchClient,
    ep_list: list[EpisodeItem],
    book_dir: str,
    *,
    use_cache: bool = False,
    force_episode_nos: set[int] | None = None,
) -> tuple[list[ChapterResult], int]:
    cache = load_cache(book_dir) if use_cache else {}
    forced_nos = force_episode_nos or set()
    results: list[ChapterResult] = []
    failed_rows: list[FailedChapter] = []
    fetched_count = 0
    ad_novel_no: int | None = None
    ad_info_printed = False

    pbar = tqdm(total=len(ep_list), desc="[info] fetching chapters", unit="chap")
    try:
        for pos, ep in enumerate(ep_list, 1):
            epi_no = episode_no(ep)
            if epi_no is not None and epi_no in cache and epi_no not in forced_nos:
                results.append({**cache[epi_no], "idx": pos})
                pbar.update(1)
                continue

            if epi_no is not None and ad_novel_no is not None:
                try:
                    ticket_data = client.probe_ad_reward_unlock(
                        AdRewardRequired(novel_no=ad_novel_no, episode_no=epi_no)
                    )
                except KnownApiBlockError as e:
                    if premium_block_from_error(e) is not None:
                        logger.info(f"[info] stopped at premium chapter: episode_no={epi_no}")
                        break
                    res: ChapterResult = {"error": str(e), "epi_no": epi_no, "epi_title": chapter_title(ep), "idx": pos}
                    results.append(res)
                    failed_rows.append(normalize_failed_chapter(ep, res, pos))
                    pbar.update(1)
                    continue
                else:
                    fetched_count += 1
                    res = client.fetch_episode(ep, pos, ticket_data=ticket_data)
                    block = known_block_from_result(res)
                    if block is not None and block[0] == "premium episode blocked":
                        logger.info(f"[info] stopped at premium chapter: episode_no={block[2]}")
                        break
                    results.append(res)
                    if not res or "error" in res:
                        failed_rows.append(normalize_failed_chapter(ep, res or {}, pos))
                    elif (cache_row := normalize_cache_row(ep, res, pos)) is not None:
                        write_cache_item(book_dir, cache_row)
                    pbar.update(1)
                    continue

            fetched_count += 1
            res = client.fetch_episode(ep, pos)
            block = known_block_from_result(res)
            if block is not None and block[0] == "premium episode blocked":
                logger.info(f"[info] stopped at premium chapter: episode_no={block[2]}")
                break
            if block is not None and block[0] == "ad reward required":
                ad_novel_no = block[1]
                if not ad_info_printed:
                    logger.info("[info] ad-gated chapters detected; using rewarded access for later free chapters")
                    ad_info_printed = True
                try:
                    ticket_data = client.probe_ad_reward_unlock(
                        AdRewardRequired(novel_no=block[1], episode_no=block[2])
                    )
                except KnownApiBlockError as e:
                    if premium_block_from_error(e) is not None:
                        logger.info(f"[info] stopped at premium chapter: episode_no={block[2]}")
                        break
                    res = {"error": str(e), "epi_no": block[2], "epi_title": chapter_title(ep), "idx": pos}
                    results.append(res)
                    failed_rows.append(normalize_failed_chapter(ep, res, pos))
                    pbar.update(1)
                    continue
                res = client.fetch_episode(ep, pos, ticket_data=ticket_data)
                block = known_block_from_result(res)
                if block is not None and block[0] == "premium episode blocked":
                    logger.info(f"[info] stopped at premium chapter: episode_no={block[2]}")
                    break

            results.append(res)
            if not res or "error" in res:
                failed_rows.append(normalize_failed_chapter(ep, res or {}, pos))
            elif (cache_row := normalize_cache_row(ep, res, pos)) is not None:
                write_cache_item(book_dir, cache_row)
            pbar.update(1)
    finally:
        pbar.close()

    if failed_rows:
        failure_path = failed_path(book_dir)
        write_jsonl(failure_path, failed_rows)
        logger.warning(f"[warn] Wrote failed chapter list: {failure_path}")
    elif fetched_count and os.path.exists(failure_path := failed_path(book_dir)):
        os.remove(failure_path)

    return results, fetched_count


def fetch_with_cache(
    client: ChapterFetchClient,
    ep_list: list[EpisodeItem],
    book_dir: str,
    *,
    use_cache: bool = False,
    force_episode_nos: set[int] | None = None,
    max_workers: int = 1,
    on_result: Callable[[int, ChapterResult], None] | None = None,
) -> tuple[list[ChapterResult], int]:
    cache = load_cache(book_dir) if use_cache else {}
    forced_nos = force_episode_nos or set()
    results: list[ChapterResult] = [{} for _ in ep_list]
    fetch_items: list[EpisodeItem] = []
    fetch_positions: list[int] = []

    for pos, ep in enumerate(ep_list):
        epi_no = episode_no(ep)
        if epi_no is not None and epi_no in cache and epi_no not in forced_nos:
            results[pos] = {**cache[epi_no], "idx": pos + 1}
            continue
        fetch_items.append(ep)
        fetch_positions.append(pos)

    fetched_position_set = set(fetch_positions)

    if fetch_items:
        pbar = tqdm(total=len(fetch_items), desc="[info] fetching chapters", unit="chap")

        def update_pbar() -> None:
            pbar.update(1)

        incremental = on_result or make_incremental_cache_writer(book_dir, ep_list)

        def routed_on_result(fetch_index: int, result: ChapterResult) -> None:
            incremental(fetch_positions[fetch_index], result)

        try:
            fetched = client.fetch_episodes_parallel(
                fetch_items,
                max_workers=max(1, int(max_workers or 1)),
                progress_cb=update_pbar,
                on_result=routed_on_result,
            )
        finally:
            pbar.close()
        for pos, res in zip(fetch_positions, fetched, strict=False):
            results[pos] = res
    elif ep_list:
        logger.info("[info] all requested chapters loaded from cache")

    failed_rows: list[FailedChapter] = []
    for pos, (ep, res) in enumerate(zip(ep_list, results, strict=False), 1):
        if not res or "error" in res:
            failed_rows.append(normalize_failed_chapter(ep, res or {}, pos))
            continue

        if (pos - 1) not in fetched_position_set:
            continue

        cache_row = normalize_cache_row(ep, res, pos)
        if cache_row is not None:
            write_cache_item(book_dir, cache_row)

    if fetch_items:
        failure_path = failed_path(book_dir)
        if failed_rows:
            write_jsonl(failure_path, failed_rows)
            logger.warning(f"[warn] Wrote failed chapter list: {failure_path}")
        elif os.path.exists(failure_path):
            os.remove(failure_path)

    return results, len(fetch_items)
