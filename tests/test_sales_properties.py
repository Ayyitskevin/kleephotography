"""Generated sales invariants: pure policy/URL checks, no DB or provider calls."""

import datetime as dt
from copy import deepcopy
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from starlette.requests import Request

from app import outreach
from app.admin import prospect_context

pytestmark = pytest.mark.unit
ROOT = "/admin/studio/prospects"
CHANNELS = ("email", "sms", "call")
DAYS = st.dates(min_value=dt.date(1, 1, 2), max_value=dt.date(9999, 12, 30))


@st.composite
def policies(draw):
    """Only persisted policy shapes; malformed rows are rejected by the schema."""
    mode = draw(st.sampled_from(("active", "snoozed", "stopped")))
    blocked = draw(st.sets(st.sampled_from(CHANNELS)))
    allowed = [c for c in CHANNELS if c not in blocked]
    return {
        "mode": mode,
        "snoozed_until": draw(DAYS).isoformat() if mode == "snoozed" else None,
        **{c + "_blocked": int(c in blocked) for c in CHANNELS},
        "preferred_channel": draw(st.sampled_from(["", *allowed])),
    }


def policy(sources, day):
    return outreach._aggregate([], sources, day.isoformat())


def decision(state):
    # Source order remains visible for audit; only the effective decision is commutative.
    return {k: v for k, v in state.items() if k not in {"members", "sources"}}


@given(st.lists(policies()), policies(), DAYS)
def test_adding_a_source_cannot_relax_outreach_restrictions(sources, extra, day):
    before = policy(sources, day)
    after = policy([*sources, extra], day)
    assert set(before["blocked"]) <= set(after["blocked"])
    assert not before["held"] or after["held"]
    assert before["snoozed_until"] <= after["snoozed_until"]
    for channel in CHANNELS:
        if outreach.blocked_reason(before, channel):
            assert outreach.blocked_reason(after, channel)


@given(st.lists(policies()), DAYS, st.data())
def test_source_order_and_duplicates_do_not_change_the_decision(sources, day, data):
    original = deepcopy(sources)
    expected = decision(policy(sources, day))
    shuffled = data.draw(st.permutations(sources))
    assert decision(policy(shuffled, day)) == expected
    assert decision(policy(sources + sources, day)) == expected
    assert sources == original


@given(st.lists(policies()), DAYS)
def test_one_stop_blocks_every_outbound_channel(sources, day):
    stop = dict(outreach.DEFAULTS, mode="stopped")
    state = policy([*sources, stop], day)
    assert state["held"] and state["label"] == "Outbound outreach stopped"
    assert all(outreach.blocked_reason(state, channel) for channel in CHANNELS)


@given(DAYS)
def test_pause_includes_its_last_day_and_expires_the_following_day(until):
    pause = dict(outreach.DEFAULTS, mode="snoozed", snoozed_until=until.isoformat())
    assert policy([pause], until - dt.timedelta(days=1))["held"]
    assert policy([pause], until)["held"]
    assert not policy([pause], until + dt.timedelta(days=1))["held"]


@given(st.lists(policies()), DAYS)
def test_channel_exclusions_are_a_union_and_conflicts_are_not_silently_chosen(sources, day):
    state = policy(sources, day)
    exclusions = {c for s in sources for c in CHANNELS if s[c + "_blocked"]}
    preferences = {s["preferred_channel"] for s in sources} - {""}
    assert set(state["blocked"]) == exclusions
    if len(preferences) > 1 or preferences & exclusions:
        assert state["preference_conflict"] and not state["preferred_channel"]
    elif preferences:
        assert {state["preferred_channel"]} == preferences
    else:
        assert not state["preferred_channel"]
    if exclusions == set(CHANNELS):
        assert state["held"]


def request(params):
    return Request({"type": "http", "query_string": urlencode(params).encode("ascii")})


@given(
    review=st.sampled_from(("due", "upcoming", "unplanned")),
    query=st.text(),
    origin=st.integers(min_value=1, max_value=10**18 - 1),
)
@example(review="due", query="//evil.example/\\?q=x&prospect_id=99#fragment\r\n", origin=1)
@example(review="unplanned", query="x" * 250, origin=10**18 - 1)
def test_review_redirect_round_trips_search_without_changing_destination(review, query, origin):
    req = request({"review": review, "q": query, "review_origin": str(origin)})
    location = prospect_context.redirect_to_prospect(42, req).headers["location"]
    parsed = urlsplit(location)
    assert not parsed.scheme and not parsed.netloc and not parsed.fragment
    assert parsed.path == ROOT + "/review-work"
    assert parse_qs(parsed.query, keep_blank_values=True) == {
        "review": [review],
        "q": [query.strip()[:200]],
        "review_origin": [str(origin)],
        "prospect_id": [str(origin)],
    }
    assert "\r" not in location and "\n" not in location and "\\" not in location


@given(origin=st.text(), query=st.text())
@example(origin="1\n", query="cafe")
@example(origin="١", query="cafe")
@example(origin="9" * 19, query="cafe")
def test_invalid_return_identity_never_becomes_a_destination(origin, query):
    valid = 1 <= len(origin) <= 18 and all(c in "0123456789" for c in origin) and int(origin) > 0
    req = request({"review": "due", "q": query, "review_origin": origin})
    response = prospect_context.redirect_to_prospect(42, req)
    if not valid:
        assert response.headers["location"] == ROOT + "/42"
    else:
        assert parse_qs(urlsplit(response.headers["location"]).query)["prospect_id"] == [
            str(int(origin))
        ]


@given(review=st.text())
@example(review="//evil.example")
def test_unknown_review_queue_keeps_the_standalone_destination(review):
    if review not in {"due", "upcoming", "unplanned"}:
        assert (
            prospect_context.redirect_to_prospect(42, request({"review": review})).headers[
                "location"
            ]
            == ROOT + "/42"
        )
