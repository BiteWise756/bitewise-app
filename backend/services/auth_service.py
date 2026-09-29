import hashlib
import hmac
import os
import random
import re
import secrets
import string
from datetime import datetime, timedelta, timezone

import requests
from firebase_admin import auth, firestore
from firebase.config import get_db
from google.cloud.firestore import FieldFilter
from services import gmail_mailer
from typing import Optional, Dict, Any

FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000")
VERIFY_OTP_TTL = timedelta(minutes=int(os.getenv("VERIFY_OTP_TTL_MINUTES", "10")))
VERIFY_RESEND_COOLDOWN = timedelta(seconds=int(os.getenv("VERIFY_RESEND_COOLDOWN_SECONDS", "60")))
VERIFY_OTP_MAX_ATTEMPTS = int(os.getenv("VERIFY_OTP_MAX_ATTEMPTS", "5"))


class AuthConfigError(Exception):
    """Authentication backend is misconfigured or unreachable.

    Password correctness can never be confirmed under this condition, so
    callers must fail closed (never accept the password).
    """


class EmailDeliveryError(Exception):
    """The verification email could not be sent."""


# Identity Toolkit error messages (from the 400 "error.message" body field)
# that mean the supplied credentials are simply wrong. Everything else that
# is not an explicit HTTP 200 is treated as a configuration/availability
# problem and fails closed.
_CREDENTIAL_ERROR_CODES = {
    "EMAIL_NOT_FOUND",
    "INVALID_PASSWORD",
    "INVALID_LOGIN_CREDENTIALS",
    "INVALID_EMAIL_OR_PASSWORD",
    "MISSING_EMAIL",
    "MISSING_PASSWORD",
    "INVALID_EMAIL",
    "USER_DISABLED",
}


def _utcnow() -> datetime:
    """Naive UTC now. Firestore returns naive UTC datetimes, so we store naive."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _generate_display_id(db) -> str:
    for _ in range(100):
        did = "USR-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if not db.collection("users").where(filter=FieldFilter("display_id", "==", did)).limit(1).get():
            return did
    return "USR-" + str(random.randint(10000, 99999))


def _exchange_id_token(custom_token: str) -> str:
    """Exchange a custom token for a verifiable Firebase ID token.

    The backend returns ID tokens to the client so that the client can send
    ``Authorization: Bearer *** on subsequent requests, which the
    server verifies with ``auth.verify_id_token``. Custom tokens cannot be
    verified that way.
    """
    if not FIREBASE_API_KEY:
        print("[auth] FIREBASE_API_KEY is not configured - cannot issue session token")
        raise AuthConfigError("Firebase API key not configured")
    try:
        resp = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={FIREBASE_API_KEY}",
            json={"token": custom_token, "returnSecureToken": True},
            timeout=10,
        )
    except Exception as e:
        print(f"[auth] ID token exchange unreachable: {type(e).__name__}")
        raise AuthConfigError("Identity Toolkit unreachable")

    if resp.status_code != 200:
        print(f"[auth] ID token exchange failed: status={resp.status_code}")
        raise AuthConfigError("Identity Toolkit rejected the session token request")

    try:
        payload = resp.json()
    except Exception:
        raise AuthConfigError("Identity Toolkit returned an invalid response")

    id_token = payload.get("idToken")
    if not id_token:
        raise AuthConfigError("Identity Toolkit returned no idToken")
    return id_token


def _verify_password_with_identity_toolkit(email: str, password: str) -> bool:
    """Verify credentials against Firebase Identity Toolkit. Fail-closed.

    Returns True ONLY when Firebase explicitly confirms the password with
    an HTTP 200 credential response. Any other outcome raises
    AuthConfigError (configuration/availability problem) or returns False
    (credentials rejected by Firebase). Never returns True on an error.
    """
    if not FIREBASE_API_KEY:
        print("[auth] FIREBASE_API_KEY is not configured - login refused")
        raise AuthConfigError("Firebase API key not configured")
    try:
        resp = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}",
            json={"email": email, "password": password, "returnSecureToken": True},
            timeout=10,
        )
    except Exception as e:
        print(f"[auth] Identity Toolkit unreachable: {type(e).__name__}")
        raise AuthConfigError("Identity Toolkit unreachable")

    if resp.status_code == 200:
        return True

    error_message = ""
    try:
        error_message = (resp.json().get("error") or {}).get("message", "")
    except Exception:
        pass

    if error_message in _CREDENTIAL_ERROR_CODES:
        return False

    if error_message == "API_KEY_INVALID":
        print("[auth] Identity Toolkit rejected the configured API key")
        raise AuthConfigError("Identity Toolkit rejected the API key")

    print(f"[auth] Unexpected Identity Toolkit response: status={resp.status_code} code={error_message!r}")
    raise AuthConfigError("Unexpected Identity Toolkit response")


def _generate_otp() -> str:
    """Cryptographically secure 6-digit numeric code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_otp(otp: str, salt: str) -> str:
    """Salted SHA-256 digest of a code; the plaintext code is never stored."""
    return hashlib.sha256(f"{salt}:{otp}".encode("utf-8")).hexdigest()


def _new_otp() -> Dict[str, Any]:
    """A fresh code with its salt, hash, and expiry (plaintext only for email)."""
    salt = secrets.token_hex(16)
    otp = _generate_otp()
    return {
        "otp": otp,
        "salt": salt,
        "hash": _hash_otp(otp, salt),
        "expires_at": _utcnow() + VERIFY_OTP_TTL,
    }


def _send_verification_email(email: str, otp: str) -> None:
    """Deliver the verification code through the Gmail API (OAuth2, HTTPS).

    Raises EmailDeliveryError when the email cannot actually be sent, so the
    caller never claims a verification email was delivered when it was not.
    """
    try:
        gmail_mailer.send_verification_email(email, otp)
    except gmail_mailer.GmailMailError as e:
        print(f"[email-verify] Gmail delivery failed: {e}")
        raise EmailDeliveryError("Verification email could not be sent")
    print(f"[email-verify] Verification email sent to {email}")


def _send_password_reset_email(email: str, otp: str) -> None:
    """Deliver the password reset code through the Gmail API (OAuth2, HTTPS).

    Raises EmailDeliveryError when the email cannot actually be sent.
    """
    try:
        gmail_mailer.send_password_reset_email(email, otp)
    except gmail_mailer.GmailMailError as e:
        print(f"[password-reset] Gmail delivery failed: {e}")
        raise EmailDeliveryError("Password reset email could not be sent")
    print(f"[password-reset] Password reset email sent to {email}")


def _mark_verified(db, uid: str) -> None:
    """Mark the account verified in Firestore and in Firebase Auth."""
    db.collection("users").document(uid).update({
        "emailVerified": True,
        "verifyOtpHash": None,
        "verifyOtpSalt": None,
        "verifyOtpExpiresAt": None,
        "verifyOtpAttempts": None,
    })
    auth.update_user(uid, email_verified=True)


def _clear_otp(db, uid: str) -> None:
    """Invalidate any in-flight code (expired, max attempts, or completed)."""
    db.collection("users").document(uid).update({
        "verifyOtpHash": None,
        "verifyOtpSalt": None,
        "verifyOtpExpiresAt": None,
        "verifyOtpAttempts": None,
    })


def _clear_reset_otp(db, uid: str) -> None:
    """Invalidate any in-flight password reset code."""
    db.collection("users").document(uid).update({
        "resetOtpHash": None,
        "resetOtpSalt": None,
        "resetOtpExpiresAt": None,
        "resetOtpAttempts": None,
    })


def _rollback_created_user(db, uid: str) -> None:
    """Best-effort cleanup of a just-created account (Firestore doc + Auth user).

    Only ever called for the user created in the current signup transaction.
    Deletion failures are swallowed so the original error keeps propagating.
    """
    try:
        db.collection("users").document(uid).delete()
    except Exception:
        pass
    try:
        auth.delete_user(uid)
    except Exception:
        pass


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthService:
    @staticmethod
    def create_user(email: str, password: str, display_name: Optional[str] = None, account_type: Optional[str] = None) -> Dict[str, Any]:
        # Validate before touching Firebase so bad input returns a clear 400
        # instead of a generic 500. Firebase enforces the same rules: a valid
        # email address and a password of at least 6 characters.
        if not email or not _EMAIL_RE.match(email):
            raise ValueError("Please enter a valid email address.")
        if not password or len(password) < 6:
            raise ValueError("Password must be at least 6 characters long.")
        try:
            user = auth.create_user(
                email=email,
                password=password,
                display_name=display_name or email.split("@")[0],
            )
        except auth.EmailAlreadyExistsError:
            raise ValueError("Email already exists")
        except ValueError:
            # firebase-admin rejected an input that slipped past our checks;
            # surface it as a client error, never a 500.
            raise ValueError("Invalid email or password. Please check your details.")
        except Exception:
            print("[auth] signup: Firebase user creation failed")
            raise Exception("Failed to create account. Please try again.")

        db = get_db()
        display_id = _generate_display_id(db)
        otp = _new_otp()
        db.collection("users").document(user.uid).set({
            "uid": user.uid,
            "display_id": display_id,
            "email": email,
            "displayName": display_name or email.split("@")[0],
            "accountType": account_type or "user",
            "createdAt": firestore.SERVER_TIMESTAMP,
            "emailVerified": False,
            "verifyOtpHash": otp["hash"],
            "verifyOtpSalt": otp["salt"],
            "verifyOtpExpiresAt": otp["expires_at"],
            "verifyOtpAttempts": 0,
            "lastVerifySentAt": _utcnow(),
            "onboardingCompleted": False,
            "age": None,
            "weight": None,
            "height": None,
            "goal": "maintain",
            "dietaryPreferences": [],
            "allergies": [],
        })

        try:
            _send_verification_email(email, otp["otp"])
        except EmailDeliveryError:
            # Roll the account back so a failed delivery leaves no orphaned
            # (never-verifiable) user behind, and be honest that no email
            # was sent.
            _rollback_created_user(db, user.uid)
            raise
        except Exception:
            # Any failure after account creation must roll the new account
            # back so no orphaned user/document remain.
            _rollback_created_user(db, user.uid)
            raise EmailDeliveryError("Verification email could not be sent")

        custom_token = auth.create_custom_token(user.uid)
        custom_token = custom_token.decode() if isinstance(custom_token, bytes) else custom_token
        try:
            id_token = _exchange_id_token(custom_token)
        except AuthConfigError:
            # No usable session token means the account cannot be used;
            # roll it back rather than returning an unusable account.
            _rollback_created_user(db, user.uid)
            raise
        return {
            "uid": user.uid,
            "display_id": display_id,
            "email": email,
            "displayName": display_name or email.split("@")[0],
            "token": id_token,
            "emailVerified": False,
        }

    @staticmethod
    def login_user(email: str, password: str) -> Dict[str, Any]:
        """Sign in with email + password.

        The password is NEVER considered valid unless Firebase Identity
        Toolkit explicitly confirms the credentials (HTTP 200). Admin SDK
        lookups are only used afterwards for profile data and the email
        verification gate - never as proof that the password is correct.
        """
        try:
            password_ok = _verify_password_with_identity_toolkit(email, password)
            if not password_ok:
                raise ValueError("Invalid email or password")

            user = auth.get_user_by_email(email)
            db = get_db()
            doc = db.collection("users").document(user.uid).get()
            data = doc.to_dict() if doc.exists else {}

            # Firebase Auth is the authoritative email-verification state;
            # keep the Firestore mirror in sync with it.
            fb_verified = bool(getattr(user, "email_verified", False))
            if fb_verified:
                if doc.exists and data.get("emailVerified") is not True:
                    db.collection("users").document(user.uid).update({"emailVerified": True})
                elif not doc.exists:
                    db.collection("users").document(user.uid).set({
                        "uid": user.uid,
                        "email": user.email,
                        "displayName": user.display_name or email.split("@")[0],
                        "createdAt": firestore.SERVER_TIMESTAMP,
                        "emailVerified": True,
                        "onboardingCompleted": False,
                    })
            elif doc.exists and data.get("emailVerified") is False:
                raise ValueError("Email not verified. Please verify your email before signing in.")

            display_id = data.get("display_id") if doc.exists else None
            if not display_id:
                display_id = _generate_display_id(db)
                db.collection("users").document(user.uid).update({"display_id": display_id})

            custom_token = auth.create_custom_token(user.uid)
            custom_token = custom_token.decode() if isinstance(custom_token, bytes) else custom_token
            return {
                "uid": user.uid,
                "display_id": display_id,
                "email": user.email,
                "displayName": user.display_name or email.split("@")[0],
                "token": _exchange_id_token(custom_token),
                "onboardingCompleted": bool(data.get("onboardingCompleted", False)),
            }
        except auth.UserNotFoundError:
            raise ValueError("Invalid email or password")
        except ValueError:
            raise
        except AuthConfigError:
            raise
        except Exception as e:
            print(f"[auth] login error: {type(e).__name__}")
            raise Exception("Unable to sign in right now. Please try again later.")

    @staticmethod
    def verify_email(email: str, code: str) -> Dict[str, Any]:
        """Confirm email verification with the emailed code.

        Validates that the code belongs to this account, has not expired,
        and has not already been used. Wrong codes count toward a per-code
        attempt limit; exceeding it invalidates the code (must resend).
        """
        try:
            user = auth.get_user_by_email(email)
        except auth.UserNotFoundError:
            e = ValueError("No account found for this email. Please sign up first.")
            e.email = email
            raise e

        db = get_db()
        doc = db.collection("users").document(user.uid).get()
        if not doc.exists:
            e = ValueError("No account found for this email. Please sign up first.")
            e.email = email
            raise e
        data = doc.to_dict()
        if data.get("emailVerified") is True:
            return {"email": email, "already_verified": True, "verified": True}

        code = (code or "").strip()
        if not code or not code.isdigit() or len(code) != 6:
            e = ValueError("Please enter the 6-digit verification code.")
            e.email = email
            raise e

        otp_hash = data.get("verifyOtpHash")
        otp_salt = data.get("verifyOtpSalt")
        expires_at = data.get("verifyOtpExpiresAt")
        attempts = int(data.get("verifyOtpAttempts") or 0)

        if not otp_hash or not otp_salt:
            e = ValueError("No active verification code. Please request a new one.")
            e.email = email
            raise e

        if not expires_at or expires_at.replace(tzinfo=None) < _utcnow():
            _clear_otp(db, user.uid)
            e = ValueError("This verification code has expired. Please request a new one.")
            e.email = email
            raise e

        if attempts >= VERIFY_OTP_MAX_ATTEMPTS:
            _clear_otp(db, user.uid)
            e = ValueError("Too many incorrect attempts. Please request a new code.")
            e.email = email
            raise e

        if not hmac.compare_digest(_hash_otp(code, otp_salt), otp_hash):
            attempts += 1
            if attempts >= VERIFY_OTP_MAX_ATTEMPTS:
                _clear_otp(db, user.uid)
                e = ValueError("Too many incorrect attempts. Please request a new code.")
            else:
                db.collection("users").document(user.uid).update({"verifyOtpAttempts": attempts})
                e = ValueError("Incorrect verification code. Please try again.")
            e.email = email
            raise e

        _mark_verified(db, user.uid)
        # The OTP proof establishes the session: mint an ID token so the
        # client can continue (onboarding for new users) without asking for
        # email/password again. If the token mint fails, verification still
        # succeeded - the client falls back to a normal sign-in.
        id_token = None
        try:
            custom_token = auth.create_custom_token(user.uid)
            custom_token = custom_token.decode() if isinstance(custom_token, bytes) else custom_token
            id_token = _exchange_id_token(custom_token)
        except AuthConfigError:
            pass
        return {
            "email": email,
            "verified": True,
            "already_verified": False,
            "source": "otp",
            "uid": user.uid,
            "display_id": data.get("display_id"),
            "token": id_token,
            "onboardingCompleted": bool(data.get("onboardingCompleted", False)),
        }

    @staticmethod
    def resend_verification(email: str) -> Dict[str, Any]:
        """Send a fresh verification code for the account (60s cooldown)."""
        try:
            user = auth.get_user_by_email(email)
        except auth.UserNotFoundError:
            raise ValueError("No account found for this email.")
        db = get_db()
        doc = db.collection("users").document(user.uid).get()
        if not doc.exists:
            raise ValueError("No account found for this email.")
        data = doc.to_dict()
        if data.get("emailVerified") is True:
            return {"already_verified": True, "email": email}
        last_sent = data.get("lastVerifySentAt")
        if last_sent and last_sent.replace(tzinfo=None) + VERIFY_RESEND_COOLDOWN > _utcnow():
            raise ValueError("Please wait a minute before requesting another email.")
        otp = _new_otp()
        _send_verification_email(email, otp["otp"])
        now = _utcnow()
        db.collection("users").document(user.uid).update({
            "verifyOtpHash": otp["hash"],
            "verifyOtpSalt": otp["salt"],
            "verifyOtpExpiresAt": otp["expires_at"],
            "verifyOtpAttempts": 0,
            "lastVerifySentAt": now,
        })
        return {"sent": True, "email": email}

    @staticmethod
    def forgot_password(email: str) -> Dict[str, Any]:
        """Send a password reset code to the given email.

        Uses a generic message regardless of whether the email exists to
        prevent email enumeration. Reuses the same OTP infrastructure as
        email verification (hash+salt, TTL, attempt limits).
        """
        email = (email or "").strip().lower()
        if not email or not _EMAIL_RE.match(email):
            raise ValueError("Please enter a valid email address.")

        # Generic response regardless of whether the email exists
        generic_response = {"sent": True, "email": email}

        try:
            user = auth.get_user_by_email(email)
        except auth.UserNotFoundError:
            # Return success even if user not found (no email enumeration)
            return generic_response

        db = get_db()
        doc = db.collection("users").document(user.uid).get()
        if not doc.exists:
            return generic_response

        data = doc.to_dict()

        # Enforce 60-second cooldown between reset requests
        last_sent = data.get("lastResetSentAt")
        if last_sent and last_sent.replace(tzinfo=None) + VERIFY_RESEND_COOLDOWN > _utcnow():
            # Still return success to prevent timing attacks
            return generic_response

        # Generate and store the reset OTP (reuses verify OTP fields pattern)
        otp = _new_otp()
        now = _utcnow()
        db.collection("users").document(user.uid).update({
            "resetOtpHash": otp["hash"],
            "resetOtpSalt": otp["salt"],
            "resetOtpExpiresAt": otp["expires_at"],
            "resetOtpAttempts": 0,
            "lastResetSentAt": now,
        })

        try:
            _send_password_reset_email(email, otp["otp"])
        except EmailDeliveryError:
            # Don't fail the request - email may not exist, and we don't
            # want to reveal that information. The OTP is stored either way.
            print(f"[password-reset] Email delivery failed for {email} (non-fatal)")

        return generic_response

    @staticmethod
    def reset_password(email: str, code: str, new_password: str) -> Dict[str, Any]:
        """Verify the password reset code and update the password.

        Validates that the code is correct, not expired, and hasn't exceeded
        the attempt limit. On success, updates the password via Firebase Auth
        and clears the reset OTP fields.
        """
        email = (email or "").strip().lower()
        code = (code or "").strip()
        new_password = (new_password or "").strip()

        if not email or not _EMAIL_RE.match(email):
            raise ValueError("Please enter a valid email address.")
        if not code or not code.isdigit() or len(code) != 6:
            raise ValueError("Please enter the 6-digit reset code.")
        if not new_password or len(new_password) < 6:
            raise ValueError("Password must be at least 6 characters long.")

        try:
            user = auth.get_user_by_email(email)
        except auth.UserNotFoundError:
            raise ValueError("Invalid or expired reset code. Please request a new one.")

        db = get_db()
        doc = db.collection("users").document(user.uid).get()
        if not doc.exists:
            raise ValueError("Invalid or expired reset code. Please request a new one.")

        data = doc.to_dict()
        otp_hash = data.get("resetOtpHash")
        otp_salt = data.get("resetOtpSalt")
        expires_at = data.get("resetOtpExpiresAt")
        attempts = int(data.get("resetOtpAttempts") or 0)

        if not otp_hash or not otp_salt:
            raise ValueError("No active reset code. Please request a new one.")

        if not expires_at or expires_at.replace(tzinfo=None) < _utcnow():
            _clear_reset_otp(db, user.uid)
            raise ValueError("This reset code has expired. Please request a new one.")

        if attempts >= VERIFY_OTP_MAX_ATTEMPTS:
            _clear_reset_otp(db, user.uid)
            raise ValueError("Too many incorrect attempts. Please request a new code.")

        if not hmac.compare_digest(_hash_otp(code, otp_salt), otp_hash):
            attempts += 1
            if attempts >= VERIFY_OTP_MAX_ATTEMPTS:
                _clear_reset_otp(db, user.uid)
                raise ValueError("Too many incorrect attempts. Please request a new code.")
            else:
                db.collection("users").document(user.uid).update({"resetOtpAttempts": attempts})
                raise ValueError("Incorrect reset code. Please try again.")

        # Code is valid - update the password via Firebase Auth
        try:
            auth.update_user(user.uid, password=new_password)
        except Exception as e:
            print(f"[password-reset] Firebase password update failed: {type(e).__name__}")
            raise Exception("Failed to update password. Please try again.")

        # Clear the reset OTP
        _clear_reset_otp(db, user.uid)

        return {"success": True, "email": email}

    @staticmethod
    def google_auth(id_token: str) -> Dict[str, Any]:
        """Sign in or sign up with a Google ID token from Firebase client SDK.

        Verifies the ID token with Firebase Admin SDK, then either finds the
        existing user or creates a new one. Returns a session token (ID token)
        for the client to use with the backend API.
        """
        if not FIREBASE_API_KEY:
            print("[auth] FIREBASE_API_KEY is not configured - Google auth refused")
            raise AuthConfigError("Firebase API key not configured")
        try:
            decoded = auth.verify_id_token(id_token)
        except Exception as e:
            print(f"[auth] Google ID token verification failed: {type(e).__name__}")
            raise ValueError("Invalid Google sign-in token. Please try again.")

        email = decoded.get("email")
        if not email:
            raise ValueError("Google account has no email. Please use an account with an email address.")

        display_name = decoded.get("name") or email.split("@")[0]
        picture = decoded.get("picture")
        email_verified = bool(decoded.get("email_verified", False))

        db = get_db()
        try:
            user = auth.get_user_by_email(email)
        except auth.UserNotFoundError:
            user = None

        if user:
            uid = user.uid
            doc = db.collection("users").document(uid).get()
            data = doc.to_dict() if doc.exists else {}
            fb_verified = bool(getattr(user, "email_verified", False))
            if fb_verified and doc.exists and data.get("emailVerified") is not True:
                db.collection("users").document(uid).update({"emailVerified": True})
            elif not doc.exists:
                db.collection("users").document(uid).set({
                    "uid": uid,
                    "email": email,
                    "displayName": display_name,
                    "photoURL": picture,
                    "createdAt": firestore.SERVER_TIMESTAMP,
                    "emailVerified": email_verified,
                    "onboardingCompleted": False,
                })
        else:
            try:
                user = auth.create_user(
                    email=email,
                    display_name=display_name,
                    photo_url=picture,
                    email_verified=email_verified,
                )
            except auth.EmailAlreadyExistsError:
                raise ValueError("An account with this email already exists. Please sign in with email and password.")
            except Exception:
                print("[auth] Google signup: Firebase user creation failed")
                raise Exception("Failed to create account. Please try again.")

            uid = user.uid
            display_id = _generate_display_id(db)
            db.collection("users").document(uid).set({
                "uid": uid,
                "display_id": display_id,
                "email": email,
                "displayName": display_name,
                "photoURL": picture,
                "accountType": "user",
                "createdAt": firestore.SERVER_TIMESTAMP,
                "emailVerified": email_verified,
                "verifyOtpHash": None,
                "verifyOtpSalt": None,
                "verifyOtpExpiresAt": None,
                "verifyOtpAttempts": None,
                "lastVerifySentAt": None,
                "onboardingCompleted": False,
                "age": None,
                "weight": None,
                "height": None,
                "goal": "maintain",
                "dietaryPreferences": [],
                "allergies": [],
            })

        custom_token = auth.create_custom_token(uid)
        custom_token = custom_token.decode() if isinstance(custom_token, bytes) else custom_token
        try:
            session_token = _exchange_id_token(custom_token)
        except AuthConfigError:
            raise Exception("Unable to create session. Please try again.")

        doc = db.collection("users").document(uid).get()
        data = doc.to_dict() if doc.exists else {}
        display_id = data.get("display_id")
        if not display_id:
            display_id = _generate_display_id(db)
            db.collection("users").document(uid).update({"display_id": display_id})

        return {
            "uid": uid,
            "display_id": display_id,
            "email": email,
            "displayName": display_name,
            "token": session_token,
            "onboardingCompleted": bool(data.get("onboardingCompleted", False)),
        }

    @staticmethod
    def facebook_auth(id_token: str) -> Dict[str, Any]:
        """Sign in or sign up with a Facebook ID token from Firebase client SDK.

        Verifies the ID token with Firebase Admin SDK, then either finds the
        existing user or creates a new one. Returns a session token (ID token)
        for the client to use with the backend API.
        """
        if not FIREBASE_API_KEY:
            print("[auth] FIREBASE_API_KEY is not configured - Facebook auth refused")
            raise AuthConfigError("Firebase API key not configured")
        try:
            decoded = auth.verify_id_token(id_token)
        except Exception as e:
            print(f"[auth] Facebook ID token verification failed: {type(e).__name__}")
            raise ValueError("Invalid Facebook sign-in token. Please try again.")

        email = decoded.get("email")
        if not email:
            raise ValueError("Facebook account has no email. Please use an account with an email address.")

        display_name = decoded.get("name") or email.split("@")[0]
        picture = decoded.get("picture")
        email_verified = bool(decoded.get("email_verified", False))

        db = get_db()
        try:
            user = auth.get_user_by_email(email)
        except auth.UserNotFoundError:
            user = None

        if user:
            uid = user.uid
            doc = db.collection("users").document(uid).get()
            data = doc.to_dict() if doc.exists else {}
            fb_verified = bool(getattr(user, "email_verified", False))
            if fb_verified and doc.exists and data.get("emailVerified") is not True:
                db.collection("users").document(uid).update({"emailVerified": True})
            elif not doc.exists:
                db.collection("users").document(uid).set({
                    "uid": uid,
                    "email": email,
                    "displayName": display_name,
                    "photoURL": picture,
                    "createdAt": firestore.SERVER_TIMESTAMP,
                    "emailVerified": email_verified,
                    "onboardingCompleted": False,
                })
        else:
            try:
                user = auth.create_user(
                    email=email,
                    display_name=display_name,
                    photo_url=picture,
                    email_verified=email_verified,
                )
            except auth.EmailAlreadyExistsError:
                raise ValueError("An account with this email already exists. Please sign in with email and password.")
            except Exception:
                print("[auth] Facebook signup: Firebase user creation failed")
                raise Exception("Failed to create account. Please try again.")

            uid = user.uid
            display_id = _generate_display_id(db)
            db.collection("users").document(uid).set({
                "uid": uid,
                "display_id": display_id,
                "email": email,
                "displayName": display_name,
                "photoURL": picture,
                "accountType": "user",
                "createdAt": firestore.SERVER_TIMESTAMP,
                "emailVerified": email_verified,
                "verifyOtpHash": None,
                "verifyOtpSalt": None,
                "verifyOtpExpiresAt": None,
                "verifyOtpAttempts": None,
                "lastVerifySentAt": None,
                "onboardingCompleted": False,
                "age": None,
                "weight": None,
                "height": None,
                "goal": "maintain",
                "dietaryPreferences": [],
                "allergies": [],
            })

        custom_token = auth.create_custom_token(uid)
        custom_token = custom_token.decode() if isinstance(custom_token, bytes) else custom_token
        try:
            session_token = _exchange_id_token(custom_token)
        except AuthConfigError:
            raise Exception("Unable to create session. Please try again.")

        doc = db.collection("users").document(uid).get()
        data = doc.to_dict() if doc.exists else {}
        display_id = data.get("display_id")
        if not display_id:
            display_id = _generate_display_id(db)
            db.collection("users").document(uid).update({"display_id": display_id})

        return {
            "uid": uid,
            "display_id": display_id,
            "email": email,
            "displayName": display_name,
            "token": session_token,
            "onboardingCompleted": bool(data.get("onboardingCompleted", False)),
        }

    @staticmethod
    def get_profile(uid: str) -> Optional[Dict[str, Any]]:
        db = get_db()
        doc = db.collection("users").document(uid).get()
        if doc.exists:
            return doc.to_dict()
        return None

    @staticmethod
    def update_profile(uid: str, data: Dict[str, Any]) -> bool:
        db = get_db()
        # Whitelist only self-editable fields. Identity, roles, and
        # server-controlled flags can never be changed through this endpoint.
        # onboardingCompleted is the user's own onboarding progress flag.
        allowed_fields = {"displayName", "age", "weight", "height", "goal", "dietaryPreferences", "allergies", "onboardingCompleted"}
        update_data = {k: v for k, v in data.items() if k in allowed_fields}
        if update_data:
            db.collection("users").document(uid).update(update_data)
        return True

    @staticmethod
    def submit_feedback(name: Optional[str], email: Optional[str], message: str) -> Dict[str, Any]:
        """Validate and deliver user feedback through the existing Gmail API.

        All inputs are trimmed and validated server-side (client-side checks
        are never trusted). The feedback email is sent to the fixed support
        address via gmail_mailer.send_feedback_email, which reuses the same
        OAuth2 credentials, scope and safe error handling as the verification
        mailer. No credentials or secrets ever leave the server.
        """
        name = (name or "").strip()
        email = (email or "").strip()
        message = (message or "").strip()

        if not message:
            raise ValueError("Please enter a feedback message.")
        if len(message) > 5000:
            raise ValueError("Feedback message is too long (max 5000 characters).")
        if len(name) > 120:
            raise ValueError("Name is too long (max 120 characters).")
        if email:
            if len(email) > 254 or not _EMAIL_RE.match(email):
                raise ValueError("Please enter a valid email address.")

        from html import escape as _html_escape
        safe_name = _html_escape(name) if name else ""
        safe_email = _html_escape(email) if email else ""
        safe_message = _html_escape(message).replace("\n", "<br>")

        submitted_at = _utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        subject = "BiteWise Feedback"
        html_body = (
            "<html><body>"
            "<p><strong>New BiteWise Feedback</strong></p>"
            f"<p>{safe_message}</p>"
            f"<p><strong>Name:</strong> {safe_name or 'Not provided'}<br>"
            f"<strong>Email:</strong> {safe_email or 'Not provided'}<br>"
            f"<strong>Submitted:</strong> {submitted_at}</p>"
            "</body></html>"
        )
        try:
            gmail_mailer.send_feedback_email(gmail_mailer.FIXED_SENDER, subject, html_body)
        except gmail_mailer.GmailMailError as e:
            print(f"[feedback] Gmail delivery failed: {e}")
            raise EmailDeliveryError("Feedback email could not be sent")
        return {"sent": True, "subject": subject}

    @staticmethod
    def submit_rating(rating: int, comment: Optional[str] = None) -> Dict[str, Any]:
        """Validate and deliver a 1-5 star rating through the existing Gmail API.

        The rating must be a whole number from 1 to 5. The comment is optional
        and capped at 2000 characters. Both are escaped before being placed in
        the HTML email body, which is sent to the fixed support address via
        gmail_mailer.send_rating_email (the same OAuth2 credential flow, scope
        and safe error handling as the verification/feedback mailers). No
        credentials or secrets ever leave the server.
        """
        if not isinstance(rating, int) or isinstance(rating, bool):
            raise ValueError("Rating must be a whole number from 1 to 5.")
        if rating < 1 or rating > 5:
            raise ValueError("Rating must be from 1 to 5 stars.")
        comment = (comment or "").strip()
        if len(comment) > 2000:
            raise ValueError("Comment is too long (max 2000 characters).")

        from html import escape as _html_escape
        safe_comment = _html_escape(comment).replace("\n", "<br>") if comment else ""
        stars = "★" * rating + "☆" * (5 - rating)
        submitted_at = _utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        subject = f"BiteWise Rating — {rating}/5"
        html_body = (
            "<html><body>"
            "<p><strong>New BiteWise Rating</strong></p>"
            f"<p><strong>Rating:</strong> {rating}/5&nbsp;({stars})</p>"
            f"<p><strong>Comment:</strong><br>{safe_comment or 'No comment provided.'}</p>"
            f"<p><strong>Submitted:</strong> {submitted_at}</p>"
            "</body></html>"
        )
        try:
            gmail_mailer.send_rating_email(gmail_mailer.FIXED_SENDER, subject, html_body)
        except gmail_mailer.GmailMailError as e:
            print(f"[rating] Gmail delivery failed: {e}")
            raise EmailDeliveryError("Rating email could not be sent")
        return {"sent": True, "subject": subject}