import os
import sys

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if sys.stderr is not None:
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tqdm
from gooey import Gooey, GooeyParser

if os.environ.get("GOOEY") == "1":

    class PatchedTqdm(tqdm.tqdm):
        def __init__(self, *args, **kwargs):
            sys.stdout.write("[debug] PatchedTqdm instantiated\n")
            sys.stdout.flush()
            class DummyFile:
                def write(self, x):
                    pass

                def flush(self):
                    pass

            kwargs["file"] = DummyFile()
            super().__init__(*args, **kwargs)

        def display(self, *args, **kwargs):
            last_pct = getattr(self, "_last_pct", -1)
            if self.total:
                pct = int(self.n * 100 / self.total)
                if pct != last_pct:
                    self._last_pct = pct
                    sys.stdout.write(f"progress: {pct}%\n")
                    sys.stdout.write(str(self) + "\n")
                    sys.stdout.flush()
            else:
                sys.stdout.write(str(self) + "\n")
                sys.stdout.flush()

    tqdm.tqdm = PatchedTqdm
    for name, module in list(sys.modules.items()):
        if name.startswith("tqdm"):
            if hasattr(module, "tqdm") and module.tqdm is not PatchedTqdm:
                try:
                    module.tqdm = PatchedTqdm
                except AttributeError:
                    pass


# fmt: skip
from src.runner import CliUsageError, build_queue_request, print_queue_summary, run_queue  # noqa: E402


def get_resource_path(relative_path):
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "gui", relative_path)  # type: ignore[attr-defined]
    return os.path.join(os.path.dirname(__file__), relative_path)


if "--ignore-gooey" not in sys.argv:
    import gooey.gui.components.footer as gooey_footer
    import wx
    from gooey.gui.components import modals
    from gooey.gui.components.tabbar import Tabbar
    from gooey.gui.containers.application import GooeyApplication
    from gooey.gui.util import wx_util
    from wx.adv import TaskBarIcon

    original_init = gooey_footer.Footer.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if self.cancel_button:
            self.cancel_button.Hide()

    gooey_footer.Footer.__init__ = patched_init

    original_show_buttons = gooey_footer.Footer.showButtons

    def patched_show_buttons(self, *buttons_to_show):
        buttons = [b for b in buttons_to_show if b not in ("cancel_button", "close_button")]
        original_show_buttons(self, *buttons)

    gooey_footer.Footer.showButtons = patched_show_buttons

    original_on_close = GooeyApplication.onClose

    def patched_on_close(self, *args, **kwargs):
        is_wx_close_event = len(args) > 0 and isinstance(args[0], wx.Event)
        if is_wx_close_event and not self.clientRunner.running():
            if modals.confirmExit():
                self.destroyGooey()
        else:
            original_on_close(self, *args, **kwargs)

    GooeyApplication.onClose = patched_on_close

    def patched_tabbar_layout(self):
        for group, panel in zip(self.options, self.configPanels, strict=False):
            panel.Reparent(self.notebook)
            self.notebook.AddPage(panel, group)
            self.notebook.Layout()

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.notebook, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self.SetSizer(sizer)
        self.Layout()

    Tabbar.layoutComponent = patched_tabbar_layout

    def patched_app_layout(self):
        self.header.Hide()
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.navbar, 1, wx.EXPAND)
        sizer.Add(self.console, 1, wx.EXPAND)
        sizer.Add(wx_util.horizontal_rule(self), 0, wx.EXPAND)
        sizer.Add(self.footer, 0, wx.EXPAND)
        self.SetMinSize((400, 300))
        self.SetSize(self.buildSpec["default_size"])
        self.SetSizer(sizer)
        self.console.Hide()
        self.Layout()
        if self.buildSpec.get("fullscreen", True):
            self.ShowFullScreen(True)
        ico_path = get_resource_path(os.path.join("assets", "program_icon.ico"))
        png_path = get_resource_path(os.path.join("assets", "program_icon.png"))

        if sys.platform == "win32" and os.path.exists(ico_path):
            icon = wx.Icon(ico_path, wx.BITMAP_TYPE_ICO)
        elif os.path.exists(png_path):
            icon = wx.Icon(png_path, wx.BITMAP_TYPE_PNG)
        else:
            icon = wx.Icon(self.buildSpec["images"]["programIcon"], wx.BITMAP_TYPE_PNG)
        self.SetIcon(icon)
        if sys.platform != "win32":
            self.taskbarIcon = TaskBarIcon(iconType=wx.adv.TBI_DOCK)
            self.taskbarIcon.SetIcon(icon)

    GooeyApplication.layoutComponent = patched_app_layout


if getattr(sys, "frozen", False):
    gooey_target = None
else:
    main_path = os.path.abspath(__file__)
    gooey_target = f'"{sys.executable}" -u "{main_path}"'


@Gooey(
    program_name="PIA Scrap - Novelpia to EPUB",
    default_size=(800, 640),
    tabbed_groups=True,
    header_bg_color="#ffffff",
    header_text_color="#000000",
    body_bg_color="#f0f0f0",
    footer_bg_color="#e0e0e0",
    clear_before_run=True,
    show_restart_button=False,
    target=gooey_target,
    language_dir=get_resource_path("languages"),
    progress_regex=r"^progress: (\d+)%",
    hide_progress_msg=True,
)
def main() -> None:
    ap = GooeyParser(description="Novelpia to EPUB packer")

    main_group = ap.add_argument_group("  Main  ")
    main_group.add_argument(
        "--novel_ids",
        dest="novel_ids",
        metavar="Novel URL / ID",
        type=str,
        nargs="*",
        default=[],
        help="e.g., 5522 or https://global.novelpia.com/novel/5522",
    )
    main_group.add_argument(
        "-out", "-o", dest="out", metavar="Output Folder", default="output", help="Default: output", widget="DirChooser"
    )
    main_group.add_argument(
        "-q",
        dest="queue",
        metavar="Queue File",
        action="append",
        help="Optional: Read novel IDs from a text file",
        widget="FileChooser",
    )
    main_group.add_argument(
        "-max",
        dest="max_chapters",
        metavar="Max Chapters",
        type=int,
        default=0,
        help="Fetch up to N chapters (0 = all)",
    )
    main_group.add_argument(
        "-start",
        dest="start_chapter",
        metavar="Start Chapter",
        type=int,
        default=None,
        help="Start fetching from this chapter number",
    )
    main_group.add_argument(
        "-end",
        dest="end_chapter",
        metavar="End Chapter",
        type=int,
        default=None,
        help="Stop fetching at this chapter number",
    )

    main_group.add_argument(
        "-lang",
        dest="lang",
        metavar="Language",
        choices=["en", "ko", "ja", "zh"],
        default="en",
        help="EPUB language code",
    )
    main_group.add_argument(
        "-format",
        dest="format_choice",
        metavar="Format",
        choices=["EPUB", "TXT"],
        default="EPUB",
        help="Choose output format",
    )

    main_group.add_argument(
        "-up",
        dest="update",
        metavar="Update existing EPUB",
        action="store_true",
        help="Reuse cached chapters and fetch only missing/new chapters",
    )
    main_group.add_argument(
        "-img",
        dest="chapter_images",
        metavar="Download images",
        action="store_true",
        help="Embed chapter images in EPUB output",
    )
    main_group.add_argument(
        "-r",
        dest="retry_failed",
        metavar="Retry Failed",
        action="store_true",
        help="Retry chapters that failed to fetch",
    )
    main_group.add_argument(
        "-v",
        dest="debug",
        metavar="Verbose Logging",
        action="store_true",
        help="Enable verbose HTTP request/response logs and extra diagnostics",
    )

    net_group = ap.add_argument_group("  Settings  ")
    net_group.add_argument(
        "-u", dest="email", metavar="Email", help="Novelpia email (overrides config tokens if provided)"
    )
    net_group.add_argument(
        "-p",
        dest="password",
        metavar="Password",
        help="Novelpia password (overrides config tokens if provided)",
        widget="PasswordField",
    )
    net_group.add_argument(
        "-t",
        dest="throttle",
        metavar="Throttle (s)",
        type=float,
        default=1.25,
        help="Seconds delay between episode requests",
    )
    net_group.add_argument(
        "-w", dest="workers", metavar="Workers", type=int, default=1, help="Parallel chapter fetch workers"
    )
    net_group.add_argument(
        "-proxy", dest="proxy", metavar="Proxy URL", default=None, help="HTTP/HTTPS proxy, e.g. http://host:port"
    )

    args = ap.parse_args()
    args.txt = getattr(args, "format_choice", "EPUB") == "TXT"

    if not args.queue:
        args.queue = []
    elif isinstance(args.queue, list) and not args.queue[0]:
        args.queue = []

    try:
        request = build_queue_request(args)
    except CliUsageError as e:
        print(f"Usage Error: {e}", file=sys.stderr)
        sys.exit(2)

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
