"""Gmail SMTP — manual sends only, every send logged by the caller in emails_log."""

import smtplib
from email.message import EmailMessage

from . import config


def configured() -> bool:
    return bool(config.GMAIL_USER and config.GMAIL_APP_PASSWORD)


def send(to: str, subject: str, body: str, reply_to: str = "", ics: dict | None = None) -> None:
    """Send a plain-text email, optionally with a calendar invite attached.

    `ics` = {"filename", "content", "method"} — content is the VCALENDAR text from
    ics.build(); method ("REQUEST"/"CANCEL") must match its METHOD so Gmail/Apple
    Mail render the in-line Accept/Decline (or removal) affordance."""
    msg = EmailMessage()
    msg["From"] = f"{config.SITE_NAME} <{config.GMAIL_USER}>"
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    if ics:
        msg.add_attachment(
            ics["content"].encode(),
            maintype="text",
            subtype="calendar",
            filename=ics["filename"],
            params={"method": ics.get("method", "REQUEST"), "charset": "UTF-8"},
        )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
        s.send_message(msg)
