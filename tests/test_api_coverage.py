from typing import Any

import pytest
import requests

from src.api.blocks import AdRewardRequired, KnownApiBlockError, assert_never, detect_known_api_block
from src.api.client import NovelpiaClient, Tokens
from src.api.http import (
    _build_request_headers,
    _handle_server_error,
    _log_failed_response,
    _log_request_preview,
    _recovery_should_trigger,
    _run_refresh_then_login,
    _try_auth_recovery,
    request_with_retries,
)
from src.api.parse import (
    ApiShapeError,
    collect_epi_content_parts,
    parse_episode_content_response,
    parse_episode_list_response,
    parse_novel_response,
    required_list,
    required_object,
    response_json_object,
)


class Response(requests.Response):
    def __init__(self, status_code=200, payload=None, text="text", reason="Reason"):
        super().__init__()
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._content = text.encode("utf-8")
        self.reason = reason
        self.url = "https://api-global.novelpia.com/test"

    def json(self, **kwargs: Any) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(self.reason, response=self)


class Session(requests.Session):
    def __init__(self, responses=()):
        super().__init__()
        self.responses = list(responses)
        self.calls = []

    def request(
        self,
        method: str | bytes,
        url: str | bytes,
        *args: Any,
        **kwargs: Any,
    ) -> requests.Response:
        self.calls.append(
            (
                method,
                url,
                kwargs.get("headers"),
                kwargs.get("params"),
                kwargs.get("json"),
                kwargs.get("data"),
                kwargs.get("timeout"),
            )
        )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_parse_edges_full_shapes():
    novel = parse_novel_response(
        Response(
            payload={
                "result": {
                    "novel": {"novel_no": 1, "novel_name": "Book", "tag_list": ["A"]},
                    "writer_list": [{"writer_name": "W"}, "bad", {}],
                    "info": {"epi_cnt": 2},
                    "tag_list": ["B"],
                }
            }
        )
    )
    result = novel.get("result", {})
    assert result.get("writer_list") == [{"writer_name": "W"}]
    assert result.get("info") == {"epi_cnt": 2}
    assert result.get("tag_list") == ["B"]

    with pytest.raises(ApiShapeError, match=r"integer at \$\.result\.list\[0\]\.epi_num"):
        parse_episode_list_response(
            Response(payload={"result": {"list": [{"epi_title": "T", "episode_no": "1", "epi_num": "x"}]}})
        )

    content = parse_episode_content_response(
        Response(
            payload={
                "result": {"data": {"epi_content": "a", "skip": 1}, "content": "b", "html": "c", "text": "d"},
                "content": "top",
            }
        )
    )
    content_result = content.get("result", {})
    assert content_result.get("data") == {"epi_content": "a"}
    assert content.get("content") == "top"
    assert collect_epi_content_parts({"epi_content2": "b", "epi_content": "a", "x": "z"}) == ["a", "b"]


def test_parse_error_edges():
    with pytest.raises(ApiShapeError, match="root expected object"):
        response_json_object(Response(payload=[]), "root")
    with pytest.raises(ApiShapeError, match="x missing"):
        required_object({}, "x", "x", "x")
    with pytest.raises(ApiShapeError, match="x expected object"):
        required_object({"x": []}, "x", "x", "x")
    with pytest.raises(ApiShapeError, match="x missing"):
        required_list({}, "x", "x", "x")
    with pytest.raises(ApiShapeError, match="x expected list"):
        required_list({"x": {}}, "x", "x", "x")
    with pytest.raises(ApiShapeError, match=r"\.result\.list\[0\]"):
        parse_episode_list_response(Response(payload={"result": {"list": [1]}}))


def test_blocks_edges():
    assert detect_known_api_block(
        {
            "code": "0008",
            "errmsg": "novel.ADVERTISEMENT_EPISODE",
            "result": {"data": {"data": {"episode_no": 1, "novel_no": 2}}},
        }
    ) == AdRewardRequired(2, 1)
    assert detect_known_api_block({"code": "x", "errmsg": "y"}) is None
    with pytest.raises(AssertionError):
        assert_never("x")
    assert str(KnownApiBlockError(AdRewardRequired(2, 1))) == "ad reward required: novel_no=2 episode_no=1"


def test_http_defensive_paths(monkeypatch, capsys):
    monkeypatch.setattr("src.api.http.time.sleep", lambda _seconds: None)
    assert _build_request_headers(Session(), "https://x/v1/member/login", {"h": "1"}) == {"h": "1"}

    class BadHeaderSession(requests.Session):
        headers = {}

        def __init__(self):
            pass

        def request(
            self,
            method: str | bytes,
            url: str | bytes,
            *args: Any,
            **kwargs: Any,
        ) -> requests.Response:
            return Response()

        def __getattribute__(self, name: str) -> Any:
            if name == "cookies":
                raise RuntimeError("bad cookies")
            return super().__getattribute__(name)

    assert _build_request_headers(BadHeaderSession(), "https://x/other", {"h": "1"}) == {"h": "1"}
    _log_request_preview("GET", Session(), {"h": "1"}, {"a": 1}, {"password": "secret"})
    _log_failed_response(Response(500, ValueError("bad json"), text="plain"))

    with pytest.raises(KnownApiBlockError):
        _handle_server_error(Response(500, {"x": 1}), 1, 3, lambda _body: AdRewardRequired(2, 1))
    with pytest.raises(requests.HTTPError, match="Server error"):
        _handle_server_error(Response(500, ValueError("bad json")), 3, 3, None)
    assert _handle_server_error(Response(500, {"message": "wait"}), 1, 2, None) is None

    out = capsys.readouterr().out
    assert "retrying" in out


def test_auth_recovery_edges(monkeypatch):
    assert _recovery_should_trigger(Response(200, ValueError("bad")), True, True, False, debug=True) is False

    ok, token, did_refresh, did_login = _run_refresh_then_login(
        lambda: (_ for _ in ()).throw(RuntimeError("stale")), lambda: "login", False, False, debug=True
    )
    assert (ok, token, did_refresh, did_login) == (True, "login", False, True)

    assert (
        _try_auth_recovery(
            Session(), "u", "GET", {}, None, None, None, 1, Response(200, {}), False, None, None, False, False
        )
        is None
    )

    s = Session([Response(200, {"ok": True})])
    recovered = _try_auth_recovery(
        s,
        "https://api-global.novelpia.com/x",
        "GET",
        {},
        {"p": 1},
        None,
        None,
        9,
        Response(401, {"errmsg": "token expire"}),
        True,
        lambda: "fresh",
        None,
        False,
        False,
    )
    assert recovered is not None
    assert recovered[0].status_code == 200
    assert s.calls[0][2]["login-at"] == "fresh"


def test_request_with_retries_request_exception_then_exhaust(monkeypatch):
    monkeypatch.setattr("src.api.http.time.sleep", lambda _seconds: None)
    s = Session([requests.Timeout("t"), Response(200, {"ok": True})])
    assert request_with_retries(s, "GET", "u").status_code == 200

    with pytest.raises(requests.Timeout):
        request_with_retries(Session([requests.Timeout("t")]), "GET", "u", max_retries=1, debug=True)


def test_client_edges(monkeypatch):
    client = NovelpiaClient(throttle=0, userkey="u", tkey="t")
    assert client.tokens == Tokens(userkey="u", tkey="t")

    client.__dict__["s"] = Session([Response(200, {"result": {"LOGINAT": "login"}})])
    assert client.login() == "login"

    client.__dict__["s"] = Session([Response(200, {"result": {"token": "reward"}})])
    assert client.ad_reward_token(AdRewardRequired(1, 2)) == "reward"

    with pytest.raises(ApiShapeError):
        client.__dict__["s"] = Session([Response(200, {"result": {}})])
        client.ad_reward_token(AdRewardRequired(1, 2))

    assert client.fetch_episode({"epi_num": 1}, 1).get("error") == "missing episode_no"
    monkeypatch.setattr(client, "episode_ticket", lambda _epi_no: {"result": {}})
    assert client.fetch_episode({"episode_no": 1, "epi_title": "T"}, 1).get("error") == "no token found"

    monkeypatch.setattr(client, "episode_ticket", lambda _epi_no: (_ for _ in ()).throw(RuntimeError("boom?_t=secret")))
    assert "_t=<redacted>" in client.fetch_episode({"episode_no": 1, "epi_title": "T"}, 1).get("error", "")
