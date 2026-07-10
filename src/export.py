import os
import time
from collections.abc import Iterator
from typing import Protocol
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

from src.const import BASE_URL
from src.contracts import ChapterResult
from src.helper import media_type_from_ext, normalize_url, sanitize_filename
from src.logutil import get_logger

logger = get_logger(__name__)


class ImageClient(Protocol):
    s: requests.Session
    timeout: int

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
IMAGE_EXTENSION_BY_MEDIA_TYPE = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
SIGNED_COOKIE_KEYS = {"Key-Pair-Id", "Signature"}
SIGNED_POLICY_COOKIE_KEYS = {"Policy", "Expires"}

IMAGE_MAX_RETRIES = 3
IMAGE_429_BACKOFF_SECONDS = 2.0
IMAGE_RETRY_DELAY_SECONDS = 1.0

# Magic-byte signatures for image sniffing. Some hosts (e.g. Novelpia's cover
# CDN) serve real images with a generic ``Content-Type: application/octet-stream``
# and a non-image URL extension (``.file``), so header/extension-based detection
# alone discards valid images. Checked in order; longer/more specific signatures
# first where prefixes could otherwise collide.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),  # WEBP also requires b"WEBP" at offset 8; checked below
)


def sniff_image_extension(data: bytes) -> str | None:
    """Return a supported image extension by inspecting magic bytes, or None."""
    for signature, ext in _IMAGE_SIGNATURES:
        if not data.startswith(signature):
            continue
        if ext == ".webp" and data[8:12] != b"WEBP":
            continue
        return ext
    return None


def cloudfront_cookie_key(cookie_name: str) -> str | None:
    key = cookie_name.removeprefix("CloudFront-")
    if key in SIGNED_COOKIE_KEYS or key in SIGNED_POLICY_COOKIE_KEYS:
        return key
    return None


def _iter_cloudfront_cookies(
    client: ImageClient, debug_dump: bool
) -> Iterator[tuple[str, str | None]]:
    """Yield (cookie_name, cookie_value) for CloudFront-signed cookies.

    Tolerates a malformed cookie jar (logs once under debug_dump) so image
    fetching degrades gracefully instead of raising.
    """
    try:
        for cookie in client.s.cookies:
            name = cookie.name
            if cloudfront_cookie_key(name) is not None:
                yield name, cookie.value
    except AttributeError as exc:
        if debug_dump:
            logger.debug(f"[debug] image cookie iteration failed: {exc}")


class ImageFetcher:
    def __init__(self, debug_dump: bool = False):
        self.debug_dump = debug_dump

    def fetch_headers(self, client: ImageClient) -> dict[str, str]:
        headers = {
            "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "referer": BASE_URL + "/",
        }
        cloudfront_parts = [
            f"{name}={value}" for name, value in _iter_cloudfront_cookies(client, self.debug_dump)
        ]
        if cloudfront_parts:
            headers["Cookie"] = "; ".join(cloudfront_parts)
        return headers

    def can_fetch_chapter_images(self, client: ImageClient) -> bool:
        matched_keys = {
            cloudfront_cookie_key(name)
            for name, _ in _iter_cloudfront_cookies(client, self.debug_dump)
        }
        return SIGNED_COOKIE_KEYS.issubset(matched_keys) and bool(
            matched_keys.intersection(SIGNED_POLICY_COOKIE_KEYS)
        )

    def fetch_bytes(self, client: ImageClient, url: str) -> bytes | None:
        fetched = self.fetch_image(client, url)
        if fetched is None:
            return None
        return fetched[0]

    def fetch_image(self, client: ImageClient, url: str) -> tuple[bytes, str] | None:
        for attempt in range(1, IMAGE_MAX_RETRIES + 1):
            try:
                resp = client.s.get(
                    url, headers=self.fetch_headers(client), timeout=client.timeout
                )
                if resp.status_code == 429:
                    time.sleep(IMAGE_429_BACKOFF_SECONDS * attempt)
                    continue
                resp.raise_for_status()
                if not resp.content:
                    return None
                media_type = str(resp.headers.get("Content-Type", "")).split(";", 1)[0].lower()
                ext = IMAGE_EXTENSION_BY_MEDIA_TYPE.get(media_type)
                if ext is None:
                    ext = os.path.splitext(urlparse(url).path)[1].lower()
                if ext not in SUPPORTED_IMAGE_EXTENSIONS:
                    # Header/URL gave no usable extension (e.g. a CDN serving a
                    # real image as application/octet-stream with a non-image
                    # URL suffix). Fall back to sniffing the actual bytes
                    # before giving up.
                    ext = sniff_image_extension(resp.content)
                if ext is None:
                    return None
                return resp.content, ext
            except (
                requests.HTTPError,
                AttributeError,
                OSError,
                RuntimeError,
                requests.RequestException,
            ) as exc:
                status_code = (
                    exc.response.status_code
                    if isinstance(exc, requests.HTTPError) and exc.response is not None
                    else 0
                )
                if isinstance(exc, requests.HTTPError) and 400 <= status_code < 500:
                    if self.debug_dump:
                        logger.debug(f"[debug] image fetch failed: {url}: {exc}")
                    return None
                if self.debug_dump:
                    logger.debug(f"[debug] image fetch failed: {url}: {exc}")
                if attempt < IMAGE_MAX_RETRIES:
                    time.sleep(IMAGE_RETRY_DELAY_SECONDS)
        return None


class EpubImageAdapter:
    def __init__(self, fetcher: ImageFetcher, client: ImageClient, embed_images: bool = True):
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

            fetched = self.fetcher.fetch_image(self.client, src)
            if fetched is None:
                continue
            img_bytes, ext = fetched

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
            logger.warning(f"[warn] Failed to fetch chapter {i}: {err}")
            continue

        html_text = res.get("html") or ""
        epi_title = res.get("epi_title") or f"Episode {i}"
        text = BeautifulSoup(html_text, "lxml").get_text("\n")

        fname = f"{i}_{sanitize_filename(epi_title)}.txt"
        with open(os.path.join(book_dir, fname), "w", encoding="utf-8") as handle:
            handle.write(text)

        total += 1

    return total
