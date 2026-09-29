"""FastAPI authentication/authorization dependencies.

Every user-scoped route must depend on ``get_current_user`` (or
``require_doctor``) and treat ``current_user["uid"]`` as the authenticated
identity. Client-supplied user IDs are never trusted as the identity source.
"""
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from firebase.config import get_db, verify_token

_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> dict:
    """Require a valid Firebase ID token; returns the decoded token claims."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    decoded = verify_token(credentials.credentials)
    if decoded is None or not decoded.get("uid"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return decoded


def get_user_doc(uid: str):
    """Firestore document snapshot for users/{uid}, or None."""
    doc = get_db().collection("users").document(uid).get()
    return doc if doc.exists else None


def is_doctor(uid: str) -> bool:
    """Whether users/{uid} has accountType == 'doctor'."""
    doc = get_user_doc(uid)
    if doc is None:
        return False
    return (doc.to_dict() or {}).get("accountType") == "doctor"


def require_doctor(current_user: dict = Depends(get_current_user)) -> dict:
    """Require an authenticated user with the doctor role."""
    if not is_doctor(current_user["uid"]):
        raise HTTPException(status_code=403, detail="Doctor role required")
    return current_user


def _staff_email_allowlist() -> list:
    import os
    raw = os.getenv("SUPPORT_STAFF_EMAILS", "").strip()
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def is_support_staff(uid: str) -> bool:
    """Staff = doctor role, admin account type, or SUPPORT_STAFF_EMAILS allowlist."""
    doc = get_user_doc(uid)
    if doc is None:
        return False
    data = doc.to_dict() or {}
    if data.get("accountType") in ("doctor", "admin"):
        return True
    email = (data.get("email") or "").strip().lower()
    return bool(email) and email in _staff_email_allowlist()


def require_support_staff(current_user: dict = Depends(get_current_user)) -> dict:
    """Require an authenticated user who may operate the support dashboard."""
    if not is_support_staff(current_user["uid"]):
        raise HTTPException(status_code=403, detail="Support staff role required")
    return current_user