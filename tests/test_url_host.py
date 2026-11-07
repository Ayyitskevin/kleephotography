"""Host checks parse the URL. A host substring is not a host."""

import pytest

from tests.urlcheck import links_google_calendar

_LINK = "https://calendar.google.com/calendar/render?action=TEMPLATE&text=Studio"


@pytest.mark.unit
def test_google_calendar_link_matches_its_host():
    assert links_google_calendar(f"Add it here:\n{_LINK}\n")
    assert links_google_calendar(f'<a href="{_LINK.replace("&", "&amp;")}">Add</a>')


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "https://calendar.google.com.evil.test/calendar/render",
        "https://notcalendar.google.com/calendar/render",
        "https://evil.test/?next=https://calendar.google.com/",
        "https://evil.test/calendar.google.com",
        "see calendar.google.com for details",
    ],
)
def test_lookalike_hosts_are_not_google_calendar(text):
    assert not links_google_calendar(text)
