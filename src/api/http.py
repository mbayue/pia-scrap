"""HTTP request retry, auth recovery, and server-error handling."""

import time
from collections.abc import Callable, Mapping
from typing import cast

import requests

from src.api.blocks import KnownApiBlock, KnownApiBlockError
from src.api.parse import ResponseLike
from src.helper import attach_auth_cookies, j, mask_kv, merge_login_at
from src.logutil import get_logger

logger = get_logger(__name__)

JsonScalar = str | int | float | bool | None

RETRY_WAIT_SECONDS = 1.0


def request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: dict | None = None,
    json: dict | None = None,
    data: dict | None = None,
    timeout: int = 30,
    max_retries: int = 3,
    allow_refresh: bool = False,
    refresh_fn: Callable[[], str | None] | None = None,
    login_fn: Callable[[], str | None] | None = None,
    known_block_fn: Callable[[Mapping[str, object]], KnownApiBlock | None] | None = None,
    debug: bool = False,
) -> ResponseLike:
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

            r = cast(
                ResponseLike,
                session.request(
                    method,
                    url,
                    headers=request_headers,
                    params=params,
                    json=json,
                    data=data,
                    timeout=timeout,
                ),
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
                        # Recovered response is still a server error; fall through
                        # to the same >=500 handling below (known-block detection,
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
    session: requests.Session, url: str, base_headers: Mapping[str, str] | None
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
    session: requests.Session,
    request_headers: Mapping[str, str] | None,
    params: dict | None,
    json: dict | None,
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
    known_block_fn: Callable[[Mapping[str, object]], KnownApiBlock | None] | None,
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
        # r is a ResponseLike; only a real
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
    session: requests.Session,
    url: str,
    method: str,
    base_headers: Mapping[str, str] | None,
    params: dict | None,
    json: dict | None,
    data: dict | None,
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
    new_response = cast(
        ResponseLike,
        session.request(
            method,
            url,
            headers=retry_headers,
            params=params,
            json=json,
            data=data,
            timeout=timeout,
        ),
    )
    return new_response, did_refresh, did_login
