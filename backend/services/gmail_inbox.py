"""Gmail inbox reader and threaded reply used by the customer-support feature.

Reads incoming mail and replies inside the same conversation thread using the
Gmail API, authenticated with OAuth2 credentials from environment variables.

    GMAIL_CLIENT_ID
    GMAIL_CLIENT_SECRET
    GMAIL_SUPPORT_REFRESH_TOKEN   (required; must have gmail.modify scope)

Security contract (mirrors gmail_mailer.py):
- Credentials come from environment variables only; nothing is hardcoded.
- Access tokens, refresh tokens, client secrets and full request headers are
  never logged or returned to callers.
- The OTP verification flow in gmail_mailer.py is untouched: reading the inbox
  uses a dedicated refresh token (GMAIL_SUPPORT_REFRESH_TOKEN) so the existing
  gmail.send-only token is never required to have read access.
- Logs contain only safe diagnostics (operation name, HTTP status, Gmail ids,
  sanitized error type).

Note on scope: reading the inbox requires a refresh token granted with the
gmail.modify (or gmail.readonly) scope. The refresh token used for OTP emails
is gmail.send-only, so a separate read-capable token MUST be provided via
GMAIL_SUPPORT_REFRESH_TOKEN. If the configured token lacks read access, calls
fail safely with GmailInboxError and the poller skips the batch.
"""

import base64
import os
import re
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_API = "gmail"
GMAIL_API_VERSION = "v1"
FIXED_SENDER = "mybitewise@gmail.com"

_MAX_BODY_LEN = 4000
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_REFRESH_ERROR_CODES = re.compile(
    r"(invalid_grant|invalid_client|unauthorized_client|access_denied|invalid_scope|"
    r"temporarily_disabled|unsupported_grant_type)"
)


class GmailInboxError(Exception):
    """Sending/reading via Gmail failed.

    Never carries credentials or raw provider content; the message is safe to
    log and to surface to staff.
    """


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _credentials() -> Credentials:
    client_id = _env("GMAIL_CLIENT_ID")
    client_secret = _env("GMAIL_CLIENT_SECRET")
    # Dedicated read token required; DO NOT fall back to the OTP token
    # (which is gmail.send-only and will fail if it lacks read access).
    refresh_token = _env("GMAIL_SUPPORT_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        missing = []
        if not client_id:
            missing.append("GMAIL_CLIENT_ID")
        if not client_secret:
            missing.append("GMAIL_CLIENT_SECRET")
        if not refresh_token:
            missing.append("GMAIL_SUPPORT_REFRESH_TOKEN (must have gmail.modify scope)")
        print("[gmail-inbox] Gmail support not configured: " + ", ".join(missing))
        raise GmailInboxError("Gmail support not configured")
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=GMAIL_TOKEN_URI,
        scopes=[GMAIL_MODIFY_SCOPE],
    )


def _service():
    try:
        return build(GMAIL_API, GMAIL_API_VERSION, credentials=_credentials(),
                     cache_discovery=False)
    except RefreshError as e:
        # Extract safe OAuth error code (mirrors gmail_mailer.py)
        code = _refresh_error_code(e)
        print(f"[gmail-inbox] OAuth token refresh failed (error_code={code}); "
              "verify GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and GMAIL_SUPPORT_REFRESH_TOKEN "
              "match the Google Cloud OAuth client for project nutrilens-ai-fd9fb "
              "(the refresh token may be expired, revoked, or the credentials may be mismatched; "
              "GMAIL_SUPPORT_REFRESH_TOKEN must have gmail.modify scope)")
        raise GmailInboxError("Gmail support not configured")
    except Exception as e:
        print(f"[gmail-inbox] build error: {type(e).__name__}")
        raise GmailInboxError("Gmail support not configured")


def _refresh_error_code(exc: Exception) -> str:
    """Extract the safe OAuth error code from a RefreshError message."""
    match = _REFRESH_ERROR_CODES.search(str(exc))
    return match.group(1) if match else type(exc).__name__


def _decode_payload(payload: Dict[str, Any]) -> str:
    """Extract plain text from a Gmail message payload (handles multipart)."""
    mime = (payload.get("mimeType") or "").lower()
    if mime == "text/plain" and payload.get("body", {}).get("data"):
        try:
            raw = payload["body"]["data"]
            return base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8", errors="replace")
        except Exception:
            return ""
    if mime == "text/html" and payload.get("body", {}).get("data"):
        try:
            raw = payload["body"]["data"]
            html = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8", errors="replace")
            text = _HTML_TAG_RE.sub(" ", html)
            text = _WS_RE.sub(" ", text)
            return text.strip()
        except Exception:
            return ""
    parts = payload.get("parts") or []
    chunks = []
    for part in parts:
        chunks.append(_decode_payload(part))
    return "\n".join(c for c in chunks if c)


def _clean_text(text: str) -> str:
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()[: _MAX_BODY_LEN]


def _header_join(values) -> str:
    if not values:
        return ""
    if isinstance(values, str):
        return values
    return ", ".join(str(v) for v in values)


def _parse_message_meta(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a Gmail message resource into a safe parsed dict."""
    headers = {h["name"].lower(): h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
    body_parts = (_decode_payload(msg.get("payload", {})) or "").strip()
    date_raw = headers.get("date", "")
    try:
        date = parsedate_to_datetime(date_raw) if date_raw else None
    except Exception:
        date = None
    return {
        "messageId": msg.get("id"),
        "threadId": msg.get("threadId"),
        "fromHeader": headers.get("from", ""),
        "toHeader": headers.get("to", ""),
        "subject": headers.get("subject", "(no subject)"),
        "messageIdHeader": headers.get("message-id", ""),
        "referencesHeader": headers.get("references", ""),
        "bodyText": _clean_text(body_parts),
        "internalDate": msg.get("internalDate"),
        "date": date.isoformat() if date else None,
    }


def _parse_address(header_value: str) -> str:
    """Best-effort extraction of a bare email address from a From/To header."""
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", header_value or "")
    return match.group(0).lower() if match else (header_value or "").strip().lower()


def fetch_unread(max_results: int = 20) -> List[Dict[str, Any]]:
    """Return unread messages sent to the support inbox.

    Skips messages the account sent to itself. Returns parsed dicts (see
    ``_parse_message_meta``). Raises GmailInboxError when reading fails.
    """
    service = _service()
    try:
        results = service.users().messages().list(
            userId="me",
            q=f"to:{FIXED_SENDER} is:unread",
            maxResults=max_results,
        ).execute()
    except HttpError as e:
        status = e.resp.status if getattr(e, "resp", None) is not None else "unknown"
        print(f"[gmail-inbox] list error: HttpError status={status}")
        raise GmailInboxError("Gmail inbox read failed")
    except Exception as e:
        print(f"[gmail-inbox] list error: {type(e).__name__}")
        raise GmailInboxError("Gmail inbox read failed")

    parsed = []
    for item in (results.get("messages") or []):
        msg_id = item.get("id")
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full").execute()
        except Exception as e:
            print(f"[gmail-inbox] get error: {type(e).__name__} id={msg_id}")
            continue
        meta = _parse_message_meta(msg)
        if not meta["messageId"]:
            continue
        sender = _parse_address(meta["fromHeader"])
        if sender == FIXED_SENDER:
            continue
        meta["senderEmail"] = sender or None
        parsed.append(meta)
    return parsed


def mark_read(message_id: str) -> None:
    """Remove the UNREAD label from a processed message. Failures are logged."""
    try:
        service = _service()
        service.users().messages().modify(
            userId="me", id=message_id,
            body={"removeLabelIds": ["UNREAD"]}).execute()
    except Exception as e:
        print(f"[gmail-inbox] mark_read error: {type(e).__name__} id={message_id}")


def send_thread_reply(
    *,
    to_email: str,
    subject: str,
    body_text: str,
    thread_id: str,
    message_id_header: str = "",
    references_header: str = "",
) -> Dict[str, Any]:
    """Send a reply inside the given Gmail thread.

    ``to_email`` is always the original sender of the thread (determined by the
    backend, never by the dashboard request). Returns {messageId, threadId}.
    """
    to_email = to_email.strip().lower()
    if not _EMAIL_RE.match(to_email):
        raise GmailInboxError("Invalid recipient email address")
    if not body_text or not body_text.strip():
        raise GmailInboxError("Reply body is empty")

    msg = EmailMessage()
    msg["From"] = FIXED_SENDER
    msg["To"] = to_email
    msg["Subject"] = subject
    if message_id_header:
        msg["In-Reply-To"] = message_id_header
        references = (references_header + " " + message_id_header).strip()
        if references:
            msg["References"] = references
    msg.set_content(body_text.strip())

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")

    service = _service()
    try:
        result = service.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": thread_id},
        ).execute()
    except HttpError as e:
        status = e.resp.status if getattr(e, "resp", None) is not None else "unknown"
        print(f"[gmail-inbox] send error: HttpError status={status}")
        raise GmailInboxError("Gmail reply failed")
    except Exception as e:
        print(f"[gmail-inbox] send error: {type(e).__name__}")
        raise GmailInboxError("Gmail reply failed")

    return {
        "messageId": result.get("id"),
        "threadId": result.get("threadId") or thread_id,
        "sent": True,
    }
