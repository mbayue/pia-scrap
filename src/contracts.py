from typing import Any, TypedDict


class NovelMeta(TypedDict, total=False):
    novel_no: int | str
    novel_name: str
    novel_full_img: str
    novel_img: str
    novel_story: str
    flag_complete: int | str
    count_epi: int | str
    tag_list: list[dict[str, Any] | str]


class Writer(TypedDict, total=False):
    writer_name: str


class NovelInfo(TypedDict, total=False):
    epi_cnt: int | str


class NovelResultRequired(TypedDict):
    novel: NovelMeta


class NovelResult(NovelResultRequired, total=False):
    writer_list: list[Writer]
    info: NovelInfo
    tag_list: list[dict[str, Any] | str]


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


class EpisodeContentData(TypedDict, total=False):
    epi_content: str


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


class FailedChapter(TypedDict, total=False):
    idx: int
    epi_no: int | None
    title: str
    url: str
    error: str
