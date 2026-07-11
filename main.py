import argparse
import os
import sys

from src.runner import CliUsageError, build_queue_request, print_queue_summary, run_queue


def should_pause_on_usage_error(argv: list[str], is_frozen: bool, os_name: str) -> bool:
    return is_frozen and os_name == "nt" and len(argv) == 1


def pause_after_usage_error() -> None:
    if not should_pause_on_usage_error(sys.argv, bool(getattr(sys, "frozen", False)), os.name):
        return
    print("\n[info] Run with a novel_id, for example:")
    print("  PowerShell: .\\pia-scrap.exe 5522")
    print("  Command Prompt: pia-scrap.exe 5522")
    input("\nPress Enter to exit...")


def main() -> None:
    ap = argparse.ArgumentParser(description="Novelpia to EPUB packer (API)")
    ap.add_argument("novel_ids", metavar="novel_id", type=int, nargs="*", help="novel_no(s), e.g., 5522 or 5522 5760")
    ap.add_argument(
        "-q",
        dest="queue",
        action="append",
        default=[],
        help="Read novel IDs or Novelpia novel URLs from a text file, one per line",
    )
    ap.add_argument("-u", dest="email", help="Novelpia email (overrides config tokens if provided)")
    ap.add_argument("-p", dest="password", help="Novelpia password (overrides config tokens if provided)")
    ap.add_argument("-out", "-o", default="output", help="Output directory")
    ap.add_argument("-max", dest="max_chapters", type=int, default=0, help="Fetch up to N chapters (0 = all)")
    ap.add_argument(
        "-start", dest="start_chapter", type=int, default=None, help="Start fetching from this chapter number"
    )
    ap.add_argument("-end", dest="end_chapter", type=int, default=None, help="Stop fetching at this chapter number")
    ap.add_argument("-lang", default="en", help="EPUB language code (default: en)")
    ap.add_argument("-proxy", default=None, help="HTTP/HTTPS proxy, e.g. http://host:port")
    ap.add_argument(
        "-v", dest="debug", action="store_true", help="Enable verbose HTTP request/response logs and extra diagnostics"
    )
    ap.add_argument(
        "-t", dest="throttle", type=float, default=1.25, help="Seconds delay between episode requests (default: 1.25)"
    )
    ap.add_argument(
        "-w",
        dest="workers",
        type=int,
        default=1,
        help=(
            "Parallel chapter fetch workers (default: 1). Increase to speed up fetching, "
            "but beware of hitting rate limits."
        ),
    )
    ap.add_argument(
        "-up", dest="update", action="store_true", help="Reuse cached chapters and fetch only missing/new chapters"
    )
    ap.add_argument("-r", dest="retry_failed", action="store_true", help="Retry chapters that failed to fetch")
    ap.add_argument("-txt", dest="txt", action="store_true", help="Output plain .txt files per episode instead of EPUB")
    args = ap.parse_args()

    try:
        request = build_queue_request(args)
    except CliUsageError as e:
        if should_pause_on_usage_error(sys.argv, bool(getattr(sys, "frozen", False)), os.name):
            ap.print_usage(sys.stderr)
            print(f"{ap.prog}: error: {e}", file=sys.stderr)
            pause_after_usage_error()
            sys.exit(2)
        ap.error(str(e))

    try:
        result = run_queue(request.novel_ids, request.options)
    except Exception as e:
        print(f"[error] {e}")
        sys.exit(1)

    if request.show_summary:
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
