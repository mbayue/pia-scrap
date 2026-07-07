import json

import requests

from src.api import (
    AD_REWARD_WAIT_SECONDS,
    AdRewardRequired,
    ApiShapeError,
    NovelpiaClient,
    detect_ad_reward_required,
    request_with_retries,
)


class FakeResponse:
    def __init__(self, status_code, payload, url="https://api-global.novelpia.com/test"):
        self.status_code = status_code
        self._payload = payload
        self.reason = "Internal Server Error"
        self.url = url
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(self.reason)


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
        self.requests.append({"method": method, "url": url, "params": params, "json": json})
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

    response = request_with_retries(
        session,
        "GET",
        "https://api-global.novelpia.com/test",
        allow_refresh=True,
        refresh_fn=lambda: refreshed.append(True) or "new-token",
        max_retries=3,
    )

    assert response.status_code == 200
    assert session.calls == 2
    assert refreshed == [True]

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

def test_probe_ad_reward_unlock_waits_grants_then_retries_ticket(monkeypatch):
    sleeps = []
    client = NovelpiaClient(throttle=0)
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
