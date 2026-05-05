"""
Email helper using the Resend HTTP API.

Reads RESEND_API_KEY and EMAIL_FROM from environment.
Falls back to logging the message body if no key is configured (so password
reset still works locally without email — link is printed to the console).
"""
from __future__ import annotations

import os
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


def send_email(
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
) -> dict:
    """Send an email via Resend. Returns the API response dict.

    If RESEND_API_KEY is not set, logs the message and returns a fake response
    so callers can still proceed in local-no-email mode.
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender  = os.environ.get("EMAIL_FROM", "onboarding@resend.dev").strip()

    if not api_key:
        log.warning("RESEND_API_KEY not set — email NOT sent. Subject: %s", subject)
        log.warning("To: %s\n%s", to, text or html)
        return {"ok": False, "skipped": True, "reason": "no_api_key"}

    payload = {
        "from":    sender,
        "to":      [to],
        "subject": subject,
        "html":    html,
    }
    if text:
        payload["text"] = text

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    resp = requests.post(RESEND_ENDPOINT, json=payload, headers=headers, timeout=15)
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text}

    return {"ok": resp.ok, "status": resp.status_code, "body": body}


if __name__ == "__main__":
    # Quick smoke test: python email_helper.py
    from dotenv import load_dotenv
    load_dotenv()

    to = os.environ.get("EMAIL_TEST_TO", "").strip()
    if not to:
        raise SystemExit("EMAIL_TEST_TO not set in .env")

    result = send_email(
        to=to,
        subject="Task Tracker — Resend smoke test",
        html=(
            "<h2>It works! 🎉</h2>"
            "<p>This is a test email from your Task Tracker app via Resend.</p>"
            "<p>If you can read this, the email pipeline is wired correctly and "
            "we can proceed to build password reset and invitation flows.</p>"
        ),
        text="It works! This is a test email from your Task Tracker app via Resend.",
    )
    print("Result:", result)
