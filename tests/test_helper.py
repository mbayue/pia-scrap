import sys
import unittest
from http.cookiejar import Cookie, CookieJar, MozillaCookieJar
from pathlib import Path

import pytest

from src.const import config_path_for_runtime
from src.helper import (
    attach_auth_cookies,
    cookie_auth_from_jar,
    extract_genre_names,
    extract_t_token,
    get_cookie_value,
    is_placeholder_userkey,
    j,
    kebab,
    load_config,
    load_netscape_cookies_text,
    looks_like_jwt,
    mask_kv,
    media_type_from_ext,
    merge_login_at,
    normalize_auth_config,
    normalize_description,
    normalize_url,
    sanitize_filename,
    save_config,
)


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


def test_normalize_description_converts_br_to_newlines():
    assert normalize_description("line1<br>line2") == "line1\nline2"


def test_normalize_description_strips_html_tags():
    assert normalize_description("<b>bold</b> text") == "bold text"


def test_normalize_description_unescapes_entities():
    assert normalize_description("a &amp; b") == "a & b"


def test_kebab_normalizes_title():
    assert kebab("Hello World!") == "hello-world"


def test_kebab_handles_empty_string():
    assert kebab("") == "book"


def test_sanitize_filename_removes_invalid_chars():
    assert sanitize_filename('file/with:bad"chars') == "file_with_bad_chars"


def test_normalize_url_prepends_https_to_protocol_relative():
    assert normalize_url("//cdn.example.com/img.jpg") == "https://cdn.example.com/img.jpg"


def test_normalize_url_prepends_base_to_relative():
    assert normalize_url("/novel/123") == "https://global.novelpia.com/novel/123"


def test_is_placeholder_userkey_detects_placeholder():
    assert is_placeholder_userkey("login-user") is True
    assert is_placeholder_userkey("real-user") is False
    assert is_placeholder_userkey(None) is False


def test_extract_genre_names_from_novel_response():
    novel = {
        "result": {
            "novel": {},
            "tag_list": [{"tag_name": "Fantasy"}, {"tag_name": "Romance"}],
        }
    }
    assert extract_genre_names(novel) == ["Fantasy", "Romance"]


def test_extract_genre_names_deduplicates():
    novel = {
        "result": {
            "novel": {"tag_list": ["Action", "Action"]},
        }
    }
    assert extract_genre_names(novel) == ["Action"]


def test_extract_genre_names_handles_string_tags():
    novel = {"result": {"novel": {"tag_list": ["A", "B"]}}}
    assert extract_genre_names(novel) == ["A", "B"]


def test_media_type_from_ext_matrix():
    assert media_type_from_ext(".jpg") == "image/jpeg"
    assert media_type_from_ext(".jpeg") == "image/jpeg"
    assert media_type_from_ext(".png") == "image/png"
    assert media_type_from_ext(".gif") == "image/gif"
    assert media_type_from_ext(".webp") == "image/webp"
    assert media_type_from_ext(".bin") == "image/jpeg"


def test_looks_like_jwt_invalid_branches():
    assert looks_like_jwt(None) is False
    assert looks_like_jwt("not.jwt") is False
    assert looks_like_jwt("😀.😀.😀") is False


def test_auth_config_and_login_header_helpers():
    assert normalize_auth_config({"login_at": " a ", "userkey": None, "tkey": 1}) == {
        "login_at": "a",
        "userkey": "",
        "tkey": "",
    }
    assert merge_login_at({"x": "1"}, "token") == {"x": "1", "login-at": "token"}
    assert merge_login_at({"x": "1"}, None) == {"x": "1"}


def test_load_config_normalizes_non_object_root(monkeypatch, tmp_path):
    config_path = tmp_path / ".api.json"
    config_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("src.helper.CONFIG_PATH", str(config_path))

    assert load_config() == {"login_at": "", "userkey": "", "tkey": ""}


def test_save_config_cleans_temp_file_on_oserror(monkeypatch, tmp_path):
    config_path = tmp_path / ".api.json"
    monkeypatch.setattr("src.helper.CONFIG_PATH", str(config_path))
    monkeypatch.setattr("src.helper.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("boom")))

    save_config({"login_at": "token"})

    assert list(tmp_path.iterdir()) == []


def test_netscape_cookie_load_text_round_trip():
    text = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            ".novelpia.com\tTRUE\t/\tFALSE\t0\tUSERKEY\tuser",
            ".novelpia.com\tTRUE\t/\tFALSE\t0\tTKEY\ttoken",
            "",
        ]
    )

    jar = load_netscape_cookies_text(text)

    auth = cookie_auth_from_jar(jar, login_at_fallback="login")
    assert get_cookie_value(jar, "userkey") == "user"
    assert auth.login_at == "login"
    assert auth.userkey == "user"
    assert auth.tkey == "token"


def test_cookie_helpers_handle_missing_or_bad_cookiejar():
    class NoCookies:
        pass

    class BadCookies(CookieJar):
        def __iter__(self):
            raise TypeError("bad")

    assert attach_auth_cookies(NoCookies(), {"x": "1"}) == {"x": "1"}
    assert attach_auth_cookies(NoCookies(), None) is None
    assert get_cookie_value(BadCookies(), "USERKEY") is None


def test_extract_t_token_ignores_non_content_urls():
    assert extract_t_token({"result": {"url": "https://example.com/?_t=wrong"}}) is None


def test_remaining_helper_edges(monkeypatch, tmp_path):
    assert normalize_url("") == ""
    assert normalize_url("https://x/img.jpg") == "https://x/img.jpg"
    assert media_type_from_ext(".JPG") == "image/jpeg"

    long_jwt = "a" * 8 + "." + "b" * 8 + "." + "c" * 8
    masked = mask_kv({"safe": long_jwt, "long": "x" * 65})
    assert masked == {"safe": "aaaaaa...cccccc", "long": "x" * 32 + "…(trunc)"}

    assert j({1, 2}) == "{1, 2}"

    config_path = tmp_path / ".api.json"
    monkeypatch.setattr("src.helper.CONFIG_PATH", str(config_path))
    monkeypatch.setattr("src.helper.os.chmod", lambda *_args: (_ for _ in ()).throw(OSError("chmod")))
    save_config({"login_at": "token"})
    assert load_config().get("login_at") == "token"

    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text(
        "\n".join(["# Netscape HTTP Cookie File", ".novelpia.com\tTRUE\t/\tFALSE\t0\tUSERKEY\tuser", ""]),
        encoding="utf-8",
    )
    from src.helper import load_netscape_cookies

    assert get_cookie_value(load_netscape_cookies(str(cookie_path)), "USERKEY") == "user"

    class UnlinkBoom:
        def __enter__(self):
            self.name = str(tmp_path / "missing-cookie-file")
            return self

        def __exit__(self, *_args):
            return False

        def write(self, _text):
            pass

    monkeypatch.setattr("src.helper.tempfile.NamedTemporaryFile", lambda *_args, **_kwargs: UnlinkBoom())
    monkeypatch.setattr("src.helper.os.unlink", lambda *_args: (_ for _ in ()).throw(OSError("unlink")))
    with pytest.raises(FileNotFoundError):
        load_netscape_cookies_text("bad")

    assert list(__import__("src.helper", fromlist=["iter_strings"]).iter_strings({"a": ["x", {"b": "y"}]})) == [
        "x",
        "y",
    ]
    assert (
        extract_t_token(
            {"result": {"deep": {"url": "https://api-global.novelpia.com/v1/novel/episode/content?_t=bad.bad.bad"}}}
        )
        == "bad.bad.bad"
    )
