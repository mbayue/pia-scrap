"""Novelpia API client: auth, tickets, content, parallel fetch."""

from __future__ import annotations

import concurrent.futures
import random
import re as _re
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import requests

from src import const
from src.api.blocks import AdRewardRequired, detect_known_api_block
from src.api.http import RETRY_WAIT_SECONDS, request_with_retries
from src.api.parse import (
    ApiShapeError,
    collect_epi_content_parts,
    parse_episode_content_response,
    parse_episode_list_response,
    parse_novel_response,
    required_object,
    response_json_object,
)
from src.contracts import ChapterResult, EpisodeContentResponse, EpisodeItem, EpisodeListResponse, NovelResponse
from src.helper import (
    cookie_auth_from_jar,
    extract_t_token,
    is_placeholder_userkey,
    load_config,
    merge_login_at,
    save_config,
)
from src.html_norm import html_from_episode_text
from src.logutil import get_logger

logger = get_logger(__name__)

AD_REWARD_WAIT_SECONDS: Final = 5.0
AD_REWARD_JITTER_SECONDS: Final = (0.1, 0.5)
# Content access can 403 even for a freshly-issued ticket, and resending the
# same _t rarely recovers -- see fetch_episode, which retries by minting a
# brand new ticket instead.
CONTENT_FETCH_ATTEMPTS: Final = 3


@dataclass
class Tokens:
    login_at: str | None = None
    tkey: str | None = None
    userkey: str | None = None


def _safe_error_message(error: Exception) -> str:
    return _re.sub(r"([?&]_t=)[^&\s]+", r"\1<redacted>", str(error))


class NovelpiaClient:
    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        proxy: str | None = None,
        timeout: int = 30,
        throttle: float = 1.25,
        userkey: str | None = None,
        tkey: str | None = None,
        debug: bool = False,
    ):
        self.s = requests.Session()
        self.s.headers.update(const.SESSION_HEADERS.copy())
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.timeout = timeout
        self.tokens = Tokens()
        self.email = email
        self.password = password
        self.throttle = throttle
        self.debug = debug
        try:
            if not userkey:
                userkey = uuid.uuid4().hex
            self.s.cookies.set("USERKEY", userkey, domain=".novelpia.com", path="/")
            self.tokens.userkey = userkey
            if tkey:
                self.s.cookies.set("TKEY", tkey, domain=".novelpia.com", path="/")
                self.tokens.tkey = tkey
        except Exception as e:
            logger.info(f"Error setting cookies: {e}")

    def close(self):
        self.s.close()

    def login(self) -> str | None:
        url = f"{const.API_BASE}/v1/member/login"
        r = request_with_retries(
            self.s,
            "POST",
            url,
            json={"email": self.email, "passwd": self.password},
            timeout=self.timeout,
            max_retries=3,
            debug=self.debug,
        )
        r.raise_for_status()
        body = response_json_object(r, "login response")
        result = required_object(body, "result", "$.result", "login response")
        login_at = result.get("LOGINAT")
        if not isinstance(login_at, str):
            raise ApiShapeError("login response", "$.result.LOGINAT", "string")
        self.tokens.login_at = login_at
        auth = cookie_auth_from_jar(self.s.cookies)
        if auth.tkey:
            self.tokens.tkey = auth.tkey
        if auth.userkey and not is_placeholder_userkey(auth.userkey):
            self.tokens.userkey = auth.userkey
        return self.tokens.login_at

    def refresh(self) -> str | None:
        url = f"{const.API_BASE}/v1/login/refresh"
        r = request_with_retries(
            self.s,
            "GET",
            url,
            headers=merge_login_at({}, self.tokens.login_at),
            timeout=self.timeout,
            max_retries=3,
            debug=self.debug,
        )
        r.raise_for_status()
        body = response_json_object(r, "refresh response")
        result = required_object(body, "result", "$.result", "refresh response")
        login_at = result.get("LOGINAT")
        if not isinstance(login_at, str):
            raise ApiShapeError("refresh response", "$.result.LOGINAT", "string")
        self.tokens.login_at = login_at
        cfg = load_config()
        cfg["login_at"] = self.tokens.login_at or ""
        save_config(
            {
                "login_at": cfg.get("login_at"),
                "userkey": cfg.get("userkey"),
                "tkey": cfg.get("tkey"),
            }
        )
        return self.tokens.login_at

    def me(self) -> dict[str, Any]:
        url = f"{const.API_BASE}/v1/login/me"
        r = request_with_retries(
            self.s,
            "GET",
            url,
            headers=merge_login_at({}, self.tokens.login_at),
            timeout=self.timeout,
            allow_refresh=True,
            refresh_fn=self.refresh,
            login_fn=self.login,
            debug=self.debug,
        )
        r.raise_for_status()
        return response_json_object(r, "login/me response")

    def novel(self, novel_id: int) -> NovelResponse:
        url = f"{const.API_BASE}/v1/novel"
        r = request_with_retries(
            self.s,
            "GET",
            url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": novel_id},
            timeout=self.timeout,
            allow_refresh=True,
            refresh_fn=self.refresh,
            login_fn=self.login,
            debug=self.debug,
        )
        r.raise_for_status()
        return parse_novel_response(r)

    def episode_list(self, novel_id: int, rows: int) -> EpisodeListResponse:
        url = f"{const.API_BASE}/v1/novel/episode/list"
        r = request_with_retries(
            self.s,
            "GET",
            url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": novel_id, "rows": rows, "sort": "ASC"},
            timeout=self.timeout,
            allow_refresh=True,
            refresh_fn=self.refresh,
            login_fn=self.login,
            debug=self.debug,
        )
        r.raise_for_status()
        return parse_episode_list_response(r)

    def episode_ticket(self, episode_no: int, *, skip_throttle: bool = False) -> dict[str, Any]:
        url = f"{const.API_BASE}/v1/novel/episode"
        headers = merge_login_at({}, self.tokens.login_at)
        params = {"episode_no": episode_no}
        if self.throttle and not skip_throttle:
            time.sleep(self.throttle + random.SystemRandom().uniform(0.1, 0.4))
        r = request_with_retries(
            self.s,
            "GET",
            url,
            headers=headers,
            params=params,
            timeout=self.timeout,
            allow_refresh=True,
            refresh_fn=self.refresh,
            login_fn=self.login,
            max_retries=3,
            debug=self.debug,
            known_block_fn=detect_known_api_block,
        )
        r.raise_for_status()
        return response_json_object(r, "episode ticket response")

    def ad_reward_token(self, reward: AdRewardRequired) -> str:
        url = f"{const.API_BASE}/v1/ad/reward/token"
        r = request_with_retries(
            self.s,
            "GET",
            url,
            headers=merge_login_at({}, self.tokens.login_at),
            params={"novel_no": reward.novel_no, "episode_no": reward.episode_no},
            timeout=self.timeout,
            allow_refresh=True,
            refresh_fn=self.refresh,
            login_fn=self.login,
            max_retries=3,
            debug=self.debug,
        )
        r.raise_for_status()
        body = response_json_object(r, "ad reward token response")
        result = required_object(body, "result", "$.result", "ad reward token response")
        token = result.get("token")
        if not isinstance(token, str) or not token:
            raise ApiShapeError("ad reward token response", "$.result.token", "non-empty string")
        return token

    def grant_ad_reward(self, reward: AdRewardRequired, token: str) -> dict[str, Any]:
        url = f"{const.API_BASE}/v1/ad/reward/grant"
        r = request_with_retries(
            self.s,
            "POST",
            url,
            headers=merge_login_at({}, self.tokens.login_at),
            json={
                "novel_no": reward.novel_no,
                "episode_no": reward.episode_no,
                "flag_success": 1,
                "token": token,
            },
            timeout=self.timeout,
            allow_refresh=True,
            refresh_fn=self.refresh,
            login_fn=self.login,
            max_retries=3,
            debug=self.debug,
        )
        r.raise_for_status()
        return response_json_object(r, "ad reward grant response")

    def probe_ad_reward_unlock(
        self, reward: AdRewardRequired, wait_seconds: float = AD_REWARD_WAIT_SECONDS
    ) -> dict[str, Any]:
        token = self.ad_reward_token(reward)
        jitter = secrets.SystemRandom().uniform(*AD_REWARD_JITTER_SECONDS)
        time.sleep(wait_seconds + jitter)
        self.grant_ad_reward(reward, token)
        return self.episode_ticket(reward.episode_no, skip_throttle=True)

    def episode_content(self, token_t: str) -> EpisodeContentResponse:
        """Fetch chapter content for a single ``_t`` ticket (single attempt).

        A 403 here usually means the short-lived ``_t`` ticket itself is
        stale/invalid, not that the session (``login_at``) expired -- refreshing
        ``login_at`` and resending the *same* ``_t`` does not fix that. Retrying
        with a fresh ticket is the caller's job (see ``fetch_episode``), which
        can call ``episode_ticket`` again; this method deliberately does not
        retry or attempt auth recovery on its own.
        """
        url = f"{const.API_BASE}/v1/novel/episode/content"
        r = request_with_retries(
            self.s,
            "GET",
            url,
            params={"_t": token_t},
            timeout=self.timeout,
            max_retries=3,
            debug=self.debug,
        )
        r.raise_for_status()
        return parse_episode_content_response(r)

    def fetch_episode(
        self, ep: EpisodeItem, idx: int = 0, ticket_data: Mapping[str, Any] | None = None
    ) -> ChapterResult:
        """Fetch ticket and content for a single episode."""
        episode_no = ep.get("episode_no")
        if episode_no is None:
            return {
                "error": "missing episode_no",
                "epi_no": None,
                "epi_title": ep.get("epi_title") or f"Episode {ep.get('epi_num')}",
                "idx": idx,
            }
        epi_no = int(episode_no)
        epi_title = ep.get("epi_title") or f"Episode {ep.get('epi_num')}"

        # Ticket + content, retried together as a pair: a 403 on content usually
        # means the short-lived _t ticket is stale, not that login_at expired, so
        # retrying re-mints a fresh ticket rather than resending the same _t (see
        # episode_content). The first attempt reuses ticket_data/tdata when the
        # caller already fetched a ticket (e.g. the ad-reward unlock flow).
        cdata: EpisodeContentResponse | None = None
        tdata: Mapping[str, Any] = {}
        for attempt in range(1, CONTENT_FETCH_ATTEMPTS + 1):
            try:
                tdata = ticket_data if (attempt == 1 and ticket_data is not None) else self.episode_ticket(epi_no)
            except Exception as e:
                return {"error": _safe_error_message(e), "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

            token_t = extract_t_token(tdata)
            if not token_t:
                return {"error": "no token found", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

            try:
                cdata = self.episode_content(token_t)
                break
            except Exception as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                if status_code == 403 and attempt < CONTENT_FETCH_ATTEMPTS:
                    logger.warning(
                        f"[warn] chapter '{epi_title}' (episode_no={epi_no}): content access "
                        f"returned 403; retrying with a fresh ticket in {RETRY_WAIT_SECONDS:.0f}s "
                        f"({attempt}/{CONTENT_FETCH_ATTEMPTS})"
                    )
                    time.sleep(RETRY_WAIT_SECONDS)
                    continue
                return {"error": _safe_error_message(e), "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        assert cdata is not None, "loop must break with cdata set or return early"

        result_block = cdata.get("result", {})
        ticket_result = tdata.get("result", {}) if isinstance(tdata, Mapping) else {}
        data_block = result_block.get("data", {}) if isinstance(result_block, dict) else {}

        parts = []
        try:
            parts = collect_epi_content_parts(data_block)
        except Exception as e:
            return {"error": f"episode content parse failed: {e}", "epi_no": epi_no, "epi_title": epi_title, "idx": idx}

        html_text = "".join(parts).strip()
        if not html_text:
            html_text = (
                result_block.get("content")
                or result_block.get("html")
                or result_block.get("text")
                or cdata.get("content")
                or ""
            )

        try:
            html = html_from_episode_text(html_text)
        except Exception as e:
            return {
                "error": f"episode HTML normalization failed: {e}",
                "epi_no": epi_no,
                "epi_title": epi_title,
                "idx": idx,
            }

        return {
            "html": html,
            "epi_title": epi_title,
            "epi_no": epi_no,
            "idx": idx,
            "signed_key": ticket_result.get("signed_key", {}) if isinstance(ticket_result, Mapping) else {},
        }

    def fetch_episodes_parallel(
        self,
        ep_list: list[EpisodeItem],
        max_workers: int = 1,
        progress_cb=None,
        on_result: Callable[[int, ChapterResult], None] | None = None,
    ) -> list[ChapterResult]:
        """Fetch multiple episodes in parallel.

        ``on_result`` (if given) is invoked for each completed chapter with its
        list index and result, letting callers persist partial progress (e.g.
        cache every chapter as it arrives) so an interrupt doesn't lose data.
        """
        results: list[ChapterResult] = [{} for _ in range(len(ep_list))]
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        future_to_idx = {executor.submit(self.fetch_episode, ep, i + 1): i for i, ep in enumerate(ep_list)}
        shutdown_done = False
        try:
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    res = future.result()
                    results[idx] = res
                except Exception as e:
                    results[idx] = {"error": str(e), "idx": idx + 1}
                if on_result is not None:
                    on_result(idx, results[idx])
                if progress_cb:
                    progress_cb()
        except KeyboardInterrupt:
            for future in future_to_idx:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            shutdown_done = True
            raise
        else:
            executor.shutdown(wait=True)
            shutdown_done = True
        finally:
            if not shutdown_done:
                executor.shutdown(wait=False, cancel_futures=True)
        return results
