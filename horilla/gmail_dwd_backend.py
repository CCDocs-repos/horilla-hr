"""
horilla/gmail_dwd_backend.py

Custom Django email backend that sends via Gmail API using a Google service account
with Domain-Wide Delegation (DWD) impersonating careers@ccdocs.com.

No app passwords. No SMTP credentials. No 2FA enrollment needed.

Required env vars:
  GMAIL_SA_KEY_PATH      path to service account JSON key (default: /run/secrets/sa.json)
  GMAIL_IMPERSONATE_USER mailbox to impersonate           (default: careers@ccdocs.com)

The SA must have the Gmail send scope delegated in Google Admin:
  https://www.googleapis.com/auth/gmail.send
"""

import base64
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


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

        if html_body:
            mime = MIMEMultipart("alternative")
            mime.attach(MIMEText(msg.body, "plain", "utf-8"))
            mime.attach(MIMEText(html_body, "html", "utf-8"))
        else:
            mime = MIMEText(msg.body, "plain", "utf-8")

        mime["Subject"] = msg.subject
        mime["From"] = msg.from_email or self._impersonate
        mime["To"] = ", ".join(msg.to)

        if msg.cc:
            mime["Cc"] = ", ".join(msg.cc)
        if msg.bcc:
            mime["Bcc"] = ", ".join(msg.bcc)
        if msg.reply_to:
            mime["Reply-To"] = ", ".join(msg.reply_to)

        return mime
