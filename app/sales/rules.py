"""Shared sales values, validation and identities; no HTTP, database or provider access."""

import datetime as dt
import hashlib
import json
import re

CHANNELS = {
    "email": "Email",
    "sms": "Text message",
    "call": "Phone call",
    "in_person": "In person",
    "other": "Other",
}
DIRECTIONS = {"out": "Outbound", "in": "Inbound"}
OUTCOMES = {
    "attempted": "Attempted contact",
    "no_answer": "No answer",
    "voicemail": "Left voicemail",
    "sent": "Manually sent",
    "conversation": "Had a conversation",
    "reply_received": "Received a reply",
    "not_interested": "Not interested",
    "other": "Other",
}
REPLY_OUTCOMES = {"reply_received", "conversation", "not_interested"}


class ActivityError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def command_digest(values: dict) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def clean_text(value: str, label: str, limit: int, *, required: bool = False) -> str:
    value = value.strip()
    if "\x00" in value or len(value) > limit or (required and not value):
        raise ActivityError(f"Enter {label} between {1 if required else 0} and {limit} characters.")
    return value


def date_value(value: str, label: str) -> str:
    try:
        parsed = dt.date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
    except ValueError:
        raise ActivityError(f"Choose a real {label} in YYYY-MM-DD format.") from None
    return value


def positive_id(value: str, label: str) -> int:
    if not re.fullmatch(r"[0-9]{1,18}", value) or int(value) < 1:
        raise ActivityError(f"The {label} is invalid. Reopen this prospect and review the form.")
    return int(value)


def operation_key(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", value):
        raise ActivityError(
            "The operation key is invalid. Reopen this prospect to start a new form."
        )
    return value


def text_key(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def phone_key(value: str | None) -> str:
    # US ten-digit and +1 forms are equivalent. Never guess other country codes.
    # Extensions are not accepted by capture; keep legacy extensions distinct.
    digits = re.sub(r"[^0-9]", "", value or "")
    return "1" + digits if len(digits) == 10 else digits


def task_change_values(action, version, note, due_date):
    if action not in {"complete", "reschedule", "cancel"}:
        raise ActivityError("Choose complete, reschedule, or cancel.")
    parsed_version = positive_id(version, "action version")
    note = clean_text(note, "a completion note or change reason", 1000, required=True)
    due_date = date_value(due_date, "due date") if action == "reschedule" else ""
    return parsed_version, note, due_date
