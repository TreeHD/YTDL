# YTDL Telegram Bot

把 YouTube（以及其他 yt-dlp 支援網站）的影片、音樂、播放清單，直接傳到 Telegram 的自架 Bot。

你只要把網址貼給 Bot；它會下載、依 Telegram 限制切檔、上傳，最後清掉暫存檔。也能訂閱頻道，在有新影片或直播時自動處理。

> [!IMPORTANT]
> 這是私人下載工具。請只下載你有權觀看、保存或分享的內容，並遵守來源平台與 Telegram 的規範。

## 先看這裡：最短上手流程

1. 到 [@BotFather](https://t.me/BotFather) 建立 Bot，取得 `BOT_TOKEN`。
2. 到 [my.telegram.org](https://my.telegram.org/) 的 **API development tools** 建立 application，取得 `TELEGRAM_API_ID` 與 `TELEGRAM_API_HASH`。
3. 在專案資料夾建立 `.env`：

   ```bash
   cp .env.example .env
   ```

4. 編輯 `.env`，至少填好這三個值：

   ```env
   BOT_TOKEN=貼上_BotFather_給你的_token
   TELEGRAM_API_ID=你的_api_id
   TELEGRAM_API_HASH=你的_api_hash
   ```

5. 建置並啟動：

   ```bash
   docker build -t ghcr.io/treehd/ytdl:latest .
   docker-compose up -d --build --force-recreate
   docker-compose logs -f ytdl-bot
   ```

6. 在 Telegram 對 Bot 輸入 `/start`，或直接貼一個影片網址。

如果日誌最後出現 `Bot is running...`，就完成了。按 `Ctrl+C` 只會停止看日誌，不會停止容器。

## 它是怎麼運作的？

```mermaid
flowchart LR
    U[你在 Telegram 貼網址或輸入指令] --> H[handlers.py<br/>檢查權限、建立一則狀態訊息]
    H --> Q[下載佇列<br/>request_queue / playlist_queue]
    Q --> P[queue_processor.py<br/>依序下載、切檔、清理]
    P --> D[downloader.py<br/>yt-dlp + Cookie + Proxy]
    D --> S[(來源網站)]
    D --> F[downloads/<br/>暫存媒體檔]
    F --> UP[uploader.py<br/>裁縮圖、切檔、上傳]
    UP --> T[Telegram]
    T --> U

    M[subscription.py<br/>定期檢查訂閱頻道] --> H
    D -.遇到 geo 限制就換下一個 proxy.-> W[PROXY_LIST]
    W -.可選.-> WP[warp-proxy]
```

用白話說：

1. Bot 先回覆一則進度訊息，例如「正在分析網址」。
2. 任務排進佇列，避免多個大檔同時塞滿記憶體和硬碟。
3. yt-dlp 下載媒體；必要時會使用 Cookie 或依序換 proxy。
4. 檔案過大時，FFmpeg 會先切成 Telegram 能接受的大小。
5. Bot 上傳完成後刪除暫存檔，只保留訂閱資料庫與你放的 Cookie。

### 一般影片、播放清單與直播的差異

| 類型 | Bot 會做什麼 |
|---|---|
| 一般影片／音樂 | 排進一般佇列，下載後上傳。 |
| 播放清單 | 排進播放清單佇列，逐部下載，避免大量檔案同時佔空間。 |
| 訂閱新影片 | 依 `SUBSCRIPTION_CHECK_INTERVAL`（預設 300 秒）檢查頻道，有新片就自動排入佇列。 |
| 訂閱直播 | 開始時同時錄兩路：streamlink 從現在錄作為保險；yt-dlp 嘗試從直播開頭封存。從頭錄製穩定 10 分鐘後，會停掉 streamlink 並刪除它的重複暫存，最後只傳從頭錄的完整版。沒有 DVR/VOD 時，streamlink 會繼續錄。 |

直播流程如下：

```mermaid
flowchart TD
    L[偵測到直播] --> A[建立同一則直播狀態訊息]
    A --> N[streamlink：從現在開始錄]
    A --> B[yt-dlp --live-from-start：從開頭封存]
    B --> C{來源有 DVR / VOD？}
    C -->|有| D[兩路暫時同錄]
    D --> J{從頭錄製穩定 10 分鐘？}
    J -->|是| K[停掉 streamlink／刪除重複暫存]
    K --> G[只保留從頭錄的完整版]
    J -->|否／中途失敗| N
    C -->|沒有| E[顯示提醒，結束從頭封存]
    E --> N
    N --> F[目前直播分段上傳]
    F --> H[直播結束或按 Stop & Upload]
    G --> H
```

## 需要準備什麼？

- 一台可執行 Docker Compose 的 Linux、NAS、VPS 或電腦。
- Docker Engine 與 Docker Compose。
- 一個 Telegram Bot token。
- 若要使用內建 Local Bot API（建議）：Telegram API ID 與 API Hash。
- 建議預留足夠磁碟空間；影片會先下載到硬碟，再上傳到 Telegram。

先確認 Docker 可用：

```bash
docker --version
docker-compose --version
```

## 第一次安裝：照著做就好

### 1. 取得程式碼

```bash
git clone <你的-repository-url> YTDL
cd YTDL
```

如果你已經有這個資料夾，直接進入即可：

```bash
cd /mnt/HDD/YTDL
```

### 2. 建立 Telegram Bot

1. 打開 [@BotFather](https://t.me/BotFather)。
2. 輸入 `/newbot`，照指示設定名稱與 username。
3. 複製 BotFather 給你的 token。
4. 不要把 token 貼到公開聊天室、README、Git commit 或截圖。

### 3. 取得 Telegram API ID 與 API Hash

1. 開啟 [my.telegram.org](https://my.telegram.org/) 並用你的 Telegram 帳號登入。
2. 點選 **API development tools**。
3. 建立 application。
4. 複製 `api_id` 與 `api_hash`。

這兩個值供本專案的 Local Bot API Server 使用，讓 Bot 能處理接近 2GB 的檔案。

### 4. 建立並填寫 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

用任何文字編輯器打開 `.env`：

```env
# 必填
BOT_TOKEN=123456789:AAExampleReplaceThisWithYourRealToken
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=請貼上_api_hash

# 建議：只讓自己或指定群組使用。留空代表任何人都能用。
ALLOWED_CHAT_IDS=123456789

# 內建 WARP proxy；不需要 proxy 可把 PROXY_LIST 留空。
PROXY_LIST=socks5://warpuser:warppass@warp-proxy:1080

# 暫存下載檔預估可使用的硬碟上限（GB）。0 表示不檢查。
MAX_DISK_GB=10
```

取得自己的 Telegram chat ID，可使用可信的查詢 Bot；群組 ID 通常是負數。`ALLOWED_CHAT_IDS` 可放多個值，以逗號分隔：

```env
ALLOWED_CHAT_IDS=123456789,-1001234567890
```

### 5. 建置並啟動

這份 `docker-compose.yml` 使用已命名的 image `ghcr.io/treehd/ytdl:latest`。因此你**有修改本機 Python 程式碼時**，必須先執行第一行，否則 Docker 可能仍跑舊 image。

```bash
docker build -t ghcr.io/treehd/ytdl:latest .
docker-compose up -d --build --force-recreate
docker-compose ps
docker-compose logs -f ytdl-bot
```

確認服務應包含：

| 容器 | 用途 |
|---|---|
| `ytdl-bot` | 接收 Telegram 指令、下載、上傳。 |
| `telegram-bot-api` | 本機 Telegram Bot API，支援大檔上傳。 |
| `warp-proxy` | 可選的 Cloudflare WARP proxy。 |
| `autoheal` | 偵測到 Local Bot API 不健康時自動重啟它。 |

### 6. 設定 BotFather 的指令選單

在 [@BotFather](https://t.me/BotFather) 輸入 `/setcommands`，選擇你的 Bot 後，把下面整段直接貼上。**每行開頭不要加 `/`**：

```text
start - 使用說明
help - 顯示使用說明
download - 下載影片（預設 1080p）
1080 - 下載 1080p 影片
720 - 下載 720p 影片
480 - 下載 480p 影片
360 - 下載 360p 影片
240 - 下載 240p 影片
music - 下載 M4A 音訊
mp3 - 下載 MP3 音訊
playlist - 下載播放清單
settings - 設定預設格式與解析度
subscribe - 訂閱頻道新影片
sublive - 訂閱頻道直播
unsubscribe - 取消新影片訂閱
unsublive - 取消直播訂閱
subscriptions - 查看所有訂閱
upgrade - 更新 yt-dlp
```

`/subvideo`、`/unsubvideo`、`/subs` 也仍可使用；它們是相容用別名，因此不放進指令選單。

## 日常怎麼用？

### 最簡單：直接貼網址

直接傳送網址給 Bot，會依 `/settings` 的設定下載。預設是 1080p 影片。

```text
https://www.youtube.com/watch?v=example
```

### 指令速查表

| 指令 | 怎麼用 | 結果 |
|---|---|---|
| `/download <網址>` | `/download https://…` | 用 1080p 下載影片。 |
| `/1080`、`/720`、`/480`、`/360`、`/240` | `/720 https://…` | 指定影片最高解析度。 |
| `/music <網址>` | `/music https://…` | 下載 M4A 音訊。 |
| `/mp3 <網址>` | `/mp3 https://…` | 下載 MP3 音訊。 |
| `/playlist <網址> [畫質]` | `/playlist https://… 720` | 逐部下載播放清單。 |
| `/settings` | `/settings` | 按按鈕設定直接貼網址時的預設格式與畫質。 |
| `/subscribe <頻道網址> [畫質]` | `/subscribe https://youtube.com/@example 1080` | 訂閱新影片。 |
| `/sublive <頻道網址> [畫質]` | `/sublive https://youtube.com/@example 720` | 訂閱直播並自動錄製。 |
| `/unsubscribe <頻道網址>` | `/unsubscribe https://youtube.com/@example` | 取消新影片訂閱。 |
| `/unsublive <頻道網址>` | `/unsublive https://youtube.com/@example` | 取消直播訂閱。 |
| `/subscriptions` | `/subscriptions` | 列出目前所有訂閱。 |
| `/upgrade` | `/upgrade` | 手動更新 yt-dlp nightly；有下載任務時會拒絕執行。 |

直播進度訊息會有兩個按鈕：

- **Stop & Upload**：停止兩條錄製線，將目前收到的片段上傳。
- **Cancel**：取消錄製並清掉未完成的暫存資料。

## 設定說明

`.env.example` 是完整範本。下表是最常會調整的項目。

| 變數 | 要不要填 | 預設／範例 | 用途 |
|---|---:|---|---|
| `BOT_TOKEN` | 必填 | `123:ABC…` | BotFather 給的 token。 |
| `TELEGRAM_API_ID` | Local Bot API 必填 | `12345678` | 從 my.telegram.org 取得。 |
| `TELEGRAM_API_HASH` | Local Bot API 必填 | `abcdef…` | 從 my.telegram.org 取得。 |
| `API_URL` | 建議保持範例值 | `http://host.docker.internal:8081/bot` | 指向本機 Telegram Bot API。若改為官方 API，檔案上傳限制會降到約 49MB。 |
| `ALLOWED_CHAT_IDS` | 強烈建議 | `123456789,-100…` | 白名單；留空代表所有人都能使用。 |
| `PROXY` | 選填 | `socks5://host:1080` | 單一 proxy，優先使用。 |
| `PROXY_LIST` | 選填 | `proxy1,proxy2` | 以逗號分隔的 proxy 清單；geo 限制會直接嘗試下一個。 |
| `WARP_USERNAME` / `WARP_PASSWORD` | 使用內建 WARP 時 | `warpuser` / `warppass` | 必須和 `PROXY_LIST` 中的帳密相同。 |
| `MAX_DISK_GB` | 建議填 | `10` | 一般下載前的暫存空間預估上限；`0` 為不檢查。 |
| `SUBSCRIPTION_CHECK_INTERVAL` | 選填 | `300` | 訂閱頻道輪詢秒數。`300` 是 5 分鐘。 |
| `YTDLP_AUTO_UPDATE` | 選填 | `true` | 容器啟動時更新 yt-dlp nightly。 |
| `YTDLP_DAILY_UPDATE` | 選填 | `true` | 每天自動更新 yt-dlp。 |
| `YTDLP_UPDATE_TIME` | 選填 | `04:00` | 每日更新時間。 |
| `YTDLP_UPDATE_TIMEZONE` | 選填 | `Asia/Taipei` | 每日更新的時區。 |

### Proxy 與 WARP

不需要 proxy：

```env
PROXY=
PROXY_LIST=
```

使用多個 proxy：

```env
PROXY_LIST=socks5://proxy-a:1080,http://proxy-b:8080
```

使用內建 WARP：

```env
PROXY_LIST=socks5://warpuser:warppass@warp-proxy:1080
WARP_USERNAME=warpuser
WARP_PASSWORD=warppass
```

遇到明確的地區限制（geo block）時，Bot 只會換下一個 proxy，不會重建 WARP IP。bot 偵測、403、timeout 等其他可重試錯誤，在正在使用 `warp-proxy` 時才可能要求 WARP rotation。

### YouTube Cookie：遇到 403、bot 偵測時再加

1. 將瀏覽器中已登入 YouTube 的 Cookie 匯出成 **Netscape cookies.txt** 格式。
2. 放到專案中的 `data/cookies.txt`：

   ```text
   YTDL/
   └── data/
       └── cookies.txt
   ```

3. 重建／重啟 Bot：

   ```bash
   docker-compose up -d --build --force-recreate
   ```

Cookie 等同登入憑證，絕對不要傳給別人，也不要提交到 Git。`.gitignore` 已排除 `data/`。

## 部署、更新與維護

### 修改程式碼後部署（最重要）

```bash
cd /mnt/HDD/YTDL
docker build -t ghcr.io/treehd/ytdl:latest .
docker-compose up -d --build --force-recreate
docker-compose logs -f ytdl-bot
```

### 只更新已發布的容器 image

如果你沒有改本機程式碼、只想取得 registry 的新版本：

```bash
docker-compose pull
docker-compose up -d --force-recreate
```

### 常用維護指令

```bash
# 查看容器是否正常
docker-compose ps

# 持續看 Bot 日誌
docker-compose logs -f ytdl-bot

# 看 Local Bot API 日誌
docker-compose logs -f telegram-bot-api

# 重啟服務，不刪資料
docker-compose restart

# 停止服務，不刪資料庫或 Cookie
docker-compose down
```

### 備份什麼？

| 路徑 | 是否要備份 | 原因 |
|---|---:|---|
| `.env` | 是 | Bot token、Telegram API 資訊與設定。 |
| `data/subscriptions.db` | 是 | 所有頻道訂閱。 |
| `data/cookies.txt` | 視需要 | YouTube Cookie；請加密保存。 |
| `downloads/` | 通常不用 | 暫存下載檔，啟動時會清空。 |

> [!WARNING]
> Bot 啟動時會清空 `downloads/`。不要把唯一的一份重要檔案放在這個目錄，也不要在下載進行中任意重建容器。

## 專案結構

```text
YTDL/
├── bot.py                 # 啟動 Bot、註冊指令、啟動背景任務
├── handlers.py            # 指令、按鈕、權限與網址解析入口
├── queue_processor.py     # 下載佇列、直播錄製、切檔、清理
├── downloader.py          # yt-dlp、Cookie、proxy 與 WARP rotation 判斷
├── uploader.py            # 上傳 Telegram、縮圖裁切、檔案切割
├── subscription.py        # 頻道新片／直播監控
├── database.py            # SQLite 訂閱資料
├── config.py              # .env 設定讀取與空間檢查
├── upgrader.py            # yt-dlp nightly 更新排程
├── telegram_utils.py      # Telegram API 重試與 flood-control 處理
├── docker-compose.yml     # 四個容器的編排
├── Dockerfile             # ytdl-bot 本機 image 建置方式
├── .env.example           # 可複製的設定範本
├── data/                  # 訂閱資料庫與選用 Cookie（不進 Git）
└── downloads/             # 暫存下載檔（不進 Git）
```

## 常見問題

### Bot 沒反應

```bash
docker-compose ps
docker-compose logs --tail=100 ytdl-bot
```

最常見原因：`BOT_TOKEN` 打錯、容器沒有啟動、或 `ALLOWED_CHAT_IDS` 沒有包含你的 chat ID。

### 影片只能傳約 49MB

通常代表 Bot 正在使用官方 Telegram API，而不是 Local Bot API。檢查 `.env`：

```env
API_URL=http://host.docker.internal:8081/bot
TELEGRAM_API_ID=你的值
TELEGRAM_API_HASH=你的值
```

再確認 Local Bot API 正常：

```bash
docker-compose ps telegram-bot-api
docker-compose logs --tail=100 telegram-bot-api
```

### 出現 YouTube 403、要求登入，或說你是 bot

先確認 yt-dlp 是最新版本：

```text
/upgrade
```

仍然失敗時，再放入 `data/cookies.txt`，然後重啟容器。Cookie 必須是 Netscape 格式。

### Geo block／這個地區不能看

在 `.env` 放入一個或多個適合地區的 proxy：

```env
PROXY_LIST=socks5://taiwan-proxy:1080,socks5://japan-proxy:1080
```

Bot 會依順序嘗試。Geo block 不會重啟 WARP，只會跳下一個 proxy。

### 我改了 Python，重啟後卻完全沒變

這是最常見的部署問題。`docker-compose.yml` 指定的是 image，不會自動把工作目錄裡的 Python 檔塞進容器。請重新 build 本機 image 後再重建容器：

```bash
docker build -t ghcr.io/treehd/ytdl:latest .
docker-compose up -d --build --force-recreate
```

### 硬碟快滿了

1. 先停止 Bot：`docker-compose down`
2. 查看 `downloads/` 與 Docker 使用空間：

   ```bash
   du -sh downloads data
   docker system df
   ```

3. 確認沒有仍要保留的檔案後，再手動清理 `downloads/` 的舊暫存檔。

## 測試

若要在主機上跑測試，先建立 Python 虛擬環境：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -s tests
```

## 使用的開源專案

- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [streamlink](https://github.com/streamlink/streamlink)
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [FFmpeg](https://ffmpeg.org/)
- [Telegram Bot API Server](https://github.com/tdlib/telegram-bot-api)
