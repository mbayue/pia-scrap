import argparse
import sys

from src.runner import QueueOptions, load_queue_file, print_queue_summary, run_queue


def main():
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
    )

    try:
        result = run_queue(novel_ids, options)
    except Exception as e:
        print(f"[error] {e}")
        sys.exit(1)

    if len(novel_ids) > 1 or args.queue:
        print_queue_summary(result["rows"])

    if result["failures"]:
        print("\n[error] Queue finished with failures:")
        for novel_id, error in result["failures"]:
            print(f"  - {novel_id}: {error}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[warn] aborted by user")
        sys.exit(130)
