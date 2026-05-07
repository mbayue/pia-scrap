import argparse
import sys
import os
import re

from dotenv import load_dotenv
from src.api import NovelpiaClient
from src.builder import build_epub, build_txt
from src.helper import load_config, save_config
from src import const

# ----------------------------
# Main Function
# ----------------------------

def load_queue_file(path):
    novel_ids = []
    novel_url_re = re.compile(r"(?:^|/)novel/(\d+)(?:\D|$)")
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            for item in line.replace(",", " ").split():
                match = novel_url_re.search(item)
                if match:
                    novel_ids.append(int(match.group(1)))
                    continue
                try:
                    novel_ids.append(int(item))
                except ValueError:
                    raise ValueError(f"{path}:{line_no}: invalid novel_id or novel URL '{item}'")
    return novel_ids

def dedupe_novel_ids(novel_ids):
    unique_ids = []
    skipped_ids = []
    seen = set()
    for novel_id in novel_ids:
        if novel_id in seen:
            skipped_ids.append(novel_id)
            continue
        seen.add(novel_id)
        unique_ids.append(novel_id)
    return unique_ids, skipped_ids

def print_queue_summary(rows):
    if not rows:
        return

    print("\n[summary]")
    print(f"{'novel_id':<10} {'status':<12} {'chapters':<8} title")
    for row in rows:
        chapters = "" if row.get("chapters") is None else str(row.get("chapters"))
        print(f"{row['novel_id']:<10} {row['status']:<12} {chapters:<8} {row['title']}")

def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Novelpia to EPUB packer (API)")
    ap.add_argument("novel_ids", metavar="novel_id", type=int, nargs="*", help="novel_no(s), e.g., 1072 or 4565 1234 468")
    ap.add_argument("-q", dest="queue", action="append", default=[], help="Read novel IDs or Novelpia novel URLs from a text file, one per line")
    ap.add_argument("-u", dest="email", help="Novelpia email (overrides config tokens if provided)")
    ap.add_argument("-p", dest="password", help="Novelpia password (overrides config tokens if provided)")
    ap.add_argument("-out", "-o", default="output", help="Output directory")
    ap.add_argument("-max", dest="max_chapters", type=int, default=0, help="Fetch up to N chapters (0 = all)")
    ap.add_argument("-start", dest="start_chapter", type=int, default=None, help="Start fetching from this chapter number")
    ap.add_argument("-end", dest="end_chapter", type=int, default=None, help="Stop fetching at this chapter number")
    ap.add_argument("-lang", default="en", help="EPUB language code (default: en)")
    ap.add_argument("-proxy", default=None, help="HTTP/HTTPS proxy, e.g. http://host:port")
    ap.add_argument("-v", dest="debug", action="store_true", help="Enable verbose HTTP request/response logs and extra diagnostics")
    ap.add_argument("-t", dest="throttle", type=float, default=1.0, help="Seconds delay between episode requests (default: 1.0)")
    ap.add_argument("-w", dest="workers", type=int, default=1, help="Parallel chapter fetch workers (default: 1). Increase to speed up fetching, but beware of hitting rate limits.")
    ap.add_argument("-up", dest="update", action="store_true", help="Reuse cached chapters and fetch only missing/new chapters")
    ap.add_argument("-r", dest="retry_failed", action="store_true", help="Retry chapters that failed to fetch")
    ap.add_argument("-txt", dest="txt", action="store_true", help="Output plain .txt files per episode instead of EPUB")
    args = ap.parse_args()

    novel_ids = list(args.novel_ids)
    for queue_path in args.queue:
        try:
            novel_ids.extend(load_queue_file(queue_path))
        except OSError as e:
            ap.error(f"Unable to read queue file '{queue_path}': {e}")
        except ValueError as e:
            ap.error(str(e))

    if not novel_ids:
        ap.error("provide at least one novel_id or -q FILE")

    novel_ids, skipped_ids = dedupe_novel_ids(novel_ids)
    if skipped_ids:
        skipped_text = ", ".join(str(novel_id) for novel_id in skipped_ids)
        print(f"[info] Skipping duplicate novel IDs: {skipped_text}")

    const.HTTP_LOG = bool(args.debug)

    cfg = load_config()
    cfg_login_at = (cfg.get("login_at") or "").strip() or None
    cfg_userkey = (cfg.get("userkey") or "").strip() or None
    cfg_tkey = (cfg.get("tkey") or "").strip() or None

    # Priority: CLI > .env > config tokens > error
    email = args.email or os.getenv("NOVELPIA_EMAIL")
    password = args.password or os.getenv("NOVELPIA_PASSWORD")

    if email and password:
        client = NovelpiaClient(email=email, password=password, proxy=args.proxy, throttle=args.throttle, userkey=cfg_userkey, tkey=cfg_tkey)
        client.login()
        # Persist/refresh tokens after successful login
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
            pass
        save_config({
            "login_at": client.tokens.login_at,
            "userkey": userkey_val or cfg_userkey or "",
            "tkey": tkey_val or client.tokens.tkey or cfg_tkey or "",
        })
    elif cfg_login_at and cfg_userkey:
        client = NovelpiaClient(email=None, password=None, proxy=args.proxy, throttle=args.throttle, userkey=cfg_userkey, tkey=cfg_tkey)
        client.tokens.login_at = cfg_login_at
    else:
        print("[error] No credentials or stored tokens found. Provide --user and --pass to login once.")
        sys.exit(2)

    summary_rows = []
    failures = []
    total = len(novel_ids)
    max_chapters = args.max_chapters if args.max_chapters and args.max_chapters > 0 else None

    for idx, novel_id in enumerate(novel_ids, 1):
        if total > 1:
            print(f"\n[queue] ({idx}/{total}) Building novel {novel_id}")

        try:
            if args.txt:
                out_dir_final, title, count = build_txt(
                    client, novel_id, args.out,
                    start_chapter=args.start_chapter,
                    end_chapter=args.end_chapter,
                    max_chapters=max_chapters,
                    language=args.lang, debug_dump=args.debug,
                    update=args.update,
                    retry_failed=args.retry_failed,
                    max_workers=args.workers,
                )
                if out_dir_final is None:
                    print(f"\n[info] No updates found for '{title}'. Existing TXT output left unchanged.")
                    summary_rows.append({"novel_id": novel_id, "status": "no updates", "chapters": 0, "title": title})
                else:
                    print(f"\n[success] Wrote TXT files under: {out_dir_final}")
                    summary_rows.append({"novel_id": novel_id, "status": "txt", "chapters": count, "title": title})
            else:
                out_file, title, count = build_epub(
                    client, novel_id, args.out,
                    start_chapter=args.start_chapter,
                    end_chapter=args.end_chapter,
                    max_chapters=max_chapters,
                    language=args.lang, debug_dump=args.debug,
                    update=args.update,
                    retry_failed=args.retry_failed,
                    max_workers=args.workers,
                )
                if out_file is None:
                    print(f"\n[info] No updates found for '{title}'. Existing EPUB left unchanged.")
                    summary_rows.append({"novel_id": novel_id, "status": "no updates", "chapters": 0, "title": title})
                else:
                    print(f"\n[success] Wrote EPUB: {out_file}")
                    summary_rows.append({"novel_id": novel_id, "status": "epub", "chapters": count, "title": title})
        except Exception as e:
            failures.append((novel_id, str(e)))
            summary_rows.append({"novel_id": novel_id, "status": "failed", "chapters": None, "title": str(e)})
            print(f"[error] Failed to build novel {novel_id}: {e}")

    if total > 1 or args.queue:
        print_queue_summary(summary_rows)

    if failures:
        print("\n[error] Queue finished with failures:")
        for novel_id, error in failures:
            print(f"  - {novel_id}: {error}")
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[warn] aborted by user")
        sys.exit(130)
