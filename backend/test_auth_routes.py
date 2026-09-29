"""HTTP-level tests for the /auth routes (error mapping to status codes).

Calls the route handlers directly (the installed starlette/httpx versions
are incompatible with TestClient). Equivalent to exercising the router.

Run:  python test_auth_routes.py   (from the backend/ directory)
"""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from models.schemas import AuthRequest, ResendVerifyRequest, VerifyRequest
from routes.auth import signup, login, verify_email, resend_verification
from services.auth_service import AuthService, AuthConfigError, EmailDeliveryError


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _call(handler, *args, **kwargs):
    """Invoke a route handler, normalizing HTTPException/JSONResponse
    into (status_code, payload) so tests stay simple."""
    try:
        out = _run(handler(*args, **kwargs))
    except HTTPException as e:
        return e.status_code, e.detail
    if isinstance(out, JSONResponse):
        return out.status_code, json.loads(out.body.decode("utf-8"))
    return 200, out


class AuthRoutesTest(unittest.TestCase):
    def test_login_correct_credentials_returns_200(self):
        with mock.patch.object(AuthService, "login_user", return_value={"uid": "u1", "token": "t"}):
            status, payload = _call(login, AuthRequest(email="a@b.com", password="pw"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])

    def test_login_wrong_password_returns_401(self):
        with mock.patch.object(AuthService, "login_user",
                               side_effect=ValueError("Invalid email or password")):
            status, detail = _call(login, AuthRequest(email="a@b.com", password="nope"))
        self.assertEqual(status, 401)
        self.assertEqual(detail, "Invalid email or password")

    def test_login_unverified_returns_401(self):
        with mock.patch.object(AuthService, "login_user",
                               side_effect=ValueError("Email not verified. Please verify your email before signing in.")):
            status, detail = _call(login, AuthRequest(email="a@b.com", password="pw"))
        self.assertEqual(status, 401)
        self.assertIn("Email not verified", detail)

    def test_login_invalid_api_key_returns_503_friendly(self):
        with mock.patch.object(AuthService, "login_user", side_effect=AuthConfigError("boom")):
            status, detail = _call(login, AuthRequest(email="a@b.com", password="pw"))
        self.assertEqual(status, 503)
        # The client must never see internal configuration details.
        self.assertNotIn("API key", detail)
        self.assertNotIn("boom", detail)

    def test_login_unexpected_error_returns_500_friendly(self):
        with mock.patch.object(AuthService, "login_user", side_effect=RuntimeError("secret detail")):
            status, detail = _call(login, AuthRequest(email="a@b.com", password="pw"))
        self.assertEqual(status, 500)
        self.assertNotIn("secret detail", detail)

    def test_signup_duplicate_email_returns_400(self):
        with mock.patch.object(AuthService, "create_user", side_effect=ValueError("Email already exists")):
            status, detail = _call(signup, AuthRequest(email="a@b.com", password="pw", displayName="A"))
        self.assertEqual(status, 400)
        self.assertEqual(detail, "Email already exists")

    def test_signup_rejects_invalid_email_returns_400(self):
        with mock.patch.object(AuthService, "create_user",
                               side_effect=ValueError("Please enter a valid email address.")):
            status, detail = _call(signup, AuthRequest(email="not-an-email", password="password123", displayName="A"))
        self.assertEqual(status, 400)
        self.assertEqual(detail, "Please enter a valid email address.")

    def test_signup_rejects_short_password_returns_400(self):
        with mock.patch.object(AuthService, "create_user",
                               side_effect=ValueError("Password must be at least 6 characters long.")):
            status, detail = _call(signup, AuthRequest(email="a@b.com", password="12345", displayName="A"))
        self.assertEqual(status, 400)
        self.assertIn("at least 6", detail)

    def test_create_user_validates_input_before_firebase(self):
        # Validation raises before any Firebase SDK call is made, so this is
        # safe to exercise without credentials or a network.
        with self.assertRaises(ValueError) as cm:
            AuthService.create_user("not-an-email", "password123", "A")
        self.assertIn("valid email", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            AuthService.create_user("a@b.com", "12345", "A")
        self.assertIn("at least 6", str(cm.exception))

    def test_signup_delivery_failure_returns_503(self):
        with mock.patch.object(AuthService, "create_user", side_effect=EmailDeliveryError("no smtp")):
            status, detail = _call(signup, AuthRequest(email="a@b.com", password="pw", displayName="A"))
        self.assertEqual(status, 503)
        self.assertIn("verification email", detail)

    def test_verify_email_correct_code_success(self):
        with mock.patch.object(AuthService, "verify_email",
                               return_value={"email": "a@b.com", "verified": True, "already_verified": False}):
            status, payload = _call(verify_email, VerifyRequest(email="a@b.com", code="123456"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["verified"])

    def test_verify_email_error_attaches_email_for_resend(self):
        e = ValueError("Incorrect verification code. Please try again.")
        e.email = "a@b.com"
        with mock.patch.object(AuthService, "verify_email", side_effect=e):
            status, payload = _call(verify_email, VerifyRequest(email="a@b.com", code="000000"))
        self.assertEqual(status, 400)
        self.assertEqual(payload["email"], "a@b.com")

    def test_resend_verification_success(self):
        with mock.patch.object(AuthService, "resend_verification", return_value={"sent": True, "email": "a@b.com"}):
            status, payload = _call(resend_verification, ResendVerifyRequest(email="a@b.com"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["sent"])

    def test_resend_delivery_failure_returns_503(self):
        with mock.patch.object(AuthService, "resend_verification", side_effect=EmailDeliveryError("no smtp")):
            status, detail = _call(resend_verification, ResendVerifyRequest(email="a@b.com"))
        self.assertEqual(status, 503)
        self.assertIn("verification email", detail)

    def test_resend_rate_limited_returns_400(self):
        with mock.patch.object(AuthService, "resend_verification",
                               side_effect=ValueError("Please wait a minute before requesting another email.")):
            status, detail = _call(resend_verification, ResendVerifyRequest(email="a@b.com"))
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
