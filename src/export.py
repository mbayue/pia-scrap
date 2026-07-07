import os
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

from src.api import NovelpiaClient
from src.const import BASE_URL
from src.contracts import ChapterResult
from src.helper import media_type_from_ext, normalize_url, sanitize_filename

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


class ImageFetcher:
    def __init__(self, debug_dump: bool = False):
        self.debug_dump = debug_dump

    def fetch_headers(self, client: NovelpiaClient) -> dict[str, str]:
        headers = {
            "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "referer": BASE_URL + "/",
        }
        cloudfront_parts: list[str] = []
        try:
            for cookie in client.s.cookies:
                cookie_name = cookie.name
                if cookie_name.startswith("CloudFront-") or cookie_name in ("Key-Pair-Id", "Policy", "Signature"):
                    cloudfront_parts.append(f"{cookie_name}={cookie.value}")
        except AttributeError as exc:
            if self.debug_dump:
                print(f"[debug] image cookie header build failed: {exc}")
        if cloudfront_parts:
            headers["Cookie"] = "; ".join(cloudfront_parts)
        return headers

    def can_fetch_chapter_images(self, client: NovelpiaClient) -> bool:
        return "Cookie" in self.fetch_headers(client)

    def fetch_bytes(self, client: NovelpiaClient, url: str) -> bytes | None:
        for attempt in range(1, 4):
            try:
                resp = client.s.get(url, headers=self.fetch_headers(client), timeout=client.timeout)
                if resp.status_code == 429:
                    time.sleep(2.0 * attempt)
                    continue
                resp.raise_for_status()
                return resp.content
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else 0
                if 400 <= status_code < 500:
                    if self.debug_dump:
                        print(f"[debug] image fetch failed: {url}: {exc}")
                    return None
                if self.debug_dump:
                    print(f"[debug] image fetch failed: {url}: {exc}")
                if attempt < 3:
                    time.sleep(1.0)
            except (AttributeError, OSError, RuntimeError, requests.RequestException) as exc:
                if self.debug_dump:
                    print(f"[debug] image fetch failed: {url}: {exc}")
                if attempt < 3:
                    time.sleep(1.0)
        return None


class EpubImageAdapter:
    def __init__(self, fetcher: ImageFetcher, client: NovelpiaClient, embed_images: bool = True):
        self.fetcher = fetcher
        self.client = client
        self.embed_images = embed_images
        self.image_cache: dict[str, str] = {}
        self.img_index = 1

    def add_images_and_rewrite(self, html_str: str) -> tuple[str, list[epub.EpubItem]]:
        soup = BeautifulSoup(html_str, "lxml")
        added_items: list[epub.EpubItem] = []

        if not self.embed_images:
            for img in soup.find_all("img"):
                img.decompose()
            if soup.body is None:
                return str(soup), added_items
            return "".join(str(child) for child in soup.body.contents), added_items

        for img in soup.find_all("img"):
            src_value = img.get("src")
            if not src_value:
                continue
            src = normalize_url(str(src_value))
            if src in self.image_cache:
                img["src"] = self.image_cache[src]
                continue

            ext = os.path.splitext(urlparse(src).path)[1].lower() or ".jpg"
            if ext not in SUPPORTED_IMAGE_EXTENSIONS:
                continue

            img_bytes = self.fetcher.fetch_bytes(self.client, src)
            if not img_bytes:
                continue

            fname = f"images/img_{self.img_index:05d}{ext}"
            self.image_cache[src] = fname
            self.img_index += 1

            item = epub.EpubItem(
                uid=f"img{self.img_index}",
                file_name=fname,
                media_type=media_type_from_ext(ext),
                content=img_bytes,
            )
            added_items.append(item)
            img["src"] = fname

        if soup.body is None:
            return str(soup), added_items
        return "".join(str(child) for child in soup.body.contents), added_items


def write_txt_chapters(book_dir: str, fetched_results: list[ChapterResult]) -> int:
    total = 0
    for i, res in enumerate(fetched_results, 1):
        if not res or "error" in res:
            err = res.get("error") if res else "Unknown error"
            print(f"[warn] Failed to fetch chapter {i}: {err}")
            continue

        html_text = res.get("html") or ""
        epi_title = res.get("epi_title") or f"Episode {i}"
        text = BeautifulSoup(html_text, "lxml").get_text("\n")

        fname = f"{i}_{sanitize_filename(epi_title)}.txt"
        with open(os.path.join(book_dir, fname), "w", encoding="utf-8") as handle:
            handle.write(text)

        total += 1

    return total
