import argparse
import base64
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from src import const
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
class QueueOptions:
    out: str = "output"
    start_chapter: int | None = None
    end_chapter: int | None = None
    max_chapters: int = 0
    lang: str = "en"
    proxy: str | None = None
    debug: bool = False
    throttle: float = 1.25
    workers: int = 1
    update: bool = False
    retry_failed: bool = False
    txt: bool = False
    email: str | None = None
    password: str | None = None
    cookie_file: str | None = None
    cookie_text: str | None = None


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

def create_client(options: QueueOptions) -> NovelpiaClient:
    _load_runtime_dotenv()
    const.HTTP_LOG = bool(options.debug)

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
        client = NovelpiaClient(
            email=None,
            password=None,
            proxy=options.proxy,
            throttle=options.throttle,
        )
        if cookie_text:
            jar = load_netscape_cookies_text(cookie_text)
        elif cookie_file:
            cookie_path = os.path.expanduser(cookie_file)
            jar = load_netscape_cookies(cookie_path)
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
        save_config({
            "login_at": auth.login_at or "",
            "userkey": auth.userkey,
            "tkey": auth.tkey or "",
        })
        return client

    if email and password:
        client = NovelpiaClient(
            email=email,
            password=password,
            proxy=options.proxy,
            throttle=options.throttle,
            userkey=cfg_userkey,
            tkey=cfg_tkey,
        )
        client.login()
        auth = cookie_auth_from_jar(client.s.cookies)
        login_userkey = None if is_placeholder_userkey(auth.userkey) else auth.userkey
        save_config({
            "login_at": client.tokens.login_at,
            "userkey": login_userkey or client.tokens.userkey or cfg_userkey,
            "tkey": auth.tkey or client.tokens.tkey or cfg_tkey,
        })
        return client

    if cfg_login_at and cfg_userkey:
        client = NovelpiaClient(
            email=None,
            password=None,
            proxy=options.proxy,
            throttle=options.throttle,
            userkey=cfg_userkey,
            tkey=cfg_tkey,
        )
        client.tokens.login_at = cfg_login_at
        return client

    raise RuntimeError("No credentials or stored tokens found. Provide email/password to login once.")


def run_queue(novel_ids: Iterable[int], options: QueueOptions, log: LogFn = print) -> QueueResult:
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
                if options.txt:
                    out_dir_final, title, count = build_txt(
                        client,
                        novel_id,
                        options.out,
                        start_chapter=options.start_chapter,
                        end_chapter=options.end_chapter,
                        max_chapters=max_chapters,
                        language=options.lang,
                        debug_dump=options.debug,
                        update=options.update,
                        retry_failed=options.retry_failed,
                        max_workers=options.workers,
                    )
                    if out_dir_final is None:
                        if options.retry_failed:
                            log(f"[info] No failed chapters to retry for '{title}'. Nothing to do.")
                        else:
                            log(f"[info] No updates found for '{title}'. Existing TXT output left unchanged.")
                        summary_rows.append({
                            "novel_id": novel_id,
                            "status": "no updates",
                            "chapters": 0,
                            "title": title,
                            "path": None,
                        })
                    else:
                        log(f"[success] Wrote TXT files under: {out_dir_final}")
                        summary_rows.append({
                            "novel_id": novel_id,
                            "status": "txt",
                            "chapters": count,
                            "title": title,
                            "path": out_dir_final,
                        })
                else:
                    out_file, title, count = build_epub(
                        client,
                        novel_id,
                        options.out,
                        start_chapter=options.start_chapter,
                        end_chapter=options.end_chapter,
                        max_chapters=max_chapters,
                        language=options.lang,
                        debug_dump=options.debug,
                        update=options.update,
                        retry_failed=options.retry_failed,
                        max_workers=options.workers,
                    )
                    if out_file is None:
                        if options.retry_failed:
                            log(f"[info] No failed chapters to retry for '{title}'. Nothing to do.")
                        else:
                            log(f"[info] No updates found for '{title}'. Existing EPUB left unchanged.")
                        summary_rows.append({
                            "novel_id": novel_id,
                            "status": "no updates",
                            "chapters": 0,
                            "title": title,
                            "path": None,
                        })
                    else:
                        log(f"[success] Wrote EPUB: {out_file}")
                        summary_rows.append({
                            "novel_id": novel_id,
                            "status": "epub",
                            "chapters": count,
                            "title": title,
                            "path": out_file,
                        })
            except Exception as e:
                failures.append((novel_id, str(e)))
                summary_rows.append({
                    "novel_id": novel_id,
                    "status": "failed",
                    "chapters": None,
                    "title": str(e),
                    "path": None,
                })
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
