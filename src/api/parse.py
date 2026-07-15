"""Typed parsing of Novelpia API JSON responses."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from src.contracts import (
    EpisodeContentData,
    EpisodeContentResponse,
    EpisodeContentResult,
    EpisodeItem,
    EpisodeListResponse,
    EpisodeListResult,
    NovelInfo,
    NovelMeta,
    NovelResponse,
    NovelResult,
    Writer,
)


class ResponseLike(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def reason(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def text(self) -> str: ...

    def json(self) -> Any: ...
    def raise_for_status(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ApiShapeError(Exception):
    label: str
    path: str
    expected: str = "present"

    def __str__(self) -> str:
        if self.expected == "present":
            return f"{self.label} missing {self.path}"
        return f"{self.label} expected {self.expected} at {self.path}"


def response_json_object(response: ResponseLike, label: str) -> dict[str, Any]:
    raw = response.json()
    if not isinstance(raw, dict):
        raise ApiShapeError(label, "$", "object")
    return raw


def required_object(data: Mapping[str, Any], key: str, path: str, label: str) -> dict[str, Any]:
    value = data.get(key)
    if key not in data:
        raise ApiShapeError(label, path)
    if not isinstance(value, dict):
        raise ApiShapeError(label, path, "object")
    return value


def required_list(data: Mapping[str, Any], key: str, path: str, label: str) -> list[Any]:
    value = data.get(key)
    if key not in data:
        raise ApiShapeError(label, path)
    if not isinstance(value, list):
        raise ApiShapeError(label, path, "list")
    return value


def parse_writers(writer_list: object) -> list[Writer]:
    """Normalize the raw ``writer_list`` payload into structured ``Writer`` rows."""
    writers: list[Writer] = []
    if not isinstance(writer_list, list):
        return writers
    for row in writer_list:
        if not isinstance(row, dict):
            continue
        writer_name = row.get("writer_name")
        if not isinstance(writer_name, str):
            continue
        writers.append({"writer_name": writer_name})
    return writers


def parse_novel_response(response: ResponseLike) -> NovelResponse:
    body = response_json_object(response, "novel response")
    result = required_object(body, "result", "$.result", "novel response")
    novel_body = required_object(result, "novel", "$.result.novel", "novel response")
    novel: NovelMeta = {}
    for key in (
        "novel_name",
        "novel_full_img",
        "novel_img",
        "novel_story",
        "flag_complete",
        "count_epi",
        "reg_dt",
        "update_dt",
    ):
        value = novel_body.get(key)
        if value is not None:
            novel[key] = value
    novel_no_raw = novel_body.get("novel_no")
    if novel_no_raw is not None:
        try:
            novel["novel_no"] = int(novel_no_raw)
        except (ValueError, TypeError) as err:
            raise ApiShapeError("novel response", "$.result.novel.novel_no", "integer") from err
    tag_list = novel_body.get("tag_list")
    if isinstance(tag_list, list):
        novel["tag_list"] = tag_list

    typed_result: NovelResult = {"novel": novel}
    writers = parse_writers(result.get("writer_list"))
    if writers:
        typed_result["writer_list"] = writers
    info_body = result.get("info")
    if isinstance(info_body, dict):
        info: NovelInfo = {}
        epi_cnt = info_body.get("epi_cnt")
        if epi_cnt is not None:
            info["epi_cnt"] = epi_cnt
        typed_result["info"] = info
    result_tag_list = result.get("tag_list")
    if isinstance(result_tag_list, list):
        typed_result["tag_list"] = result_tag_list
    return {"result": typed_result}


def parse_episode_list_response(response: ResponseLike) -> EpisodeListResponse:
    body = response_json_object(response, "episode list response")
    result = required_object(body, "result", "$.result", "episode list response")
    rows = required_list(result, "list", "$.result.list", "episode list response")
    episodes: list[EpisodeItem] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ApiShapeError("episode list response", f"$.result.list[{index}]", "object")
        episode: EpisodeItem = {}
        epi_title = row.get("epi_title")
        if epi_title is not None:
            episode["epi_title"] = epi_title
        for key in ("episode_no", "epi_num"):
            raw = row.get(key)
            if raw is not None:
                try:
                    episode[key] = int(raw)
                except (ValueError, TypeError) as err:
                    raise ApiShapeError("episode list response", f"$.result.list[{index}].{key}", "integer") from err
        episodes.append(episode)
    typed_result: EpisodeListResult = {"list": episodes}
    return {"result": typed_result}


def parse_episode_content_response(response: ResponseLike) -> EpisodeContentResponse:
    body = response_json_object(response, "episode content response")
    result = required_object(body, "result", "$.result", "episode content response")
    data = result.get("data")
    typed_result: EpisodeContentResult = {}
    if data is not None:
        if not isinstance(data, dict):
            raise ApiShapeError("episode content response", "$.result.data", "object")
        content_data: EpisodeContentData = {}
        for key, value in data.items():
            if str(key).startswith("epi_content") and isinstance(value, str):
                content_data[str(key)] = value
        typed_result["data"] = content_data
    for key in ("content", "html", "text"):
        value = result.get(key)
        if isinstance(value, str):
            match key:
                case "content":
                    typed_result["content"] = value
                case "html":
                    typed_result["html"] = value
                case "text":
                    typed_result["text"] = value
    response_body: EpisodeContentResponse = {"result": typed_result}
    content = body.get("content")
    if isinstance(content, str):
        response_body["content"] = content
    return response_body


def collect_epi_content_parts(data_block: Mapping[str, Any]) -> list[str]:
    """Collect and order ``epi_content*`` text fragments from a content data block."""
    import re as _re

    parts: list[str] = []

    def _key(k: str) -> tuple[int, int]:
        m = _re.search(r"(\d+)$", k)
        return (0 if k == "epi_content" else 1, int(m.group(1)) if m else 0)

    for k in sorted(
        (kk for kk in data_block.keys() if str(kk).startswith("epi_content")),
        key=_key,
    ):
        v = data_block.get(k)
        if isinstance(v, str) and v:
            parts.append(v)
    return parts
