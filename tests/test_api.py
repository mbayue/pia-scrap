import json

import requests

from src.api import (
    AD_REWARD_WAIT_SECONDS,
    AdRewardRequired,
    ApiShapeError,
    KnownApiBlockError,
    NovelpiaClient,
    PremiumEpisodeBlocked,
    detect_ad_reward_required,
    detect_premium_episode_blocked,
    request_with_retries,
)
from src.contracts import BlockKind, ChapterResult, EpisodeItem, format_block_label, parse_block_label


class FakeResponse:
    def __init__(
        self,
        status_code,
        payload,
        url="https://api-global.novelpia.com/test",
        reason="Internal Server Error",
    ):
        self.status_code = status_code
        self._payload = payload
        self.reason = reason
        self.url = url
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(self.reason, response=self)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.headers = {}
        self.cookies = []

    def request(self, method, url, *, headers=None, params=None, json=None, data=None, timeout=30):
        self.calls += 1
        return self.responses.pop(0)


class FakeCookie:
    def __init__(self, name: str, value: str):
        self.name = name
        self.value = value

class RecordingSession(FakeSession):
    def __init__(self, responses):
        super().__init__(responses)
        self.requests = []

    def request(self, method, url, *, headers=None, params=None, json=None, data=None, timeout=30):
        self.requests.append({"method": method, "url": url, "headers": headers, "params": params, "json": json})
        return super().request(method, url, headers=headers, params=params, json=json, data=data, timeout=timeout)

def test_request_with_retries_retries_http_500_then_returns_success(monkeypatch, capsys):
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    session = FakeSession([
        FakeResponse(500, {"errmsg": "Too many requests. Please try again later."}),
        FakeResponse(200, {"errmsg": "", "result": "ok"}),
    ])

    response = request_with_retries(session, "GET", "https://api-global.novelpia.com/test", max_retries=3)

    assert session.calls == 2
    assert response.status_code == 200
    assert "[warn] Too many requests. Please try again later. retrying in 1s (1/3)" in capsys.readouterr().out


def test_request_with_retries_retries_502(monkeypatch):
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    session = FakeSession([
        FakeResponse(502, {"errmsg": "bad gateway"}, reason="Bad Gateway"),
        FakeResponse(200, {"result": "ok"}),
    ])

    response = request_with_retries(session, "GET", "https://api-global.novelpia.com/test", max_retries=3)

    assert session.calls == 2
    assert response.status_code == 200


def test_request_with_retries_raises_concise_api_message_after_final_500(monkeypatch):
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    session = FakeSession([
        FakeResponse(500, {"errmsg": "Too many requests. Please try again later."}),
        FakeResponse(500, {"errmsg": "Too many requests. Please try again later."}),
        FakeResponse(500, {"errmsg": "Too many requests. Please try again later."}),
    ])

    try:
        request_with_retries(session, "GET", "https://api-global.novelpia.com/test", max_retries=3)
    except requests.HTTPError as exc:
        assert str(exc) == "Too many requests. Please try again later."
    else:
        raise AssertionError("expected HTTPError")

    assert session.calls == 3


def test_request_with_retries_does_not_retry_401_before_auth_recovery(monkeypatch):
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    session = FakeSession([
        FakeResponse(401, {"errmsg": "token expire"}),
    ])

    response = request_with_retries(session, "GET", "https://api-global.novelpia.com/test", max_retries=3)

    assert session.calls == 1
    assert response.status_code == 401

def test_request_with_retries_refreshes_then_retries_expired_token(monkeypatch):
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    session = FakeSession([
        FakeResponse(401, {"errmsg": "token expire"}),
        FakeResponse(200, {"result": "ok"}),
    ])
    refreshed = []

    def refresh() -> str:
        refreshed.append(True)
        return "new-token"

    response = request_with_retries(
        session,
        "GET",
        "https://api-global.novelpia.com/test",
        allow_refresh=True,
        refresh_fn=refresh,
        max_retries=3,
    )

    assert response.status_code == 200
    assert session.calls == 2
    assert refreshed == [True]


def test_request_with_retries_refreshes_on_500_token_expired(monkeypatch):
    # Novelpia returns an expired session token as HTTP 500 (not 401/403).
    # The old code only recovered on 401/403 or non-5xx bodies, so a token-expiry
    # 500 retried forever without refreshing. This pins the corrected behaviour.
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    session = FakeSession([
        FakeResponse(500, {"errmsg": "The token has expired"}),
        FakeResponse(200, {"result": "ok"}),
    ])
    refreshed = []

    def refresh() -> str:
        refreshed.append(True)
        return "new-token"

    response = request_with_retries(
        session,
        "GET",
        "https://api-global.novelpia.com/test",
        allow_refresh=True,
        refresh_fn=refresh,
        max_retries=3,
    )

    assert response.status_code == 200
    assert session.calls == 2
    assert refreshed == [True]


def test_request_with_retries_retries_recovered_response_that_is_still_500(monkeypatch):
    # Auth recovery can succeed and still receive a *different*, unrelated 500 on
    # the retried request (e.g. a transient server hiccup right after re-login).
    # That recovered-but-still-500 response must not bypass known-block detection
    # and retry/backoff -- it should be handled exactly like a normal 500 instead
    # of being returned to the caller immediately.
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    session = FakeSession([
        FakeResponse(500, {"errmsg": "The token has expired"}),
        FakeResponse(500, {"errmsg": "Unrelated server hiccup"}),
        FakeResponse(200, {"result": "ok"}),
    ])
    refreshed = []

    def refresh() -> str:
        refreshed.append(True)
        return "new-token"

    response = request_with_retries(
        session,
        "GET",
        "https://api-global.novelpia.com/test",
        allow_refresh=True,
        refresh_fn=refresh,
        max_retries=5,
    )

    assert response.status_code == 200
    assert session.calls == 3
    assert refreshed == [True]


def test_request_with_retries_rebuilds_auth_headers_after_refresh(monkeypatch):
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    session = RecordingSession([
        FakeResponse(401, {"errmsg": "token expire"}),
        FakeResponse(200, {"result": "ok"}),
    ])
    session.cookies = [
        FakeCookie("USERKEY", "old-user"),
        FakeCookie("TKEY", "old-token"),
    ]

    def refresh():
        session.cookies = [
            FakeCookie("USERKEY", "new-user"),
            FakeCookie("TKEY", "new-token"),
        ]
        return "fresh-login"

    response = request_with_retries(
        session,
        "GET",
        "https://api-global.novelpia.com/test",
        headers={"login-at": "stale-login"},
        allow_refresh=True,
        refresh_fn=refresh,
        max_retries=3,
    )

    assert response.status_code == 200
    assert session.requests[0]["headers"]["login-at"] == "stale-login"
    assert session.requests[0]["headers"]["Cookie"] == "USERKEY=old-user; TKEY=old-token; last_login=basic"
    assert session.requests[1]["headers"]["login-at"] == "fresh-login"
    assert session.requests[1]["headers"]["Cookie"] == "USERKEY=new-user; TKEY=new-token; last_login=basic"

def test_request_with_retries_logs_in_when_refresh_fails(monkeypatch):
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    session = FakeSession([
        FakeResponse(200, {"errmsg": "token expire"}),
        FakeResponse(200, {"result": "ok"}),
    ])
    logins = []

    def fail_refresh():
        raise RuntimeError("stale")

    response = request_with_retries(
        session,
        "GET",
        "https://api-global.novelpia.com/test",
        allow_refresh=True,
        refresh_fn=fail_refresh,
        login_fn=lambda: logins.append(True),
        max_retries=3,
    )

    assert response.status_code == 200
    assert session.calls == 2
    assert logins == [True]


def test_novelpia_client_close_closes_session(monkeypatch):
    closed = []
    client = NovelpiaClient()
    monkeypatch.setattr(client.s, "close", lambda: closed.append(True))

    client.close()

    assert closed == [True]

def test_fetch_episodes_parallel_propagates_keyboard_interrupt():
    class InterruptingClient(NovelpiaClient):
        def fetch_episode(self, ep: EpisodeItem, idx: int = 0, ticket_data=None) -> ChapterResult:
            raise KeyboardInterrupt

    client = InterruptingClient(throttle=0)

    try:
        client.fetch_episodes_parallel([{"episode_no": 1}], max_workers=1)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("expected KeyboardInterrupt")

def test_login_ignores_placeholder_userkey_cookie():
    password = "test-" + "password"
    client = NovelpiaClient(email="email@example.com", password=password, userkey="generated-user", throttle=0)
    fake_session = FakeSession([FakeResponse(200, {"result": {"LOGINAT": "login-token"}})])
    fake_session.cookies = [
        FakeCookie("USERKEY", "login-user"),
        FakeCookie("TKEY", "login-t"),
    ]
    client.__dict__["s"] = fake_session

    client.login()

    assert client.tokens.login_at == "login-token"
    assert client.tokens.userkey == "generated-user"
    assert client.tokens.tkey == "login-t"

def test_refresh_merges_login_at_into_existing_config(monkeypatch, tmp_path):
    config_path = tmp_path / ".api.json"
    config_path.write_text('{"userkey": "stored-user", "tkey": "stored-t"}', encoding="utf-8")
    client = NovelpiaClient(throttle=0)
    client.__dict__["s"] = FakeSession([FakeResponse(200, {"result": {"LOGINAT": "fresh-login"}})])
    monkeypatch.setattr("src.helper.CONFIG_PATH", str(config_path))

    assert client.refresh() == "fresh-login"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "login_at": "fresh-login",
        "userkey": "stored-user",
        "tkey": "stored-t",
    }

def test_novel_returns_validated_consumed_shape():
    client = NovelpiaClient(throttle=0)
    client.__dict__["s"] = FakeSession([
        FakeResponse(200, {"result": {"novel": {"novel_name": "Book", "count_epi": 1}}}),
    ])

    result = client.novel(123)

    assert result["result"]["novel"].get("novel_name") == "Book"

def test_episode_list_reports_missing_consumed_key():
    client = NovelpiaClient(throttle=0)
    client.__dict__["s"] = FakeSession([FakeResponse(200, {"result": {}})])

    try:
        client.episode_list(123, rows=10)
    except ApiShapeError as exc:
        assert exc.path == "$.result.list"
        assert str(exc) == "episode list response missing $.result.list"
    else:
        raise AssertionError("expected ApiShapeError")

def test_episode_content_reports_bad_consumed_shape():
    client = NovelpiaClient(throttle=0)
    client.__dict__["s"] = FakeSession([FakeResponse(200, {"result": {"data": []}})])

    try:
        client.episode_content("token")
    except ApiShapeError as exc:
        assert exc.path == "$.result.data"
        assert str(exc) == "episode content response expected object at $.result.data"
    else:
        raise AssertionError("expected ApiShapeError")

def test_episode_content_makes_single_plain_request(monkeypatch):
    # episode_content deliberately does not retry or attempt login_at auth
    # recovery: a 403/expired-token response here almost always means the
    # short-lived _t ticket itself is stale, which refreshing login_at cannot
    # fix. Retrying with a freshly-minted ticket is fetch_episode's job.
    captured = {}

    def fake_request_with_retries(session, method, url, **kwargs):
        captured.update(kwargs)
        return FakeResponse(200, {"result": {"data": {"epi_content": "<p>ok</p>"}}})

    client = NovelpiaClient(throttle=0)
    client.tokens.tkey = "auth-token"
    monkeypatch.setattr("src.api.request_with_retries", fake_request_with_retries)

    response = client.episode_content("token")

    result = response.get("result")
    assert result is not None
    data = result.get("data")
    assert data is not None
    assert data["epi_content"] == "<p>ok</p>"
    assert "allow_refresh" not in captured
    assert "refresh_fn" not in captured
    assert "login_fn" not in captured


def test_detect_ad_reward_required_from_failure_shape():
    body = {
        "statusCode": 500,
        "errmsg": "novel.ADVERTISEMENT_EPISODE",
        "code": "0008",
        "result": {
            "name": "NOVEL_ERROR",
            "data": {
                "novel_no": 23,
                "data": {
                    "episode_no": 2407,
                    "novel_no": 23,
                    "epi_num": 31,
                },
            },
        },
    }

    reward = detect_ad_reward_required(body)

    assert reward == AdRewardRequired(novel_no=23, episode_no=2407)

def test_detect_premium_episode_blocked_from_failure_shape():
    body = {
        "statusCode": 500,
        "errmsg": "novel.PREMIUM_EPISODE",
        "code": "0009",
        "result": {
            "name": "NOVEL_ERROR",
            "data": {
                "novel_no": "23",
                "data": {
                    "episode_no": "2408",
                    "novel_no": "23",
                    "epi_num": 32,
                },
            },
        },
    }

    blocked = detect_premium_episode_blocked(body)

    assert blocked == PremiumEpisodeBlocked(novel_no=23, episode_no=2408)

def test_detect_known_blocks_ignore_malformed_or_unrelated_body():
    malformed_premium = {"code": "0009", "errmsg": "novel.PREMIUM_EPISODE", "result": {"data": {}}}
    malformed_ad = {"code": "0008", "errmsg": "novel.ADVERTISEMENT_EPISODE", "result": {"data": {}}}
    unrelated = {"code": "9999", "errmsg": "novel.OTHER"}

    assert detect_premium_episode_blocked(malformed_premium) is None
    assert detect_premium_episode_blocked(unrelated) is None
    assert detect_ad_reward_required(malformed_ad) is None
    assert detect_ad_reward_required(unrelated) is None

def test_known_api_block_error_formats_block_marker():
    error = KnownApiBlockError(PremiumEpisodeBlocked(novel_no=23, episode_no=2408))

    assert str(error) == "premium episode blocked: novel_no=23 episode_no=2408"

def test_parse_block_label_round_trips_all_block_kinds():
    for kind in BlockKind:
        label = format_block_label(kind, novel_no=23, episode_no=2408)

        assert parse_block_label(label) == (kind, 23, 2408)

def test_episode_ticket_classifies_ad_block_without_retrying(monkeypatch):
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    client = NovelpiaClient(throttle=0)
    session = FakeSession([
        FakeResponse(500, {
            "errmsg": "novel.ADVERTISEMENT_EPISODE",
            "code": "0008",
            "result": {"data": {"novel_no": 23, "data": {"episode_no": 2407}}},
        }),
    ])
    client.__dict__["s"] = session

    try:
        client.episode_ticket(2407)
    except KnownApiBlockError as exc:
        assert exc.block == AdRewardRequired(novel_no=23, episode_no=2407)
    else:
        raise AssertionError("expected KnownApiBlockError")

    assert session.calls == 1

def test_episode_ticket_classifies_premium_block_without_retrying(monkeypatch):
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    client = NovelpiaClient(throttle=0)
    session = FakeSession([
        FakeResponse(500, {
            "errmsg": "novel.PREMIUM_EPISODE",
            "code": "0009",
            "result": {"data": {"novel_no": 23, "data": {"episode_no": 2408}}},
        }),
    ])
    client.__dict__["s"] = session

    try:
        client.episode_ticket(2408)
    except KnownApiBlockError as exc:
        assert exc.block == PremiumEpisodeBlocked(novel_no=23, episode_no=2408)
    else:
        raise AssertionError("expected KnownApiBlockError")

    assert session.calls == 1

def test_episode_ticket_unknown_500_still_retries(monkeypatch):
    monkeypatch.setattr("src.api.time.sleep", lambda _: None)
    client = NovelpiaClient(throttle=0)
    session = FakeSession([
        FakeResponse(500, {"errmsg": "temporary"}),
        FakeResponse(500, {"errmsg": "temporary"}),
        FakeResponse(500, {"errmsg": "temporary"}),
    ])
    client.__dict__["s"] = session

    try:
        client.episode_ticket(2409)
    except requests.HTTPError as exc:
        assert str(exc) == "temporary"
    else:
        raise AssertionError("expected HTTPError")

    assert session.calls == 3

def test_probe_ad_reward_unlock_waits_grants_then_retries_ticket(monkeypatch):
    sleeps = []
    client = NovelpiaClient(throttle=1.25)
    client.tokens.login_at = "login-token"
    session = RecordingSession([
        FakeResponse(200, {"result": {"token": "reward-token"}}),
        FakeResponse(201, {"result": {"granted": True}}),
        FakeResponse(200, {"result": {"_t": "episode-token"}}),
    ])
    client.__dict__["s"] = session
    monkeypatch.setattr("src.api.secrets.SystemRandom.uniform", lambda _self, low, high: 0.3)
    monkeypatch.setattr("src.api.time.sleep", lambda seconds: sleeps.append(seconds))

    result = client.probe_ad_reward_unlock(AdRewardRequired(novel_no=23, episode_no=2407))

    assert sleeps == [AD_REWARD_WAIT_SECONDS + 0.3]
    assert result == {"result": {"_t": "episode-token"}}
    assert [call["method"] for call in session.requests] == ["GET", "POST", "GET"]
    assert session.requests[0]["url"].endswith("/v1/ad/reward/token")
    assert session.requests[0]["params"] == {"novel_no": 23, "episode_no": 2407}
    assert session.requests[1]["url"].endswith("/v1/ad/reward/grant")
    assert session.requests[1]["json"] == {
        "novel_no": 23,
        "episode_no": 2407,
        "flag_success": 1,
        "token": "reward-token",
    }
    assert session.requests[2]["url"].endswith("/v1/novel/episode")
    assert session.requests[2]["params"] == {"episode_no": 2407}

def test_fetch_episode_retries_with_fresh_ticket_on_transient_403(monkeypatch):
    # A 403 on content means the _t ticket is stale, not that login_at expired,
    # so the retry must mint a brand new ticket rather than resend the same _t.
    sleeps = []
    session = FakeSession([
        FakeResponse(200, {"result": {"_t": "ticket-token-1"}}),
        FakeResponse(403, {}, url="https://api-global.novelpia.com/content?_t=secret-token"),
        FakeResponse(200, {"result": {"_t": "ticket-token-2"}}),
        FakeResponse(200, {"result": {"data": {"epi_content": "<p>ok</p>"}}}),
    ])
    client = NovelpiaClient(throttle=0)
    client.__dict__["s"] = session
    monkeypatch.setattr("src.api.time.sleep", lambda seconds: sleeps.append(seconds))

    result = client.fetch_episode({"episode_no": 1, "epi_num": 1, "epi_title": "One"}, 1)

    assert result.get("html")
    assert result.get("error") is None
    assert sleeps == [1.0]
    assert session.calls == 4

def test_fetch_episode_redacts_content_token_after_persistent_403(monkeypatch):
    # Every attempt mints its own fresh ticket (secret-token-1/2/3), and all
    # three content fetches 403 -- fetch_episode gives up after
    # CONTENT_FETCH_ATTEMPTS and surfaces the last error with the token redacted.
    session = FakeSession([
        FakeResponse(200, {"result": {"_t": "secret-token-1"}}),
        FakeResponse(403, {}, reason="Forbidden for url: https://api-global.novelpia.com/content?_t=secret-token-1"),
        FakeResponse(200, {"result": {"_t": "secret-token-2"}}),
        FakeResponse(403, {}, reason="Forbidden for url: https://api-global.novelpia.com/content?_t=secret-token-2"),
        FakeResponse(200, {"result": {"_t": "secret-token-3"}}),
        FakeResponse(403, {}, reason="Forbidden for url: https://api-global.novelpia.com/content?_t=secret-token-3"),
    ])
    client = NovelpiaClient(throttle=0)
    client.__dict__["s"] = session
    monkeypatch.setattr("src.api.time.sleep", lambda _seconds: None)

    result = client.fetch_episode({"episode_no": 1, "epi_num": 1, "epi_title": "One"}, 1)

    assert "secret-token" not in str(result.get("error"))
    assert "_t=<redacted>" in str(result.get("error"))
    assert session.calls == 6

def test_fetch_episode_returns_error_on_bad_content_shape(monkeypatch):
    client = NovelpiaClient(throttle=0)
    monkeypatch.setattr(client, "episode_ticket", lambda _epi_no: {"result": {"_t": "token"}})
    def bad_content(_token):
        raise ApiShapeError("episode content response", "$.result.data", "object")

    monkeypatch.setattr(client, "episode_content", bad_content)

    result = client.fetch_episode({"episode_no": 123, "epi_title": "Bad"}, idx=4)

    assert result == {
        "error": "episode content response expected object at $.result.data",
        "epi_no": 123,
        "epi_title": "Bad",
        "idx": 4,
    }


def test_fetch_episode_returns_error_on_bad_direct_url_content_shape(monkeypatch):
    # The direct_url branch (no _t token, only a content-endpoint URL) must run
    # its response through _parse_episode_content_response just like the
    # token_t/episode_content branch does, instead of trusting r.json() as-is.
    client = NovelpiaClient(throttle=0)
    monkeypatch.setattr(client, "episode_ticket", lambda _epi_no: {"result": {}})
    monkeypatch.setattr(
        "src.api.extract_t_token",
        lambda _tdata: (None, "https://api-global.novelpia.com/v1/novel/episode/content?_t=plain-token"),
    )

    class DirectUrlSession:
        def get(self, url, timeout=30):
            return FakeResponse(200, {"result": {"data": []}}, url=url)

    client.__dict__["s"] = DirectUrlSession()

    result = client.fetch_episode({"episode_no": 123, "epi_title": "Bad"}, idx=4)

    assert result == {
        "error": "episode content response expected object at $.result.data",
        "epi_no": 123,
        "epi_title": "Bad",
        "idx": 4,
    }


def test_fetch_episode_returns_error_on_html_normalization_failure(monkeypatch):
    client = NovelpiaClient(throttle=0)
    monkeypatch.setattr(client, "episode_ticket", lambda _epi_no: {"result": {"_t": "token"}})
    monkeypatch.setattr(client, "episode_content", lambda _token: {"result": {"data": {"epi_content": "<p>x</p>"}}})

    def fail_normalize(_html):
        raise RuntimeError("bad html")

    monkeypatch.setattr("src.api.html_from_episode_text", fail_normalize)

    result = client.fetch_episode({"episode_no": 123, "epi_title": "Bad"}, idx=4)

    assert result == {
        "error": "episode HTML normalization failed: bad html",
        "epi_no": 123,
        "epi_title": "Bad",
        "idx": 4,
    }
