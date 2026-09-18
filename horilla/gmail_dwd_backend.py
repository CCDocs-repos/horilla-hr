"""
horilla/gmail_dwd_backend.py

Custom Django email backend that sends via Gmail API using a Google service account
with Domain-Wide Delegation (DWD) impersonating careers@ccdocs.com.

No app passwords. No SMTP credentials. No 2FA enrollment needed.

Required env vars:
  GMAIL_SA_KEY_PATH         path to service account JSON key (default: /run/secrets/sa.json)
  GMAIL_IMPERSONATE_USER    mailbox to impersonate           (default: careers@ccdocs.com)
  GMAIL_FROM_DISPLAY_NAME   display name in From header      (default: Call Center Doctors Careers)

The SA must have the following scopes delegated in Google Admin:
  https://www.googleapis.com/auth/gmail.send
  https://www.googleapis.com/auth/gmail.settings.basic
"""

import base64
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]


class GmailDWDBackend(BaseEmailBackend):
    """Send Django email messages via Gmail API using DWD service-account impersonation."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        sa_path = os.environ.get("GMAIL_SA_KEY_PATH", "/run/secrets/sa.json")
        impersonate = os.environ.get("GMAIL_IMPERSONATE_USER", "careers@ccdocs.com")

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(
                sa_path, scopes=_SCOPES
            ).with_subject(impersonate)
            self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            self._impersonate = impersonate
        except Exception as exc:
            if not self.fail_silently:
                raise
            logger.error("GmailDWDBackend init failed: %s", exc)
            self.service = None

        # Fetch the careers@ Send Mail As signature once on init so it can be
        # appended to every outgoing HTML body without a per-send API call.
        self._signature_html = ""
        if self.service:
            try:
                sa_resp = (
                    self.service.users()
                    .settings()
                    .sendAs()
                    .list(userId="me")
                    .execute()
                )
                primary = next(
                    (s for s in sa_resp.get("sendAs", []) if s.get("isPrimary")),
                    None,
                )
                self._signature_html = (primary or {}).get("signature", "")
            except Exception as exc:
                logger.warning(
                    "Could not fetch signature for %s: %s", impersonate, exc
                )
                self._signature_html = ""

    def send_messages(self, email_messages):
        """Send each EmailMessage via the Gmail API. Returns number of messages sent."""
        if not self.service:
            return 0

        sent = 0
        for msg in email_messages:
            try:
                mime = self._build_mime(msg)
                raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
                # Gmail API requires padding stripped
                raw = raw.rstrip("=")
                self.service.users().messages().send(
                    userId="me", body={"raw": raw}
                ).execute()
                sent += 1
            except Exception as exc:
                if not self.fail_silently:
                    raise
                logger.error(
                    "GmailDWDBackend failed to send to %s: %s",
                    getattr(msg, "to", "?"),
                    exc,
                )
        return sent

    def _build_mime(self, msg):
        """
        Convert a Django EmailMessage (or EmailMultiAlternatives) to a MIME object.
        Handles plain-text only and HTML alternatives.
        """
        # Determine if there is an HTML alternative
        html_body = None
        if hasattr(msg, "alternatives"):
            for content, mimetype in msg.alternatives:
                if mimetype == "text/html":
                    html_body = content
                    break

        # Append careers@ signature to HTML body before building the MIME part.
        if html_body and self._signature_html:
            html_body = html_body + "<br><br>" + self._signature_html

        if html_body:
            mime = MIMEMultipart("alternative")
            mime.attach(MIMEText(msg.body, "plain", "utf-8"))
            mime.attach(MIMEText(html_body, "html", "utf-8"))
        else:
            mime = MIMEText(msg.body, "plain", "utf-8")

        mime["Subject"] = msg.subject
        # Force the full display name regardless of what Django passes in from_email.
        display_name = os.environ.get(
            "GMAIL_FROM_DISPLAY_NAME", "Call Center Doctors Careers"
        )
        mime["From"] = formataddr((display_name, self._impersonate))
        mime["To"] = ", ".join(msg.to)

        if msg.cc:
            mime["Cc"] = ", ".join(msg.cc)
        if msg.bcc:
            mime["Bcc"] = ", ".join(msg.bcc)
        if msg.reply_to:
            mime["Reply-To"] = ", ".join(msg.reply_to)

        return mime
