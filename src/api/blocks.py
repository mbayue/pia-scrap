"""Known Novelpia API business-rule blocks (ad reward / premium)."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


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


@dataclass(frozen=True, slots=True)
class AdRewardRequired:
    novel_no: int
    episode_no: int


@dataclass(frozen=True, slots=True)
class PremiumEpisodeBlocked:
    novel_no: int
    episode_no: int


KnownApiBlock = AdRewardRequired | PremiumEpisodeBlocked


def assert_never(value: object) -> None:
    raise AssertionError(f"unreachable value: {value!r}")


@dataclass(frozen=True, slots=True)
class KnownApiBlockError(Exception):
    block: KnownApiBlock

    def __str__(self) -> str:
        match self.block:
            case AdRewardRequired(novel_no=novel_no, episode_no=episode_no):
                return format_block_label(BlockKind.AD_REWARD, novel_no, episode_no)
            case PremiumEpisodeBlocked(novel_no=novel_no, episode_no=episode_no):
                return format_block_label(BlockKind.PREMIUM, novel_no, episode_no)
            case unreachable:
                assert_never(unreachable)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _known_block_episode_numbers(body: Mapping[str, Any], code: str, errmsg: str) -> tuple[int, int] | None:
    if body.get("code") != code or body.get("errmsg") != errmsg:
        return None
    result = body.get("result")
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    episode_data = data.get("data")
    if not isinstance(episode_data, dict):
        return None
    novel_no = _int_or_none(data.get("novel_no") or episode_data.get("novel_no"))
    episode_no = _int_or_none(episode_data.get("episode_no"))
    if novel_no is None or episode_no is None:
        return None
    return novel_no, episode_no


def detect_ad_reward_required(body: Mapping[str, Any]) -> AdRewardRequired | None:
    numbers = _known_block_episode_numbers(body, "0008", "novel.ADVERTISEMENT_EPISODE")
    if numbers is None:
        return None
    novel_no, episode_no = numbers
    return AdRewardRequired(novel_no=novel_no, episode_no=episode_no)


def detect_premium_episode_blocked(body: Mapping[str, Any]) -> PremiumEpisodeBlocked | None:
    numbers = _known_block_episode_numbers(body, "0009", "novel.PREMIUM_EPISODE")
    if numbers is None:
        return None
    novel_no, episode_no = numbers
    return PremiumEpisodeBlocked(novel_no=novel_no, episode_no=episode_no)


def detect_known_api_block(body: Mapping[str, Any]) -> KnownApiBlock | None:
    ad_reward = detect_ad_reward_required(body)
    if ad_reward is not None:
        return ad_reward
    return detect_premium_episode_blocked(body)
