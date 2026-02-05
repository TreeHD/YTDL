# 🎬 YouTube Downloader Telegram Bot

A powerful Telegram bot for downloading videos and audio from YouTube and other platforms supported by yt-dlp.

Developed by using AntiGravity with Claude 4.5 Opus.

## ✨ Features

- **Video Download** - Download videos in various qualities (1080p, 720p, 480p, 240p)
- **Audio Download** - Extract audio in M4A format
- **Playlist Download** - Download entire playlists at once
- **Channel Subscription** - Subscribe to channels and get new videos automatically
- **Large File Support** - Supports files up to 2GB via Local Bot API Server
- **Auto-Splitting** - Automatically splits large files into parts
- **Proxy Rotation** - Bypass geo-restrictions with multiple proxy support
- **Disk Management** - Set maximum disk usage limits
- **Cancel Downloads** - Cancel ongoing downloads with a button
- **Queue System** - Process multiple downloads in sequence

## � Project Structure

```
YTDL/
├── bot.py              # Main entry point
├── config.py           # Configuration and environment loading
├── database.py         # SQLite database for subscriptions
├── downloader.py       # yt-dlp download functions
├── uploader.py         # Telegram upload functions
├── handlers.py         # Bot command handlers
├── queue_processor.py  # Queue processing logic
├── subscription.py     # Channel subscription monitoring
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

## �🚀 Quick Start

### Prerequisites

- Docker & Docker Compose
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Telegram API credentials (from [my.telegram.org](https://my.telegram.org))

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/ytdl-telegram-bot.git
   cd ytdl-telegram-bot
   ```

2. **Configure environment variables**
   ```bash
   cp .env.example .env
   nano .env
   ```

3. **Edit `.env` with your settings:**
   ```env
   BOT_TOKEN=your_bot_token_here
   ALLOWED_CHAT_IDS=               # Leave empty to allow all users
   API_URL=http://host.docker.internal:8081/bot
   PROXY=                          # Optional: single proxy
   PROXY_LIST=                     # Optional: comma-separated proxies
   MAX_DISK_GB=10                  # Maximum disk usage in GB
   SUBSCRIPTION_CHECK_INTERVAL=300 # Check for new videos every 5 minutes
   TELEGRAM_API_ID=your_api_id
   TELEGRAM_API_HASH=your_api_hash
   ```

4. **Start the bot**
   ```bash
   docker-compose up -d --build
   ```

5. **Check logs**
   ```bash
   docker-compose logs -f ytdl-bot
   ```

## 📖 Usage

### Basic Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help message |
| `/help` | Show help message |

### Video Download

Simply send a video URL to download in 1080p (default):
```
https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

Or use quality-specific commands:

| Command | Quality |
|---------|---------|
| `/1080 <URL>` | 1080p |
| `/720 <URL>` | 720p |
| `/480 <URL>` | 480p |
| `/240 <URL>` | 240p |

**Example:**
```
/720 https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

### Audio Download

Use the `/music` command to download audio only (M4A format):
```
/music https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

Or click the "🎵 Download Audio" button after a video is uploaded.

### Playlist Download

Download entire playlists:
```
/playlist https://www.youtube.com/playlist?list=PLxxxxx
```

With custom quality:
```
/playlist https://www.youtube.com/playlist?list=PLxxxxx 720
```

### Channel Subscription

Subscribe to a YouTube channel to get new videos automatically:
```
/subscribe https://www.youtube.com/@ChannelName
```

With custom quality:
```
/subscribe https://www.youtube.com/@ChannelName 720
```

Manage subscriptions:
```
/subscriptions      # List your subscriptions
/subs               # Alias for /subscriptions
/unsubscribe https://www.youtube.com/@ChannelName
```

### Cancel Download

During download, click the "❌ Cancel" button to stop the current download.

## ⚙️ Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BOT_TOKEN` | Telegram Bot Token (required) | - |
| `ALLOWED_CHAT_IDS` | Comma-separated user/group IDs allowed to use the bot | Empty (all allowed) |
| `API_URL` | Local Bot API Server URL | `https://api.telegram.org/bot` |
| `PROXY` | Single proxy URL | - |
| `PROXY_LIST` | Comma-separated proxy URLs for rotation | - |
| `MAX_DISK_GB` | Maximum disk usage in GB (0 = unlimited) | `0` |
| `SUBSCRIPTION_CHECK_INTERVAL` | How often to check for new videos (seconds) | `300` |
| `TELEGRAM_API_ID` | Telegram API ID for Local Bot API | - |
| `TELEGRAM_API_HASH` | Telegram API Hash for Local Bot API | - |

### Proxy Configuration

**Single Proxy:**
```env
PROXY=http://user:pass@proxy.example.com:8080
```

**Multiple Proxies (for geo-restriction bypass):**
```env
PROXY_LIST=http://proxy1:8080,socks5://proxy2:1080,http://proxy3:8080
```

The bot will try each proxy in order when encountering geo-restriction errors.

### User Restriction

To restrict bot usage to specific users:
```env
ALLOWED_CHAT_IDS=123456789,987654321,-100123456789
```

- Positive numbers = User IDs
- Negative numbers starting with `-100` = Group IDs

Leave empty to allow all users.

## 🏗️ Architecture

```
┌─────────────────┐     ┌─────────────────────┐
│   Telegram      │────▶│  Local Bot API      │
│   Client        │     │  Server (Optional)  │
└─────────────────┘     └──────────┬──────────┘
                                   │
                        ┌──────────▼──────────┐
                        │    YTDL Bot         │
                        │  ┌───────────────┐  │
                        │  │  Queue System │  │
                        │  └───────┬───────┘  │
                        │          │          │
                        │  ┌───────▼───────┐  │
                        │  │   yt-dlp      │  │
                        │  │   + FFmpeg    │  │
                        │  └───────────────┘  │
                        │          │          │
                        │  ┌───────▼───────┐  │
                        │  │   SQLite DB   │  │
                        │  │ (Subscriptions)│  │
                        │  └───────────────┘  │
                        └─────────────────────┘
```

### Data Persistence

- **Downloads**: `./downloads/` - Downloaded video files (temporary)
- **Database**: `./data/subscriptions.db` - Channel subscriptions (SQLite)

### Local Bot API Server

The Local Bot API Server allows:
- Upload files up to **2GB** (vs 50MB standard limit)
- Faster uploads (direct disk access)
- Lower RAM usage (streaming uploads)

## 🧪 Testing

You can run automated tests to ensure everything is working correctly.

### Run tests inside Docker
```bash
./run_tests.sh
```

### Run tests locally
Requires `pytest` and dependencies installed:
```bash
pip install -r requirements.txt  # If you create one
python -m unittest discover tests
```

## 🔧 Troubleshooting

### Bot not responding
```bash
docker-compose logs -f ytdl-bot
```

### "Not enough disk space" error
- Check `MAX_DISK_GB` setting
- Clear old downloads: `rm -rf ./downloads/*`

### Geo-restriction errors
- Add proxies to `PROXY_LIST`
- Use VPN on your server

### Upload timeout
- Check if Local Bot API is running
- Increase timeout values in code

### Subscriptions not working
- Check `SUBSCRIPTION_CHECK_INTERVAL` setting
- Verify database exists: `ls ./data/subscriptions.db`

## 📝 License

MIT License

## 🙏 Credits

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - Video downloading
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) - Telegram API
- [FFmpeg](https://ffmpeg.org/) - Media processing
 
