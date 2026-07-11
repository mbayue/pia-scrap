import concurrent.futures
import random
import re as _re
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import requests

from src import const
from src.contracts import (
    BlockKind,
    ChapterResult,
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
    format_block_label,
)
from src.helper import (
    attach_auth_cookies,
    cookie_auth_from_jar,
    extract_t_token,
    is_placeholder_userkey,
    j,
    load_config,
    mask_kv,
    merge_login_at,
    save_config,
)
from src.logutil import get_logger
from src.novel import html_from_episode_text

logger = get_logger(__name__)

JsonScalar = str | int | float | bool | None
JsonObject = Mapping[str, JsonScalar | list[JsonScalar] | Mapping[str, JsonScalar]]


class ResponseLike(Protocol):
    status_code: int
    reason: str
    url: str
    text: str

    def json(self) -> Any: ...
    def raise_for_status(self) -> None: ...


class RequestSession(Protocol):
    def request(
        self,
        method: Any,
        url: Any,
        *,
        headers: Any = None,
        params: Any = None,
        json: Any = None,
        data: Any = None,
        timeout: Any = 30,
    ) -> Any: ...


# ----------------------------
# API Client
# ----------------------------


@dataclass
class Tokens:
    login_at: str | None = None
    tkey: str | None = None
    userkey: str | None = None


@dataclass(frozen=True, slots=True)
class ApiShapeError(Exception):
    label: str
    path: str
    expected: str = "present"

    def __str__(self) -> str:
        if self.expected == "present":
            return f"{self.label} missing {self.path}"
        return f"{self.label} expected {self.expected} at {self.path}"


RETRY_WAIT_SECONDS = 1.0
AD_REWARD_WAIT_SECONDS: Final = 5.0
AD_REWARD_JITTER_SECONDS: Final = (0.1, 0.5)
# Content access can 403 even for a freshly-issued ticket, and resending the
# same _t rarely recovers -- see fetch_episode, which retries by minting a
# brand new ticket instead.
CONTENT_FETCH_ATTEMPTS: Final = 3


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
    if body.get("code") != code and body.get("errmsg") != errmsg:
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


def _response_json_object(response: ResponseLike, label: str) -> dict[str, Any]:
    raw = response.json()
    if not isinstance(raw, dict):
        raise ApiShapeError(label, "$", "object")
    return raw


def _required_object(data: Mapping[str, Any], key: str, path: str, label: str) -> dict[str, Any]:
    value = data.get(key)
    if key not in data:
        raise ApiShapeError(label, path)
    if not isinstance(value, dict):
        raise ApiShapeError(label, path, "object")
    return value


def _required_list(data: Mapping[str, Any], key: str, path: str, label: str) -> list[Any]:
    value = data.get(key)
    if key not in data:
        raise ApiShapeError(label, path)
    if not isinstance(value, list):
        raise ApiShapeError(label, path, "list")
    return value


def _parse_writers(writer_list: object) -> list[Writer]:
    """Normalize the raw ``writer_list`` payload into structured ``Writer`` rows."""
    writers: list[Writer] = []
    if not isinstance(writer_list, list):
        return writers
    for row in writer_list:
        if not isinstance(row, dict):
            continue
        writer: Writer = {}
        writer_name = row.get("writer_name")
        if isinstance(writer_name, str):
            writer["writer_name"] = writer_name
        writers.append(writer)
    return writers


def _parse_novel_response(response: ResponseLike) -> NovelResponse:
    body = _response_json_object(response, "novel response")
    result = _required_object(body, "result", "$.result", "novel response")
    novel_body = _required_object(result, "novel", "$.result.novel", "novel response")
    novel: NovelMeta = {}
    for key in (
        "novel_no",
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
    tag_list = novel_body.get("tag_list")
    if isinstance(tag_list, list):
        novel["tag_list"] = tag_list

    typed_result: NovelResult = {"novel": novel}
    writers = _parse_writers(result.get("writer_list"))
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


def _parse_episode_list_response(response: ResponseLike) -> EpisodeListResponse:
    body = _response_json_object(response, "episode list response")
    result = _required_object(body, "result", "$.result", "episode list response")
    rows = _required_list(result, "list", "$.result.list", "episode list response")
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
                except (ValueError, TypeError):
                    episode[key] = raw  # type: ignore[assignment]
        episodes.append(episode)
    typed_result: EpisodeListResult = {"list": episodes}
    return {"result": typed_result}


def _parse_episode_content_response(response: ResponseLike) -> EpisodeContentResponse:
    body = _response_json_object(response, "episode content response")
    result = _required_object(body, "result", "$.result", "episode content response")
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


def _safe_error_message(error: Exception) -> str:
    return _re.sub(r"([?&]_t=)[^&\s]+", r"\1<redacted>", str(error))


def _collect_epi_content_parts(data_block: Mapping[str, Any]) -> list[str]:
    """Collect and order ``epi_content*`` text fragments from a content data block."""
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
        data = r.json()
        result = data.get("result")
        if not isinstance(result, dict) or "LOGINAT" not in result:
            raise ApiShapeError("login", data)
        self.tokens.login_at = result["LOGINAT"]
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
        resp = r.json()
        result = resp.get("result")
        if not isinstance(result, dict) or "LOGINAT" not in result:
            raise ApiShapeError("refresh", resp)
        self.tokens.login_at = result["LOGINAT"]
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
        return _response_json_object(r, "login/me response")

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
        return _parse_novel_response(r)

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
        return _parse_episode_list_response(r)

    def episode_ticket(self, episode_no: int, *, skip_throttle: bool = False) -> dict[str, Any]:
        url = f"{const.API_BASE}/v1/novel/episode"
        headers = merge_login_at({}, self.tokens.login_at)
        params = {"episode_no": episode_no}
        # Throttle once per chapter before the ticket/content pair.
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
        return _response_json_object(r, "episode ticket response")

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
        body = _response_json_object(r, "ad reward token response")
        result = _required_object(body, "result", "$.result", "ad reward token response")
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
        return _response_json_object(r, "ad reward grant response")

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
        return _parse_episode_content_response(r)

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
        data_block = result_block.get("data", {}) if isinstance(result_block, dict) else {}

        parts = []
        try:
            parts = _collect_epi_content_parts(data_block)
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


def request_with_retries(
    session: RequestSession,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: JsonObject | None = None,
    json: JsonObject | None = None,
    data: JsonObject | None = None,
    timeout: int = 30,
    max_retries: int = 3,
    allow_refresh: bool = False,
    refresh_fn: Callable[[], str | None] | None = None,
    login_fn: Callable[[], str | None] | None = None,
    known_block_fn: Callable[[Mapping[str, Any]], KnownApiBlock | None] | None = None,
    debug: bool = False,
) -> Any:
    """Execute an HTTP request with retry, auth recovery, and block detection.

    Flow per attempt:
    1. Build headers (attach auth cookies)
    2. Send request
    3. If 5xx: try auth recovery -> handle server error -> retry
    4. If non-5xx: try auth recovery -> return
    5. On RequestException: retry with backoff
    """
    attempt = 0
    last_exc = None
    did_refresh = False
    did_login = False
    base_headers = headers
    while attempt < max_retries:
        attempt += 1
        try:
            request_headers = _build_request_headers(session, url, base_headers)

            if debug:
                logger.info(f"[api]   -> {method} {url} (attempt {attempt}/{max_retries})")
                _log_request_preview(method, session, request_headers, params, json)

            r = session.request(
                method, url, headers=request_headers, params=params, json=json, data=data, timeout=timeout
            )

            # Novelpia surfaces an expired/invalid session token as HTTP 500 with a
            # "token ... expired" body (not a 401/403), so a 5xx can still be an
            # auth problem worth recovering before the plain server-error retry.
            should_recover = _recovery_should_trigger(r, allow_refresh, bool(refresh_fn or login_fn), did_login)

            if r.status_code >= 500:
                if should_recover:
                    recovered = _try_auth_recovery(
                        session,
                        url,
                        method,
                        base_headers,
                        params,
                        json,
                        data,
                        timeout,
                        r,
                        allow_refresh,
                        refresh_fn,
                        login_fn,
                        did_refresh,
                        did_login,
                        debug=debug,
                    )
                    if recovered is not None:
                        r, did_refresh, did_login = recovered
                        if r.status_code < 500:
                            return r
                        # Recovered response is still a server error; fall through to
                        # the same >=500 handling below (known-block detection,
                        # retry/raise) instead of bypassing it.
                recovered_response = _handle_server_error(r, attempt, max_retries, known_block_fn, debug=debug)
                if recovered_response is not None:
                    r = recovered_response
                    return r
                continue

            recovered = _try_auth_recovery(
                session,
                url,
                method,
                base_headers,
                params,
                json,
                data,
                timeout,
                r,
                allow_refresh,
                refresh_fn,
                login_fn,
                did_refresh,
                did_login,
                debug=debug,
            )
            if recovered is not None:
                r, did_refresh, did_login = recovered
            return r
        except requests.RequestException as e:
            if debug:
                logger.info(f"[api] !! {method} {url} failed on attempt {attempt}: {e}")
            last_exc = e
            if attempt < max_retries:
                time.sleep(RETRY_WAIT_SECONDS)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("request attempts exhausted")


def _build_request_headers(
    session: RequestSession, url: str, base_headers: Mapping[str, str] | None
) -> Mapping[str, str] | None:
    """Attach auth cookies to the request headers, skipping the login endpoint.

    Best-effort: a failure to build headers is logged and the original headers
    are returned unchanged so the request can still proceed.
    """
    if "/v1/member/login" in url:
        return base_headers
    try:
        return attach_auth_cookies(session, base_headers)
    except Exception as e:  # noqa: BLE001 - best-effort header injection
        logger.error(f"Error occurred while attaching auth cookies: {e}")
        return base_headers


def _log_request_preview(
    method: str,
    session: RequestSession,
    request_headers: Mapping[str, str] | None,
    params: JsonObject | None,
    json: JsonObject | None,
) -> None:
    eff_headers: dict[str, str] = {}
    try:
        eff_headers.update(getattr(session, "headers", {}) or {})
        if request_headers:
            eff_headers.update(request_headers)
    except Exception as e:  # noqa: BLE001 - debug preview only
        logger.info(f"[api]   req-headers: <unavailable> ({e})")
    if params:
        logger.info(f"[api]   params:  {j(mask_kv(params))}")
    if json is not None:
        logger.info(f"[api]   json:    {j(mask_kv(json))}")


def _log_failed_response(r: ResponseLike) -> None:
    logger.info(f"[api]   <- {r.status_code} {r.reason} from {r.url}")
    try:
        logger.info(f"[api]   <- Response content: {j(mask_kv(r.json()))}")
    except Exception:  # noqa: BLE001 - response may not be valid JSON
        logger.info(f"[api]   <- Response content: {r.text}")


def _handle_server_error(
    r: ResponseLike,
    attempt: int,
    max_retries: int,
    known_block_fn: Callable[[Mapping[str, Any]], KnownApiBlock | None] | None,
    *,
    debug: bool = False,
) -> ResponseLike | None:
    """Handle a >=500 response: log, detect known API blocks, retry or raise.

    Returns ``None`` if the caller should ``continue`` the retry loop, or a
    replacement response when a known block was raised (handled by caller).
    """
    if debug:
        _log_failed_response(r)

    api_message = ""
    try:
        body = r.json()
    except Exception as e:  # noqa: BLE001 - error body may be malformed
        if debug:
            logger.error(f"Error occurred while reading API error message: {e}")
    else:
        if isinstance(body, Mapping):
            if r.status_code == 500 and known_block_fn is not None:
                block = known_block_fn(body)
                if block is not None:
                    raise KnownApiBlockError(block)
            api_message = body.get("errmsg") or body.get("message") or ""

    detail = api_message or "Server error"
    if attempt >= max_retries:
        # r is a ResponseLike (the RequestSession Protocol); only a real
        # requests.Response is accepted by requests.HTTPError's response arg.
        if isinstance(r, requests.Response):
            raise requests.HTTPError(detail, response=r)
        raise requests.HTTPError(detail)
    logger.warning(f"[warn] {detail} retrying in {RETRY_WAIT_SECONDS:.0f}s ({attempt}/{max_retries})")
    time.sleep(RETRY_WAIT_SECONDS)
    return None


def _recovery_should_trigger(
    r: ResponseLike,
    allow_refresh: bool,
    has_auth_fns: bool,
    did_login: bool,
    *,
    debug: bool = False,
) -> bool:
    """Decide whether an expired-session response warrants auth recovery.

    Novelpia surfaces an expired/invalid session token as HTTP 500 with a
    ``"token ... expired"`` body (not a 401/403), so a 500 can still be an
    auth problem worth recovering.
    """
    if not allow_refresh or not has_auth_fns or did_login:
        return False
    if r.status_code in (401, 403):
        return True
    msg = ""
    try:
        body = r.json()
        msg = (body.get("errmsg") or body.get("message") or "").lower()
    except Exception as e:  # noqa: BLE001 - error body may be malformed
        if debug:
            logger.error(f"Error occurred while reading API error response: {e}")
    return "token" in msg and "expire" in msg


def _run_refresh_then_login(
    refresh_fn: Callable[[], str | None] | None,
    login_fn: Callable[[], str | None] | None,
    did_refresh: bool,
    did_login: bool,
    *,
    debug: bool = False,
) -> tuple[bool, str | None, bool, bool]:
    """Run refresh then full login; return (success, login_at, did_refresh, did_login)."""
    success = False
    recovered_login_at: str | None = None
    if refresh_fn and not did_refresh:
        if debug:
            logger.info("[api] Session token expired (HTTP 500), trying refresh...")
        try:
            recovered_login_at = refresh_fn()
            did_refresh = True
            success = True
        except Exception:  # noqa: BLE001 - refresh best-effort
            if debug:
                logger.info("[api] Refresh failed.")
    if not success and login_fn and not did_login:
        if debug:
            logger.info("[api] Refresh failed or unavailable, trying full re-login...")
        try:
            recovered_login_at = login_fn()
            did_login = True
            success = True
        except Exception as e:  # noqa: BLE001 - login best-effort
            if debug:
                logger.info(f"[api] Re-login failed: {e}")
    return success, recovered_login_at, did_refresh, did_login


def _try_auth_recovery(
    session: RequestSession,
    url: str,
    method: str,
    base_headers: Mapping[str, str] | None,
    params: JsonObject | None,
    json: JsonObject | None,
    data: JsonObject | None,
    timeout: int,
    r: ResponseLike,
    allow_refresh: bool,
    refresh_fn: Callable[[], str | None] | None,
    login_fn: Callable[[], str | None] | None,
    did_refresh: bool,
    did_login: bool,
    *,
    debug: bool = False,
) -> tuple[ResponseLike, bool, bool] | None:
    """Attempt auth recovery (refresh, then full login) when Novelpia reports the
    session token expired -- surfaced as HTTP 500 with a ``"token ... expired"``
    body (not a 401/403).

    Returns the re-requested response plus updated refresh/login flags, or
    ``None`` when no recovery was triggered (the original response stands).
    """
    if not _recovery_should_trigger(r, allow_refresh, bool(refresh_fn or login_fn), did_login, debug=debug):
        return None

    success, recovered_login_at, did_refresh, did_login = _run_refresh_then_login(
        refresh_fn, login_fn, did_refresh, did_login, debug=debug
    )
    if not success:
        return None

    retry_headers = base_headers
    if recovered_login_at:
        retry_headers = merge_login_at(base_headers or {}, recovered_login_at)
    try:
        if "/v1/member/login" not in url:
            retry_headers = attach_auth_cookies(session, retry_headers)
    except Exception as e:  # noqa: BLE001 - best-effort header injection
        logger.error(f"Error occurred while attaching auth cookies: {e}")
    new_response = session.request(
        method, url, headers=retry_headers, params=params, json=json, data=data, timeout=timeout
    )
    return new_response, did_refresh, did_login
