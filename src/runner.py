import base64
import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv

from src import const
from src.api import NovelpiaClient
from src.builder import build_epub, build_txt
from src.helper import get_cookie_value, load_config, load_netscape_cookies, load_netscape_cookies_text, save_config


LogFn = Callable[[str], None]


@dataclass
class QueueOptions:
    out: str = "output"
    start_chapter: Optional[int] = None
    end_chapter: Optional[int] = None
    max_chapters: int = 0
    lang: str = "en"
    proxy: Optional[str] = None
    debug: bool = False
    throttle: float = 1.25
    workers: int = 1
    update: bool = False
    retry_failed: bool = False
    txt: bool = False
    email: Optional[str] = None
    password: Optional[str] = None
    cookie_file: Optional[str] = None
    cookie_text: Optional[str] = None


def _parse_novel_token(item: str) -> int:
    match = re.search(r"(?:^|/)novel/(\d+)(?:\D|$)", item)
    if match:
        return int(match.group(1))
    return int(item)


def parse_queue_lines(lines: Iterable[str], source: str = "queue") -> List[int]:
    novel_ids: List[int] = []
    for line_no, line in enumerate(lines, 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for item in line.replace(",", " ").split():
            try:
                novel_ids.append(_parse_novel_token(item))
            except ValueError:
                raise ValueError(f"{source}:{line_no}: invalid novel_id or novel URL '{item}'")
    return novel_ids


def load_queue_file(path: str) -> List[int]:
    with open(path, "r", encoding="utf-8") as f:
        return parse_queue_lines(f, source=path)


def dedupe_novel_ids(novel_ids: Iterable[int]) -> Tuple[List[int], List[int]]:
    unique_ids: List[int] = []
    skipped_ids: List[int] = []
    seen = set()
    for novel_id in novel_ids:
        if novel_id in seen:
            skipped_ids.append(novel_id)
            continue
        seen.add(novel_id)
        unique_ids.append(novel_id)
    return unique_ids, skipped_ids


def _cookie_text_from_env() -> Optional[str]:
    raw_text = os.getenv("NOVELPIA_COOKIE_TEXT")
    if raw_text:
        return raw_text.replace("\\n", "\n")
    encoded_text = os.getenv("NOVELPIA_COOKIE_TEXT_B64")
    if encoded_text:
        try:
            return base64.b64decode(encoded_text).decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"Invalid NOVELPIA_COOKIE_TEXT_B64: {e}")
    return None

def create_client(options: QueueOptions) -> NovelpiaClient:
    load_dotenv()
    const.HTTP_LOG = bool(options.debug)

    cfg = load_config()
    cfg_login_at = (cfg.get("login_at") or "").strip() or None
    cfg_userkey = (cfg.get("userkey") or "").strip() or None
    cfg_tkey = (cfg.get("tkey") or "").strip() or None

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
        else:
            cookie_path = os.path.expanduser(cookie_file)
            jar = load_netscape_cookies(cookie_path)
        client.s.cookies.update(jar)
        userkey_val = get_cookie_value(client.s.cookies, "USERKEY")
        tkey_val = get_cookie_value(client.s.cookies, "TKEY")
        login_at_val = (
            get_cookie_value(client.s.cookies, "LOGINAT")
            or get_cookie_value(client.s.cookies, "login_at")
            or os.getenv("NOVELPIA_LOGIN_AT")
            or cfg_login_at
        )
        client.tokens.userkey = userkey_val
        client.tokens.tkey = tkey_val
        client.tokens.login_at = login_at_val
        if not userkey_val:
            raise RuntimeError("Netscape cookie file did not contain USERKEY. Export cookies for novelpia.com and try again.")
        save_config({
            "login_at": login_at_val or "",
            "userkey": userkey_val,
            "tkey": tkey_val or "",
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
        userkey_val = None
        tkey_val = None
        try:
            for c in client.s.cookies:
                if c.name == "USERKEY":
                    userkey_val = c.value
                elif c.name == "TKEY":
                    tkey_val = c.value
        except Exception as e:
            print(f"Error occurred while fetching cookies: {e}")
        save_config({
            "login_at": client.tokens.login_at,
            "userkey": userkey_val or cfg_userkey or "",
            "tkey": tkey_val or client.tokens.tkey or cfg_tkey or "",
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


def run_queue(novel_ids: Iterable[int], options: QueueOptions, log: LogFn = print) -> Dict:
    novel_ids, skipped_ids = dedupe_novel_ids(novel_ids)
    if not novel_ids:
        raise ValueError("provide at least one novel_id")
    if skipped_ids:
        log("[info] Skipping duplicate novel IDs: " + ", ".join(str(novel_id) for novel_id in skipped_ids))

    client = create_client(options)
    summary_rows: List[Dict] = []
    failures: List[Tuple[int, str]] = []
    total = len(novel_ids)
    max_chapters = options.max_chapters if options.max_chapters and options.max_chapters > 0 else None

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
                    log(f"[info] No updates found for '{title}'. Existing TXT output left unchanged.")
                    summary_rows.append({"novel_id": novel_id, "status": "no updates", "chapters": 0, "title": title, "path": None})
                else:
                    log(f"[success] Wrote TXT files under: {out_dir_final}")
                    summary_rows.append({"novel_id": novel_id, "status": "txt", "chapters": count, "title": title, "path": out_dir_final})
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
                    log(f"[info] No updates found for '{title}'. Existing EPUB left unchanged.")
                    summary_rows.append({"novel_id": novel_id, "status": "no updates", "chapters": 0, "title": title, "path": None})
                else:
                    log(f"[success] Wrote EPUB: {out_file}")
                    summary_rows.append({"novel_id": novel_id, "status": "epub", "chapters": count, "title": title, "path": out_file})
        except Exception as e:
            failures.append((novel_id, str(e)))
            summary_rows.append({"novel_id": novel_id, "status": "failed", "chapters": None, "title": str(e), "path": None})
            log(f"[error] Failed to build novel {novel_id}: {e}")

    return {"rows": summary_rows, "failures": failures, "skipped_ids": skipped_ids}


def print_queue_summary(rows: List[Dict]) -> None:
    if not rows:
        return

    print("\n[summary]")
    print(f"{'novel_id':<10} {'status':<12} {'chapters':<8} title")
    for row in rows:
        chapters = "" if row.get("chapters") is None else str(row.get("chapters"))
        print(f"{row['novel_id']:<10} {row['status']:<12} {chapters:<8} {row['title']}")
