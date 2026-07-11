"""Integration tests that exercise the real pipeline (API → fetch → build → output).

These tests require valid Novelpia credentials via environment variables.
They skip automatically when credentials are not available.
"""

import os

import pytest

from src.runner import QueueOptions, create_client

NOVELPIA_EMAIL = os.getenv("NOVELPIA_EMAIL")
NOVELPIA_PASSWORD = os.getenv("NOVELPIA_PASSWORD")

requires_auth = pytest.mark.skipif(
    not (NOVELPIA_EMAIL and NOVELPIA_PASSWORD),
    reason="Requires NOVELPIA_EMAIL and NOVELPIA_PASSWORD env vars",
)


@requires_auth
def test_create_client_authenticates():
    """Verify that create_client successfully authenticates with stored credentials."""
    options = QueueOptions(
        email=NOVELPIA_EMAIL,
        password=NOVELPIA_PASSWORD,
    )
    client = create_client(options)
    try:
        me = client.me()
        assert me.get("statusCode") == 200 or "result" in me
    finally:
        client.close()


@requires_auth
def test_fetch_novel_metadata(tmp_path):
    """Fetch novel metadata and episode list for a known novel."""
    from src.novel import fetch_novel_and_episodes

    options = QueueOptions(
        email=NOVELPIA_EMAIL,
        password=NOVELPIA_PASSWORD,
        out=str(tmp_path),
    )
    client = create_client(options)
    try:
        data, episodes, title, status = fetch_novel_and_episodes(client, 49)
        assert title
        assert len(episodes) > 0
        assert status in ("paid", "free", "unknown")
    finally:
        client.close()


@requires_auth
def test_build_small_epub(tmp_path):
    """Build an EPUB from a small chapter range."""
    from src.builder import build_epub

    options = QueueOptions(
        email=NOVELPIA_EMAIL,
        password=NOVELPIA_PASSWORD,
        out=str(tmp_path),
    )
    client = create_client(options)
    try:
        out_file, title, count = build_epub(
            client,
            49,
            str(tmp_path),
            max_chapters=3,
        )
        assert out_file is not None
        assert os.path.exists(out_file)
        assert out_file.endswith(".epub")
        assert count > 0
    finally:
        client.close()


@requires_auth
def test_build_small_txt(tmp_path):
    """Build TXT files from a small chapter range."""
    from src.builder import build_txt

    options = QueueOptions(
        email=NOVELPIA_EMAIL,
        password=NOVELPIA_PASSWORD,
        out=str(tmp_path),
    )
    client = create_client(options)
    try:
        out_dir, title, count = build_txt(
            client,
            49,
            str(tmp_path),
            max_chapters=3,
        )
        assert out_dir is not None
        assert os.path.isdir(out_dir)
        assert count > 0
    finally:
        client.close()


@requires_auth
def test_fetch_single_episode(tmp_path):
    """Fetch a single episode and verify content."""
    from src.novel import fetch_novel_and_episodes

    options = QueueOptions(
        email=NOVELPIA_EMAIL,
        password=NOVELPIA_PASSWORD,
        out=str(tmp_path),
    )
    client = create_client(options)
    try:
        data, episodes, title, status = fetch_novel_and_episodes(client, 49)
        assert len(episodes) > 0

        ep = episodes[0]
        result = client.fetch_episode(ep, 0)
        assert result is not None
        # Should have either html (success) or error (blocked)
        assert "html" in result or "error" in result
    finally:
        client.close()


@requires_auth
def test_build_epub_update_mode(tmp_path):
    """Build EPUB in update mode (reuse cached chapters)."""
    from src.builder import build_epub

    options = QueueOptions(
        email=NOVELPIA_EMAIL,
        password=NOVELPIA_PASSWORD,
        out=str(tmp_path),
    )
    client = create_client(options)
    try:
        # First build
        out_file1, title1, count1 = build_epub(
            client, 49, str(tmp_path), max_chapters=3,
        )
        assert out_file1 is not None
        assert count1 > 0

        # Second build with update=True — should reuse cache
        out_file2, title2, count2 = build_epub(
            client, 49, str(tmp_path), max_chapters=3, update=True,
        )
        # May return None if no new chapters (expected in update mode)
        if out_file2 is not None:
            assert os.path.exists(out_file2)
    finally:
        client.close()


@requires_auth
def test_build_epub_retry_failed_mode(tmp_path):
    """Build EPUB in retry-failed mode."""
    from src.builder import build_epub

    options = QueueOptions(
        email=NOVELPIA_EMAIL,
        password=NOVELPIA_PASSWORD,
        out=str(tmp_path),
    )
    client = create_client(options)
    try:
        # First build
        build_epub(client, 49, str(tmp_path), max_chapters=3)

        # Retry with retry_failed=True — should be a no-op if no failures
        out_file, title, count = build_epub(
            client, 49, str(tmp_path), max_chapters=3, retry_failed=True,
        )
        # Expected: None (no failed chapters to retry)
        assert out_file is None
    finally:
        client.close()


@requires_auth
def test_run_queue_multi_novel(tmp_path):
    """Run queue with multiple novels."""
    from src.runner import QueueOptions, run_queue

    options = QueueOptions(
        email=NOVELPIA_EMAIL,
        password=NOVELPIA_PASSWORD,
        out=str(tmp_path),
        max_chapters=2,
    )
    result = run_queue([49], options)
    assert len(result["rows"]) == 1
    assert result["rows"][0]["status"] in ("epub", "txt", "no updates")
