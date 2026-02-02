# Bot Usage Instructions

## 1. UserID Restriction (Whitelist)
The feature is **already included** in the latest code.
To enable it:
1.  Open `.env` file.
2.  Find `ALLOWED_CHAT_IDS=`
3.  Add the User IDs you want to allow, separated by commas.
    *   Example: `ALLOWED_CHAT_IDS=12345678,98765432`
    *   Example (Group): `ALLOWED_CHAT_IDS=-100123456789`
4.  **Save the file.**
5.  **Restart the bot** (see below).

## 2. Configuration
All settings are now in the `.env` file.
*   `BOT_TOKEN`: Your Telegram Bot Token.
*   `API_URL`: Local Bot API URL (e.g., `http://host.docker.internal:8081/bot`).
*   `PROXY`: HTTP Proxy (optional).

## 3. How to Restart
After changing `.env`, you must restart the container for changes to take effect:
```bash
docker-compose down
docker-compose up -d --build
```

## 4. Updates Implemented
*   ✅ **UserID Restriction**: Blocks unauthorized users.
*   ✅ **Smart Progress**: One message updates with progress bar.
*   ✅ **RAM Optimization**: Garbage collection + Streamed uploads.
