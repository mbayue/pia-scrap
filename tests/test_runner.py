import pytest

from src.runner import dedupe_novel_ids, parse_queue_lines


def test_parse_queue_lines_accepts_ids_urls_commas_and_comments():
    lines = [
        "49, https://global.novelpia.com/novel/5522?sid=main1 # keep these",
        "",
        "468",
    ]

    assert parse_queue_lines(lines, source="queue") == [49, 5522, 468]


def test_parse_queue_lines_reports_source_and_line():
    with pytest.raises(ValueError, match="web:2: invalid novel_id or novel URL 'bad'"):
        parse_queue_lines(["49", "bad"], source="web")


def test_dedupe_novel_ids_preserves_order_and_reports_skips():
    unique_ids, skipped_ids = dedupe_novel_ids([49, 5522, 49, 468, 5522])

    assert unique_ids == [49, 5522, 468]
    assert skipped_ids == [49, 5522]
