import requests

from src.api import request_with_retries
from src.api import NovelpiaClient


class FakeResponse:
    def __init__(self, status_code, payload, url="https://api-global.novelpia.com/test"):
        self.status_code = status_code
        self._payload = payload
        self.reason = "Internal Server Error"
        self.url = url
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.headers = {}
        self.cookies = []

    def request(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


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


def test_novelpia_client_close_closes_session(monkeypatch):
    closed = []
    client = NovelpiaClient()
    monkeypatch.setattr(client.s, "close", lambda: closed.append(True))

    client.close()

    assert closed == [True]


def test_fetch_episode_returns_error_on_bad_content_shape(monkeypatch):
    client = NovelpiaClient(throttle=0)
    monkeypatch.setattr(client, "episode_ticket", lambda _epi_no: {"result": {"_t": "token"}})
    monkeypatch.setattr(client, "episode_content", lambda _token: {"result": {"data": []}})

    result = client.fetch_episode({"episode_no": 123, "epi_title": "Bad"}, idx=4)

    assert result == {
        "error": "episode content parse failed: 'list' object has no attribute 'keys'",
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
