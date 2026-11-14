"""Validate and atomically replace the shared Netscape cookie file."""

import io
import os
import tempfile
import time
import warnings
from contextlib import contextmanager
from http.cookiejar import MozillaCookieJar

import config

MAX_COOKIE_BYTES = 1024 * 1024


def is_cookie_admin(user_id):
    # Deliberately fail closed, unlike the public download allowlist.
    try:
        ids = {int(value.strip()) for value in
               os.getenv('COOKIE_ADMIN_IDS', '').split(',') if value.strip()}
    except ValueError:
        return False
    return user_id in ids


def validate_cookies(data):
    if not data or len(data) > MAX_COOKIE_BYTES:
        raise ValueError('Cookie file must be between 1 byte and 1 MiB.')
    try:
        text = data.decode('utf-8-sig').replace('\r\n', '\n')
        if '\x00' in text:
            raise ValueError()
        jar = MozillaCookieJar()
        # Do not let parser warnings expose cookie rows in logs.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            jar._really_load(io.StringIO(text), '<uploaded cookies>', True, True)
        cookies = list(jar)
        if not cookies:
            raise ValueError()
    except Exception:
        raise ValueError('Invalid cookies: export a Netscape cookies.txt file (not JSON or HTML).') from None
    expired = sum(c.expires is not None and c.expires != 0 and c.expires <= time.time()
                  for c in cookies)
    if expired == len(cookies):
        raise ValueError('All cookies have expired. Export a fresh file after signing in again.')
    return text.encode('utf-8'), len(cookies), expired


def install_cookies(data):
    data, count, expired = validate_cookies(data)
    directory = os.path.dirname(os.path.abspath(config.COOKIE_FILE))
    os.makedirs(directory, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix='.cookies-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(path, config.COOKIE_FILE)
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return count, expired


@contextmanager
def cookie_snapshot(source):
    """Keep yt-dlp's cookie writes from overwriting a newer upload."""
    if not source:
        yield None
        return
    fd, path = tempfile.mkstemp(prefix='ytdl-cookies-', suffix='.txt')
    try:
        with os.fdopen(fd, 'wb') as output, open(source, 'rb') as original:
            output.write(original.read())
        yield path
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
