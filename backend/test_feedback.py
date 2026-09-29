"""Tests for the /auth/feedback endpoint and AuthService.submit_feedback.

The Gmail API is fully mocked, so no network and no real OAuth is ever used.
These tests verify server-side validation, safe error mapping, per-IP rate
limiting, HTML escaping, and that no secrets leak into responses.

Run:  python test_feedback.py   (from the backend/ directory)
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import HTTPException  # noqa: E402

from models.schemas import FeedbackRequest  # noqa: E402
from routes import auth as routes_auth  # noqa: E402
from routes.auth import submit_feedback  # noqa: E402
from services import auth_service, gmail_mailer  # noqa: E402
from services.auth_service import AuthService, EmailDeliveryError  # noqa: E402
from services.gmail_mailer import GmailMailError  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _fake_http_request(host="127.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def _call_handler(handler, body, host="127.0.0.1"):
    """Invoke a route handler, normalizing HTTPException into
    (status_code, detail)."""
    try:
        out = _run(handler(body, _fake_http_request(host)))
    except HTTPException as e:
        return e.status_code, e.detail
    return 200, out


class FeedbackServiceTest(unittest.TestCase):

    def test_valid_feedback_calls_mailer_with_sender_subject_and_escaped_body(self):
        with mock.patch.object(
                auth_service.gmail_mailer, "send_feedback_email",
                return_value={"messageId": "m1", "sent": True}) as send:
            result = AuthService.submit_feedback(
                "Alice", "alice@example.com", "Love the app!")
        send.assert_called_once()
        args = send.call_args[0]
        self.assertEqual(args[0], gmail_mailer.FIXED_SENDER)
        self.assertEqual(args[1], "BiteWise Feedback")
        self.assertIn("Love the app!", args[2])
        self.assertIn("Alice", args[2])
        self.assertIn("alice@example.com", args[2])
        self.assertEqual(result, {"sent": True, "subject": "BiteWise Feedback"})

    def test_html_payload_is_escaped(self):
        evil = '<script>alert("x")</script>'
        with mock.patch.object(
                auth_service.gmail_mailer, "send_feedback_email",
                return_value={"sent": True}) as send:
            AuthService.submit_feedback("A<b>", "a@b.com", evil)
        body = send.call_args[0][2]
        self.assertNotIn("<script>", body)
        self.assertNotIn("<b>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("&lt;b&gt;", body)

    def test_optional_fields_are_reported_when_absent(self):
        with mock.patch.object(
                auth_service.gmail_mailer, "send_feedback_email",
                return_value={"sent": True}) as send:
            AuthService.submit_feedback(None, None, "hello")
        body = send.call_args[0][2]
        self.assertIn("Not provided", body)

    def test_rejects_empty_message(self):
        with self.assertRaises(ValueError) as cm:
            AuthService.submit_feedback("Alice", None, "   ")
        self.assertIn("message", str(cm.exception).lower())

    def test_rejects_message_too_long(self):
        with self.assertRaises(ValueError):
            AuthService.submit_feedback(None, None, "x" * 5001)

    def test_rejects_name_too_long(self):
        with self.assertRaises(ValueError):
            AuthService.submit_feedback("x" * 121, None, "hi")

    def test_rejects_invalid_email(self):
        with self.assertRaises(ValueError):
            AuthService.submit_feedback("Alice", "not-an-email", "hi")

    def test_mail_error_becomes_email_delivery_error_without_secrets(self):
        with mock.patch.object(
                auth_service.gmail_mailer, "send_feedback_email",
                side_effect=GmailMailError("Gmail delivery failed")):
            with self.assertRaises(EmailDeliveryError) as cm:
                AuthService.submit_feedback(None, None, "hello")
        self.assertEqual(str(cm.exception), "Feedback email could not be sent")

    def test_no_credentials_leak_into_returned_result(self):
        fake_secrets = {"client_id": "s3cret-id", "secret": "s3cret-value"}
        with mock.patch.object(
                auth_service.gmail_mailer, "send_feedback_email",
                return_value=fake_secrets):
            result = AuthService.submit_feedback(None, None, "hello")
        self.assertEqual(
            result, {"sent": True, "subject": "BiteWise Feedback"})


class FeedbackRouteTest(unittest.TestCase):

    def setUp(self):
        routes_auth._feedback_log.clear()

    def test_valid_submission_returns_200(self):
        with mock.patch.object(
                AuthService, "submit_feedback",
                return_value={"sent": True, "subject": "BiteWise Feedback"}):
            status, payload = _call_handler(
                submit_feedback,
                FeedbackRequest(name="Ali", email="ali@e.com", message="Nice"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])

    def test_validation_error_returns_400_with_user_message(self):
        with mock.patch.object(
                AuthService, "submit_feedback",
                side_effect=ValueError("Please enter a feedback message.")):
            status, detail = _call_handler(
                submit_feedback,
                FeedbackRequest(name=None, email=None, message=""))
        self.assertEqual(status, 400)
        self.assertEqual(detail, "Please enter a feedback message.")

    def test_delivery_error_returns_503_friendly(self):
        with mock.patch.object(
                AuthService, "submit_feedback",
                side_effect=EmailDeliveryError("Feedback email could not be sent")):
            status, detail = _call_handler(
                submit_feedback, FeedbackRequest(message="hello"))
        self.assertEqual(status, 503)
        self.assertIn("feedback email", detail.lower())
        self.assertNotIn("Gmail", detail)

    def test_unexpected_error_returns_500_without_internals(self):
        with mock.patch.object(
                AuthService, "submit_feedback",
                side_effect=RuntimeError("secret crash detail")):
            status, detail = _call_handler(
                submit_feedback, FeedbackRequest(message="hello"))
        self.assertEqual(status, 500)
        self.assertNotIn("secret crash detail", detail)
        self.assertNotIn("RuntimeError", detail)

    def test_rate_limited_after_max_submissions(self):
        with mock.patch.object(
                AuthService, "submit_feedback",
                return_value={"sent": True, "subject": "BiteWise Feedback"}):
            for i in range(routes_auth.FEEDBACK_MAX_PER_IP):
                status, _ = _call_handler(
                    submit_feedback, FeedbackRequest(message="m%d" % i))
                self.assertEqual(status, 200)
            status, detail = _call_handler(
                submit_feedback, FeedbackRequest(message="too many"))
        self.assertEqual(status, 429)
        self.assertIn("too many", detail.lower())

    def test_rate_limit_is_per_ip(self):
        with mock.patch.object(
                AuthService, "submit_feedback",
                return_value={"sent": True, "subject": "BiteWise Feedback"}):
            for i in range(routes_auth.FEEDBACK_MAX_PER_IP):
                _call_handler(
                    submit_feedback, FeedbackRequest(message="m%d" % i),
                    host="10.0.0.1")
            # A different IP is not blocked by the first IP's quota.
            status, _ = _call_handler(
                submit_feedback, FeedbackRequest(message="other ip"),
                host="10.0.0.2")
        self.assertEqual(status, 200)

    def test_does_not_require_authentication(self):
        # The endpoint must serve anonymous users; submitting with no token
        # should not reject the request before validation/mailing logic.
        with mock.patch.object(
                AuthService, "submit_feedback",
                return_value={"sent": True, "subject": "BiteWise Feedback"}):
            status, _ = _call_handler(
                submit_feedback, FeedbackRequest(message="anon"))
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)