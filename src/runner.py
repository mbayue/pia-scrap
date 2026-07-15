import argparse
import base64
import math
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast
from pathlib import Path

from dotenv import load_dotenv

from src.api import NovelpiaClient
from src.builder import build_epub, build_txt
from src.contracts import QueueResult, QueueSummaryRow
from src.helper import (
    cookie_auth_from_jar,
    is_placeholder_userkey,
    load_config,
    load_netscape_cookies,
    load_netscape_cookies_text,
    save_config,
)
from src.logutil import get_logger

logger = get_logger(__name__)

LogFn = Callable[[str], None]


def _load_runtime_dotenv() -> None:
    if getattr(sys, "frozen", False):
        load_dotenv(Path(sys.executable).resolve().with_name(".env"))
        return
    load_dotenv()


@dataclass
class OutputOptions:
    """Output configuration: where and how to write results."""

    out: str = "output"
    lang: str = "en"
    txt: bool = False
    chapter_images: bool = False


@dataclass
class FetchOptions:
    """Download behavior: chapter range, concurrency, throttling."""

    start_chapter: int | None = None
    end_chapter: int | None = None
    max_chapters: int = 0
    throttle: float = 1.25
    workers: int = 1
    proxy: str | None = None
    debug: bool = False
    update: bool = False
    retry_failed: bool = False


@dataclass
class AuthOptions:
    """Authentication: credentials or cookies."""

    email: str | None = None
    password: str | None = None
    cookie_file: str | None = None
    cookie_text: str | None = None


@dataclass
class QueueOptions(OutputOptions, FetchOptions, AuthOptions):
    """Composed options for backward compatibility. Combines output, fetch, and auth."""

    pass


@dataclass(frozen=True, slots=True)
class QueueRequest:
    novel_ids: list[int]
    options: QueueOptions
    show_summary: bool


class CliUsageError(ValueError):
    pass


def validate_queue_options(options: QueueOptions) -> None:
    if options.workers < 1:
        raise CliUsageError("-w/--workers must be at least 1")
    if not math.isfinite(options.throttle):
        raise CliUsageError("-t/--throttle must be a finite number")
    if options.throttle < 0:
        raise CliUsageError("-t/--throttle must be 0 or greater")
    if options.max_chapters < 0:
        raise CliUsageError("-max must be 0 or greater")
    if options.start_chapter is not None and options.start_chapter < 1:
        raise CliUsageError("-start must be at least 1")
    if options.end_chapter is not None and options.end_chapter < 1:
        raise CliUsageError("-end must be at least 1")
    if (
        options.start_chapter is not None
        and options.end_chapter is not None
        and options.start_chapter > options.end_chapter
    ):
        raise CliUsageError("-start must be less than or equal to -end")


def _parse_novel_token(item: str) -> int:
    match = re.search(r"(?:^|/)novel/(\d+)(?:\D|$)", item)
    if match:
        return int(match.group(1))
    return int(item)


def parse_queue_lines(lines: Iterable[str], source: str = "queue") -> list[int]:
    novel_ids: list[int] = []
    for line_no, line in enumerate(lines, 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for item in line.replace(",", " ").split():
            try:
                novel_ids.append(_parse_novel_token(item))
            except ValueError:
                raise ValueError(f"{source}:{line_no}: invalid novel_id or novel URL '{item}'") from None
    return novel_ids


def load_queue_file(path: str) -> list[int]:
    with open(path, encoding="utf-8") as f:
        return parse_queue_lines(f, source=path)


def build_queue_request(args: argparse.Namespace) -> QueueRequest:
    novel_ids = list(args.novel_ids)
    for queue_path in args.queue:
        try:
            novel_ids.extend(load_queue_file(queue_path))
        except OSError as e:
            raise CliUsageError(f"Unable to read queue file '{queue_path}': {e}") from e
        except ValueError as e:
            raise CliUsageError(str(e)) from e

    if not novel_ids:
        raise CliUsageError("provide at least one novel_id or -q FILE")

    options = QueueOptions(
        out=args.out,
        start_chapter=args.start_chapter,
        end_chapter=args.end_chapter,
        max_chapters=args.max_chapters,
        lang=args.lang,
        proxy=args.proxy,
        debug=args.debug,
        throttle=args.throttle,
        workers=args.workers,
        update=args.update,
        retry_failed=args.retry_failed,
        txt=args.txt,
        chapter_images=getattr(args, "chapter_images", False),
        email=args.email,
        password=args.password,
        cookie_file=getattr(args, "cookie_file", None),
        cookie_text=getattr(args, "cookie_text", None),
    )
    validate_queue_options(options)

    return QueueRequest(
        novel_ids=novel_ids,
        options=options,
        show_summary=len(novel_ids) > 1 or bool(args.queue),
    )


def dedupe_novel_ids(novel_ids: Iterable[int]) -> tuple[list[int], list[int]]:
    unique_ids: list[int] = []
    skipped_ids: list[int] = []
    seen = set()
    for novel_id in novel_ids:
        if novel_id in seen:
            skipped_ids.append(novel_id)
            continue
        seen.add(novel_id)
        unique_ids.append(novel_id)
    return unique_ids, skipped_ids


def _cookie_text_from_env() -> str | None:
    raw_text = os.getenv("NOVELPIA_COOKIE_TEXT")
    if raw_text:
        return raw_text.replace("\\n", "\n")
    encoded_text = os.getenv("NOVELPIA_COOKIE_TEXT_B64")
    if encoded_text:
        try:
            return base64.b64decode(encoded_text).decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"Invalid NOVELPIA_COOKIE_TEXT_B64: {e}") from e
    return None


def _auth_from_cookies(
    cookie_text: str | None,
    cookie_file: str | None,
    cfg_login_at: str | None,
    options: FetchOptions,
) -> NovelpiaClient:
    """Create client from Netscape cookie text or file."""
    client = NovelpiaClient(
        email=None,
        password=None,
        proxy=options.proxy,
        throttle=options.throttle,
        debug=options.debug,
    )
    if cookie_text:
        jar = load_netscape_cookies_text(cookie_text)
    elif cookie_file:
        jar = load_netscape_cookies(os.path.expanduser(cookie_file))
    else:
        raise RuntimeError("cookie_text or cookie_file required")
    auth = cookie_auth_from_jar(jar, os.getenv("NOVELPIA_LOGIN_AT") or cfg_login_at)
    client.tokens.userkey = auth.userkey
    client.tokens.tkey = auth.tkey
    client.tokens.login_at = auth.login_at
    if not auth.userkey or is_placeholder_userkey(auth.userkey):
        raise RuntimeError(
            "Netscape cookie file did not contain USERKEY. Export cookies for novelpia.com and try again."
        )
    client.s.cookies.update(jar)
    save_config(
        {
            "login_at": auth.login_at or "",
            "userkey": auth.userkey,
            "tkey": auth.tkey or "",
        }
    )
    return client


def _auth_from_credentials(
    email: str,
    password: str,
    cfg_userkey: str | None,
    cfg_tkey: str | None,
    options: FetchOptions,
) -> NovelpiaClient:
    """Create client from email/password login."""
    client = NovelpiaClient(
        email=email,
        password=password,
        proxy=options.proxy,
        throttle=options.throttle,
        userkey=cfg_userkey,
        tkey=cfg_tkey,
        debug=options.debug,
    )
    client.login()
    auth = cookie_auth_from_jar(client.s.cookies)
    login_userkey = None if is_placeholder_userkey(auth.userkey) else auth.userkey
    save_config(
        {
            "login_at": client.tokens.login_at,
            "userkey": login_userkey or client.tokens.userkey or cfg_userkey,
            "tkey": auth.tkey or client.tokens.tkey or cfg_tkey,
        }
    )
    return client


def _auth_from_stored_tokens(
    cfg_login_at: str,
    cfg_userkey: str,
    cfg_tkey: str | None,
    options: FetchOptions,
) -> NovelpiaClient:
    """Create client from stored tokens in .api.json."""
    client = NovelpiaClient(
        email=None,
        password=None,
        proxy=options.proxy,
        throttle=options.throttle,
        userkey=cfg_userkey,
        tkey=cfg_tkey,
        debug=options.debug,
    )
    client.tokens.login_at = cfg_login_at
    return client


def create_client(options: QueueOptions) -> NovelpiaClient:
    """Create an authenticated NovelpiaClient from options (cookie, credentials, or stored tokens)."""
    _load_runtime_dotenv()

    cfg = load_config()
    cfg_login_at = cfg.get("login_at") or None
    cfg_userkey = cfg.get("userkey") or None
    if is_placeholder_userkey(cfg_userkey):
        cfg_userkey = None
    cfg_tkey = cfg.get("tkey") or None

    email = options.email or os.getenv("NOVELPIA_EMAIL")
    password = options.password or os.getenv("NOVELPIA_PASSWORD")
    cookie_file = options.cookie_file or os.getenv("NOVELPIA_COOKIE_FILE")
    cookie_text = options.cookie_text or _cookie_text_from_env()

    if cookie_text or cookie_file:
        return _auth_from_cookies(cookie_text, cookie_file, cfg_login_at, options)
    if email and password:
        return _auth_from_credentials(email, password, cfg_userkey, cfg_tkey, options)
    if cfg_login_at and cfg_userkey:
        return _auth_from_stored_tokens(cfg_login_at, cfg_userkey, cfg_tkey, options)

    raise RuntimeError("No credentials or stored tokens found. Provide email/password to login once.")


def run_queue(novel_ids: Iterable[int], options: QueueOptions, log: LogFn = print) -> QueueResult:
    """Download and build all novels in the queue. Returns a QueueResult with summary rows and failures."""
    novel_ids, skipped_ids = dedupe_novel_ids(novel_ids)
    if not novel_ids:
        raise ValueError("provide at least one novel_id")
    if skipped_ids:
        log("[info] Skipping duplicate novel IDs: " + ", ".join(str(novel_id) for novel_id in skipped_ids))

    client = create_client(options)
    summary_rows: list[QueueSummaryRow] = []
    failures: list[tuple[int, str]] = []
    total = len(novel_ids)
    max_chapters = options.max_chapters if options.max_chapters and options.max_chapters > 0 else None

    try:
        for idx, novel_id in enumerate(novel_ids, 1):
            if total > 1:
                log(f"[queue] ({idx}/{total}) Building novel {novel_id}")

            try:
                builder_fn = build_txt if options.txt else build_epub
                fmt_label = "TXT" if options.txt else "EPUB"
                build_kwargs = {
                    "start_chapter": options.start_chapter,
                    "end_chapter": options.end_chapter,
                    "max_chapters": max_chapters,
                    "language": options.lang,
                    "debug_dump": options.debug,
                    "update": options.update,
                    "retry_failed": options.retry_failed,
                    "max_workers": options.workers,
                }
                if not options.txt:
                    build_kwargs["chapter_images"] = options.chapter_images
                out_result, title, count = builder_fn(
                    client,
                    novel_id,
                    options.out,
                    **cast(Any, build_kwargs),
                )
                if out_result is None:
                    reason = "No failed chapters to retry" if options.retry_failed else "No updates found"
                    log(f"[info] {reason} for '{title}'. Existing {fmt_label} left unchanged.")
                    summary_rows.append(
                        {
                            "novel_id": novel_id,
                            "status": "no updates",
                            "chapters": 0,
                            "title": title,
                            "path": None,
                        }
                    )
                else:
                    log(f"[success] Wrote {fmt_label}: {out_result}")
                    summary_rows.append(
                        {
                            "novel_id": novel_id,
                            "status": fmt_label.lower(),
                            "chapters": count,
                            "title": title,
                            "path": out_result,
                        }
                    )
            except Exception as e:
                failures.append((novel_id, str(e)))
                summary_rows.append(
                    {
                        "novel_id": novel_id,
                        "status": "failed",
                        "chapters": None,
                        "title": str(e),
                        "path": None,
                    }
                )
                log(f"[error] Failed to build novel {novel_id}: {e}")
    finally:
        client.close()

    return {"rows": summary_rows, "failures": failures, "skipped_ids": skipped_ids}


def print_queue_summary(rows: list[QueueSummaryRow]) -> None:
    if not rows:
        return

    logger.info("\n[summary]")
    logger.info(f"{'novel_id':<10} {'status':<12} {'chapters':<8} title")
    for row in rows:
        chapters = "" if row.get("chapters") is None else str(row.get("chapters"))
        logger.info(f"{row['novel_id']:<10} {row['status']:<12} {chapters:<8} {row['title']}")
