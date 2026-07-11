import re
from enum import Enum
from typing import TypedDict


class NovelMeta(TypedDict, total=False):
    novel_no: int | str
    novel_name: str
    novel_full_img: str
    novel_img: str
    novel_story: str
    flag_complete: int | str
    count_epi: int | str
    reg_dt: str
    update_dt: str
    tag_list: list[dict[str, object] | str]


class Writer(TypedDict, total=False):
    writer_name: str


class NovelInfo(TypedDict, total=False):
    epi_cnt: int | str


class NovelResultRequired(TypedDict):
    novel: NovelMeta


class NovelResult(NovelResultRequired, total=False):
    writer_list: list[Writer]
    info: NovelInfo
    tag_list: list[dict[str, object] | str]


class NovelResponse(TypedDict):
    result: NovelResult


class EpisodeItem(TypedDict, total=False):
    episode_no: int | str
    epi_num: int | str
    epi_title: str


class EpisodeListResult(TypedDict):
    list: list[EpisodeItem]


class EpisodeListResponse(TypedDict):
    result: EpisodeListResult


EpisodeContentData = dict[str, str]


class EpisodeContentResult(TypedDict, total=False):
    data: EpisodeContentData
    content: str
    html: str
    text: str


class EpisodeContentResponse(TypedDict, total=False):
    result: EpisodeContentResult
    content: str


class ChapterResult(TypedDict, total=False):
    html: str
    error: str
    epi_no: int | None
    epi_title: str
    idx: int


def chapter_is_error(ch: ChapterResult) -> bool:
    """Check if a ChapterResult represents an error."""
    return "error" in ch and bool(ch["error"])


class FailedChapter(TypedDict, total=False):
    idx: int
    epi_no: int | None
    title: str
    url: str
    error: str


class QueueSummaryRow(TypedDict):
    novel_id: int
    status: str
    chapters: int | None
    title: str
    path: str | None


class QueueResult(TypedDict):
    rows: list[QueueSummaryRow]
    failures: list[tuple[int, str]]
    skipped_ids: list[int]


# Canonical source of truth for the label strings that flow between the live
# API client (which produces them in KnownApiBlockError.__str__) and the cache
# layer (which parses them back out of stored error strings). Keep these in one
# place so the two representations cannot drift apart.
class BlockKind(str, Enum):
    AD_REWARD = "ad reward required"
    PREMIUM = "premium episode blocked"


_BLOCK_LABEL_PATTERN = "|".join(re.escape(kind.value) for kind in BlockKind)
_BLOCK_RE = re.compile(
    rf"^(?P<label>{_BLOCK_LABEL_PATTERN}): "
    r"novel_no=(?P<novel_no>\d+) episode_no=(?P<episode_no>\d+)$"
)


def format_block_label(kind: BlockKind, novel_no: int, episode_no: int) -> str:
    return f"{kind.value}: novel_no={novel_no} episode_no={episode_no}"


def parse_block_label(text: str) -> tuple[BlockKind, int, int] | None:
    match = _BLOCK_RE.match(text)
    if match is None:
        return None
    label = match.group("label")
    kind = BlockKind(label)
    return kind, int(match.group("novel_no")), int(match.group("episode_no"))
