import html
import os
from collections.abc import Callable
from typing import Protocol

import requests
from ebooklib import epub
from tqdm import tqdm

from src.const import BASE_URL
from src.contracts import ChapterResult, EpisodeItem, NovelResponse, chapter_is_error
from src.export import EpubImageAdapter, ImageFetcher, sniff_image_extension
from src.helper import ensure_dir, extract_genre_names, kebab, normalize_description, normalize_url
from src.logutil import get_logger

logger = get_logger(__name__)

# ebooklib's add_metadata() passes attribute dicts straight to lxml without
# registering namespace prefixes, so a plain "opf:scheme" key raises
# ValueError: Invalid attribute name. Clark notation ("{uri}local") is the
# form lxml accepts for a namespaced attribute without a prefix declaration.
_OPF_SCHEME_ATTR = f"{{{epub.NAMESPACES['OPF']}}}scheme"


class EpubClient(Protocol):
    s: requests.Session
    timeout: int

    def fetch_episodes_parallel(
        self,
        ep_list: list[EpisodeItem],
        max_workers: int = 1,
        progress_cb: Callable[[], None] | None = None,
        on_result: Callable[[int, ChapterResult], None] | None = None,
    ) -> list[ChapterResult]: ...


def _epub_date(raw: object) -> str | None:
    """Extract a ``YYYY-MM-DD`` date from Novelpia's ``"YYYY-MM-DD HH:MM:SS"``
    timestamp for use as ``dc:date``. Returns ``None`` for missing/placeholder
    values (Novelpia uses ``"0000-00-00 00:00:00"`` for unset dates).
    """
    if not isinstance(raw, str) or not raw:
        return None
    date_part = raw.split(" ", 1)[0]
    if len(date_part) != 10 or date_part.startswith("0000"):
        return None
    return date_part


class EpubBuilder:
    def __init__(self, out_dir: str, debug_dump: bool = False):
        self.out_dir = out_dir
        self.debug_dump = debug_dump
        ensure_dir(out_dir)
        self._image_fetcher = ImageFetcher(debug_dump=debug_dump)

    def _fetch_bytes(self, client: EpubClient, url: str) -> bytes | None:
        return self._image_fetcher.fetch_bytes(client, url)

    def build(
        self,
        client: EpubClient,
        novel: NovelResponse,
        episodes: list[EpisodeItem],
        filename_hint: str | None = None,
        language: str = "en",
        author_fallback: str = "Unknown",
        css_text: str | None = None,
        novel_id: int | None = None,
        fetched_results: list[ChapterResult] | None = None,
        max_workers: int = 1,
        image_cache_dir: str | None = None,
        chapter_images: bool = False,
    ) -> tuple[str, str, int]:
        result = novel["result"]
        nv = result["novel"]
        title = nv.get("novel_name", f"novel_{nv.get('novel_no', '')}")
        writers = result.get("writer_list") or []
        author = (writers[0].get("writer_name") if writers else None) or author_fallback
        status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
        description = normalize_description(nv.get("novel_story") or "")
        genres = extract_genre_names(novel)

        book = epub.EpubBook()
        book.set_identifier(f"novelpia-{nv.get('novel_no')}")
        book.set_title(title)
        book.set_language(language)
        book.add_author(author)

        if description:
            book.add_metadata("DC", "description", description)

        for genre in genres:
            book.add_metadata("DC", "subject", genre)

        src_url = f"{BASE_URL}/novel/{novel_id}" if novel_id else ""
        if src_url:
            book.add_metadata("DC", "source", src_url)
            book.add_metadata("DC", "identifier", src_url, {_OPF_SCHEME_ATTR: "URL", "id": "source-url"})

        published_date = _epub_date(nv.get("reg_dt"))
        if published_date:
            book.add_metadata("DC", "date", published_date)

        # Cover. Try novel_full_img first, then fall back to novel_img: Novelpia
        # sometimes returns a non-image placeholder for novel_full_img (points at
        # a stray "images.novelpia.com" path segment instead of a real file), so
        # a non-empty field isn't proof the fetch will actually succeed.
        has_cover = False
        cover_file_name = "cover.jpg"
        for cover_field_value in (nv.get("novel_full_img"), nv.get("novel_img")):
            cover_url = normalize_url(cover_field_value or "")
            if not cover_url:
                continue
            fetched_cover = self._image_fetcher.fetch_image(client, cover_url)
            if fetched_cover is None:
                continue
            cover_bytes, cover_ext = fetched_cover
            if sniff_image_extension(cover_bytes) is None:
                # Header/URL extension said this was a supported image type,
                # but the actual bytes aren't a recognized image signature
                # (e.g. an HTML error page or corrupted response labeled as
                # an image). Don't trust it for the cover -- fall through to
                # the next candidate field instead of embedding bad data.
                continue
            cover_file_name = f"cover{cover_ext}"
            book.set_cover(cover_file_name, cover_bytes)
            has_cover = True
            break

        # CSS
        default_css = css_text or (
            """
            body { line-height: 1.6; }
            h1, h2, h3 { page-break-after: avoid; }
            nav h2 { font-size: 2em; }
            img { max-width: 100%; height: auto; }
            .epi-title { font-size: 1.4em; font-weight: 600; margin: 0 0 0.6em; }
            .about-cover { float: left; margin: 0 1.2em .7em 0; }
            .about-cover img { width: 240px; max-width: 40vw; border-radius: 12px;
                box-shadow: 0 2px 8px rgba(0,0,0,.15); }
            .about-clear { clear: both; height: 0; }
            """
        )
        style = epub.EpubItem(
            uid="style", file_name="style/main.css", media_type="text/css", content=default_css.encode("utf-8")
        )
        book.add_item(style)

        spine: list[str | epub.EpubHtml] = ["nav"]
        toc: list[epub.EpubHtml] = []
        image_adapter = EpubImageAdapter(
            self._image_fetcher,
            client,
            embed_images=chapter_images,
            image_cache_dir=image_cache_dir,
        )

        if fetched_results is None:
            pbar = tqdm(total=len(episodes), desc="[info] fetching chapters", unit="chap")

            def update_pbar():
                pbar.update(1)

            try:
                fetched_results = client.fetch_episodes_parallel(
                    episodes,
                    max_workers=max_workers,
                    progress_cb=update_pbar,
                )
            finally:
                pbar.close()

        for i, res in enumerate(fetched_results, 1):
            if not res or chapter_is_error(res):
                err = res.get("error") if res else "Unknown error"
                logger.warning(f"[warn] Failed to fetch chapter {i}: {err}")
                continue

            html_text = res.get("html") or ""
            epi_title = res.get("epi_title") or f"Episode {i}"

            signed_key = res.get("signed_key")
            html_text, new_imgs = image_adapter.add_images_and_rewrite(
                html_text, signed_key, embed_images=bool(signed_key)
            )

            chapter = epub.EpubHtml(
                title=epi_title,
                file_name=f"chap_{i:04d}.xhtml",
                lang=language,
                content=(
                    f'<html xmlns="http://www.w3.org/1999/xhtml">'
                    f"<head><title>{html.escape(epi_title)}</title>"
                    f'<link rel="stylesheet" href="style/main.css"/></head>'
                    f'<body><h2 class="epi-title">{html.escape(epi_title)}</h2>{html_text}</body></html>'
                ),
            )
            chapter.add_item(style)

            book.add_item(chapter)
            spine.append(chapter)
            toc.append(chapter)

            for item in new_imgs:
                book.add_item(item)

        # About / metadata page
        meta_parts = []
        meta_parts.append(f"<h1>{html.escape(title)}</h1>")
        if has_cover:
            meta_parts.append(
                f"<p class='about-cover'><img src='{cover_file_name}' alt='Cover'/></p>"
            )
        meta_parts.append(f"<p><strong>Author:</strong> {html.escape(author)}</p>")
        meta_parts.append(f"<p><strong>Chapters:</strong> {len(episodes)}</p>")
        meta_parts.append(f"<p><strong>Status:</strong> {html.escape(status)}</p>")
        if genres:
            meta_parts.append(f"<p><strong>Genre:</strong> {html.escape(', '.join(genres))}</p>")
        if src_url:
            meta_parts.append(f"<p><strong>Source:</strong> <a href='{src_url}'>{src_url}</a></p>")
        if description:
            escaped_description = html.escape(description).replace("\n", "<br/>")
            meta_parts.append(f"<div class='about-clear'></div><p>{escaped_description}</p>")
        meta_html = (
            "<html><head><link rel='stylesheet' href='style/main.css'/></head><body>"
            + "".join(meta_parts)
            + "</body></html>"
        )
        about = epub.EpubHtml(title="About", file_name="about.xhtml", lang=language, content=meta_html)
        about.add_item(style)
        book.add_item(about)
        spine.insert(1, about)
        toc.insert(0, about)

        # TOC, NCX, Nav
        book.toc = toc
        book.add_item(epub.EpubNcx())
        nav = epub.EpubNav(title="Table of Contents")
        nav.add_item(style)
        book.add_item(nav)

        # Spine & CSS
        book.spine = spine

        base = kebab(filename_hint or title)
        book_dir = os.path.join(self.out_dir, base)
        ensure_dir(book_dir)
        out_path = os.path.join(book_dir, f"{base}.epub")
        epub.write_epub(out_path, book, {})
        return out_path, title, len([r for r in fetched_results if r and not chapter_is_error(r)])
