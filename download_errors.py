"""Conservative yt-dlp diagnostics: a 403 alone never proves expired cookies."""


def classify_error(error):
    text = str(getattr(error, 'source', error)).lower().replace('’', "'")
    if any(p in text for p in ('cookies are no longer valid', 'cookies have expired',
                                'cookie has expired', 'cookies are invalid')):
        return 'cookie_expired'
    if any(p in text for p in ('does not look like a netscape', 'failed to load cookies',
                                'invalid netscape', 'invalid cookies file')):
        return 'cookie_format'
    if any(p in text for p in ("confirm you're not a bot", 'confirm you are not a bot',
                                'http error 403', 'http error 429')):
        return 'blocked'
    if any(p in text for p in ('proxyerror', 'proxy error', 'proxy authentication required',
                                'http error 407', 'socks', 'tunnel connection failed')):
        return 'proxy'
    if any(p in text for p in ('not available in your country',
                                'not made this video available in your country',
                                'geo-restricted', 'geographic restriction')):
        return 'geo'
    if any(p in text for p in ('sign in to confirm your age', 'cookies are required',
                                'login required', 'login_required', 'sign in to',
                                'private video', 'use --cookies', 'authentication required')):
        return 'auth'
    if any(p in text for p in ('timed out', 'timeout', 'connection refused',
                                'connection reset', 'could not resolve host',
                                'name or service not known', 'temporary failure in name resolution')):
        return 'network'
    return 'unknown'


def error_hint(error):
    return {
        'cookie_expired': '網站回報 cookies 已失效／遭撤銷。請重新登入並匯出 cookie.txt，由管理者私訊上傳給 bot；不應視為 proxy 故障。',
        'cookie_format': 'Cookies 檔案格式錯誤。請重新匯出 Netscape 格式的 cookie.txt。',
        'auth': '網站要求登入或帳號權限。Cookies 可能缺少、失效，或帳號無權觀看；請更新 cookie.txt 並確認帳號能開啟影片。這不代表 proxy 故障。',
        'blocked': '網站拒絕存取／要求機器人驗證。可能與 cookies、出口 IP 或限流有關；無法只憑此判定 cookies 失效或 proxy 故障。請先更新 cookies，仍失敗再檢查 proxy/IP。',
        'geo': '影片有地區限制。請使用可觀看地區的 PROXY／PROXY_LIST。',
        'proxy': 'Proxy 連線或驗證失敗。請檢查 proxy 位址、帳密與服務狀態；這不是 cookies 失效的證據。',
        'network': '網路連線失敗／逾時。請檢查 DNS、目標網站與 proxy（如有設定）；這不是 cookies 失效的證據。',
    }.get(classify_error(error), '')


class DownloadDiagnosticError(Exception):
    def __init__(self, message, source=None):
        super().__init__(message)
        self.source = source if source is not None else message


def diagnostic_error(error):
    if isinstance(error, DownloadDiagnosticError):
        return error
    hint = error_hint(error)
    if not hint and isinstance(error, Exception):
        return error
    return DownloadDiagnosticError(hint or str(error), source=str(error))
