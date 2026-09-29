"""BiteWise verification and feedback email delivery through the Gmail API.

Uses Google's official API client (google-api-python-client) authenticated
with OAuth2 credentials read from environment variables only:

    GMAIL_CLIENT_ID
    GMAIL_CLIENT_SECRET
    GMAIL_REFRESH_TOKEN

The OAuth scope is restricted to https://www.googleapis.com/auth/gmail.send
and the From address is fixed to the authenticated sender account
mybitewise@gmail.com (no GMAIL_FROM override). No SMTP and no third-party
delivery services are used.

Security rules enforced here:
- Credentials come from environment variables only; nothing is hardcoded.
- Access tokens, refresh tokens, client secrets, authorization headers and
  full request headers are NEVER logged or returned to callers.
- Logs contain only safe diagnostics (operation name, HTTP status, Gmail
  message id, sanitized error type).
"""

import base64
import os
import re
from email.message import EmailMessage

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_API = "gmail"
GMAIL_API_VERSION = "v1"
FIXED_SENDER = "mybitewise@gmail.com"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REFRESH_ERROR_CODES = re.compile(
    r"(invalid_grant|invalid_client|unauthorized_client|access_denied|invalid_scope|"
    r"temporarily_disabled|unsupported_grant_type)"
)


def _refresh_error_code(exc: Exception) -> str:
    """Extract the safe OAuth error code from a RefreshError message."""
    match = _REFRESH_ERROR_CODES.search(str(exc))
    return match.group(1) if match else type(exc).__name__


class GmailMailError(Exception):
    """The verification email could not be delivered via the Gmail API.

    Never carries credentials or raw provider error content; the message is
    safe to surface to end users and to log.
    """


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _build_raw_mime(to_email: str, subject: str, html_body: str) -> str:
    """Build an RFC 2822 MIME message and return it URL-safe Base64 encoded.

    The MIME message contains the fixed sender, the recipient, the subject
    and an HTML body.
    """
    msg = EmailMessage()
    msg["From"] = FIXED_SENDER
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(html_body, subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return raw.rstrip("=")


def _send_mime(to_email: str, subject: str, html_body: str) -> dict:
    """Send ``html_body`` to ``to_email`` via the Gmail API (shared path).

    1. Validates the recipient email address.
    2. Reads OAuth2 credentials from the environment.
    3. Creates the Gmail API client with scope gmail.send only.
    4. Builds the MIME message (From/To/Subject/HTML body).
    5. URL-safe Base64 encodes the MIME message into requestBody.raw.
    6. Calls gmail.users.messages.send(userId="me").
    7. Returns a safe success result (message id only).
    8. Never returns or logs credentials, tokens or raw provider content.

    Raises GmailMailError when the email cannot actually be sent.
    """
    to_email = to_email.strip().lower()
    if not _EMAIL_RE.match(to_email):
        raise GmailMailError("Invalid recipient email address")

    client_id = _env("GMAIL_CLIENT_ID")
    client_secret = _env("GMAIL_CLIENT_SECRET")
    refresh_token = _env("GMAIL_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        print("[gmail-mailer] Gmail API not configured: GMAIL_CLIENT_ID, "
              "GMAIL_CLIENT_SECRET or GMAIL_REFRESH_TOKEN is missing")
        raise GmailMailError("Gmail API not configured")

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=GMAIL_TOKEN_URI,
        scopes=[GMAIL_SEND_SCOPE],
    )

    try:
        service = build(GMAIL_API, GMAIL_API_VERSION, credentials=credentials,
                        cache_discovery=False)
        raw = _build_raw_mime(to_email, subject, html_body)
        result = service.users().messages().send(
            userId="me", body={"raw": raw}).execute()
    except HttpError as e:
        status = e.resp.status if getattr(e, "resp", None) is not None else "unknown"
        print(f"[gmail-mailer] Gmail API error: HttpError status={status}")
        raise GmailMailError("Gmail delivery failed")
    except RefreshError as e:
        code = _refresh_error_code(e)
        print(f"[gmail-mailer] Gmail OAuth token refresh failed (error_code={code}); "
              "verify GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN match "
              "the Google Cloud OAuth client for project nutrilens-ai-fd9fb (the refresh "
              "token may be expired, revoked, or the credentials may be mismatched)")
        raise GmailMailError("Gmail delivery failed")
    except Exception as e:
        print(f"[gmail-mailer] Gmail delivery error: {type(e).__name__}")
        raise GmailMailError("Gmail delivery failed")

    message_id = result.get("id") if isinstance(result, dict) else None
    print(f"[gmail-mailer] sent messageId={message_id}")
    return {"messageId": message_id, "sent": True}


def _verification_html(otp: str) -> str:
    """HTML body of the OTP verification email."""
    expires_minutes = _env("VERIFY_OTP_TTL_MINUTES") or "10"
    return (
        "<html><body>"
        "<p>Welcome to BiteWise!</p>"
        f"<p>Your verification code is: <strong>{otp}</strong></p>"
        f"<p>Enter this code in the app to activate your account. The code "
        f"expires in {expires_minutes} minutes.</p>"
        "<p>If you didn't create an account, you can safely ignore this email.</p>"
        "</body></html>"
    )


def send_verification_email(to_email: str, otp: str) -> dict:
    """Send the BiteWise verification OTP to ``to_email`` via the Gmail API.

    See ``_send_mime`` for the security contract. The OTP is embedded in the
    HTML body exactly as before; this function preserves the existing
    verification-email behavior.
    """
    return _send_mime(to_email, "Your BiteWise verification code", _verification_html(otp))


def send_feedback_email(to_email: str, subject: str, html_body: str) -> dict:
    """Send a feedback-style message through the existing Gmail API client.

    Shares the exact OAuth2 credential flow, scope, validation and safe error
    handling with ``send_verification_email``. No SMTP, no new credentials,
    and no secrets are ever exposed to callers.
    """
    return _send_mime(to_email, subject, html_body)


def send_rating_email(to_email: str, subject: str, html_body: str) -> dict:
    """Send a rating message through the existing Gmail API client.

    Shares the exact OAuth2 credential flow, scope, validation and safe error
    handling with ``send_verification_email`` / ``send_feedback_email``. No
    SMTP, no new credentials, and no secrets are ever exposed to callers.
    """
    return _send_mime(to_email, subject, html_body)


def _reset_password_html(otp: str) -> str:
    """HTML body of the password reset OTP email."""
    expires_minutes = _env("VERIFY_OTP_TTL_MINUTES") or "10"
    return (
        "<html><body>"
        "<p>We received a request to reset your BiteWise password.</p>"
        f"<p>Your password reset code is: <strong>{otp}</strong></p>"
        f"<p>Enter this code in the app to set a new password. The code "
        f"expires in {expires_minutes} minutes.</p>"
        "<p>If you didn't request a password reset, you can safely ignore this email. "
        "Your password will not be changed.</p>"
        "</body></html>"
    )


def send_password_reset_email(to_email: str, otp: str) -> dict:
    """Send a password reset OTP to ``to_email`` via the Gmail API.

    Reuses the same OAuth2 credential flow, scope, validation and safe error
    handling as ``send_verification_email``. The OTP is embedded in the HTML
    body; the plaintext code is never stored or logged.
    """
    return _send_mime(to_email, "Your BiteWise password reset code", _reset_password_html(otp))
