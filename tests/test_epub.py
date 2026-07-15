import hashlib

import requests

from src.api import NovelpiaClient
from src.contracts import NovelResponse
from src.epub import EpubBuilder, _epub_date
from src.export import EpubImageAdapter, ImageFetcher, write_txt_chapters


class OkResponse:
    status_code = 200
    content = b"image-bytes"

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}

    def raise_for_status(self):
        return None


class FakeImageResponse:
    """Like OkResponse, but with settable content for byte-sniffing tests."""

    status_code = 200

    def __init__(self, content: bytes, headers: dict[str, str] | None = None):
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        return None


class ErrorResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.content = b""
        self.headers: dict[str, str] = {}

    def raise_for_status(self):
        response = requests.Response()
        response.status_code = self.status_code
        raise requests.HTTPError("blocked", response=response)


def _failing_client(monkeypatch) -> NovelpiaClient:
    client = NovelpiaClient(throttle=0)
    monkeypatch.setattr(client.s, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("network down")))
    return client


def test_fetch_bytes_logs_debug_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("src.export.time.sleep", lambda _seconds: None)
    builder = EpubBuilder(str(tmp_path), debug_dump=True)

    result = builder._fetch_bytes(_failing_client(monkeypatch), "https://example.com/image.jpg")

    assert result is None
    assert "[debug] image fetch failed: https://example.com/image.jpg: network down" in capsys.readouterr().out


def test_fetch_bytes_stays_quiet_without_debug(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("src.export.time.sleep", lambda _seconds: None)
    builder = EpubBuilder(str(tmp_path), debug_dump=False)

    result = builder._fetch_bytes(_failing_client(monkeypatch), "https://example.com/image.jpg")

    assert result is None
    assert capsys.readouterr().out == ""


def test_build_continues_when_chapter_image_fetch_fails(monkeypatch, tmp_path):
    written = []
    novel: NovelResponse = {
        "result": {
            "novel": {"novel_no": 49, "novel_name": "Book", "flag_complete": 0},
            "writer_list": [{"writer_name": "Author"}],
        },
    }
    client = _failing_client(monkeypatch)

    monkeypatch.setattr("src.export.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("src.epub.epub.write_epub", lambda path, _book, _opts: written.append(path))

    result = EpubBuilder(str(tmp_path)).build(
        client,
        novel,
        [{"episode_no": 1, "epi_title": "One"}],
        filename_hint="Book",
        fetched_results=[{"epi_no": 1, "epi_title": "One", "html": '<p><img src="https://example.com/i.jpg"></p>'}],
        chapter_images=True,
    )

    assert result == (str(tmp_path / "book" / "book.epub"), "Book", 1)
    assert written == [str(tmp_path / "book" / "book.epub")]


def test_build_strips_chapter_images_without_cloudfront_cookies(monkeypatch, tmp_path):
    written = []
    novel: NovelResponse = {
        "result": {
            "novel": {"novel_no": 49, "novel_name": "Book", "flag_complete": 0},
            "writer_list": [{"writer_name": "Author"}],
        },
    }
    password = "test-" + "password"
    client: NovelpiaClient = NovelpiaClient(email="user@example.com", password=password, throttle=0)

    def _fake_get(*_args, **_kwargs):
        resp = requests.Response()
        resp.status_code = 404
        resp._content = b""
        return resp

    monkeypatch.setattr(client.s, "get", _fake_get)
    monkeypatch.setattr("src.epub.epub.write_epub", lambda _path, book, _opts: written.append(book))

    EpubBuilder(str(tmp_path)).build(
        client,
        novel,
        [{"episode_no": 1, "epi_title": "One"}],
        filename_hint="Book",
        fetched_results=[
            {"epi_no": 1, "epi_title": "One", "html": '<p>before<img src="https://pv-gn.novelpia.com/i.png">after</p>'}
        ],
        chapter_images=True,
    )

    chapter = next(item for item in written[0].get_items() if item.file_name == "chap_0001.xhtml")
    assert "before" in chapter.content
    assert "after" in chapter.content
    assert all(not item.file_name.startswith("images/") for item in written[0].get_items())


def test_image_fetch_requires_complete_cloudfront_signed_cookie_set():
    client = NovelpiaClient(throttle=0)
    fetcher = ImageFetcher()

    client.s.cookies.set("CloudFront-Key-Pair-Id", "key")
    client.s.cookies.set("CloudFront-Signature", "sig")

    assert fetcher.can_fetch_chapter_images(client) is False

    client.s.cookies.set("CloudFront-Policy", "policy")

    assert fetcher.can_fetch_chapter_images(client) is True


def test_build_about_page_includes_genres(monkeypatch, tmp_path):
    written = []
    novel: NovelResponse = {
        "result": {
            "novel": {
                "novel_no": 49,
                "novel_name": "Book",
                "flag_complete": 1,
                "tag_list": [{"tag_name": "Fantasy"}, {"name": "Comedy"}, "Drama"],
            },
            "writer_list": [{"writer_name": "Author"}],
        },
    }
    client = _failing_client(monkeypatch)

    monkeypatch.setattr("src.export.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("src.epub.epub.write_epub", lambda path, book, _opts: written.append((path, book)))

    EpubBuilder(str(tmp_path)).build(
        client,
        novel,
        [{"episode_no": 1, "epi_title": "One"}],
        filename_hint="Book",
        fetched_results=[{"epi_no": 1, "epi_title": "One", "html": "<p>ok</p>"}],
    )

    about = next(item for item in written[0][1].get_items() if item.file_name == "about.xhtml")
    assert "<strong>Genre:</strong> Fantasy, Comedy, Drama" in about.content


def test_build_falls_back_to_novel_img_when_full_img_bytes_are_not_a_real_image(monkeypatch, tmp_path):
    # Regression: novel_full_img can come back with a Content-Type/URL that
    # *claims* a supported image type (e.g. a CDN/proxy error page mislabeled
    # as "image/jpeg") while the actual body isn't a real image. fetch_image
    # can't tell the difference by header/extension alone, so the cover path
    # must sniff the bytes itself and fall through to novel_img instead of
    # embedding the bad response.
    written = []
    novel: NovelResponse = {
        "result": {
            "novel": {
                "novel_no": 49,
                "novel_name": "Book",
                "flag_complete": 0,
                "novel_full_img": "https://example.com/bad.jpg",
                "novel_img": "https://example.com/good.jpg",
            },
            "writer_list": [{"writer_name": "Author"}],
        },
    }
    client = NovelpiaClient(throttle=0)

    def fake_get(url, *_args, **_kwargs):
        if url == "https://example.com/bad.jpg":
            # Mislabeled: valid image Content-Type, but the body is an HTML
            # error page, not real image bytes.
            return FakeImageResponse(b"<html>error</html>", {"Content-Type": "image/jpeg"})
        return FakeImageResponse(b"\xff\xd8\xffreal-jpeg-bytes", {"Content-Type": "image/jpeg"})

    monkeypatch.setattr(client.s, "get", fake_get)
    monkeypatch.setattr("src.epub.epub.write_epub", lambda path, book, _opts: written.append((path, book)))

    EpubBuilder(str(tmp_path)).build(
        client,
        novel,
        [{"episode_no": 1, "epi_title": "One"}],
        filename_hint="Book",
        fetched_results=[{"epi_no": 1, "epi_title": "One", "html": "<p>ok</p>"}],
    )

    book = written[0][1]
    cover_items = [item for item in book.get_items() if item.file_name == "cover.jpg"]
    assert len(cover_items) == 1
    assert cover_items[0].content == b"\xff\xd8\xffreal-jpeg-bytes"


def test_epub_image_adapter_rewrites_and_caches_image_when_fetch_succeeds(monkeypatch, tmp_path):
    client = NovelpiaClient(throttle=0)
    monkeypatch.setattr(client.s, "get", lambda *_args, **_kwargs: OkResponse())
    cache_dir = tmp_path / ".cache" / "images"
    adapter = EpubImageAdapter(ImageFetcher(), client, image_cache_dir=str(cache_dir))

    rewritten, items = adapter.add_images_and_rewrite('<p><img src="/cover.png"></p>')

    assert 'src="images/img_00001.png"' in rewritten
    assert len(items) == 1
    assert items[0].file_name == "images/img_00001.png"
    assert items[0].content == b"image-bytes"
    expected_path = cache_dir / f"{hashlib.sha256(b'https://global.novelpia.com/cover.png').hexdigest()}.png"
    assert expected_path.read_bytes() == b"image-bytes"


def test_epub_image_adapter_uses_cached_image_without_signed_key(monkeypatch, tmp_path):
    src = "https://pv-gn.novelpia.com/i.png"
    cache_dir = tmp_path / ".cache" / "images"
    cache_dir.mkdir(parents=True)
    image_bytes = b"\x89PNG\r\n\x1a\nimage"
    (cache_dir / f"{hashlib.sha256(src.encode()).hexdigest()}.png").write_bytes(image_bytes)
    client = _failing_client(monkeypatch)
    adapter = EpubImageAdapter(ImageFetcher(), client, image_cache_dir=str(cache_dir))

    rewritten, items = adapter.add_images_and_rewrite(f'<p><img src="{src}"></p>', embed_images=True)

    assert 'src="images/img_00001.png"' in rewritten
    assert len(items) == 1
    assert items[0].content == image_bytes


def test_epub_image_adapter_returns_fragment_without_html_body_wrappers(monkeypatch):
    client = NovelpiaClient(throttle=0)
    monkeypatch.setattr(client.s, "get", lambda *_args, **_kwargs: OkResponse())
    adapter = EpubImageAdapter(ImageFetcher(), client)

    rewritten, _items = adapter.add_images_and_rewrite('<p><img src="/cover.png"></p>')

    assert "<html" not in rewritten
    assert "<body" not in rewritten
    assert rewritten.startswith("<p>")


def test_epub_image_adapter_preserves_external_image_when_fetch_fails(monkeypatch):
    monkeypatch.setattr("src.export.time.sleep", lambda _seconds: None)
    adapter = EpubImageAdapter(ImageFetcher(), _failing_client(monkeypatch))

    rewritten, items = adapter.add_images_and_rewrite('<p><img src="https://example.com/missing.jpg"></p>')

    assert 'src="https://example.com/missing.jpg"' in rewritten
    assert items == []


def test_fetch_bytes_does_not_retry_permanent_4xx(monkeypatch, capsys):
    monkeypatch.setattr("src.export.time.sleep", lambda _seconds: (_ for _ in ()).throw(AssertionError("no retry")))
    client = NovelpiaClient(throttle=0)
    calls = []

    def get_blocked(*_args, **_kwargs) -> ErrorResponse:
        calls.append(True)
        return ErrorResponse(403)

    monkeypatch.setattr(client.s, "get", get_blocked)

    result = ImageFetcher(debug_dump=True).fetch_bytes(client, "https://example.com/missing.jpg")

    assert result is None
    assert calls == [True]
    assert "image fetch failed" in capsys.readouterr().out


def test_epub_image_adapter_derives_extension_for_unsupported_image_url(monkeypatch):
    client = NovelpiaClient(throttle=0)
    monkeypatch.setattr(client.s, "get", lambda *_args, **_kwargs: OkResponse({"Content-Type": "image/png"}))
    adapter = EpubImageAdapter(ImageFetcher(), client)

    rewritten, items = adapter.add_images_and_rewrite('<p><img src="/file.bin"></p>')

    assert 'src="images/img_00001.png"' in rewritten
    assert len(items) == 1
    assert items[0].file_name == "images/img_00001.png"


def test_write_txt_chapters_exports_successful_chapter_and_skips_failed(tmp_path, capsys):
    count = write_txt_chapters(
        str(tmp_path),
        [
            {"epi_no": 1, "epi_title": "One", "html": "<p>Hello<br>world</p>"},
            {"epi_no": 2, "epi_title": "Two", "error": "blocked"},
        ],
    )

    assert count == 1
    assert (tmp_path / "1_One.txt").read_text(encoding="utf-8") == "Hello\nworld"
    assert "[warn] Failed to fetch chapter 2: blocked" in capsys.readouterr().out


def test_epub_date_extracts_date_part():
    assert _epub_date("2024-01-15 10:30:00") == "2024-01-15"


def test_epub_date_returns_none_for_empty():
    assert _epub_date("") is None


def test_epub_date_returns_none_for_none():
    assert _epub_date(None) is None
