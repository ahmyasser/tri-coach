"""Sign-in helpers: Google OAuth (authorization-code flow) and email sign-in links sent through Resend.

Plain HTTPS calls with the standard library, so no extra dependencies.
"""

from __future__ import annotations

import html
import json
import logging
import urllib.parse
import urllib.request
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def base_url(settings: Settings) -> str:
    host = settings.public_host
    return f"http://{host}" if host.startswith(("localhost", "127.0.0.1")) else f"https://{host}"


def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _get_json(url: str, bearer: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


class Google:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.redirect_uri = base_url(settings) + "/auth/google/callback"

    @property
    def enabled(self) -> bool:
        return bool(self.settings.google_client_id and self.settings.google_client_secret)

    def auth_url(self, state: str) -> str:
        return GOOGLE_AUTH + "?" + urllib.parse.urlencode({
            "client_id": self.settings.google_client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": "openid email profile", "state": state, "prompt": "select_account",
        })

    def user_for_code(self, code: str) -> dict[str, Any]:
        """Exchange the callback code; returns Google's userinfo (sub, email, email_verified, name)."""
        tokens = _post_form(GOOGLE_TOKEN, {
            "code": code, "client_id": self.settings.google_client_id, "client_secret": self.settings.google_client_secret,
            "redirect_uri": self.redirect_uri, "grant_type": "authorization_code",
        })
        return _get_json(GOOGLE_USERINFO, tokens["access_token"])


class Mailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.resend_api_key and self.settings.email_from)

    def send_link(self, to: str, link: str, invited: bool = False) -> bool:
        """Email a one-time sign-in link. Without Resend configured, the link is only logged (for the admin)."""
        brand = self.settings.brand_name
        subject = f"Your {brand} invite" if invited else f"Sign in to {brand}"
        lead = f"You're invited to the {brand} beta." if invited else f"Here's your link to sign in to {brand}."
        text = f"{lead}\n\nOpen this link within 20 minutes:\n{link}\n\nIf you didn't ask for this, ignore this email."
        body = (f'<div style="font-family:system-ui,sans-serif;font-size:15px;line-height:1.5;color:#111">'
                f"<p>{html.escape(lead)}</p>"
                f'<p><a href="{html.escape(link)}" style="display:inline-block;background:#111;color:#fff;padding:12px 20px;'
                f'border-radius:10px;text-decoration:none">{"Join" if invited else "Sign in"}</a></p>'
                f'<p style="color:#666;font-size:13px">The link works once, for 20 minutes. If you didn\'t ask for it, ignore this email.</p></div>')
        if not self.enabled:
            log.warning("email not configured; sign-in link for %s: %s", to, link)
            return False
        req = urllib.request.Request(
            "https://api.resend.com/emails", method="POST",
            data=json.dumps({"from": self.settings.email_from, "to": [to], "subject": subject, "html": body, "text": text}).encode(),
            headers={"Authorization": f"Bearer {self.settings.resend_api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15):
                return True
        except Exception:
            log.exception("sending email to %s failed", to)
            return False
