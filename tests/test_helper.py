import sys
import unittest
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path

from src.const import config_path_for_runtime
from src.helper import attach_auth_cookies, extract_t_token, j, load_config, mask_kv, save_config


@unittest.skipIf(sys.platform != "win32", "Windows frozen executable path semantics")
def test_config_path_for_frozen_exe_lives_next_to_binary():
    exe_path = Path("C:/app/dist/pia-scrap.exe")

    assert config_path_for_runtime(executable=exe_path, frozen=True) == Path("C:/app/dist/.api.json")


def test_mask_kv_masks_nested_tokens():
    data = {
        "result": {"LOGINAT": "secret-login", "nested": {"_t": "secret-token"}},
        "password": "secret-password",
        "safe": "value",
    }

    assert mask_kv(data) == {
        "result": {"LOGINAT": "***", "nested": {"_t": "***"}},
        "password": "***",
        "safe": "value",
    }


def test_j_serializes_unicode_json():
    assert j({"message": "안녕"}) == '{"message": "안녕"}'


def test_save_config_writes_json_atomically(monkeypatch, tmp_path):
    config_path = tmp_path / ".api.json"
    monkeypatch.setattr("src.helper.CONFIG_PATH", str(config_path))

    save_config({"login_at": "token", "userkey": "user", "tkey": "t"})

    assert load_config() == {"login_at": "token", "userkey": "user", "tkey": "t"}
    assert list(tmp_path.iterdir()) == [config_path]

def test_load_config_returns_empty_on_malformed_json(monkeypatch, tmp_path):
    config_path = tmp_path / ".api.json"
    config_path.write_text("{bad", encoding="utf-8")
    monkeypatch.setattr("src.helper.CONFIG_PATH", str(config_path))

    assert load_config() == {}


def test_load_config_returns_empty_on_non_utf8(monkeypatch, tmp_path):
    config_path = tmp_path / ".api.json"
    config_path.write_bytes(b"\xff")
    monkeypatch.setattr("src.helper.CONFIG_PATH", str(config_path))

    assert load_config() == {}


def test_attach_auth_cookies_preserves_existing_cookie_header():
    class Session:
        def __init__(self):
            self.cookies = MozillaCookieJar()
            self.cookies.set_cookie(_cookie("USERKEY", "user"))
            self.cookies.set_cookie(_cookie("TKEY", "token"))

    assert attach_auth_cookies(Session(), {"Cookie": "existing=1"}) == {"Cookie": "existing=1"}

def test_attach_auth_cookies_adds_userkey_tkey_and_last_login():
    class Session:
        def __init__(self):
            self.cookies = MozillaCookieJar()
            self.cookies.set_cookie(_cookie("USERKEY", "user"))
            self.cookies.set_cookie(_cookie("TKEY", "token"))

    assert attach_auth_cookies(Session(), {}) == {"Cookie": "USERKEY=user; TKEY=token; last_login=basic"}

def test_mask_kv_masks_nested_lowercase_loginat_and_t():
    data = {"outer": [{"loginat": "secret"}, {"url": "https://x?_t=still-long-token-value"}]}

    assert mask_kv(data) == {"outer": [{"loginat": "***"}, {"url": "https://x?_t=still-long-token-value"}]}

def test_extract_t_token_prefers_jwt_at_top_level_result():
    jwt = "a" * 20 + "." + "b" * 20 + "." + "c" * 20
    tdata = {"result": {"_t": jwt, "other": "value"}}

    assert extract_t_token(tdata) == jwt


def test_extract_t_token_finds_jwt_in_nested_result_dict():
    jwt = "a" * 20 + "." + "b" * 20 + "." + "c" * 20
    tdata = {"result": {"nested": {"token": jwt}}}

    assert extract_t_token(tdata) == jwt


def test_extract_t_token_falls_back_to_non_jwt_string_when_no_jwt_found():
    tdata = {"result": {"_t": "plain-non-jwt-token"}}

    assert extract_t_token(tdata) == "plain-non-jwt-token"


def test_extract_t_token_finds_token_embedded_in_content_endpoint_url():
    url = "https://api-global.novelpia.com/v1/novel/episode/content?_t=url-embedded-token"
    tdata = {"result": {"deep": {"link": url}}}

    assert extract_t_token(tdata) == "url-embedded-token"


def test_extract_t_token_returns_none_when_nothing_found():
    tdata = {"result": {"unrelated": "value"}}

    assert extract_t_token(tdata) is None


def _cookie(name: str, value: str) -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=".novelpia.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )
