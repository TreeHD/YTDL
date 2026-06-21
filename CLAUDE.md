# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Deploy

```bash
docker compose build ytdl-bot && docker compose up -d ytdl-bot
docker compose logs -f ytdl-bot
```

## Tests

```bash
python -m pytest tests/
python -m pytest tests/test_downloader_utils.py  # single test file
```

## Architecture

A Telegram bot that downloads videos/audio via yt-dlp, runs as a Docker container alongside a local Telegram Bot API server and a Cloudflare WARP proxy.

### Data Flow

1. **handlers.py** — Parses commands, validates permissions, creates a `status_msg`, pushes `(chat_id, url, message_id, quality, status_msg, ...)` tuples into async queues
2. **queue_processor.py** — Consumes queues sequentially. Runs yt-dlp in `run_in_executor` (blocking I/O). Manages live stream recording via streamlink subprocesses. Handles segmented upload for large files.
3. **downloader.py** — yt-dlp wrapper with proxy rotation. `_apply_cookie()` is the central hook for all yt-dlp invocations (injects cookies, EJS runtime, logging). `RETRY_ERRORS` + `is_retryable_error()` control which errors trigger proxy fallover and WARP IP rotation.
4. **uploader.py** — Handles chunked streaming upload to Local Bot API, file splitting via ffmpeg, thumbnail cropping.
5. **subscription.py** — `SubscriptionMonitor` polls channels every N seconds, deduplicates via `is_video_processed()` in SQLite, tracks active live recordings to prevent duplicates.

### Key Patterns

- **Single status message**: One message per task, edited in-place. Never spam multiple messages. Pass `status_msg` through the queue.
- **tg_retry()**: All Telegram API calls in high-frequency paths must use this wrapper (handles flood control + NetworkError).
- **Proxy rotation**: `get_proxy_list()` returns ordered list. On retryable errors, iterate to next proxy. WARP proxy failures trigger `/restart` endpoint for IP rotation.
- **yt-dlp is synchronous**: Always wrap in `loop.run_in_executor(None, lambda: ...)`.
- **Memory management**: `PYTHONMALLOC=malloc` in Dockerfile (musl returns pages to OS on free). `_free_memory()` called after every task.
- **Live stream recording**: Uses streamlink (not yt-dlp) for real-time recording. Segments at 1.9GB via binary TS concatenation. Graceful shutdown: SIGINT → SIGTERM → SIGKILL.
- **From-start download**: Uses yt-dlp `--live-from-start`. Monitors file growth, splits at 1.9GB boundaries by reading byte ranges from the growing file while yt-dlp keeps writing.

### Environment & Config

- `config.py` reads all settings from env vars (BOT_TOKEN, API_URL, PROXY_LIST, ALLOWED_CHAT_IDS, etc.)
- Cookie file at `./data/cookies.txt` enables EJS challenge solver (deno runtime at `/opt/deno/bin`, only added to PATH when cookies are present)
- Local Bot API allows uploads up to ~1.95GB; standard API caps at 49MB
- Docker compose: ytdl-bot (mem_limit 512m, swap to 1g), telegram-bot-api, warp-proxy, autoheal

### Database

SQLite at `./data/subscriptions.db`. Tables: `subscriptions` (channel monitoring), `processed_videos` (deduplication).

## Language

The user communicates in Traditional Chinese (zh-TW). Respond in Chinese unless code/technical terms are involved.
