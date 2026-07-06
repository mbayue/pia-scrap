import json
import os
from collections.abc import Iterable

from bs4 import BeautifulSoup
from tqdm import tqdm

from src.contracts import ChapterResult, EpisodeItem, FailedChapter, NovelResponse
from src.epub import EpubBuilder
from src.helper import ensure_dir, kebab, sanitize_filename
from src.novel import fetch_novel_and_episodes

# ----------------------------
# Main Build Function
# ----------------------------

def _episode_no(ep: EpisodeItem) -> int | None:
    episode_no = ep.get("episode_no")
    if episode_no is None:
        return None
    try:
        return int(episode_no)
    except Exception:
        return None

def _chapter_idx(ep: EpisodeItem, fallback: int) -> int:
    try:
        return int(ep.get("epi_num") or fallback)
    except Exception:
        return fallback

def _chapter_title(ep: EpisodeItem) -> str:
    return ep.get("epi_title") or f"Episode {ep.get('epi_num')}"

def _cache_dir(book_dir: str) -> str:
    return os.path.join(book_dir, ".cache/")

def _cache_file_path(book_dir: str, epi_no: int) -> str:
    return os.path.join(_cache_dir(book_dir), f"{epi_no}.json")

def _failed_path(book_dir: str) -> str:
    return os.path.join(book_dir, "failed_chapters.jsonl")

def _load_jsonl(path: str) -> list[FailedChapter]:
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
                    rows.append({
                        "idx": int(row.get("idx") or 0),
                        "epi_no": int(row["epi_no"]) if row.get("epi_no") is not None else None,
                        "title": str(row.get("title") or ""),
                        "url": str(row.get("url") or ""),
                        "error": str(row.get("error") or ""),
                    })
            except json.JSONDecodeError:
                continue
    return rows

def _load_cache(book_dir: str) -> dict[int, ChapterResult]:
    cache: dict[int, ChapterResult] = {}
    cache_dir = _cache_dir(book_dir)
    if os.path.isdir(cache_dir):
        for name in os.listdir(cache_dir):
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(cache_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    row = json.load(f)
            except Exception as e:
                print(f"[warn] Ignoring unreadable cache file {path}: {e}")
                continue
            if not isinstance(row, dict):
                continue
            raw_epi_no = row.get("epi_no")
            if raw_epi_no is None:
                continue
            try:
                epi_no = int(raw_epi_no)
            except Exception as e:
                print(f"[warn] Ignoring cache row with invalid epi_no in {path}: {e}")
                continue
            html_text = row.get("html")
            if isinstance(html_text, str) and html_text:
                cache[epi_no] = {
                    "idx": int(row.get("idx") or 0),
                    "epi_no": epi_no,
                    "epi_title": str(row.get("epi_title") or ""),
                    "html": html_text,
                }
    return cache

def _load_failed_episode_nos(book_dir: str) -> set[int]:
    failed: set[int] = set()
    for row in _load_jsonl(_failed_path(book_dir)):
        epi_no = row.get("epi_no")
        if isinstance(epi_no, int):
            failed.add(epi_no)
    return failed

def _write_jsonl(path: str, rows: Iterable[FailedChapter]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

def _write_cache_item(book_dir: str, row: ChapterResult) -> None:
    row_epi_no = row.get("epi_no")
    if row_epi_no is None:
        raise ValueError("cache row missing epi_no")
    epi_no = int(row_epi_no)
    ensure_dir(_cache_dir(book_dir))
    with open(_cache_file_path(book_dir, epi_no), "w", encoding="utf-8") as f:
        json.dump(row, f, ensure_ascii=False, indent=2)

def _fetch_with_cache(client, ep_list: list[EpisodeItem], book_dir: str, *,
                      use_cache: bool = False, force_episode_nos: set[int] | None = None,
                      max_workers: int = 1) -> tuple[list[ChapterResult], int]:
    cache = _load_cache(book_dir) if use_cache else {}
    force_episode_nos = force_episode_nos or set()
    results: list[ChapterResult] = [{} for _ in ep_list]
    fetch_items: list[EpisodeItem] = []
    fetch_positions: list[int] = []

    for pos, ep in enumerate(ep_list):
        epi_no = _episode_no(ep)
        if epi_no is not None and epi_no in cache and epi_no not in force_episode_nos:
            cached: ChapterResult = {**cache[epi_no], "idx": pos + 1}
            results[pos] = cached
            continue
        fetch_items.append(ep)
        fetch_positions.append(pos)

    fetched_position_set = set(fetch_positions)

    if fetch_items:
        pbar = tqdm(total=len(fetch_items), desc="[info] fetching chapters", unit="chap")

        def update_pbar():
            pbar.update(1)

        fetched = client.fetch_episodes_parallel(
            fetch_items,
            max_workers=max(1, int(max_workers or 1)),
            progress_cb=update_pbar,
        )
        pbar.close()
        for pos, res in zip(fetch_positions, fetched, strict=False):
            results[pos] = res
    elif ep_list:
        print("[info] all requested chapters loaded from cache")

    failed_rows: list[FailedChapter] = []
    for pos, (ep, res) in enumerate(zip(ep_list, results, strict=False), 1):
        epi_no = _episode_no(ep)
        title = _chapter_title(ep)
        idx = _chapter_idx(ep, pos)
        if not res or "error" in res:
            failed_rows.append({
                "idx": idx,
                "epi_no": epi_no,
                "title": title,
                "url": f"https://global.novelpia.com/viewer/{epi_no}" if epi_no else "",
                "error": (res or {}).get("error", "Unknown error"),
            })
            continue

        if (pos - 1) not in fetched_position_set:
            continue

        cache_epi_no = res.get("epi_no") or epi_no
        if cache_epi_no is None:
            continue
        cache_row: ChapterResult = {
            "idx": idx,
            "epi_no": int(cache_epi_no),
            "epi_title": res.get("epi_title") or title,
            "html": res.get("html") or "",
        }
        _write_cache_item(book_dir, cache_row)

    if fetch_items:
        if failed_rows:
            _write_jsonl(_failed_path(book_dir), failed_rows)
            print(f"[warn] Wrote failed chapter list: {_failed_path(book_dir)}")
        else:
            failed_file = _failed_path(book_dir)
            if os.path.exists(failed_file):
                os.remove(failed_file)

    return results, len(fetch_items)

def build_epub(client, novel_id, out_dir, start_chapter=None, end_chapter=None, max_chapters=None,
               language="en", debug_dump=False, update=False, retry_failed=False, max_workers=1):
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, start_chapter, end_chapter, max_chapters)

    builder = EpubBuilder(out_dir, debug_dump=debug_dump)

    base = kebab(title)
    book_dir = os.path.join(out_dir, base)
    ensure_dir(book_dir)
    retry_nos = _load_failed_episode_nos(book_dir) if retry_failed else set()
    if retry_failed and not retry_nos:
        print("[info] no failed chapters to retry")
    fetched_results, fetched_count = _fetch_with_cache(
        client,
        ep_list,
        book_dir,
        use_cache=(update or retry_failed),
        force_episode_nos=retry_nos,
        max_workers=max_workers,
    )
    if (update or retry_failed) and fetched_count == 0:
        return None, title, 0

    build_metadata(book_dir, data_novel, novel_id, ep_list, max_chapters)

    return builder.build(
        client=client,
        novel=data_novel,
        episodes=ep_list,
        filename_hint=title,
        language=language,
        novel_id=novel_id,
        fetched_results=fetched_results,
        max_workers=max_workers,
    )

def build_txt(client, novel_id, out_dir, start_chapter=None, end_chapter=None, max_chapters=None,
              language="en", debug_dump=False, update=False, retry_failed=False, max_workers=1):
    data_novel, ep_list, title = fetch_novel_and_episodes(client, novel_id, start_chapter, end_chapter, max_chapters)

    base = kebab(title)
    book_dir = os.path.join(out_dir, base)
    ensure_dir(book_dir)

    total = 0
    retry_nos = _load_failed_episode_nos(book_dir) if retry_failed else set()
    if retry_failed and not retry_nos:
        print("[info] no failed chapters to retry")
    fetched_results, fetched_count = _fetch_with_cache(
        client,
        ep_list,
        book_dir,
        use_cache=(update or retry_failed),
        force_episode_nos=retry_nos,
        max_workers=max_workers,
    )
    if (update or retry_failed) and fetched_count == 0:
        return None, title, 0

    for i, res in enumerate(fetched_results, 1):
        if not res or "error" in res:
            err = res.get("error") if res else "Unknown error"
            print(f"[warn] Failed to fetch chapter {i}: {err}")
            continue

        html_text = res.get("html") or ""
        epi_title = res.get("epi_title") or f"Episode {i}"

        soup = BeautifulSoup(html_text, "lxml")
        text = soup.get_text("\n")

        fname = f"{i}_{sanitize_filename(epi_title)}.txt"
        with open(os.path.join(book_dir, fname), "w", encoding="utf-8") as f:
            f.write(text)

        total += 1

    build_metadata(book_dir, data_novel, novel_id, ep_list, max_chapters)

    return book_dir, title, total

def build_metadata(book_dir, data_novel: NovelResponse, novel_id, ep_list: list[EpisodeItem], max_chapters=None):
    result = data_novel["result"]
    nv = result["novel"]
    title = nv.get("novel_name", f"novel_{nv.get('novel_no','')}")
    epi_cnt = result.get("info", {}).get("epi_cnt") or nv.get("count_epi") or 0
    writers = result.get("writer_list") or []
    author = (writers[0].get("writer_name") if writers and writers[0].get("writer_name") else "Unknown Author")
    status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
    description = (nv.get("novel_story") or "").strip()

    tag_items = (result.get("tag_list")
                 or nv.get("tag_list")
                 or [])
    tags: list[str] = []
    for t in tag_items:
        if isinstance(t, str):
            tags.append(t)
        elif isinstance(t, dict):
            val = t.get("tag_name") or t.get("name") or t.get("title")
            if isinstance(val, str):
                tags.append(val)

    seen = set()
    uniq_tags = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq_tags.append(t)

    meta = {
        "url": f"https://global.novelpia.com/novel/{novel_id}",
        "title": nv.get("novel_name") or title,
        "author": author,
        "tags": uniq_tags,
        "chapter": len(ep_list) if (max_chapters and max_chapters > 0) else (int(epi_cnt) if epi_cnt else len(ep_list)),
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
