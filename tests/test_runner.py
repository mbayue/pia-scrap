from argparse import Namespace
from dataclasses import dataclass

import requests
from requests.cookies import RequestsCookieJar

import main
from src.runner import (
    CliUsageError,
    QueueOptions,
    build_queue_request,
    create_client,
    dedupe_novel_ids,
    parse_queue_lines,
    run_queue,
)


def test_parse_queue_lines_accepts_ids_urls_commas_and_comments():
    lines = [
        "49, https://global.novelpia.com/novel/5522?sid=main1 # keep these",
        "",
        "468",
    ]

    assert parse_queue_lines(lines, source="queue") == [49, 5522, 468]


def test_parse_queue_lines_reports_source_and_line():
    try:
        parse_queue_lines(["49", "bad"], source="web")
    except ValueError as exc:
        assert str(exc) == "web:2: invalid novel_id or novel URL 'bad'"
    else:
        raise AssertionError("expected ValueError")


def test_dedupe_novel_ids_preserves_order_and_reports_skips():
    unique_ids, skipped_ids = dedupe_novel_ids([49, 5522, 49, 468, 5522])

    assert unique_ids == [49, 5522, 468]
    assert skipped_ids == [49, 5522]


def test_run_queue_closes_client(monkeypatch):
    closed = []

    class DummyClient:
        def close(self):
            closed.append(True)

    monkeypatch.setattr("src.runner.create_client", lambda _options: DummyClient())
    monkeypatch.setattr("src.runner.build_epub", lambda *_args, **_kwargs: ("book.epub", "Book", 1))

    result = run_queue([49], QueueOptions())

    assert result["failures"] == []
    assert closed == [True]


def test_run_queue_closes_client_after_build_failure(monkeypatch):
    closed = []

    class DummyClient:
        def close(self):
            closed.append(True)

    def fail_build(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("src.runner.create_client", lambda _options: DummyClient())
    monkeypatch.setattr("src.runner.build_epub", fail_build)

    result = run_queue([49], QueueOptions())

    assert result["failures"] == [(49, "boom")]
    assert closed == [True]

def test_main_merges_cli_and_queue_options(monkeypatch, tmp_path):
    queue_path = tmp_path / "queue.txt"
    queue_path.write_text("5522\n49\n", encoding="utf-8")
    captured = {}
    secret = "p" + "w"

    def fake_run_queue(novel_ids, options):
        captured["novel_ids"] = list(novel_ids)
        captured["options"] = options
        return {"rows": [], "failures": [], "skipped_ids": []}

    monkeypatch.setattr("sys.argv", [
        "main.py",
        "468",
        "-q",
        str(queue_path),
        "-out",
        "books",
        "-max",
        "5",
        "-start",
        "2",
        "-end",
        "6",
        "-lang",
        "ko",
        "-proxy",
        "http://proxy",
        "-v",
        "-t",
        "0.5",
        "-w",
        "3",
        "-up",
        "-r",
        "-txt",
        "-u",
        "u@example.com",
        "-p",
        secret,
    ])
    monkeypatch.setattr("main.run_queue", fake_run_queue)

    main.main()

    options = captured["options"]
    assert captured["novel_ids"] == [468, 5522, 49]
    assert options == QueueOptions(
        out="books",
        start_chapter=2,
        end_chapter=6,
        max_chapters=5,
        lang="ko",
        proxy="http://proxy",
        debug=True,
        throttle=0.5,
        workers=3,
        update=True,
        retry_failed=True,
        txt=True,
        email="u@example.com",
        password=secret,
    )

def test_build_queue_request_reports_missing_queue_file(tmp_path):
    missing = tmp_path / "missing.txt"

    try:
        build_queue_request(_cli_args(queue=[str(missing)]))
    except CliUsageError as exc:
        assert f"Unable to read queue file '{missing}'" in str(exc)
    else:
        raise AssertionError("expected CliUsageError")

def test_build_queue_request_reports_queue_line_source(tmp_path):
    queue_path = tmp_path / "queue.txt"
    queue_path.write_text("49\nbad\n", encoding="utf-8")

    try:
        build_queue_request(_cli_args(queue=[str(queue_path)]))
    except CliUsageError as exc:
        assert str(exc) == f"{queue_path}:2: invalid novel_id or novel URL 'bad'"
    else:
        raise AssertionError("expected CliUsageError")

def test_build_queue_request_sets_summary_for_queue_file(tmp_path):
    queue_path = tmp_path / "queue.txt"
    queue_path.write_text("49\n", encoding="utf-8")

    request = build_queue_request(_cli_args(queue=[str(queue_path)]))

    assert request.novel_ids == [49]
    assert request.show_summary is True

def _cli_args(**overrides):
    values = {
        "novel_ids": [],
        "queue": [],
        "out": "output",
        "start_chapter": None,
        "end_chapter": None,
        "max_chapters": 0,
        "lang": "en",
        "proxy": None,
        "debug": False,
        "throttle": 1.25,
        "workers": 1,
        "update": False,
        "retry_failed": False,
        "txt": False,
        "email": None,
        "password": None,
    }
    values.update(overrides)
    return Namespace(**values)

@dataclass
class AuthTokens:
    login_at: str | None = None
    userkey: str | None = None
    tkey: str | None = None

class AuthClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tokens = AuthTokens()
        self.tokens.userkey = kwargs.get("userkey") or "generated-user"
        self.s = requests.Session()
        self.login_calls = 0

    def login(self):
        self.login_calls += 1
        self.tokens.login_at = "login-token"
        self.s.cookies.set("USERKEY", "login-user")
        self.s.cookies.set("TKEY", "login-t")

def as_auth_client(client: object) -> AuthClient:
    assert isinstance(client, AuthClient)
    return client

def test_create_client_prefers_cookie_text_over_email_and_config(monkeypatch):
    saved = []
    jar = RequestsCookieJar()
    jar.set("USERKEY", "cookie-user")
    jar.set("TKEY", "cookie-t")
    jar.set("LOGINAT", "cookie-login")

    monkeypatch.setenv("NOVELPIA_EMAIL", "env@example.com")
    monkeypatch.setenv("NOVELPIA_PASSWORD", "env-pw")
    monkeypatch.setattr("src.runner.load_dotenv", lambda: None)
    monkeypatch.setattr("src.runner.load_config", lambda: {
        "login_at": "cfg-login",
        "userkey": "cfg-user",
        "tkey": "cfg-t",
    })
    monkeypatch.setattr("src.runner.load_netscape_cookies_text", lambda _text: jar)
    monkeypatch.setattr("src.runner.save_config", lambda cfg: saved.append(cfg))
    monkeypatch.setattr("src.runner.NovelpiaClient", AuthClient)

    secret = "cli-" + "pw"
    client = as_auth_client(
        create_client(QueueOptions(email="cli@example.com", password=secret, cookie_text="cookies"))
    )

    assert client.login_calls == 0
    assert client.tokens.login_at == "cookie-login"
    assert client.tokens.userkey == "cookie-user"
    assert client.tokens.tkey == "cookie-t"
    assert saved == [{"login_at": "cookie-login", "userkey": "cookie-user", "tkey": "cookie-t"}]

def test_create_client_prefers_email_password_over_stored_tokens(monkeypatch):
    saved = []
    monkeypatch.setattr("src.runner.load_dotenv", lambda: None)
    monkeypatch.delenv("NOVELPIA_COOKIE_FILE", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_TEXT", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_TEXT_B64", raising=False)
    monkeypatch.setattr("src.runner.load_config", lambda: {
        "login_at": "cfg-login",
        "userkey": "cfg-user",
        "tkey": "cfg-t",
    })
    monkeypatch.setattr("src.runner.save_config", lambda cfg: saved.append(cfg))
    monkeypatch.setattr("src.runner.NovelpiaClient", AuthClient)

    secret = "cli-" + "pw"
    client = as_auth_client(create_client(QueueOptions(email="cli@example.com", password=secret)))

    kwargs = client.kwargs
    assert kwargs["email"] == "cli@example.com"
    assert kwargs["password"] == secret
    assert kwargs["userkey"] == "cfg-user"
    assert client.login_calls == 1
    assert saved == [{"login_at": "login-token", "userkey": "cfg-user", "tkey": "login-t"}]

def test_create_client_keeps_generated_userkey_when_login_cookie_is_placeholder(monkeypatch):
    saved = []
    monkeypatch.setattr("src.runner.load_dotenv", lambda: None)
    monkeypatch.delenv("NOVELPIA_COOKIE_FILE", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_TEXT", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_TEXT_B64", raising=False)
    monkeypatch.setattr("src.runner.load_config", lambda: {})
    monkeypatch.setattr("src.runner.save_config", lambda cfg: saved.append(cfg))
    monkeypatch.setattr("src.runner.NovelpiaClient", AuthClient)

    secret = "cli-" + "pw"
    client = as_auth_client(create_client(QueueOptions(email="cli@example.com", password=secret)))

    assert client.login_calls == 1
    assert saved == [{"login_at": "login-token", "userkey": "generated-user", "tkey": "login-t"}]

def test_create_client_ignores_stored_placeholder_userkey(monkeypatch):
    saved = []
    monkeypatch.setattr("src.runner.load_dotenv", lambda: None)
    monkeypatch.delenv("NOVELPIA_COOKIE_FILE", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_TEXT", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_TEXT_B64", raising=False)
    monkeypatch.setattr("src.runner.load_config", lambda: {"userkey": "login-user", "tkey": "stored-t"})
    monkeypatch.setattr("src.runner.save_config", lambda cfg: saved.append(cfg))
    monkeypatch.setattr("src.runner.NovelpiaClient", AuthClient)

    secret = "cli-" + "pw"
    client = as_auth_client(create_client(QueueOptions(email="cli@example.com", password=secret)))

    assert client.kwargs["userkey"] is None
    assert saved == [{"login_at": "login-token", "userkey": "generated-user", "tkey": "login-t"}]

def test_create_client_uses_stored_tokens_when_no_inputs(monkeypatch):
    monkeypatch.setattr("src.runner.load_dotenv", lambda: None)
    monkeypatch.delenv("NOVELPIA_EMAIL", raising=False)
    monkeypatch.delenv("NOVELPIA_PASSWORD", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_FILE", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_TEXT", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_TEXT_B64", raising=False)
    monkeypatch.setattr("src.runner.load_config", lambda: {
        "login_at": "cfg-login",
        "userkey": "cfg-user",
        "tkey": "cfg-t",
    })
    monkeypatch.setattr("src.runner.NovelpiaClient", AuthClient)

    client = as_auth_client(create_client(QueueOptions()))

    kwargs = client.kwargs
    assert kwargs["email"] is None
    assert kwargs["userkey"] == "cfg-user"
    assert client.tokens.login_at == "cfg-login"
    assert client.login_calls == 0

def test_create_client_uses_env_cookie_text_before_cookie_file(monkeypatch):
    saved = []
    jar = RequestsCookieJar()
    jar.set("USERKEY", "text-user")

    monkeypatch.setenv("NOVELPIA_COOKIE_FILE", "cookie-file")
    monkeypatch.setenv("NOVELPIA_COOKIE_TEXT", "cookie-text")
    monkeypatch.setattr("src.runner.load_dotenv", lambda: None)
    monkeypatch.setattr("src.runner.load_config", lambda: {})
    monkeypatch.setattr("src.runner.load_netscape_cookies_text", lambda text: jar if text == "cookie-text" else None)
    monkeypatch.setattr("src.runner.load_netscape_cookies", lambda _path: _fail_cookie_file_load())
    monkeypatch.setattr("src.runner.save_config", lambda cfg: saved.append(cfg))
    monkeypatch.setattr("src.runner.NovelpiaClient", AuthClient)

    client = as_auth_client(create_client(QueueOptions()))

    assert client.tokens.userkey == "text-user"
    assert saved == [{"login_at": "", "userkey": "text-user", "tkey": ""}]

def test_create_client_uses_env_login_at_before_config_for_cookies(monkeypatch):
    saved = []
    jar = RequestsCookieJar()
    jar.set("USERKEY", "cookie-user")

    monkeypatch.setenv("NOVELPIA_LOGIN_AT", "env-login")
    monkeypatch.setattr("src.runner.load_dotenv", lambda: None)
    monkeypatch.setattr("src.runner.load_config", lambda: {"login_at": "cfg-login"})
    monkeypatch.setattr("src.runner.load_netscape_cookies_text", lambda _text: jar)
    monkeypatch.setattr("src.runner.save_config", lambda cfg: saved.append(cfg))
    monkeypatch.setattr("src.runner.NovelpiaClient", AuthClient)

    client = as_auth_client(create_client(QueueOptions(cookie_text="cookies")))

    assert client.tokens.login_at == "env-login"
    assert saved == [{"login_at": "env-login", "userkey": "cookie-user", "tkey": ""}]

def test_create_client_rejects_cookie_auth_without_userkey(monkeypatch):
    monkeypatch.setattr("src.runner.load_dotenv", lambda: None)
    monkeypatch.setattr("src.runner.load_config", lambda: {})
    monkeypatch.setattr("src.runner.load_netscape_cookies_text", lambda _text: RequestsCookieJar())
    monkeypatch.setattr("src.runner.NovelpiaClient", AuthClient)

    try:
        create_client(QueueOptions(cookie_text="cookies"))
    except RuntimeError as exc:
        assert "Netscape cookie file did not contain USERKEY" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_create_client_rejects_cookie_auth_placeholder_userkey(monkeypatch):
    jar = RequestsCookieJar()
    jar.set("USERKEY", "login-user")
    monkeypatch.setattr("src.runner.load_dotenv", lambda: None)
    monkeypatch.setattr("src.runner.load_config", lambda: {})
    monkeypatch.setattr("src.runner.load_netscape_cookies_text", lambda _text: jar)
    monkeypatch.setattr("src.runner.NovelpiaClient", AuthClient)

    try:
        create_client(QueueOptions(cookie_text="cookies"))
    except RuntimeError as exc:
        assert "Netscape cookie file did not contain USERKEY" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_create_client_raises_without_credentials_or_tokens(monkeypatch):
    monkeypatch.setattr("src.runner.load_dotenv", lambda: None)
    monkeypatch.delenv("NOVELPIA_EMAIL", raising=False)
    monkeypatch.delenv("NOVELPIA_PASSWORD", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_FILE", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_TEXT", raising=False)
    monkeypatch.delenv("NOVELPIA_COOKIE_TEXT_B64", raising=False)
    monkeypatch.setattr("src.runner.load_config", lambda: {})

    try:
        create_client(QueueOptions())
    except RuntimeError as exc:
        assert "No credentials or stored tokens found" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

def _fail_cookie_file_load():
    raise AssertionError("cookie file should not load")
