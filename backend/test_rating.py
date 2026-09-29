"""Tests for the /auth/rating endpoint and AuthService.submit_rating.

The Gmail API is fully mocked, so no network and no real OAuth is ever used.
These tests verify server-side validation of the 1-5 rating, optional comment
handling, HTML escaping, safe error mapping, per-IP rate limiting and that no
secrets leak into responses.

Run:  python test_rating.py   (from the backend/ directory)
"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from models.schemas import RatingRequest  # noqa: E402
from routes import auth as routes_auth  # noqa: E402
from routes.auth import submit_rating  # noqa: E402
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


class RatingServiceTest(unittest.TestCase):

    def test_valid_rating_1_to_5_accepted(self):
        for rating in (1, 2, 3, 4, 5):
            with self.subTest(rating=rating), mock.patch.object(
                    auth_service.gmail_mailer, "send_rating_email",
                    return_value={"messageId": "m", "sent": True}) as send:
                result = AuthService.submit_rating(rating, None)
            send.assert_called_once()
            args = send.call_args[0]
            self.assertEqual(args[0], gmail_mailer.FIXED_SENDER)
            self.assertEqual(args[1], f"BiteWise Rating — {rating}/5")
            self.assertIn(f"{rating}/5", args[2])
            self.assertEqual(result, {"sent": True, "subject": f"BiteWise Rating — {rating}/5"})

    def test_missing_rating_rejected(self):
        with self.assertRaises(ValueError) as cm:
            AuthService.submit_rating(None, None)
        self.assertIn("whole number", str(cm.exception))

    def test_missing_rating_rejected_by_schema(self):
        with self.assertRaises(ValidationError):
            RatingRequest.model_validate({"comment": "only a comment"})

    def test_rating_below_1_rejected(self):
        for rating in (0, -1, -100):
            with self.subTest(rating=rating), self.assertRaises(ValueError) as cm:
                AuthService.submit_rating(rating, None)
            self.assertEqual(str(cm.exception), "Rating must be from 1 to 5 stars.")

    def test_rating_above_5_rejected(self):
        for rating in (6, 7, 100):
            with self.subTest(rating=rating), self.assertRaises(ValueError) as cm:
                AuthService.submit_rating(rating, None)
            self.assertEqual(str(cm.exception), "Rating must be from 1 to 5 stars.")

    def test_optional_comment_absent_reported(self):
        with mock.patch.object(
                auth_service.gmail_mailer, "send_rating_email",
                return_value={"sent": True}) as send:
            AuthService.submit_rating(5, "")
        body = send.call_args[0][2]
        self.assertIn("No comment provided.", body)
        self.assertIn("★", body)  # filled stars shown for rating 5

    def test_optional_comment_included_when_present(self):
        with mock.patch.object(
                auth_service.gmail_mailer, "send_rating_email",
                return_value={"sent": True}) as send:
            AuthService.submit_rating(4, "Excellent app!")
        body = send.call_args[0][2]
        self.assertIn("Excellent app!", body)
        self.assertIn("4/5", body)

    def test_comment_too_long_rejected(self):
        with self.assertRaises(ValueError) as cm:
            AuthService.submit_rating(5, "x" * 2001)
        self.assertIn("2000", str(cm.exception))

    def test_comment_html_is_escaped(self):
        evil = '<script>alert("x")</script>'
        with mock.patch.object(
                auth_service.gmail_mailer, "send_rating_email",
                return_value={"sent": True}) as send:
            AuthService.submit_rating(5, evil)
        body = send.call_args[0][2]
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_gmail_mailer_receives_rating_correctly(self):
        with mock.patch.object(
                auth_service.gmail_mailer, "send_rating_email",
                return_value={"messageId": "msg-5", "sent": True}) as send:
            AuthService.submit_rating(5, "Love it")
        send.assert_called_once()
        to, subject, html = send.call_args[0]
        self.assertEqual(to, gmail_mailer.FIXED_SENDER)
        self.assertEqual(subject, "BiteWise Rating — 5/5")
        self.assertIn("5/5", html)
        self.assertIn("★★★★★", html)
        self.assertIn("Love it", html)
        self.assertIn("Submitted", html)

    def test_delivery_failure_returns_safe_error(self):
        with mock.patch.object(
                auth_service.gmail_mailer, "send_rating_email",
                side_effect=GmailMailError("Gmail delivery failed")):
            with self.assertRaises(EmailDeliveryError) as cm:
                AuthService.submit_rating(5, None)
        self.assertEqual(str(cm.exception), "Rating email could not be sent")

    def test_no_credentials_leak_into_returned_result(self):
        fake_secrets = {"client_id": "s3cret-id", "secret": "s3cret-value"}
        with mock.patch.object(
                auth_service.gmail_mailer, "send_rating_email",
                return_value=fake_secrets):
            result = AuthService.submit_rating(3, None)
        self.assertEqual(
            result, {"sent": True, "subject": "BiteWise Rating — 3/5"})


class RatingRouteTest(unittest.TestCase):

    def setUp(self):
        routes_auth._feedback_log.clear()

    def test_valid_rating_returns_200(self):
        with mock.patch.object(
                AuthService, "submit_rating",
                return_value={"sent": True, "subject": "BiteWise Rating — 5/5"}):
            status, payload = _call_handler(
                submit_rating, RatingRequest(rating=5, comment="Great"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])

    def test_rating_below_1_returns_400(self):
        with mock.patch.object(
                AuthService, "submit_rating",
                side_effect=ValueError("Rating must be from 1 to 5 stars.")):
            status, detail = _call_handler(
                submit_rating, RatingRequest(rating=0, comment=None))
        self.assertEqual(status, 400)
        self.assertEqual(detail, "Rating must be from 1 to 5 stars.")

    def test_rating_above_5_returns_400(self):
        with mock.patch.object(
                AuthService, "submit_rating",
                side_effect=ValueError("Rating must be from 1 to 5 stars.")):
            status, detail = _call_handler(
                submit_rating, RatingRequest(rating=6, comment=None))
        self.assertEqual(status, 400)
        self.assertEqual(detail, "Rating must be from 1 to 5 stars.")

    def test_delivery_error_returns_503_friendly(self):
        with mock.patch.object(
                AuthService, "submit_rating",
                side_effect=EmailDeliveryError("Rating email could not be sent")):
            status, detail = _call_handler(
                submit_rating, RatingRequest(rating=5, comment=None))
        self.assertEqual(status, 503)
        self.assertIn("rating email", detail.lower())
        self.assertNotIn("Gmail", detail)

    def test_unexpected_error_returns_500_without_internals(self):
        with mock.patch.object(
                AuthService, "submit_rating",
                side_effect=RuntimeError("secret crash detail")):
            status, detail = _call_handler(
                submit_rating, RatingRequest(rating=5, comment=None))
        self.assertEqual(status, 500)
        self.assertNotIn("secret crash detail", detail)
        self.assertNotIn("RuntimeError", detail)

    def test_rate_limited_after_max_submissions(self):
        with mock.patch.object(
                AuthService, "submit_rating",
                return_value={"sent": True, "subject": "BiteWise Rating — 5/5"}):
            for i in range(routes_auth.FEEDBACK_MAX_PER_IP):
                status, _ = _call_handler(
                    submit_rating, RatingRequest(rating=5, comment="m%d" % i))
                self.assertEqual(status, 200)
            status, detail = _call_handler(
                submit_rating, RatingRequest(rating=5, comment="too many"))
        self.assertEqual(status, 429)
        self.assertIn("too many", detail.lower())

    def test_rate_limit_is_per_ip(self):
        with mock.patch.object(
                AuthService, "submit_rating",
                return_value={"sent": True, "subject": "BiteWise Rating — 5/5"}):
            for i in range(routes_auth.FEEDBACK_MAX_PER_IP):
                _call_handler(
                    submit_rating, RatingRequest(rating=5, comment="m%d" % i),
                    host="10.0.0.1")
            status, _ = _call_handler(
                submit_rating, RatingRequest(rating=5, comment="other ip"),
                host="10.0.0.2")
        self.assertEqual(status, 200)

    def test_does_not_require_authentication(self):
        with mock.patch.object(
                AuthService, "submit_rating",
                return_value={"sent": True, "subject": "BiteWise Rating — 5/5"}):
            status, _ = _call_handler(
                submit_rating, RatingRequest(rating=5, comment=None))
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)