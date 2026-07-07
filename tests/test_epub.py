from src.api import NovelpiaClient
from src.contracts import NovelResponse
from src.epub import EpubBuilder
from src.export import EpubImageAdapter, ImageFetcher, write_txt_chapters


class OkResponse:
    status_code = 200
    content = b"image-bytes"

    def raise_for_status(self):
        return None


def _failing_client(monkeypatch):
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
    )

    assert result == (str(tmp_path / "book" / "book.epub"), "Book", 1)
    assert written == [str(tmp_path / "book" / "book.epub")]


def test_epub_image_adapter_rewrites_image_when_fetch_succeeds(monkeypatch):
    client = NovelpiaClient(throttle=0)
    monkeypatch.setattr(client.s, "get", lambda *_args, **_kwargs: OkResponse())
    adapter = EpubImageAdapter(ImageFetcher(), client)

    rewritten, items = adapter.add_images_and_rewrite('<p><img src="/cover.png"></p>')

    assert 'src="images/img_00001.png"' in rewritten
    assert len(items) == 1
    assert items[0].file_name == "images/img_00001.png"
    assert items[0].content == b"image-bytes"


def test_epub_image_adapter_preserves_external_image_when_fetch_fails(monkeypatch):
    monkeypatch.setattr("src.export.time.sleep", lambda _seconds: None)
    adapter = EpubImageAdapter(ImageFetcher(), _failing_client(monkeypatch))

    rewritten, items = adapter.add_images_and_rewrite('<p><img src="https://example.com/missing.jpg"></p>')

    assert 'src="https://example.com/missing.jpg"' in rewritten
    assert items == []


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
