import hashlib
import re
from urllib.parse import urlparse, urlunparse

# Accepted hosts: instagram.com (www./m.), tiktok.com (www./vm./vt.),
# youtube.com (www./music.), youtu.be (www.). Fully anchored so lookalike
# domains like evil.com, instagram.com.evil.com or evilinstagram.com fail.
_HOST_RE = re.compile(
    r"^(?:"
    r"(?:www\.|m\.)?instagram\.com"
    r"|(?:www\.|vm\.|vt\.)?tiktok\.com"
    r"|(?:www\.|music\.)?youtube\.com"
    r"|(?:www\.)?youtu\.be"
    r")$"
)

# Instagram links must point at actual content pages (or the bare domain),
# not arbitrary paths such as /comments.
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"})
_INSTAGRAM_PATH_RE = re.compile(
    r"^(?:/|/(?:p|reel|reels|stories|explore|direct|tv|accounts|discover)(?:/.*)?)?$"
)


def clean_url(url: str) -> tuple[str, str]:
    if not url:
        return "", ""
    parsed = urlparse(url)
    cleaned = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    url_hash = hashlib.sha256(cleaned.encode()).hexdigest()
    return cleaned, url_hash


def is_valid_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
        _ = parsed.port  # force validation of malformed ports (e.g. ":abc")
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname
    if not _HOST_RE.match(host):
        return False
    if host in _INSTAGRAM_HOSTS:
        return bool(_INSTAGRAM_PATH_RE.match(parsed.path))
    return True