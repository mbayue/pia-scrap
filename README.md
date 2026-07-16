# PIA Scrap: Novelpia to EPUB

Create EPUB or TXT output from Novelpia novels using Novelpia's API. Given one or more `novel_id` values, for example `5522`, the script fetches novel metadata, episodes, chapter HTML, and cover data, then writes output with cache and retry metadata. Use `-img` to include chapter images.

> Only download content your account may access. Follow Novelpia's Terms and respect copyright.

---

## What's new in 2.9.0

* Desktop GUI client (run with `--gui`) featuring a polished interface, start/stop task controls, and custom layouts.
* Consolidated `main.py` entrypoint (installed as the `pia` command) supporting CLI, `--gui`, and `--web` modes.
* Reorganized file structure separating client scripts into `gui/` and `web/` folders.

## What's new in 2.8.0

* `-img` fetches chapter images with the episode's signed CloudFront key, caches them, and adds them to the EPUB. Chapter, About, and table-of-contents pages use the book stylesheet.
* Invalid novel and episode IDs fail while parsing API responses.
* The web dashboard validates job options before starting a worker.
* CI runs mypy and pytest. API test doubles satisfy Pyright/Pylance without suppressions.


## Features

* Fetches through the API, without browser automation.
* Uses `ThreadPoolExecutor` for concurrent chapter downloads. Set concurrency with `-w`.
* Reuses per-chapter JSON files in `.cache/` with `-up` to fetch only new or missing chapters.
* Writes `failed_chapters.jsonl` and can retry those chapters with `-r`.
* Waits for ad-gated chapters and stops at premium-only chapters.
* Shows progress with `tqdm`.
* Downloads selected chapter ranges with `-start` and `-end`.
* Loads credentials from `.env` through `python-dotenv`.
* Retries server errors and refreshes expired sessions when credentials are present.
* Creates EPUB files with a cover, About page, genre metadata, table of contents, NCX, and navigation document.
* Optionally downloads, caches, and embeds inline images with `-img`.
* Includes a FastAPI dashboard with dark mode, progress, job history, and log filtering.

---

## What it does

* Authenticates against `https://api-global.novelpia.com` and stores `login_at` token + cookies in `.api.json`.
* `.api.json` stores session secrets. Git ignores it. Do not commit it. Re-login if it is exposed.
* Calls `novel/episode/list` to collect metadata and episodes.
* For each episode, requests a ticket, extracts the `_t` token, then fetches chapter content.
* Normalizes chapter HTML and adds a minimal stylesheet. With `-img`, downloads and embeds chapter images.
* Adds an About page with the title, author, genres, status, source, description, and cover when available.

---

## Requirements

* Python 3.10+
* Core packages: `requests`, `beautifulsoup4`, `ebooklib`, `tqdm`, `python-dotenv`, `PySocks`, `lxml`
* Web dashboard (optional): `fastapi`, `uvicorn`
* Desktop GUI (optional): `Gooey`

## Setup & Installation

1. **Create a virtual environment `.venv`**:
   ```bash
   python -m venv .venv
   ```

2. **Activate the virtual environment**:
   * **Windows (PowerShell)**:
     ```powershell
     .\.venv\Scripts\Activate.ps1
     ```
   * **Linux/macOS**:
     ```bash
     source .venv/bin/activate
     ```

3. **Install the package in editable mode**:
   * **CLI only**:
     ```bash
     pip install -e .
     ```
   * **With Web app dashboard**:
     ```bash
     pip install -e ".[web]"
     ```
   * **With Desktop GUI client**:
     ```bash
     pip install -e ".[gui]"
     ```
   * **All (with dev tools)**:
     ```bash
     pip install -e ".[web,gui,dev]"
     ```

This registers the global **`pia`** script command inside your active virtual environment.

---

## Prebuilt releases

Ready-to-use compiled binaries (Windows and Linux) for both CLI and GUI are available under the GitHub Releases section.

To build the CLI executable locally, install PyInstaller and run:

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --clean --noconfirm pia-scrap.spec
```

To build the GUI executable locally, install PyInstaller and run:

```bash
pip install -r requirements-gui.txt pyinstaller
pyinstaller --clean --noconfirm pia-scrap-gui.spec
```

The executable is written to `dist/`.

---

## CLI

```text
pia [NOVEL_ID ...] [-q FILE] [-u EMAIL] [-p PASSWORD]
    [-out DIR | -o DIR] [-max N]
    [-start START_CHAPTER] [-end END_CHAPTER]
    [-lang en] [-proxy URL] [-t SECONDS]
     [-w N] [-up] [-r] [-v] [-img] [-txt]
```

Arguments

* `NOVEL_ID` (positional): one or more numeric `novel_no` values, such as `5522` or `5522 5760`.
* `-q`: read novel IDs or Novelpia novel URLs from a text file, one per line. Blank lines and `#` comments are ignored.
* `-u`, `-p`: log in once and save tokens to `.api.json` for reuse. Prefer `.env` for passwords because CLI passwords can appear in shell history and process lists.
* `-out`, `-o`: output directory. Default: `output`.
* `-max`: fetch up to N episodes. `0` or no value fetches all episodes.
* `-start`: first chapter to fetch.
* `-end`: last chapter to fetch.
* `-lang`: EPUB language code. Default: `en`.
* `-proxy`: HTTP, HTTPS, or SOCKS proxy, such as `http://host:port` or `socks5h://host:port`.
* `-t`: seconds to wait between episode requests. Default: `1.25`.
* `-w`: parallel chapter workers. Default: `1`. Works with paid and free accounts. Free and unknown accounts still unlock ads per chapter and stop at the first premium chapter in list order. Start with `2` to `4` workers on free accounts to reduce rate-limit risk.
* `-up`: reuse per-chapter JSON files in `.cache/` and fetch only missing chapters.
* `-r`: retry chapters that failed to fetch.
* `-v`: verbose request logs.
* `-img`: fetch, cache, and embed chapter images in EPUB output. Omit for text-only chapters.
* `-txt`: export a TXT file per episode instead of an EPUB.

---

## Quick start

1. First run with your Novelpia credentials (tokens are persisted to `.api.json`; see the `-u`/`-p` note above about `.env`):

   ```bash
   pia 5522 -u you@example.com -p "your-password"
   ```

2. Subsequent runs can reuse stored tokens (no password on the command line):

   ```bash
   pia 5522
   ```

3. Update an ongoing novel later without redownloading cached chapters:

   ```bash
   pia 5522 -up
   ```

   If a long normal download is cancelled or crashes, rerun the same novel with `-up`. Chapters already saved in `.cache/` are skipped, and only missing chapters are fetched.

4. Include chapter images and cache their bytes for later rebuilds:

   ```bash
   pia 5522 -img
   ```

5. Queue multiple novels with the same options:

   ```bash
   pia 5522 5760 -up
   ```

6. Keep an update queue in a text file:

   ```txt
   # novels.txt
   https://global.novelpia.com/novel/5522
   https://global.novelpia.com/novel/5760
   ```

   ```bash
   pia -q novels.txt -up
   ```

   Queued runs print a final summary showing each novel ID, status, chapter count, and title or error.
   Duplicate novel IDs are skipped after positional IDs and queue files are merged.

7. Retry only chapters that failed during a previous run:

   ```bash
   pia 5522 -r
   ```

Mode behavior summary:

| Mode | Fetches | Uses `.cache/` | Best for |
| --- | --- | --- | --- |
| Normal | All selected chapters | Saves successful fresh fetches; may refresh existing cache | Fresh full download/build |
| `-up` | Missing selected chapters only | Skips cached chapters, fetches missing ones | Resuming interrupted long runs; updating ongoing novels |
| `-r` | Episodes listed in `failed_chapters.jsonl` only | Rebuilds from cached chapters plus retried successes | Retrying failed chapters without redownloading everything |

### Desktop GUI

Run the desktop GUI application:

```bash
pia --gui
```

The GUI provides a clean, modern interface with:
- Novel Selection: Enter novel IDs or load from a queue file
- Authentication: Email/password login (optional if already logged in)
- Output Options: Output directory, format (EPUB/TXT), language, chapter images
- Chapter Range: Limit which chapters to download
- Download Options: Workers, throttle, update mode, retry failed
- Advanced Options: Proxy settings and debug logging
- Real-time progress bar and log output
- Start/Stop controls for download management

### Web app

Run the local web dashboard:

```bash
pia --web
```

Open `http://127.0.0.1:8000`, paste novel IDs or Novelpia novel URLs, then start an EPUB job. The web UI polls progress, shows logs, keeps recent jobs in the browser, and downloads each finished EPUB once.

The dashboard is for local use on `127.0.0.1`. It accepts credentials and full cookie exports. Do not expose it directly to the internet. For any other deployment, add HTTPS and authentication through a reverse proxy. Treat `.env`, `.api.json`, and cookie text as secrets.

Docker Compose publishes the dashboard only on `127.0.0.1:8000`. Uvicorn listens on `0.0.0.0` inside the container so port mapping works. Do not bind the host to `0.0.0.0:8000` without adding authentication.

Authentication is optional for public content. For ad-gated chapters, premium content, or `-img`, paste a full Netscape cookie export into the Authentication panel. Image hosts may require `CloudFront-Key-Pair-Id`, `CloudFront-Policy`, and `CloudFront-Signature` cookies.

To export browser cookies as Netscape `cookies.txt`, you can use [kairi003/Get-cookies.txt-LOCALLY](https://github.com/kairi003/Get-cookies.txt-Locally), an extension that exports cookies locally in Netscape format.

### Environment variables (`.env`)

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

## Output details

Alongside the EPUB, the tool writes:

* `metadata.json`: title, author, tags when available, total chapters, status, description, and source URL.
* `chapters.jsonl`: one JSON line per chapter with its index, title, and web-reader URL.
* `.cache/<episode_no>.json`: one cached chapter JSON file per episode, used by `-up` and `-r`.
* `.cache/images/`: URL-hashed chapter-image bytes written by `-img`, reused by later `-img` EPUB rebuilds.
* `failed_chapters.jsonl`: failed chapter records. This file is written only when one or more chapters fail. A premium stop for a free account is not a failed chapter.

Output files are written under `output/<title>/`:

```text
output/<title>/<title>.epub or output/<title>/<episode-title>.txt
```

---

## Example session

```text
[auth] Logged in as: FoggyRam2237
[info] User status: free
[info] extracting metadata…
[info] title='The Reborn Calico Princess: Dancing with the System' author='Tata' chapter=2 status=Ongoing
[info] fetching chapters: 100%|█████████████████████████████████████████████████████████████████████████| 2/2 [00:03<00:00,  1.82s/chap]

[success] Wrote EPUB: output\the-reborn-calico-princess-dancing-with-the-system\the-reborn-calico-princess-dancing-with-the-system.epub
```

---

## Tips and troubleshooting

* Rate limits, temporary content failures, and server errors retry before a chapter is marked failed.
* Ad-gated runs print one notice for the first gated chapter, then continue quietly.
* When `-up` finds every server chapter in cache, it leaves existing EPUB and TXT output unchanged.
* `-img` is required for chapter images. For missing `pv-gn.novelpia.com` images, paste full browser Netscape cookies. These URLs may need CloudFront cookies before the tool can fetch them.

---

## Development and testing

This repo uses `ruff` for linting and `pytest` for tests.

```bash
ruff check
pytest
```

---

## License

Provided "as is" for personal use only. Do not redistribute content. Follow Novelpia's Terms of Service and copyright rules.
