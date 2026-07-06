from src.epub import EpubBuilder


class FailingSession:
    def get(self, *_args, **_kwargs):
        raise RuntimeError("network down")


class DummyClient:
    timeout = 1
    tokens = type("Tokens", (), {"login_at": None})()
    s = FailingSession()


def test_fetch_bytes_logs_debug_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("src.epub.time.sleep", lambda _seconds: None)
    builder = EpubBuilder(str(tmp_path), debug_dump=True)

    result = builder._fetch_bytes(DummyClient(), "https://example.com/image.jpg")

    assert result is None
    assert "[debug] image fetch failed: https://example.com/image.jpg: network down" in capsys.readouterr().out


def test_fetch_bytes_stays_quiet_without_debug(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("src.epub.time.sleep", lambda _seconds: None)
    builder = EpubBuilder(str(tmp_path), debug_dump=False)

    result = builder._fetch_bytes(DummyClient(), "https://example.com/image.jpg")

    assert result is None
    assert capsys.readouterr().out == ""
