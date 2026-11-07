"""Hostname checks for URLs embedded in rendered text or email bodies."""

import re
from urllib.parse import urlparse

_HTTP_URL = re.compile(r"https://[^\s\"'<>]+")


def links_google_calendar(text: str) -> bool:
    """True when some URL in `text` has host calendar.google.com.

    Compared on the parsed hostname. A substring check also matches
    `calendar.google.com.evil.test` and `notcalendar.google.com`.
    """
    return any(urlparse(raw).hostname == "calendar.google.com" for raw in _HTTP_URL.findall(text))
