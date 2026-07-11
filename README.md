# PIA SCRAP (API): Novelpia → EPUB

Create EPUB or TXT output from Novelpia novels using Novelpia’s API. Given one or more `novel_id` values (for example, `5522`), the script fetches novel metadata, episodes, chapter HTML, images, and cover data, then writes output with cache and retry metadata.

> Use responsibly. Only download what your account can legitimately access. Respect Novelpia’s Terms and copyright.

---

## What's New in 2.6.0

* **Dark mode web dashboard**: toggle between light/dark themes, persisted in browser.
* **Progress bar**: visual progress indicator on the web dashboard with color-coded states.
* **Log filtering**: real-time text filter for log output on the web dashboard.
* **Job history API**: `GET /api/jobs` returns all jobs sorted by creation time.
* **Rate limiting**: web API limits concurrent jobs to 4 (HTTP 429 on overflow).
* **Integration tests**: 8 tests exercising the full pipeline (API → fetch → build → output).
* **CI lint gate**: ruff check + format runs on every push.
* **Code quality**: 25 issues fixed — thread-safe debug logging, DRY refactors, missing tests, type improvements.
* **Optional web dependencies**: `fastapi`/`uvicorn` moved to `[web]` extras — CLI installs stay lean.

---

## What's New in 2.5.0

* **Fixed stale-ticket retry storm**: chapter content fetches that hit a transient `403` now retry with a freshly-minted episode ticket instead of resending the same expired one, and no longer waste time on pointless session refreshes that couldn't have fixed the problem. Downloads recover in ~1 retry instead of stalling for several seconds per chapter.
* **Fixed missing EPUB covers**: covers now fall back from `novel_full_img` to `novel_img` when the primary field points to a bad/placeholder image, and image detection sniffs file signatures instead of trusting an untrustworthy `Content-Type` header.
* **Richer EPUB metadata**: EPUBs now carry `dc:description`, `dc:subject` (genre tags), `dc:source`, and `dc:date` alongside title/author/language, so library apps like Calibre can sort and search by them.
* **Fixed literal `<br>` in descriptions**: novel synopses use `<br>` as a plain-text line break, not real HTML; both the EPUB About page and `metadata.json` now render real line breaks instead of the literal `<br>` characters.
* **Centralized logging**: internal modules use structured logging instead of ad-hoc `print()` calls.
* **Internal refactor**: `src/api.py` response parsing now uses typed contracts instead of loose dicts, and chapter caching, failed-chapter tracking, and fetch orchestration are split into focused modules (`chapter_cache.py`, `chapter_pipeline.py`). No behavior change for CLI/web usage.

---

## Features

* API-based fetch (no browser automation).
* **Parallel Fetching**: Uses `ThreadPoolExecutor` for concurrent chapter downloads.
* **Configurable Workers**: Tune chapter fetch concurrency with `-w`.
* **Incremental Updates**: Reuse per-chapter JSON files in `.cache/` with `-up` to fetch only missing/new chapters.
* **Failed Chapter Retry**: Writes `failed_chapters.jsonl` and can refetch those chapters with `-r`.
* **Free Account Flow**: Handles ad-gated chapters with a short access wait and stops at premium-only chapters.
* **Progress Reporting**: Real-time visual feedback with `tqdm` progress bars.
* **Flexible Chapter Selection**: Support for downloading specific chapter ranges (`-start`/`-end`).
* **Environment Variable Support**: Securely store credentials in a `.env` file via `python-dotenv`.
* **Session Recovery**: Retries server errors and refreshes expired sessions when credentials are available.
* EPUB output with cover, About page, genre metadata, per-chapter files, ToC, NCX/Nav.
* Preserves inline images (downloaded and embedded).
* **Web Dashboard**: FastAPI-based UI with dark mode, progress tracking, job history, and log filtering.

---

## What It Does

* Authenticates against `https://api-global.novelpia.com` and stores `login_at` token + cookies in `.api.json`.
* `.api.json` holds your session secrets. It is git-ignored and untracked. Never commit it, and treat it like a password. Re-login if it is ever exposed.
* Calls `novel/episode/list` to collect metadata and episodes.
* For each episode, requests a ticket, extracts the `_t` token, then fetches chapter content.
* Normalizes HTML (images, structure), embeds images into the EPUB, adds a minimal stylesheet.
* Adds an About page with Title, Author, Genre, Status, Source, Description, and cover when available.

---

## Requirements

* Python 3.10+
* Core packages: `requests`, `beautifulsoup4`, `ebooklib`, `tqdm`, `python-dotenv`, `PySocks`, `lxml`
* Web dashboard (optional): `fastapi`, `uvicorn`

Install packages (CLI only):

```bash
pip install -r requirements.txt
```

Install packages including the web dashboard:

```bash
pip install -r requirements-web.txt
# or: pip install -e ".[web]"
```

---

## Prebuilt Release Zips

Tagged releases include:

* `pia-scrap-linux.zip`
* `pia-scrap-windows.zip`

Each zip contains the executable, `README.md`, and `.env.example`. Copy `.env.example` to `.env` beside the executable to use environment variables.

Linux:

```bash
./pia-scrap 5522
```

Windows:

```powershell
.\pia-scrap.exe 5522
```

To build the same executable locally, install PyInstaller and run:

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --clean --noconfirm pia-scrap.spec
```

The executable is written to `dist/`.

---

## CLI

```text
python main.py [NOVEL_ID ...] [-q FILE] [-u EMAIL] [-p PASSWORD]
                   [-out DIR | -o DIR] [-max N]
                   [-start START_CHAPTER] [-end END_CHAPTER]
                   [-lang en] [-proxy URL] [-t SECONDS]
                   [-w N] [-up] [-r] [-v] [-txt]
```

Arguments

* `NOVEL_ID` (positional) — one or more numeric `novel_no` values, e.g. `5522` or `5522 5760`
* `-q` — read novel IDs or Novelpia novel URLs from a text file, one per line. Blank lines and `#` comments are ignored.
* `-u`, `-p` — login once; tokens saved to `.api.json` for reuse. Prefer `.env` for passwords; CLI passwords can appear in shell history and process lists.
* `-out`, `-o` — output directory (default: `output`).
* `-max` — fetch up to N episodes (0 or unset = all).
* `-start` — start fetching from this chapter number.
* `-end` — stop fetching at this chapter number.
* `-lang` — EPUB language code (default `en`).
* `-proxy` — HTTP/HTTPS/SOCKS proxy, e.g. `http://host:port` or `socks5h://host:port`.
* `-t` — seconds to wait between episode requests (default `1.25`).
* `-w` — parallel chapter fetch workers (default `1`). Increase to speed up fetching, but beware of hitting rate limits.
* `-up` — reuse per-chapter JSON files in `.cache/` to fetch only chapters missing from cache.
* `-r` — retry chapters that failed to fetch.
* `-v` — verbose request logs.
* `-txt` — export as .txt per episode instead of EPUB.

---

## Quick Start

1. First run with your Novelpia credentials (tokens are persisted to `.api.json`; see the `-u`/`-p` note above about `.env`):

   ```bash
   python main.py 5522 -u you@example.com -p "your-password"
   ```

2. Subsequent runs can reuse stored tokens (no password on the command line):

   ```bash
   python main.py 5522
   ```

3. Update an ongoing novel later without redownloading cached chapters:

   ```bash
   python main.py 5522 -up
   ```

   If a long normal download is cancelled or crashes, rerun the same novel with `-up`. Chapters already saved in `.cache/` are skipped, and only missing chapters are fetched.

4. Queue multiple novels with the same options:

   ```bash
   python main.py 5522 5760 -up
   ```

5. Keep an update queue in a text file:

   ```txt
   # novels.txt
   https://global.novelpia.com/novel/5522
   https://global.novelpia.com/novel/5760
   ```

   ```bash
   python main.py -q novels.txt -up
   ```

   Queued runs print a final summary showing each novel ID, status, chapter count, and title or error.
   Duplicate novel IDs are skipped after positional IDs and queue files are merged.

6. Retry only chapters that failed during a previous run:

   ```bash
   python main.py 5522 -r
   ```

Mode behavior summary:

| Mode | Fetches | Uses `.cache/` | Best for |
| --- | --- | --- | --- |
| Normal | All selected chapters | Saves successful fresh fetches; may refresh existing cache | Fresh full download/build |
| `-up` | Missing selected chapters only | Skips cached chapters, fetches missing ones | Resuming interrupted long runs; updating ongoing novels |
| `-r` | Episodes listed in `failed_chapters.jsonl` only | Rebuilds from cached chapters plus retried successes | Retrying failed chapters without redownloading everything |

### Web App

Run a compact local dashboard from a browser:

```bash
pip install -r requirements-web.txt
python -m uvicorn web_app:app --reload
```

Open `http://127.0.0.1:8000`, paste novel IDs or Novelpia novel URLs, and start a background EPUB job. The web UI is EPUB-only, polls progress, shows logs, keeps recent jobs in the browser, and auto-downloads finished EPUB files once per job.

The dashboard is designed for local use on `127.0.0.1`. It accepts credentials and full cookie exports, so do not expose it directly on a public interface. If you deploy it beyond localhost, put it behind your own HTTPS/auth reverse proxy and treat `.env`, `.api.json`, and cookie text as secrets.

Authentication is optional for public content. For ad-gated chapters, premium content, or chapter images, open **Authentication** and paste a full Netscape cookie export from your browser. Image hosts may require CloudFront cookies (`CloudFront-Key-Pair-Id`, `CloudFront-Policy`, `CloudFront-Signature`).

To export browser cookies as Netscape `cookies.txt`, you can use [kairi003/Get-cookies.txt-LOCALLY](https://github.com/kairi003/Get-cookies.txt-Locally), an extension that exports cookies locally in Netscape format.

### Environment Variables (.env)

You can create a `.env` file in the root directory to store credentials or cookies:

```env
NOVELPIA_EMAIL=your_email@example.com
NOVELPIA_PASSWORD=your_password

# Optional Netscape cookie auth
NOVELPIA_COOKIE_FILE=cookies.txt
NOVELPIA_COOKIE_TEXT_B64=
```

`NOVELPIA_COOKIE_TEXT_B64` is recommended for multiline Netscape cookies. Encode a cookie file with PowerShell:

```powershell
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content .\cookies.txt -Raw)))
```

A template is provided in `.env.example`.

---

## Output Details

Alongside the EPUB, the tool writes:

* `metadata.json` — title, author, tags (when available), total chapters, status, description, source URL.
* `chapters.jsonl` — one JSON line per chapter: index, title, URL of the web reader for that episode.
* `.cache/<episode_no>.json` — one cached chapter JSON file per episode, used by `-up` and `-r`.
* `failed_chapters.jsonl` — failed chapter records, written only when one or more chapters fail. Premium stop for free accounts is not treated as a failed chapter.

Output files are written under `output/<title>/`:

```text
output/<title>/<title>.epub or output/<title>/<episode-title>.txt
```

---

## Example Session

```text
[auth] Logged in as: FoggyRam2237
[info] User status: free
[info] extracting metadata…
[info] title='The Reborn Calico Princess: Dancing with the System' author='Tata' chapter=2 status=Ongoing
[info] fetching chapters: 100%|█████████████████████████████████████████████████████████████████████████| 2/2 [00:03<00:00,  1.82s/chap]

[success] Wrote EPUB: output\the-reborn-calico-princess-dancing-with-the-system\the-reborn-calico-princess-dancing-with-the-system.epub
```

---

## Tips & Troubleshooting

* Rate-limit, transient content access, and server-error responses trigger retry delays before a chapter is marked failed.
* Ad-gated runs print one notice when the first gated chapter is detected, then continue quietly for later gated chapters.
* No-op updates — when `-up` finds every server chapter already cached, existing EPUB/TXT outputs are left unchanged.
* Missing images — paste full browser Netscape cookies if chapter images use `pv-gn.novelpia.com`; those URLs may require CloudFront cookies before images can be embedded.

---

## Development & Testing

This repo uses `ruff` for linting and `pytest` for tests.

```bash
pip install -r requirements-web.txt -e ".[dev]"
ruff check .
pytest            # runs the full suite (pythonpath is configured in pyproject.toml)
```

---

## License

Provided “as is”, for personal use only. Do not redistribute the content. Follow Novelpia’s Terms of Service and Copyright.
