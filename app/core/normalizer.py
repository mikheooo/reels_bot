import hashlib
import re
from urllib.parse import urlparse, urlunparse


def clean_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    cleaned = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    url_hash = hashlib.sha256(cleaned.encode()).hexdigest()
    return cleaned, url_hash

def is_valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://(www\.)?(instagram\.com|tiktok\.com|youtube\.com|youtu\.be)/.*", url))
