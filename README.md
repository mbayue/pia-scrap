# PIA SCRAP (API): Novelpia → EPUB

Create EPUB or TXT output from Novelpia novels using Novelpia’s API. Given one or more `novel_id` values (for example, `5522`), the script fetches novel metadata, episodes, chapter HTML, images, and cover data, then writes output with cache and retry metadata.

> Use responsibly. Only download what your account can legitimately access. Respect Novelpia’s Terms and copyright.

---

## What's New in 2.4.1

* Prints logged-in account status: `free`, `paid`, or `unknown`.
* Handles free-account ad-gated chapters with one notice and a short wait.
* Stops at premium-only chapters for free accounts, then writes the chapters already fetched.
* Retries transient chapter-content `403` responses and redacts `_t` tokens in saved errors.
* Adds genre metadata to the EPUB About page when tags are available.
* Splits internals into typed auth, API, cache, pipeline, export, CLI, and web-job helpers.

---

## What's New in 2.4.2

* **Web-app concurrency fix**: parallel background jobs no longer clobber each other's progress — each job now gets an isolated, thread-local progress sink.
* **Test collection fix**: `pytest` runs with `pythonpath = ["."]`, so a bare `pytest` invocation collects the suite (previously failed with `ModuleNotFoundError: No module named 'src'`).
* **Credential hygiene**: `.api.json` and the example `output/` folder are no longer tracked and are git-ignored, so session credentials can't be committed by accident.

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

---

## What It Does

* Authenticates against `https://api-global.novelpia.com` and stores `login_at` token + cookies in `.api.json`.
* `.api.json` holds your session secrets. It is git-ignored and untracked — never commit it, and treat it like a password. Re-login if it is ever exposed.
* Calls `novel/episode/list` to collect metadata and episodes.
* For each episode, requests a ticket, extracts the `_t` token, then fetches chapter content.
* Normalizes HTML (images, structure), embeds images into the EPUB, adds a minimal stylesheet.
* Adds an About page with Title, Author, Genre, Status, Source, Description, and cover when available.

---

## Requirements

* Python 3.10+
* Packages: `requests`, `beautifulsoup4`, `ebooklib`, `tqdm`, `python-dotenv`, `fastapi`, `uvicorn`, `PySocks`

Install packages:

```bash
pip install -r requirements.txt
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
* `-u`, `-p` — login once; tokens saved to `.api.json` for reuse.
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

1) First run with your Novelpia credentials (tokens are persisted to `.api.json`):

```bash
python main.py 5522 -u you@example.com -p "your-password"
```

1) Subsequent runs can reuse stored tokens (no password on the command line):

```bash
python main.py 5522
```

1) Update an ongoing novel later without redownloading cached chapters:

```bash
python main.py 5522 -up
```

1) Queue multiple novels with the same options:

```bash
python main.py 5522 5760 -up
```

1) Keep an update queue in a text file:

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

1) Retry only chapters that failed during a previous run:

```bash
python main.py 5522 -r
```

### Web App

Run a compact local dashboard from a browser:

```bash
pip install -r requirements.txt
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

* **Auto-Recovery**: 401/expired tokens are now automatically handled if credentials are found in `.env` or provided via CLI.
* **Retry Handling**: Rate-limit, transient content access, and server-error responses trigger retry delays before a chapter is marked failed.
* Free accounts — ad-gated chapters may take longer because access is granted after a short wait. Premium-only chapters stop the run for free accounts, and already fetched chapters still produce EPUB/TXT output.
* Ad-gated runs print one notice when the first gated chapter is detected, then continue quietly for later gated chapters.
* No-op updates — when `-up` finds every server chapter already cached, existing EPUB/TXT outputs are left unchanged.
* Missing images — paste full browser Netscape cookies if chapter images use `pv-gn.novelpia.com`; those URLs may require CloudFront cookies before images can be embedded.
* HTTP debug — pass `-v` to print masked headers/params and short body previews.

---

## Development & Testing

This repo uses `ruff` for linting and `pytest` for tests.

```bash
pip install -r requirements.txt
ruff check .
pytest            # runs the full suite (pythonpath is configured in pyproject.toml)
```

The web app routes each background EPUB job through its own isolated progress sink, so concurrent jobs report progress independently without interfering with one another.

---

## License

Provided “as is”, for personal use only. Do not redistribute the content. Follow Novelpia’s Terms of Service and Copyright.
