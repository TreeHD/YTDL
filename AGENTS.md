# Developer Guidelines for AI Agents (YTDL Telegram Bot)

This document serves as the absolute source of truth for AI assistants/agents making modifications to the YTDL Telegram Bot repository. Follow these architectural decisions, rules, and best practices stringently to preserve the code's stability, UX, and performance.

---

## 📌 1. Core Architecture & Workflow Constraints

### `handlers.py` (Command Interception)
- **Role**: Intercepts user commands (`/music`, `/1080`, `/playlist`, etc.), enforces permissions, and parses arguments.
- **UX Rule**: When a user initiates a download, send an initial `status_msg` (e.g., "🔍 Analyzing URL..."). **Do NOT edit or spam multiple individual messages down the line.** Instead, push the `status_msg` instance into the `request_queue` or `playlist_queue` alongside the URL tuple.

### `queue_processor.py` (Async Task Manager)
- **Role**: Handles sequential and single downloads so RAM and disk space are not instantly overwhelmed.
- **UX Rule**: Re-use the `status_msg` passed from `handlers.py` for all UX updates ("Downloading 50%", "Checking file size", "Uploading part 1"). Update the single message box sequentially.
- **Robustness Rule**: Wrap Telegram message edits (`edit_text`, `send_message`, `send_video`, etc.) in the custom `tg_retry` async function. This function intercepts **`RetryAfter` (Telegram Flood Control/Rate Limiting)**, waits the designated seconds, and seamlessly retries the API call. *Never call `bot.send_message` directly without it in high-frequency loops.*

### `downloader.py` (`yt-dlp` Abstraction)
- **Geo-Restriction Rule**: The YouTube DL client is bound to `get_proxy_list()`. Iterate over proxies. If `yt-dlp` raises an exception that strings-matches `GEO_RESTRICTION_ERRORS`, move on to the next proxy quietly until one secures the download.
- **Filesystem Rule**: Cap filenames using the `%(title).80s` outtmpl constraint. Windows systems violently reject filenames resulting in `[Errno 36] Filename too long` if an arbitrary YouTube title + unique ID goes beyond 255 chars in path length.

### `uploader.py` (Multipart Chunk Uploads)
- **Thumbnail Rule**: Telegram dynamically stretches non-square custom thumbnails. **Before appending ANY thumbnail** to a multipart payload or standard bot API, wrap the path in `crop_to_square(thumb_path)`. This function utilizes Python's `Pillow` library to extract a 1:1 rectangular slice.
- **File Splitting**: Identifies large media and triggers `split_video(max_size_bytes)` to cleanly segment the file for localized Telegram API limits.

---

## 📌 2. Concurrency & Blocking Operations
Telegram bots using `python-telegram-bot` v20+ are strictly natively asynchronous (`asyncio`).
- **Rule**: `yt-dlp` creates synchronously blocking socket connections and massive I/O loops. **ALWAYS** invoke its extraction hooks using `loop.run_in_executor(None, lambda: ...)`:
  ```python
  loop = asyncio.get_running_loop()
  file_path = await loop.run_in_executor(None, lambda: download_content(url))
  ```
- Failure to do this will pause the entire Telegram bot polling reactor across all active users while a video downloads.

---

## 📌 3. Local Bot API vs Standard Bot API
The bot switches logics depending on whether `API_URL` acts globally or points to a `.local` internal Docker network proxy.
- **If `api.telegram.org` is NOT structurally present** in the `.env` var `API_URL`, the framework assumes a Local Server configuration.
  - Action: Uploads will funnel into the generic raw HTTP endpoints of `uploader.py` (`upload_video_streaming`). Splitting constraints move up to **2GB (`LOCAL_API_LIMIT`)**.
- **If Standard API (Vanilla)**:
  - Action: Upload wrapper delegates to the `Application.bot` commands inside `queue_processor.py`. Splitting constraints drop drastically to **50MB (`STANDARD_API_LIMIT`)**.

---

## 📌 4. Mandatory Error Handling Practices
1. **Unbound Local Errors**: Watch Python scoping. Do NOT initialize imports locally inside functions (e.g., `import glob` midway through `process_queue`). Keep dependency declarations at module heads.
2. **Subprocess Death Scenarios**: If you fire `asyncio.create_subprocess_exec` (like ffmpeg live-recording streams), assess `process.returncode` upon exit. If errors are present (`!= 0`), cleanly `update_status_msg` before returning. DO NOT SWALLOW silent crashes or delete the message bubble completely.
3. **Aggressive Cleanup**: Ensure downloaded files (`.mp4`, `.mkv`, `.m4a`, and cropped thumbnails) are nuked from `./downloads/` inside `finally:` blocks. Due to multi-threading race conditions, `os.remove` must *always* be wrapped:
   ```python
   try:
       os.remove(file_path)
   except:
       pass
   ```
4. **Memory Leaks**: `gc.collect()` should be called following large asynchronous file I/O thread drops. Ensure the `finally:` block of `queue_processor/process_queue` contains it.

---

## 📌 5. Standard Deployment Test
Every time you structurally change logic inside Python files:
1. Verify Syntax.
2. Remind the USER to flush and deploy the container overlay.
   ```bash
   docker-compose up -d --build --force-recreate
   docker-compose logs -f ytdl-bot
   ```
