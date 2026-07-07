import html
import os

from ebooklib import epub
from tqdm import tqdm

from src.api import NovelpiaClient
from src.const import BASE_URL
from src.contracts import ChapterResult, EpisodeItem, NovelResponse
from src.export import EpubImageAdapter, ImageFetcher
from src.helper import ensure_dir, kebab, normalize_url

# ----------------------------
# EPUB Builder
# ----------------------------

def _genre_names(novel: NovelResponse) -> list[str]:
    result = novel["result"]
    nv = result["novel"]
    tag_items = result.get("tag_list") or nv.get("tag_list") or []
    names: list[str] = []
    for tag in tag_items:
        if isinstance(tag, str):
            names.append(tag)
            continue
        if isinstance(tag, dict):
            name = tag.get("tag_name") or tag.get("name") or tag.get("title")
            if isinstance(name, str):
                names.append(name)
    return list(dict.fromkeys(names))

class EpubBuilder:
    def __init__(self, out_dir: str, debug_dump: bool = False):
        self.out_dir = out_dir
        self.debug_dump = debug_dump
        ensure_dir(out_dir)
        self._image_fetcher = ImageFetcher(debug_dump=debug_dump)

    def _fetch_headers(self, client: NovelpiaClient, url: str) -> dict[str, str]:
        return self._image_fetcher.fetch_headers(client)

    def _fetch_bytes(self, client: NovelpiaClient, url: str) -> bytes | None:
        return self._image_fetcher.fetch_bytes(client, url)

    def build(self, client: NovelpiaClient, novel: NovelResponse, episodes: list[EpisodeItem],
              filename_hint: str | None = None, language: str = "en",
              author_fallback: str = "Unknown", css_text: str | None = None,
              novel_id: int | None = None, fetched_results: list[ChapterResult] | None = None,
              max_workers: int = 1) -> tuple[str, str, int]:
        result = novel["result"]
        nv = result["novel"]
        title = nv.get("novel_name", f"novel_{nv.get('novel_no','')}")
        writers = result.get("writer_list") or []
        author = (writers[0].get("writer_name") if writers else None) or author_fallback
        status = "Completed" if str(nv.get("flag_complete", 0)) == "1" else "Ongoing"
        description = (nv.get("novel_story") or "").strip()

        book = epub.EpubBook()
        book.set_identifier(f"novelpia-{nv.get('novel_no')}")
        book.set_title(title)
        book.set_language(language)
        book.add_author(author)

        # Cover
        cover_url = normalize_url(nv.get("novel_full_img") or nv.get("novel_img") or "")
        cover_bytes = self._fetch_bytes(client, cover_url) if cover_url else None
        has_cover = False
        if cover_bytes:
            book.set_cover("cover.jpg", cover_bytes)
            has_cover = True

        # CSS
        default_css = css_text or (
            """
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial; line-height: 1.6; }
            h1, h2, h3 { page-break-after: avoid; }
            img { max-width: 100%; height: auto; }
            .epi-title { font-size: 1.4em; font-weight: 600; margin: 0 0 0.6em; }
            """
        )
        style = epub.EpubItem(uid="style", file_name="style/main.css",
                              media_type="text/css", content=default_css.encode("utf-8"))
        book.add_item(style)

        spine: list[str | epub.EpubHtml] = ["nav"]
        toc: list[epub.EpubHtml] = []
        image_adapter = EpubImageAdapter(
            self._image_fetcher,
            client,
            embed_images=self._image_fetcher.can_fetch_chapter_images(client),
        )

        if fetched_results is None:
            pbar = tqdm(total=len(episodes), desc="[info] fetching chapters", unit="chap")

            def update_pbar():
                pbar.update(1)

            fetched_results = client.fetch_episodes_parallel(
                episodes,
                max_workers=max_workers,
                progress_cb=update_pbar,
            )
            pbar.close()

        for i, res in enumerate(fetched_results, 1):
            if not res or "error" in res:
                err = res.get("error") if res else "Unknown error"
                print(f"[warn] Failed to fetch chapter {i}: {err}")
                continue

            html_text = res.get("html") or ""
            epi_title = res.get("epi_title") or f"Episode {i}"

            html_text, new_imgs = image_adapter.add_images_and_rewrite(html_text)

            chapter = epub.EpubHtml(
                title=epi_title,
                file_name=f"chap_{i:04d}.xhtml",
                lang=language,
                content=(
                    f"<html xmlns=\"http://www.w3.org/1999/xhtml\">"
                    f"<head><title>{html.escape(epi_title)}</title>"
                    f"<link rel=\"stylesheet\" href=\"style/main.css\"/></head>"
                    f"<body><h2 class=\"epi-title\">{html.escape(epi_title)}</h2>{html_text}</body></html>"
                ),
            )

            book.add_item(chapter)
            spine.append(chapter)
            toc.append(chapter)

            for item in new_imgs:
                book.add_item(item)

        # About / metadata page
        src_url = f"{BASE_URL}/novel/{novel_id}" if novel_id else ""
        meta_parts = []
        meta_parts.append(f"<h1>{html.escape(title)}</h1>")
        if has_cover:
            meta_parts.append(
                "<p><img src='cover.jpg' alt='Cover' "
                "style='width:230px;max-width:90%;height:auto;border-radius:12px;"
                "box-shadow:0 2px 8px rgba(0,0,0,.15)'/></p>"
            )
        meta_parts.append(f"<p><strong>Author:</strong> {html.escape(author)}</p>")
        meta_parts.append(f"<p><strong>Chapters:</strong> {len(episodes)}</p>")
        meta_parts.append(f"<p><strong>Status:</strong> {html.escape(status)}</p>")
        genres = _genre_names(novel)
        if genres:
            meta_parts.append(f"<p><strong>Genre:</strong> {html.escape(', '.join(genres))}</p>")
        if src_url:
            meta_parts.append(f"<p><strong>Source:</strong> <a href='{src_url}'>{src_url}</a></p>")
        if description:
            meta_parts.append(f"<p>{html.escape(description)}</p>")
        meta_html = (
            "<html><head><link rel='stylesheet' href='style/main.css'/></head><body>"
             + "".join(meta_parts) + "</body></html>"
        )
        about = epub.EpubHtml(title="About", file_name="about.xhtml", lang=language, content=meta_html)
        book.add_item(about)
        spine.insert(1, about)
        toc.insert(0, about)

        # TOC, NCX, Nav
        book.toc = toc
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # Spine & CSS
        book.spine = spine

        base = kebab(filename_hint or title)
        book_dir = os.path.join(self.out_dir, base)
        ensure_dir(book_dir)
        out_path = os.path.join(book_dir, f"{base}.epub")
        epub.write_epub(out_path, book, {})
        return out_path, title, len([r for r in fetched_results if r and "error" not in r])
