# PIA SCRAP (API): Novelpia → EPUB

Create a clean EPUB from Novelpia novels using Novelpia’s API. Given one or more `novel_id` values (e.g., `49`), the script fetches the novel data, episode list, pulls episode data, embeds images and cover, and writes a nicely structured EPUB with metadata.

> Use responsibly. Only download what your account can legitimately access. Respect Novelpia’s Terms and copyright.

---

## Features

* API-based fetch (no browser automation).
* **Parallel Fetching**: Uses `ThreadPoolExecutor` for high-performance concurrent chapter downloads.
* **Configurable Workers**: Tune chapter fetch concurrency with `-w`.
* **Incremental Updates**: Reuse per-chapter JSON files in `.cache/ `-up` to fetch only missing/new chapters.
* **Failed Chapter Retry**: Writes `failed_chapters.jsonl` and can refetch those chapters with `-r`.
* **Progress Reporting**: Real-time visual feedback with `tqdm` progress bars.
* **Flexible Chapter Selection**: Support for downloading specific chapter ranges (`-start`/`-end`).
* **Environment Variable Support**: Securely store credentials in a `.env` file via `python-dotenv`.
* **Advanced Automation**: Automatically handles rate limits (429) with smart backoff and session expiration (401) with auto re-login.
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

* Python 3.9+
* Packages: `requests`, `beautifulsoup4`, `ebooklib`, `tqdm`, `python-dotenv`

Install packages:

```bash
pip install -r requirements.txt
```

---

## CLI

```
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
* `-proxy` — HTTP/HTTPS proxy, e.g. `http://host:port`.
* `-t` — seconds to wait between episode requests (default `1.0`).
* `-w` — parallel chapter fetch workers (default `1`). Increase to speed up fetching, but beware of hitting rate limits.
* `-up` — reuse per-chapter JSON files in `.cache/` tofetch only chapters missing from cache.
* `-r` — retry chapters that failed to fetch.
* `-v` — verbose request logs.
* `-txt` — export as .txt per episode instead of EPUB.

---

## Quick Start

1) First run with your Novelpia credentials (tokens are persisted to `.api.json`):

```bash
python main.py 49 -u you@example.com -p "your-password"
```

2) Subsequent runs can reuse stored tokens (no password on the command line):

```bash
python main.py 49
```

3) Update an ongoing novel later without redownloading cached chapters:

```bash
python main.py 49 -up
```

4) Queue multiple novels with the same options:

```bash
python main.py 4565 1234 468 -up
```

5) Keep an update queue in a text file:

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

6) Retry only chapters that failed during a previous run:

```bash
python main.py 49 -r
```

### Environment Variables (.env)
You can create a `.env` file in the root directory to store your credentials securely:

```env
NOVELPIA_EMAIL=your_email@example.com
NOVELPIA_PASSWORD=your_password
```
A template is provided in `.env.example`.

---

## Output Details

Alongside the EPUB, the tool writes:

* `metadata.json` — title, author, tags (when available), total chapters, status, description, source URL.
* `chapters.jsonl` — one JSON line per chapter: index, title, URL of the web reader for that episode.
* `.cache/<episode_no>.json` — one cached chapter JSON file per episode, used by `-up` and `-r`.
* `failed_chapters.jsonl` — failed chapter records, written only when one or more chapters fail.

Output files are written under `output/<title>/`:

```
output/<title>/<title>.epub or output/<title>/<episode-title>.txt
```

---

## Example Session

```
[auth] Logged in as: FoggyRam2237
[info] extracting metadata…
[info] title='The Reborn Calico Princess: Dancing with the System' author='Tata' chapter=2 status=Ongoing
[info] fetching chapters: 100%|█████████████████████████████████████████████████████████████████████████| 2/2 [00:03<00:00,  1.82s/chap]

[success] Wrote EPUB: output\the-reborn-calico-princess-dancing-with-the-system\the-reborn-calico-princess-dancing-with-the-system.epub
```

---

## Tips & Troubleshooting

* **Auto-Recovery**: 401/expired tokens are now automatically handled if credentials are found in `.env` or provided via CLI.
* **Smart Backoff**: 429/Rate limits trigger an automatic exponential backoff and dynamic throttle adjustment.
* No-op updates — when `-up` finds every server chapter already cached, the existing EPUB/TXT files are left unchanged.
* Missing images — some external hosts may block requests; those images will remain as external links.
* HTTP debug — pass `-v` to print masked headers/params and short body previews.

---

## License

Provided “as is”, for personal use only. Do not redistribute the content. Follow Novelpia’s Terms of Service and Copyright.
