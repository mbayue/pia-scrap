# PIA SCRAP (API): Novelpia → EPUB

Create a clean EPUB from Novelpia novels using Novelpia’s API. Given one or more `novel_id` values (e.g., `49`), the script fetches the novel data, episode list, pulls episode data, embeds images and cover, and writes a nicely structured EPUB with metadata.

> Use responsibly. Only download what your account can legitimately access. Respect Novelpia’s Terms and copyright.

---

## Features

* API-based fetch (no browser automation).
* **Parallel Fetching**: Uses `ThreadPoolExecutor` for high-performance concurrent chapter downloads.
* **Configurable Workers**: Tune chapter fetch concurrency with `-w`.
* **Incremental Updates**: Reuse per-chapter JSON files in `.cache/` with `-up` to fetch only missing/new chapters.
* **Failed Chapter Retry**: Writes `failed_chapters.jsonl` and can refetch those chapters with `-r`.
* **Progress Reporting**: Real-time visual feedback with `tqdm` progress bars.
* **Flexible Chapter Selection**: Support for downloading specific chapter ranges (`-start`/`-end`).
* **Environment Variable Support**: Securely store credentials in a `.env` file via `python-dotenv`.
* **Advanced Automation**: Automatically handles server-side rate-limit responses with retry and session expiration (401) with auto re-login.
* Proper EPUB with cover, About page, per‑chapter files, ToC, NCX/Nav.
* Preserves inline images (downloaded and embedded).

---

## What It Does

* Authenticates against `https://api-global.novelpia.com` and stores `login_at` token + cookies in `.api.json`.
* Calls `novel/episode/list` to collect metadata and episodes.
* For each episode, requests a ticket, extracts the `_t` token, then fetches the episode data.
* Normalizes HTML (images, structure), embeds images into the EPUB, adds a minimal stylesheet.
* Adds an About page with Title, Author, Status, Source, Description, and cover when available.

---

## Requirements

* Python 3.10+
* Packages: `requests`, `beautifulsoup4`, `ebooklib`, `tqdm`, `python-dotenv`, `fastapi`, `uvicorn`, `PySocks`

Install packages:

```bash
pip install -r requirements.txt
```

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

* `NOVEL_ID` (positional) — one or more numeric `novel_no` values, e.g. `49` or `41 1348 216`
* `-q` — read novel IDs or Novelpia novel URLs from a text file, one per line. Blank lines and `#` comments are ignored.
* `-u`, `-p` — login once; tokens saved to `.api.json` for reuse.
* `-out`, `-o` — output directory (default: `output`).
* `-max` — fetch up to N episodes (0 or unset = all).
* `-start` — start fetching from this chapter number.
* `-end` — stop fetching at this chapter number.
* `-lang` — EPUB language code (default `en`).
* `-proxy` — HTTP/HTTPS/SOCKS proxy, e.g. `http://host:port` or `socks5h://host:port`.
* `-t` — seconds to wait between episode requests (default `1.0`).
* `-w` — parallel chapter fetch workers (default `1`). Increase to speed up fetching, but beware of hitting rate limits.
* `-up` — reuse per-chapter JSON files in `.cache/` to fetch only chapters missing from cache.
* `-r` — retry chapters that failed to fetch.
* `-v` — verbose request logs.
* `-txt` — export as .txt per episode instead of EPUB.

---

## Quick Start

1) First run with your Novelpia credentials (tokens are persisted to `.api.json`):

```bash
python main.py 49 -u you@example.com -p "your-password"
```

1) Subsequent runs can reuse stored tokens (no password on the command line):

```bash
python main.py 49
```

1) Update an ongoing novel later without redownloading cached chapters:

```bash
python main.py 49 -up
```

1) Queue multiple novels with the same options:

```bash
python main.py 4565 1234 468 -up
```

1) Keep an update queue in a text file:

```txt
# novels.txt
https://global.novelpia.com/novel/4565
https://global.novelpia.com/novel/1234
https://global.novelpia.com/novel/468
```

```bash
python main.py -q novels.txt -up
```

Queued runs print a final summary showing each novel ID, status, chapter count, and title or error.
Duplicate novel IDs are skipped after positional IDs and queue files are merged.

1) Retry only chapters that failed during a previous run:

```bash
python main.py 49 -r
```

### Web App

Run a compact local dashboard from a browser:

```bash
pip install -r requirements.txt
python -m uvicorn web_app:app --reload
```

Open `http://127.0.0.1:8000`, paste novel IDs or Novelpia novel URLs, and start a background EPUB job. The web UI is EPUB-only, polls progress, shows terminal-style logs, keeps recent jobs in the browser, and auto-downloads finished EPUB files once per job.

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

### Docker

Build and run the production web server:

```bash
docker build -t pia-scrap .
docker run --rm -p 8000:8000 -v ./output:/app/output pia-scrap
```

Bind Docker to localhost unless you have added external auth:

```bash
docker run --rm -p 127.0.0.1:8000:8000 -v ./output:/app/output pia-scrap
```

With environment variables:

```bash
docker run --rm -p 8000:8000 --env-file .env -v ./output:/app/output pia-scrap
```

---

## Output Details

Alongside the EPUB, the tool writes:

* `metadata.json` — title, author, tags (when available), total chapters, status, description, source URL.
* `chapters.jsonl` — one JSON line per chapter: index, title, URL of the web reader for that episode.
* `.cache/<episode_no>.json` — one cached chapter JSON file per episode, used by `-up` and `-r`.
* `failed_chapters.jsonl` — failed chapter records, written only when one or more chapters fail.

Output files are written under `output/<title>/`:

```text
output/<title>/<title>.epub or output/<title>/<episode-title>.txt
```

---

## Example Session

```text
[auth] Logged in as: FoggyRam2237
[info] extracting metadata…
[info] title='The Reborn Calico Princess: Dancing with the System' author='Tata' chapter=2 status=Ongoing
[info] fetching chapters: 100%|█████████████████████████████████████████████████████████████████████████| 2/2 [00:03<00:00,  1.82s/chap]

[success] Wrote EPUB: output\the-reborn-calico-princess-dancing-with-the-system\the-reborn-calico-princess-dancing-with-the-system.epub
```

---

## Tips & Troubleshooting

* **Auto-Recovery**: 401/expired tokens are now automatically handled if credentials are found in `.env` or provided via CLI.
* **Retry Handling**: Rate-limit and server-error responses trigger a fixed retry delay and dynamic throttle adjustment.
* No-op updates — when `-up` finds every server chapter already cached, existing EPUB/TXT outputs are left unchanged.
* Missing images — paste full browser Netscape cookies if chapter images use `pv-gn.novelpia.com`; those URLs may require CloudFront cookies before images can be embedded.
* HTTP debug — pass `-v` to print masked headers/params and short body previews.

---

## License

Provided “as is”, for personal use only. Do not redistribute the content. Follow Novelpia’s Terms of Service and Copyright.
