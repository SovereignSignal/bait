"""Validate provider-supplied link fields before publishing them. Validation does not verify ownership."""
from __future__ import annotations
import re
from urllib.parse import urlsplit


def web_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not re.match(r'^https?://', text, re.I) or re.search(r'[\s\x00-\x1f\\]', text):
        return None
    try:
        url = urlsplit(text)
        if url.username or url.password or not url.hostname or '.' not in url.hostname or url.hostname.endswith('.local'):
            return None
        _ = url.port  # malformed ports are not valid links
    except ValueError:
        return None
    return text


def github_url(value: object) -> str | None:
    url = web_url(value)
    if not url:
        return None
    p = urlsplit(url)
    return url if p.hostname == 'github.com' and re.fullmatch(r'/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?/?', p.path) else None


def x_handle(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().removeprefix('@')
    if re.match(r'^https?://', text, re.I):
        url = web_url(text)
        if not url:
            return None
        p = urlsplit(url)
        if p.hostname not in {'x.com', 'twitter.com', 'www.x.com', 'www.twitter.com'} or p.query or p.fragment:
            return None
        text = p.path.removeprefix('/').removesuffix('/')
    missing = {'none', 'unknown', 'null', 'undefined', 'search', 'home', 'intent', 'share', 'not_found', 'notidentified'}
    return text if re.fullmatch(r'[A-Za-z0-9_]{1,15}', text) and text.lower() not in missing else None
