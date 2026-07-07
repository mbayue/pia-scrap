import json
import os

from src.chapter_cache import episode_no as _episode_no
from src.chapter_pipeline import (
    AccountChapterPolicy,
    ChapterFetchMode,
    ChapterSelection,
    fetch_chapters,
    select_episodes,
)
from src.contracts import EpisodeItem, NovelResponse
from src.epub import EpubBuilder
from src.export import write_txt_chapters
from src.helper import ensure_dir, kebab
from src.novel import fetch_novel_and_episodes


def build_epub(
    client,
    novel_id,
    out_dir,
    start_chapter=None,
    end_chapter=None,
    max_chapters=None,
    language="en",
    debug_dump=False,
    update=False,
    retry_failed=False,
    max_workers=1,
):
    data_novel, ep_list, title, account_status = fetch_novel_and_episodes(client, novel_id)
    ep_list = select_episodes(ep_list, ChapterSelection(start_chapter, end_chapter, max_chapters))

    builder = EpubBuilder(out_dir, debug_dump=debug_dump)

    base = kebab(title)
    book_dir = os.path.join(out_dir, base)
    ensure_dir(book_dir)
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


def build_txt(
    client,
    novel_id,
    out_dir,
    start_chapter=None,
    end_chapter=None,
    max_chapters=None,
    language="en",
    debug_dump=False,
    update=False,
    retry_failed=False,
    max_workers=1,
):
    data_novel, ep_list, title, account_status = fetch_novel_and_episodes(client, novel_id)
    ep_list = select_episodes(ep_list, ChapterSelection(start_chapter, end_chapter, max_chapters))

    base = kebab(title)
    book_dir = os.path.join(out_dir, base)
    ensure_dir(book_dir)

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
    if (update or retry_failed) and fetched_count == 0:
        return None, title, 0

    total = write_txt_chapters(book_dir, fetched_results)

    build_metadata(book_dir, data_novel, novel_id, ep_list, max_chapters)

    return book_dir, title, total


def build_metadata(book_dir, data_novel: NovelResponse, novel_id, ep_list: list[EpisodeItem], max_chapters=None):
    result = data_novel["result"]
    nv = result["novel"]
    title = nv.get("novel_name", f"novel_{nv.get('novel_no', '')}")
    epi_cnt = result.get("info", {}).get("epi_cnt") or nv.get("count_epi") or 0
    writers = result.get("writer_list") or []
    author = writers[0].get("writer_name") if writers and writers[0].get("writer_name") else "Unknown Author"
    status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
    description = (nv.get("novel_story") or "").strip()

    tag_items = result.get("tag_list") or nv.get("tag_list") or []
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
