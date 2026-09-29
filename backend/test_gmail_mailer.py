"""Unit tests for services.gmail_mailer (Gmail API verification email) and
its integration with auth_service.

Gmail API calls are fully mocked: no network, no real OAuth. Tests verify the
MIME message, URL-safe Base64 encoding, API call shape, safe error handling
and that secrets never appear in logs or returned values.

Run:  python test_gmail_mailer.py   (from the backend/ directory)
"""
import base64
import contextlib
import email
import email.header
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import auth_service, gmail_mailer  # noqa: E402
from services.auth_service import EmailDeliveryError  # noqa: E402
from services.gmail_mailer import GmailMailError, send_feedback_email, send_rating_email, send_verification_email  # noqa: E402

FAKE_CLIENT_ID = "fake-client-id-123.apps.googleusercontent.com"
FAKE_CLIENT_SECRET = "fake-client-secret-value"
FAKE_REFRESH_TOKEN = "1//fake-refresh-token-value"
FAKE_ENV = {
    "GMAIL_CLIENT_ID": FAKE_CLIENT_ID,
    "GMAIL_CLIENT_SECRET": FAKE_CLIENT_SECRET,
    "GMAIL_REFRESH_TOKEN": FAKE_REFRESH_TOKEN,
}
GMAIL_KEYS = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")


def _capture_stdout(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class GmailMailerTest(unittest.TestCase):

    def setUp(self):
        self.send_kwargs = None

    def _fake_service(self, execute_result=None, execute_error=None):
        """A mock Gmail service recording the send call."""
        service = mock.Mock()

        def fake_send(**kwargs):
            self.send_kwargs = kwargs
            call = mock.Mock()
            if execute_error is not None:
                call.execute.side_effect = execute_error
            else:
                call.execute.return_value = execute_result if execute_result is not None else {}
            return call

        service.users.return_value.messages.return_value.send.side_effect = fake_send
        return service

    def _decode_raw(self):
        raw = self.send_kwargs["body"]["raw"]
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))

    # ---------- success path ----------

    def test_send_success_calls_api_and_returns_safe_result(self):
        service = self._fake_service(execute_result={"id": "msg-abc123"})
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            result = send_verification_email("User@Example.com", "123456")

        self.assertEqual(result, {"messageId": "msg-abc123", "sent": True})
        self.assertEqual(self.send_kwargs["userId"], "me")
        self.assertIn("raw", self.send_kwargs["body"])

    def test_send_mime_contains_from_to_subject_and_otp(self):
        service = self._fake_service(execute_result={"id": "msg-1"})
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            send_verification_email("user@example.com", "987654")

        msg = email.message_from_bytes(self._decode_raw())
        self.assertEqual(msg["From"], "mybitewise@gmail.com")
        self.assertEqual(msg["To"], "user@example.com")
        self.assertEqual(msg["Subject"], "Your BiteWise verification code")
        self.assertEqual(msg.get_content_type(), "text/html")
        body = msg.get_payload(decode=True).decode("utf-8")
        self.assertIn("987654", body)
        self.assertIn("Welcome to BiteWise!", body)

    def test_send_raw_is_url_safe_base64_without_padding(self):
        service = self._fake_service(execute_result={"id": "msg-1"})
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            send_verification_email("user@example.com", "123456")

        raw = self.send_kwargs["body"]["raw"]
        self.assertNotIn("+", raw)
        self.assertNotIn("/", raw)
        self.assertFalse(raw.endswith("="))
        # round-trip: re-encoding the decoded bytes gives the same raw string
        decoded = self._decode_raw()
        self.assertEqual(
            base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("="), raw)

    def test_credentials_use_gmail_send_scope_only(self):
        captured = {}

        def fake_credentials_init(**kwargs):
            captured.update(kwargs)
            return mock.Mock()

        service = self._fake_service(execute_result={"id": "msg-1"})
        with mock.patch.object(gmail_mailer, "Credentials", fake_credentials_init), \
             mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            send_verification_email("user@example.com", "123456")

        self.assertEqual(captured["scopes"],
                         ["https://www.googleapis.com/auth/gmail.send"])
        self.assertEqual(captured["token_uri"],
                         "https://oauth2.googleapis.com/token")
        self.assertIsNone(captured["token"])
        self.assertEqual(captured["client_id"], FAKE_CLIENT_ID)
        self.assertEqual(captured["client_secret"], FAKE_CLIENT_SECRET)
        self.assertEqual(captured["refresh_token"], FAKE_REFRESH_TOKEN)

    # ---------- input validation ----------

    def test_invalid_recipient_rejected_without_api_call(self):
        service = mock.Mock()
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            with self.assertRaises(GmailMailError) as ctx:
                send_verification_email("not-an-email", "123456")
        self.assertEqual(str(ctx.exception), "Invalid recipient email address")
        service.users.assert_not_called()

    # ---------- missing configuration ----------

    def test_missing_credentials_fails_safely_and_logs_no_secrets(self):
        empty = {k: "" for k in GMAIL_KEYS}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, empty, clear=False):
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(GmailMailError) as ctx:
                    send_verification_email("user@example.com", "123456")
        self.assertEqual(str(ctx.exception), "Gmail API not configured")
        for secret in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_REFRESH_TOKEN):
            self.assertNotIn(secret, buf.getvalue())
            self.assertNotIn(secret, str(ctx.exception))

    # ---------- error handling ----------

    def test_http_error_handled_safely(self):
        from googleapiclient.errors import HttpError
        resp = mock.Mock(status=403)
        err = HttpError(resp, b'{"error":{"message":"insufficient-permissions"}}')
        service = self._fake_service(execute_error=err)
        buf = io.StringIO()
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(GmailMailError) as ctx:
                send_verification_email("user@example.com", "123456")
        self.assertEqual(str(ctx.exception), "Gmail delivery failed")
        logs = buf.getvalue()
        self.assertIn("403", logs)
        self.assertNotIn("insufficient-permissions", logs)
        for secret in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_REFRESH_TOKEN):
            self.assertNotIn(secret, logs)

    def test_refresh_error_handled_safely_with_actionable_log(self):
        from google.auth.exceptions import RefreshError
        err = RefreshError(
            "unauthorized_client: Client is unauthorized to retrieve access "
            "tokens using this method")
        service = self._fake_service(execute_error=err)
        buf = io.StringIO()
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(GmailMailError) as ctx:
                send_verification_email("user@example.com", "123456")
        self.assertEqual(str(ctx.exception), "Gmail delivery failed")
        logs = buf.getvalue()
        self.assertIn("unauthorized_client", logs)
        self.assertIn("GMAIL_CLIENT_ID", logs)
        for secret in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_REFRESH_TOKEN):
            self.assertNotIn(secret, logs)

    def test_generic_error_handled_safely(self):
        service = self._fake_service(execute_error=RuntimeError("boom"))
        buf = io.StringIO()
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(GmailMailError) as ctx:
                send_verification_email("user@example.com", "123456")
        self.assertEqual(str(ctx.exception), "Gmail delivery failed")
        logs = buf.getvalue()
        self.assertIn("RuntimeError", logs)
        self.assertNotIn("boom", logs)

    def test_success_logs_never_contain_secrets(self):
        service = self._fake_service(execute_result={"id": "msg-1"})
        buf = io.StringIO()
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False), \
             contextlib.redirect_stdout(buf):
            send_verification_email("user@example.com", "123456")
        logs = buf.getvalue()
        self.assertIn("messageId=msg-1", logs)
        for secret in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_REFRESH_TOKEN):
            self.assertNotIn(secret, logs)
        self.assertNotIn("Bearer ", logs)

    # ---------- send_feedback_email (shared Gmail API path) ----------

    def test_feedback_email_uses_shared_path_and_correct_mime(self):
        service = self._fake_service(execute_result={"id": "msg-fb1"})
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            result = send_feedback_email(
                "user@example.com", "BiteWise Feedback",
                "<html><body><p>Thanks for the feedback</p></body></html>")

        self.assertEqual(result, {"messageId": "msg-fb1", "sent": True})
        self.assertEqual(self.send_kwargs["userId"], "me")
        self.assertIn("raw", self.send_kwargs["body"])
        msg = email.message_from_bytes(self._decode_raw())
        self.assertEqual(msg["From"], "mybitewise@gmail.com")
        self.assertEqual(msg["To"], "user@example.com")
        self.assertEqual(msg["Subject"], "BiteWise Feedback")
        self.assertEqual(msg.get_content_type(), "text/html")
        body = msg.get_payload(decode=True).decode("utf-8")
        self.assertIn("Thanks for the feedback", body)

    def test_feedback_email_invalid_recipient_rejected_without_api_call(self):
        service = mock.Mock()
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            with self.assertRaises(GmailMailError) as ctx:
                send_feedback_email("not-an-email", "Subject", "<html></html>")
        self.assertEqual(str(ctx.exception), "Invalid recipient email address")
        service.users.assert_not_called()

    def test_feedback_email_missing_credentials_fails_safely(self):
        empty = {k: "" for k in GMAIL_KEYS}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, empty, clear=False), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(GmailMailError) as ctx:
                send_feedback_email("user@example.com", "Subject", "<html></html>")
        self.assertEqual(str(ctx.exception), "Gmail API not configured")
        for secret in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_REFRESH_TOKEN):
            self.assertNotIn(secret, buf.getvalue())
            self.assertNotIn(secret, str(ctx.exception))

    def test_feedback_email_error_handled_safely(self):
        from googleapiclient.errors import HttpError
        resp = mock.Mock(status=403)
        err = HttpError(resp, b'{"error":{"message":"quota-exceeded"}}')
        service = self._fake_service(execute_error=err)
        buf = io.StringIO()
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(GmailMailError) as ctx:
                send_feedback_email("user@example.com", "Subject", "<html></html>")
        self.assertEqual(str(ctx.exception), "Gmail delivery failed")
        logs = buf.getvalue()
        self.assertIn("403", logs)
        self.assertNotIn("quota-exceeded", logs)
        for secret in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_REFRESH_TOKEN):
            self.assertNotIn(secret, logs)

    # ---------- send_rating_email (shared Gmail API path) ----------

    def test_rating_email_uses_shared_path_and_correct_mime(self):
        service = self._fake_service(execute_result={"id": "msg-rate1"})
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            result = send_rating_email(
                "user@example.com", "BiteWise Rating — 5/5",
                "<html><body><p><strong>Rating:</strong> 5/5</p></body></html>")

        self.assertEqual(result, {"messageId": "msg-rate1", "sent": True})
        self.assertEqual(self.send_kwargs["userId"], "me")
        self.assertIn("raw", self.send_kwargs["body"])
        msg = email.message_from_bytes(self._decode_raw())
        self.assertEqual(msg["From"], "mybitewise@gmail.com")
        self.assertEqual(msg["To"], "user@example.com")
        # Non-ASCII subjects are RFC 2047 encoded by EmailMessage.
        decoded_subject = "".join(
            part.decode(charset or "utf-8") if isinstance(part, bytes) else part
            for part, charset in email.header.decode_header(msg["Subject"]))
        self.assertEqual(decoded_subject, "BiteWise Rating — 5/5")
        self.assertEqual(msg.get_content_type(), "text/html")
        body = msg.get_payload(decode=True).decode("utf-8")
        self.assertIn("Rating:</strong> 5/5", body)

    def test_rating_email_invalid_recipient_rejected_without_api_call(self):
        service = mock.Mock()
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            with self.assertRaises(GmailMailError) as ctx:
                send_rating_email("not-an-email", "Subject", "<html></html>")
        self.assertEqual(str(ctx.exception), "Invalid recipient email address")
        service.users.assert_not_called()

    def test_rating_email_error_handled_safely(self):
        from googleapiclient.errors import HttpError
        resp = mock.Mock(status=400)
        err = HttpError(resp, b'{"error":{"message":"invalid-shape"}}')
        service = self._fake_service(execute_error=err)
        buf = io.StringIO()
        with mock.patch.object(gmail_mailer, "build", return_value=service), \
             mock.patch.dict(os.environ, FAKE_ENV, clear=False), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(GmailMailError) as ctx:
                send_rating_email("user@example.com", "Subject", "<html></html>")
        self.assertEqual(str(ctx.exception), "Gmail delivery failed")
        logs = buf.getvalue()
        self.assertIn("400", logs)
        self.assertNotIn("invalid-shape", logs)
        for secret in (FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, FAKE_REFRESH_TOKEN):
            self.assertNotIn(secret, logs)

    # ---------- auth_service integration ----------

    def test_auth_service_converts_mail_error_to_email_delivery_error(self):
        with mock.patch.object(
                auth_service.gmail_mailer, "send_verification_email",
                side_effect=GmailMailError("Gmail delivery failed")):
            with self.assertRaises(EmailDeliveryError):
                auth_service._send_verification_email("user@example.com", "123456")

    def test_auth_service_success_propagates_safely(self):
        with mock.patch.object(
                auth_service.gmail_mailer, "send_verification_email",
                return_value={"messageId": "msg-1", "sent": True}):
            auth_service._send_verification_email("user@example.com", "123456")


if __name__ == "__main__":
    unittest.main()
