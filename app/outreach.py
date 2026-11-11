"""Read-only sales outreach policy shared by drafts, queues and client views.

Explicit handoffs define a client-wide scope. Restrictions combine conservatively;
identity guesses never connect unrelated records. Callers supply the studio day.
"""

from contextlib import closing

from . import db

CHANNELS = {"email": "Email", "sms": "Text message", "call": "Phone call"}
DEFAULTS = {
    "mode": "active",
    "snoozed_until": "",
    "email_blocked": 0,
    "sms_blocked": 0,
    "call_blocked": 0,
    "preferred_channel": "",
    "reason": "",
}


def _aggregate(members, sources, today):
    blocked = [c for c in CHANNELS if any(s[c + "_blocked"] for s in sources)]
    pauses = [
        s["snoozed_until"]
        for s in sources
        if s["mode"] == "snoozed" and s["snoozed_until"] >= today
    ]
    stopped = any(s["mode"] == "stopped" for s in sources)
    until = max(pauses, default="")
    held = stopped or bool(until) or len(blocked) == len(CHANNELS)
    preferred = {s["preferred_channel"] for s in sources if s["preferred_channel"]}
    conflict = len(preferred) > 1 or bool(preferred.intersection(blocked))
    return {
        "members": members,
        "sources": sources,
        "held": held,
        "snoozed_until": until,
        "blocked": blocked,
        "preferred_channel": next(iter(preferred)) if preferred and not conflict else "",
        "preference_conflict": conflict,
        "label": "Outbound outreach stopped"
        if stopped
        else f"Outbound outreach paused through {until}"
        if until
        else "All outbound channels excluded"
        if held
        else "No outreach hold",
    }


def index(con, today):
    """One snapshot of the small solo CRM, reused per page (no per-row queries).

    Keep raw membership and preference versions in the result for signed forms and
    draft freshness. No date field changes otherwise-current drafts every midnight.
    """
    rows = list(
        con.execute(
            "SELECT p.id,p.business,h.client_id FROM prospects p "
            "LEFT JOIN prospect_handoffs h ON h.prospect_id=p.id ORDER BY p.id"
        )
    )
    policies = {
        r["prospect_id"]: dict(r)
        for r in con.execute("SELECT * FROM prospect_outreach_preferences ORDER BY prospect_id")
    }
    groups = {}
    for r in rows:
        key = ("client", r["client_id"]) if r["client_id"] else ("prospect", r["id"])
        groups.setdefault(key, []).append(dict(r))
    by_prospect, by_client = {}, {}
    for (kind, identity), members in groups.items():
        state = _aggregate(
            members, [policies[m["id"]] for m in members if m["id"] in policies], today
        )
        by_prospect.update({m["id"]: state for m in members})
        if kind == "client":
            by_client[identity] = state
    return {"prospects": by_prospect, "clients": by_client}


def read_index(today):
    with closing(db.connect()) as con:
        con.execute("BEGIN")
        return index(con, today)


def for_prospect(con, prospect_id, today, *, extra_client_id=None):
    states = index(con, today)
    own = states["prospects"][prospect_id]
    other = states["clients"].get(extra_client_id)
    if not other or other == own:
        return own
    members = {r["id"]: r for r in own["members"] + other["members"]}
    sources = {r["prospect_id"]: r for r in own["sources"] + other["sources"]}
    return _aggregate(
        [members[k] for k in sorted(members)], [sources[k] for k in sorted(sources)], today
    )


def held_ids(states, kind="prospects"):
    return {identity for identity, state in states[kind].items() if state["held"]}


def blocked_reason(state, channel=None):
    if state["held"]:
        return state["label"] + ". Review outreach preferences before planning another contact."
    if channel in state["blocked"]:
        return CHANNELS[channel] + " is excluded from outbound outreach. Review preferences first."
    return ""
