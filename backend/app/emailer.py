"""Outbound email. `console` backend (dev/test) logs the message and keeps it in
memory so tests can read magic links; `smtp` sends via SMTP_* settings."""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import get_settings

log = logging.getLogger(__name__)


@dataclass
class SentEmail:
    to: str
    subject: str
    body: str


OUTBOX: list[SentEmail] = []  # console backend only


def send(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    if settings.email_backend == "smtp":
        if not settings.smtp_host:
            raise RuntimeError("SMTP_HOST is required when EMAIL_BACKEND=smtp")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = settings.email_from, to, subject
        msg.set_content(body)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password or "")
            smtp.send_message(msg)
        return
    if settings.is_production:
        raise RuntimeError("console email backend is not allowed in production")
    OUTBOX.append(SentEmail(to, subject, body))
    log.warning("[console email] to=%s subject=%s\n%s", to, subject, body)
