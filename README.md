# YTDL Telegram Bot

Telegram bot for downloading videos and audio from YouTube and other yt-dlp supported platforms.

## Features

- Video download in multiple qualities (1080p, 720p, 480p, 240p)
- Audio extraction in M4A or MP3 format
- Playlist batch download
- Channel subscription with automatic new video notifications
- Large file support up to 2GB via Local Bot API Server
- Auto-splitting for oversized files
- Proxy rotation and Cloudflare WARP integration
- Cookie jar support to bypass YouTube bot detection
- Configurable disk usage limits
- Download queue with cancel support

## Quick Start

1. Copy and edit the environment file:
   ```bash
   cp .env.example .env
   ```

2. Start all services:
   ```bash
   docker-compose up -d
   ```

3. Check logs:
   ```bash
   docker-compose logs -f ytdl-bot
   ```

## Services

| Service | Description |
|---------|-------------|
| `ytdl-bot` | The main bot application |
| `telegram-bot-api` | Local Bot API server for large file uploads |
| `warp-proxy` | Cloudflare WARP SOCKS5 proxy for geo-restriction bypass |

## Commands

| Command | Description |
|---------|-------------|
| `/start`, `/help` | Show help |
| `/settings` | Bot settings |
| `/1080`, `/720`, `/480`, `/240` | Download video at specified quality |
| `/music <url>` | Download audio (M4A) |
| `/mp3 <url>` | Download audio (MP3) |
| `/playlist <url> [quality]` | Download playlist |
| `/subscribe <url> [quality]` | Subscribe to channel |
| `/unsubscribe <url>` | Unsubscribe from channel |
| `/subscriptions`, `/subs` | List subscriptions |
| `/upgrade` | Upgrade yt-dlp to nightly when no download task is active |

Sending a URL directly downloads at 1080p by default.

## Configuration

See [`.env.example`](.env.example) for all available options.

yt-dlp is upgraded to the nightly channel before the bot starts by default.
Set `YTDLP_AUTO_UPDATE=false` to disable the startup upgrade. The `/upgrade`
command remains available to authorized users regardless of this setting.
The command refuses to run while any queued, downloading, uploading, playlist,
or live task is active.

The bot also checks for a nightly update every day at `04:00` in
`Asia/Taipei`. Configure this with `YTDLP_UPDATE_TIME` (`HH:MM`) and
`YTDLP_UPDATE_TIMEZONE` (IANA timezone name), or disable it with
`YTDLP_DAILY_UPDATE=false`. If any queued, downloading, uploading, or live task
is active at the scheduled time, that day's update is skipped until tomorrow.

### Proxy Setup

Single proxy:
```env
PROXY=socks5://user:pass@host:1080
```

Multiple proxies (tried in order on geo-restriction errors):
```env
PROXY_LIST=socks5://proxy1:1080,http://proxy2:8080
```

To use the built-in WARP proxy:
```env
PROXY_LIST=socks5://warpuser:warppass@warp-proxy:1080
```

### Cookie Jar (YouTube 403 Fix)

Export YouTube cookies in Netscape format and place at `./data/cookies.txt`. The existing volume mount exposes it to the container automatically. Optional — the bot works without it.

### User Restriction

```env
ALLOWED_CHAT_IDS=123456789,-100987654321
```

Leave empty to allow all users.

## Project Structure

```
bot.py              Main entry point
config.py           Configuration and environment loading
database.py         SQLite database for subscriptions
downloader.py       yt-dlp download logic
uploader.py         Telegram upload logic
handlers.py         Bot command handlers
queue_processor.py  Download queue processing
subscription.py     Channel subscription monitor
```

## Data Persistence

| Path | Content |
|------|---------|
| `./downloads/` | Temporary downloaded files |
| `./data/subscriptions.db` | Channel subscriptions (SQLite) |
| `./data/cookies.txt` | Optional yt-dlp cookie jar |

## Testing

```bash
./run_tests.sh
```

## Credits

- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [FFmpeg](https://ffmpeg.org/)
