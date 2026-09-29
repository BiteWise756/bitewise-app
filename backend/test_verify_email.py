"""Unit tests for auth (AuthService) with a mocked Firestore, Firebase Auth
and Identity Toolkit REST endpoint.

Run:  python test_verify_email.py   (from the backend/ directory)
"""
import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import auth_service
from services.auth_service import AuthService, AuthConfigError, EmailDeliveryError

FAKE_API_KEY = "FAKE_KEY_1234567890"


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeDoc:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data)


class FakeDocRef:
    def __init__(self, store, uid):
        self._store = store
        self._uid = uid

    def get(self):
        return FakeDoc(self._store.get(self._uid))

    def set(self, data):
        self._store[self._uid] = dict(data)

    def update(self, data):
        d = dict(self._store.get(self._uid) or {})
        d.update(data)
        self._store[self._uid] = d

    def delete(self):
        self._store.pop(self._uid, None)


class FakeCol:
    def __init__(self, store):
        self._store = store

    def document(self, uid):
        return FakeDocRef(self._store, uid)

    def where(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def get(self):
        return []


class FakeAuth:
    class EmailAlreadyExistsError(Exception):
        pass

    class UserNotFoundError(Exception):
        pass

    def __init__(self):
        self.users = {}
        self.updated = []
        self.deleted = []

    def create_user(self, email, password, display_name=None, email_verified=False):
        if any(u["email"] == email for u in self.users.values()):
            raise FakeAuth.EmailAlreadyExistsError("already exists")
        uid = "uid_" + str(len(self.users) + 1)
        self.users[uid] = {
            "uid": uid,
            "email": email,
            "display_name": display_name,
            "email_verified": email_verified,
        }
        return SimpleObj(uid=uid)

    def create_custom_token(self, uid):
        return b"token-" + uid.encode()

    def get_user_by_email(self, email):
        for u in self.users.values():
            if u["email"] == email:
                return SimpleObj(
                    uid=u["uid"],
                    email=email,
                    display_name=u["display_name"],
                    email_verified=u.get("email_verified", False),
                )
        raise FakeAuth.UserNotFoundError("no user")

    def get_user(self, uid):
        u = self.users.get(uid)
        if not u:
            raise FakeAuth.UserNotFoundError("no user")
        return SimpleObj(
            uid=uid,
            email=u["email"],
            display_name=u["display_name"],
            email_verified=u.get("email_verified", False),
        )

    def update_user(self, uid, **kwargs):
        self.updated.append((uid, kwargs))

    def delete_user(self, uid):
        self.users.pop(uid, None)
        self.deleted.append(uid)


class SimpleObj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class VerifyEmailTest(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.auth = FakeAuth()
        self.db = mock.Mock()
        self.db.collection.side_effect = lambda name: FakeCol(self.store) if name == "users" else None
        self.db.collection("users")
        self.sent = []

        self.post = mock.patch.object(auth_service.requests, "post")
        self.mock_post = self.post.start()
        self.addCleanup(self.post.stop)
        self.mock_post.return_value = FakeResp(200, {"idToken": "tok", "email": "x"})

        patchers = [
            mock.patch.object(auth_service, "auth", self.auth),
            mock.patch.object(auth_service, "firestore", SimpleObj(SERVER_TIMESTAMP="TIMESTAMP")),
            mock.patch.object(auth_service, "get_db", return_value=self.db),
            mock.patch.object(auth_service, "FIREBASE_API_KEY", FAKE_API_KEY),
            mock.patch.object(auth_service, "_send_verification_email",
                              side_effect=lambda email, link: self.sent.append((email, link)) or None),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    # ---------- helpers ----------

    def _signup(self, email="user@example.com"):
        return AuthService.create_user(email, "secret123", "Test User")

    def _make_verified_user(self, email="user@example.com"):
        """A fully verified account: Firebase says verified, Firestore agrees."""
        self.auth.create_user(email=email, password="secret123", display_name="Test User", email_verified=True)
        uid = list(self.auth.users.keys())[-1]
        self.store[uid] = {"uid": uid, "email": email, "display_id": "USR-AAA11", "emailVerified": True}
        return uid

    def _set_rest(self, status_code, error_message=None):
        if error_message is None:
            self.mock_post.return_value = FakeResp(status_code, {"idToken": "tok", "email": "x"})
        else:
            self.mock_post.return_value = FakeResp(status_code, {"error": {"message": error_message}})

    # ---------- signup ----------

    def test_signup_creates_unverified_user_and_sends_otp(self):
        result = self._signup()
        self.assertFalse(result["emailVerified"])
        doc = self.store[result["uid"]]
        self.assertFalse(doc["emailVerified"])
        self.assertTrue(doc["verifyOtpHash"])
        self.assertTrue(doc["verifyOtpSalt"])
        self.assertTrue(doc["verifyOtpExpiresAt"])
        self.assertEqual(doc["verifyOtpAttempts"], 0)
        self.assertEqual(len(self.sent), 1)
        email, otp = self.sent[0]
        self.assertEqual(email, "user@example.com")
        # The code must be a 6-digit numeric string.
        self.assertRegex(otp, r"^\d{6}$")
        # The plaintext code must NOT be stored - only its salted hash.
        self.assertNotEqual(doc["verifyOtpHash"], otp)
        self.assertEqual(auth_service._hash_otp(otp, doc["verifyOtpSalt"]), doc["verifyOtpHash"])
        # A brand-new account has NOT completed onboarding.
        self.assertFalse(doc["onboardingCompleted"])

    def test_signup_cleans_up_when_email_delivery_fails(self):
        with mock.patch.object(auth_service, "_send_verification_email",
                               side_effect=EmailDeliveryError("SMTP not configured")):
            with self.assertRaises(EmailDeliveryError):
                AuthService.create_user("user@example.com", "secret123", "Test User")
        # Account must be rolled back: no Firestore doc, no Firebase user.
        self.assertEqual(self.store, {})
        self.assertEqual(self.auth.users, {})
        self.assertEqual(len(self.auth.deleted), 1)

    def test_signup_unexpected_send_failure_rolls_back_no_orphan(self):
        # Regression: any failure after account creation (not just a known
        # EmailDeliveryError) must NOT leave an orphaned Auth user or
        # Firestore document behind.
        with mock.patch.object(auth_service, "_send_verification_email",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(EmailDeliveryError) as ctx:
                AuthService.create_user("user@example.com", "secret123", "Test User")
        self.assertIn("could not be sent", str(ctx.exception).lower())
        self.assertEqual(self.store, {})
        self.assertEqual(self.auth.users, {})
        self.assertEqual(len(self.auth.deleted), 1)

    def test_signup_duplicate_email_rejected(self):
        self.auth.create_user(email="dup@example.com", password="x")
        with self.assertRaises(ValueError) as ctx:
            AuthService.create_user("dup@example.com", "secret123", "Test User")
        self.assertIn("Email already exists", str(ctx.exception))

    # ---------- verification (OTP) ----------

    def _sent_otp(self):
        return self.sent[-1][1]

    def test_verify_incorrect_code_rejected_with_email_attached(self):
        result = self._signup()
        with self.assertRaises(ValueError) as ctx:
            AuthService.verify_email("user@example.com", "000000")
        self.assertIn("Incorrect verification code", str(ctx.exception))
        self.assertEqual(ctx.exception.email, "user@example.com")
        # The failed attempt must be counted.
        self.assertEqual(self.store[result["uid"]]["verifyOtpAttempts"], 1)

    def test_verify_malformed_code_rejected(self):
        self._signup()
        for bad in ("", "abc", "12", "1234567", "abc123"):
            with self.assertRaises(ValueError) as ctx:
                AuthService.verify_email("user@example.com", bad)
            self.assertIn("6-digit", str(ctx.exception))

    def test_verify_correct_code_success_syncs_firestore(self):
        result = self._signup()
        out = AuthService.verify_email("user@example.com", self._sent_otp())
        self.assertTrue(out["verified"])
        self.assertEqual(out["source"], "otp")
        doc = self.store[result["uid"]]
        self.assertTrue(doc["emailVerified"])
        self.assertIsNone(doc["verifyOtpHash"])
        self.assertIsNone(doc["verifyOtpSalt"])
        self.assertIsNone(doc["verifyOtpExpiresAt"])
        self.assertIsNone(doc["verifyOtpAttempts"])
        self.assertEqual(self.auth.updated, [(result["uid"], {"email_verified": True})])

    def test_verify_correct_code_mints_session_and_reports_onboarding(self):
        # OTP success must hand back a usable session (uid + token) so the
        # client can continue to onboarding without re-entering credentials,
        # and must NOT claim onboarding is complete (OTP != onboarding).
        result = self._signup()
        doc_before = self.store[result["uid"]]
        out = AuthService.verify_email("user@example.com", self._sent_otp())
        self.assertEqual(out["uid"], result["uid"])
        self.assertEqual(out["display_id"], doc_before["display_id"])
        self.assertEqual(out["token"], "tok")  # mocked Identity Toolkit idToken
        self.assertFalse(out["onboardingCompleted"])

    def test_verify_correct_code_survives_session_mint_failure(self):
        # Even if the ID token exchange fails, verification itself must
        # succeed (the client then falls back to a normal sign-in).
        result = self._signup()
        with mock.patch.object(auth_service, "_exchange_id_token",
                               side_effect=AuthConfigError("toolkit down")):
            out = AuthService.verify_email("user@example.com", self._sent_otp())
        self.assertTrue(out["verified"])
        self.assertIsNone(out["token"])
        self.assertTrue(self.store[result["uid"]]["emailVerified"])

    def test_verify_otp_reuse_after_verification_rejected(self):
        # An already-verified account must never be re-verified with an old code.
        result = self._signup()
        old_otp = self._sent_otp()
        AuthService.verify_email("user@example.com", old_otp)
        # A second attempt with the same (already used) code is treated as
        # "already verified" and the code is gone - it cannot be reused.
        out = AuthService.verify_email("user@example.com", old_otp)
        self.assertTrue(out["already_verified"])
        self.assertTrue(out["verified"])
        self.assertIsNone(self.store[result["uid"]]["verifyOtpHash"])
        self.assertEqual(len(self.auth.updated), 1)

    def test_verify_expired_otp_rejected_with_email_attached(self):
        result = self._signup()
        self.store[result["uid"]]["verifyOtpExpiresAt"] = datetime.utcnow() - timedelta(minutes=11)
        with self.assertRaises(ValueError) as ctx:
            AuthService.verify_email("user@example.com", self._sent_otp())
        self.assertIn("expired", str(ctx.exception))
        self.assertEqual(ctx.exception.email, "user@example.com")
        # Expired code must be invalidated (cannot keep guessing it).
        self.assertIsNone(self.store[result["uid"]]["verifyOtpHash"])

    def test_verify_max_attempts_invalidates_otp(self):
        result = self._signup()
        for i in range(auth_service.VERIFY_OTP_MAX_ATTEMPTS - 1):
            with self.assertRaises(ValueError):
                AuthService.verify_email("user@example.com", "000000")
        self.assertEqual(self.store[result["uid"]]["verifyOtpAttempts"],
                         auth_service.VERIFY_OTP_MAX_ATTEMPTS - 1)
        # The attempt that reaches the limit invalidates the code.
        with self.assertRaises(ValueError) as ctx:
            AuthService.verify_email("user@example.com", "000000")
        self.assertIn("Too many incorrect attempts", str(ctx.exception))
        self.assertIsNone(self.store[result["uid"]]["verifyOtpHash"])
        # Even the correct code no longer works after invalidation.
        with self.assertRaises(ValueError) as ctx:
            AuthService.verify_email("user@example.com", self._sent_otp())
        self.assertIn("No active verification code", str(ctx.exception))

    def test_verify_unknown_user_rejected(self):
        with self.assertRaises(ValueError):
            AuthService.verify_email("nope@example.com", "123456")

    def test_verify_already_verified_is_idempotent(self):
        self._signup()
        self.store[list(self.auth.users.keys())[-1]]["emailVerified"] = True
        out = AuthService.verify_email("user@example.com", "123456")
        self.assertTrue(out["already_verified"])
        self.assertTrue(out["verified"])

    # ---------- login matrix (fail-closed) ----------

    def test_login_correct_password_verified_email_success(self):
        uid = self._make_verified_user()
        self._set_rest(200)
        out = AuthService.login_user("user@example.com", "secret123")
        self.assertEqual(out["uid"], uid)

    def test_login_reports_onboarding_status(self):
        uid = self._make_verified_user()
        self._set_rest(200)
        out = AuthService.login_user("user@example.com", "secret123")
        # A legacy doc without the flag must be reported as NOT onboarded so
        # the client routes the user through the onboarding wizard.
        self.assertFalse(out["onboardingCompleted"])
        self.store[uid]["onboardingCompleted"] = True
        out = AuthService.login_user("user@example.com", "secret123")
        self.assertTrue(out["onboardingCompleted"])

    def test_update_profile_persists_onboarding_completion(self):
        result = self._signup()
        AuthService.update_profile(result["uid"], {"goal": "Weight Loss", "onboardingCompleted": True})
        self.assertTrue(self.store[result["uid"]]["onboardingCompleted"])
        self.assertEqual(self.store[result["uid"]]["goal"], "Weight Loss")
        # Server-controlled / identity fields stay locked out.
        AuthService.update_profile(result["uid"], {"display_id": "HACKED", "emailVerified": True})
        self.assertNotEqual(self.store[result["uid"]].get("display_id"), "HACKED")
        self.assertNotEqual(self.store[result["uid"]].get("emailVerified"), True)

    def test_login_wrong_password_verified_email_rejected(self):
        self._make_verified_user()
        self._set_rest(400, "INVALID_LOGIN_CREDENTIALS")
        with self.assertRaises(ValueError) as ctx:
            AuthService.login_user("user@example.com", "wrong-password")
        self.assertIn("Invalid email or password", str(ctx.exception))

    def test_login_correct_password_unverified_email_rejected(self):
        self.auth.create_user(email="user@example.com", password="secret123", display_name="Test User", email_verified=False)
        uid = list(self.auth.users.keys())[-1]
        self.store[uid] = {"uid": uid, "email": "user@example.com", "display_id": "USR-AAA11", "emailVerified": False}
        self._set_rest(200)
        with self.assertRaises(ValueError) as ctx:
            AuthService.login_user("user@example.com", "secret123")
        self.assertIn("Email not verified", str(ctx.exception))

    def test_login_wrong_password_unverified_email_rejected(self):
        self.auth.create_user(email="user@example.com", password="secret123", display_name="Test User", email_verified=False)
        uid = list(self.auth.users.keys())[-1]
        self.store[uid] = {"uid": uid, "email": "user@example.com", "display_id": "USR-AAA11", "emailVerified": False}
        self._set_rest(400, "INVALID_LOGIN_CREDENTIALS")
        with self.assertRaises(ValueError) as ctx:
            AuthService.login_user("user@example.com", "wrong-password")
        self.assertIn("Invalid email or password", str(ctx.exception))

    def test_login_nonexistent_email_rejected(self):
        self._set_rest(400, "EMAIL_NOT_FOUND")
        with self.assertRaises(ValueError) as ctx:
            AuthService.login_user("ghost@example.com", "whatever")
        self.assertIn("Invalid email or password", str(ctx.exception))

    def test_login_invalid_api_key_fails_closed(self):
        self._make_verified_user()
        self._set_rest(400, "API_KEY_INVALID")
        with self.assertRaises(AuthConfigError):
            AuthService.login_user("user@example.com", "secret123")
        # Must never produce a "success" or even "Invalid email or password"
        # for a configuration failure - the API key situation is a server
        # problem, and the password must not be accepted.
        self.mock_post.assert_called_once()

    def test_login_missing_api_key_fails_closed(self):
        self._make_verified_user()
        with mock.patch.object(auth_service, "FIREBASE_API_KEY", None):
            with self.assertRaises(AuthConfigError):
                AuthService.login_user("user@example.com", "secret123")
        self.mock_post.assert_not_called()

    def test_login_network_error_fails_closed(self):
        self._make_verified_user()
        self.mock_post.side_effect = Exception("connection refused")
        with self.assertRaises(AuthConfigError):
            AuthService.login_user("user@example.com", "secret123")

    def test_login_unexpected_response_fails_closed(self):
        self._make_verified_user()
        self._set_rest(500, "INTERNAL_ERROR")
        with self.assertRaises(AuthConfigError):
            AuthService.login_user("user@example.com", "secret123")

    def test_login_syncs_firebase_verified_to_firestore(self):
        # Firebase says verified, Firestore mirror is stale -> login must
        # succeed AND heal the mirror.
        self.auth.create_user(email="user@example.com", password="secret123", display_name="Test User", email_verified=True)
        uid = list(self.auth.users.keys())[-1]
        self.store[uid] = {"uid": uid, "email": "user@example.com", "display_id": "USR-AAA11", "emailVerified": False}
        self._set_rest(200)
        out = AuthService.login_user("user@example.com", "secret123")
        self.assertEqual(out["uid"], uid)
        self.assertTrue(self.store[uid]["emailVerified"])

    def test_login_legacy_account_without_verification_field_allowed(self):
        self.auth.create_user(email="legacy@example.com", password="x", email_verified=False)
        uid = list(self.auth.users.keys())[-1]
        self.store[uid] = {"uid": uid, "email": "legacy@example.com", "display_id": "USR-ABC12"}
        self._set_rest(200)
        out = AuthService.login_user("legacy@example.com", "x")
        self.assertEqual(out["uid"], uid)

    # ---------- resend ----------

    def test_resend_returns_already_verified_when_verified(self):
        result = self._signup()
        self.store[result["uid"]]["emailVerified"] = True
        out = AuthService.resend_verification("user@example.com")
        self.assertTrue(out["already_verified"])
        self.assertEqual(len(self.sent), 1)  # no second email

    def test_resend_regenerates_and_sends_new_otp(self):
        result = self._signup()
        old_otp = self._sent_otp()
        old_hash = self.store[result["uid"]]["verifyOtpHash"]
        self.store[result["uid"]]["lastVerifySentAt"] = datetime.utcnow() - timedelta(minutes=5)
        out = AuthService.resend_verification("user@example.com")
        self.assertTrue(out["sent"])
        self.assertEqual(len(self.sent), 2)
        email, new_otp = self.sent[1]
        self.assertEqual(email, "user@example.com")
        self.assertRegex(new_otp, r"^\d{6}$")
        self.assertNotEqual(new_otp, old_otp)
        new_hash = self.store[result["uid"]]["verifyOtpHash"]
        self.assertNotEqual(new_hash, old_hash)
        # Attempt counter must reset with the fresh code.
        self.assertEqual(self.store[result["uid"]]["verifyOtpAttempts"], 0)
        # New code must verify successfully.
        out2 = AuthService.verify_email("user@example.com", new_otp)
        self.assertTrue(out2["verified"])

    def test_resend_rate_limited(self):
        result = self._signup()
        self.store[result["uid"]]["lastVerifySentAt"] = datetime.utcnow()
        with self.assertRaises(ValueError) as ctx:
            AuthService.resend_verification("user@example.com")
        self.assertIn("wait a minute", str(ctx.exception))
        self.assertEqual(len(self.sent), 1)

    def test_resend_unknown_email_rejected(self):
        with self.assertRaises(ValueError):
            AuthService.resend_verification("ghost@example.com")

    def test_resend_failure_does_not_advance_cooldown(self):
        result = self._signup()
        self.store[result["uid"]]["lastVerifySentAt"] = datetime.utcnow() - timedelta(minutes=5)
        last_sent = self.store[result["uid"]]["lastVerifySentAt"]
        with mock.patch.object(auth_service, "_send_verification_email",
                               side_effect=EmailDeliveryError("SMTP not configured")):
            with self.assertRaises(EmailDeliveryError):
                AuthService.resend_verification("user@example.com")
        # Cooldown must not advance when nothing was actually sent, so the
        # user can retry immediately instead of being rate-limited on a lie.
        self.assertEqual(self.store[result["uid"]]["lastVerifySentAt"], last_sent)

    # ---------- log hygiene ----------

    def test_login_error_logs_contain_no_api_key(self):
        self._make_verified_user()
        self._set_rest(400, "API_KEY_INVALID")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(AuthConfigError):
                AuthService.login_user("user@example.com", "secret123")
        output = buf.getvalue()
        self.assertNotIn(FAKE_API_KEY, output)
        self.assertNotIn("secret123", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
