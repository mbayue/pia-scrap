"""Novelpia API package — public surface preserved for ``from src.api import ...``."""

from __future__ import annotations

# Re-export modules used by tests via monkeypatch paths (src.api.time / secrets).
import secrets
import time

from src.api.blocks import (
    AdRewardRequired,
    BlockKind,
    KnownApiBlock,
    KnownApiBlockError,
    PremiumEpisodeBlocked,
    assert_never,
    detect_ad_reward_required,
    detect_known_api_block,
    detect_premium_episode_blocked,
    format_block_label,
    parse_block_label,
)
from src.api.client import (
    AD_REWARD_JITTER_SECONDS,
    AD_REWARD_WAIT_SECONDS,
    CONTENT_FETCH_ATTEMPTS,
    NovelpiaClient,
    Tokens,
)
from src.api.http import RETRY_WAIT_SECONDS, request_with_retries
from src.api.parse import ApiShapeError, ResponseLike
from src.html_norm import html_from_episode_text

__all__ = [
    "AD_REWARD_JITTER_SECONDS",
    "AD_REWARD_WAIT_SECONDS",
    "CONTENT_FETCH_ATTEMPTS",
    "AdRewardRequired",
    "ApiShapeError",
    "BlockKind",
    "KnownApiBlock",
    "KnownApiBlockError",
    "NovelpiaClient",
    "PremiumEpisodeBlocked",
    "RETRY_WAIT_SECONDS",
    "ResponseLike",
    "Tokens",
    "assert_never",
    "detect_ad_reward_required",
    "detect_known_api_block",
    "detect_premium_episode_blocked",
    "format_block_label",
    "html_from_episode_text",
    "parse_block_label",
    "request_with_retries",
    "secrets",
    "time",
]
