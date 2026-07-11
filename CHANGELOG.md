# Changelog

All notable changes to PIA Scrap will be documented in this file.

## [2.6.0] - 2026-07-11

### Added

- Dark mode support for web dashboard with manual toggle and auto-detection
- Progress bar visualization on web dashboard
- Log filtering on web dashboard
- Job history API (`GET /api/jobs`)
- Integration tests for full pipeline (API → fetch → build → output)
- Rate limiting on web API (max 4 concurrent jobs, HTTP 429 on overflow)
- `chapter_is_error()` helper in `contracts.py`
- `extract_genre_names()` shared utility in `helper.py`
- `debug` parameter on `NovelpiaClient` and `request_with_retries` (replaces global `HTTP_LOG`)
- `on_complete` callback on `run_job` and `create_job` in `web_jobs.py`
- `OutputOptions`, `FetchOptions`, `AuthOptions` dataclasses (composed into `QueueOptions`)
- CI lint job (ruff check + format) in GitHub Actions
- Docstrings on `build_epub`, `build_txt`, `run_queue`, `create_client`
- Cache hit rate logging in chapter fetch functions

### Changed

- `build_epub` and `build_txt` now share `_prepare_chapters()` orchestration
- Runner queue processing unified (epub/txt branches merged)
- Genre/tag extraction deduplicated into `extract_genre_names()`
- `fetch_with_account_policy` refactored with `_handle_fetched_result()` and `_try_ad_reward_fetch()` helpers
- `request_with_retries` documented with flow docstring
- `EpisodeItem.episode_no` and `epi_num` normalized to `int` at parse boundary
- `fastapi` and `uvicorn` moved to optional `[web]` dependency group
- Dev dependency version upper bounds added

### Fixed

- Web dashboard dark mode: input text now visible (replaced hardcoded `#fff` with CSS variables)
- `login()` and `refresh()` now raise `ApiShapeError` on unexpected API shape
- `typing_extensions` added to `requirements.txt` (was missing, broke Docker builds)
- Global mutable `HTTP_LOG` replaced with thread-safe `debug` parameter
- Dead `pass` removed from `save_config()`
- tqdm monkey-patching documentation expanded

## [2.5.0] - 2025-01-01

### Added

- Web dashboard with FastAPI
- Docker support
- PyInstaller build for Windows executable
- Ad-reward unlock flow for free accounts
- Chapter caching for interrupt-resilient downloads
- Update and retry-failed modes
- SOCKS proxy support

### Changed

- Migrated from asyncio to threading-based concurrency
- Protocol-based dependency injection for testability

## [2.0.0] - 2024-01-01

### Added

- EPUB generation with cover images and metadata
- TXT export mode
- CLI with queue file support
- Batch download with parallel workers

## [1.0.0] - 2023-01-01

### Added

- Initial release
- Basic novel scraping and EPUB output
