import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from models.schemas import AuthRequest, FeedbackRequest, ProfileUpdate, RatingRequest, ResendVerifyRequest, VerifyRequest, GoogleAuthRequest, FacebookAuthRequest, ForgotPasswordRequest, ResetPasswordRequest
from services.auth_service import AuthService, AuthConfigError, EmailDeliveryError
from utils.auth import get_current_user

router = APIRouter(prefix="/auth", tags=["Authentication"])

# Lightweight in-memory per-IP rate limiter for feedback submissions.
# The feedback endpoint is open (no authentication) so this prevents a single
# client from spamming our support inbox.
FEEDBACK_MAX_PER_IP = 10
FEEDBACK_WINDOW_SECONDS = 3600
_feedback_log: dict[str, deque] = defaultdict(deque)

def _feedback_allow(ip: str) -> bool:
    now = time.time()
    log = _feedback_log[ip]
    while log and now - log[0] > FEEDBACK_WINDOW_SECONDS:
        log.popleft()
    if len(log) >= FEEDBACK_MAX_PER_IP:
        return False
    log.append(now)
    return True

@router.post("/signup")
async def signup(request: AuthRequest):
    try:
        result = AuthService.create_user(request.email, request.password, request.displayName, request.accountType)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except EmailDeliveryError:
        raise HTTPException(
            status_code=503,
            detail="We couldn't send the verification email. Please try again later.",
        )
    except AuthConfigError:
        raise HTTPException(
            status_code=503,
            detail="Unable to create your account right now. Please try again later.",
        )
    except Exception as e:
        print(f"[auth] signup error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred while creating your account. Please try again.",
        )

@router.post("/login")
async def login(request: AuthRequest):
    try:
        result = AuthService.login_user(request.email, request.password)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except AuthConfigError:
        raise HTTPException(
            status_code=503,
            detail="Unable to verify your credentials right now. Please try again later.",
        )
    except Exception as e:
        print(f"[auth] login error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="Unable to sign in right now. Please try again later.",
        )

@router.post("/verify-email")
async def verify_email(request: VerifyRequest):
    try:
        result = AuthService.verify_email(request.email, request.code)
        return {"success": True, "data": result}
    except ValueError as e:
        # Attach the account email (when known) so the verification page can
        # offer a resend without asking the user to type it again.
        content = {"detail": str(e)}
        if getattr(e, "email", None):
            content["email"] = e.email
        return JSONResponse(status_code=400, content=content)
    except Exception as e:
        print(f"[auth] verify-email error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="Something went wrong. Please try again.",
        )

@router.post("/resend-verification")
async def resend_verification(request: ResendVerifyRequest):
    try:
        result = AuthService.resend_verification(request.email)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except EmailDeliveryError:
        raise HTTPException(
            status_code=503,
            detail="We couldn't send the verification email. Please try again later.",
        )
    except Exception as e:
        print(f"[auth] resend-verification error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="Something went wrong. Please try again.",
        )

@router.post("/forgot-password")
async def forgot_password(request: ForgotPasswordRequest):
    """Send a password reset code to the given email.

    Returns a generic success message regardless of whether the email exists
    to prevent email enumeration attacks.
    """
    try:
        result = AuthService.forgot_password(request.email)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[auth] forgot-password error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="Something went wrong. Please try again.",
        )

@router.post("/reset-password")
async def reset_password(request: ResetPasswordRequest):
    """Verify the reset code and update the password.

    If the code is incorrect or expired, returns an appropriate error message.
    On success, updates the password and returns a success response.
    """
    try:
        result = AuthService.reset_password(request.email, request.code, request.newPassword)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[auth] reset-password error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="Something went wrong. Please try again.",
        )

@router.post("/google")
async def google_auth(request: GoogleAuthRequest):
    try:
        result = AuthService.google_auth(request.idToken)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AuthConfigError:
        raise HTTPException(
            status_code=503,
            detail="Unable to verify your Google account right now. Please try again later.",
        )
    except Exception as e:
        print(f"[auth] google-auth error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="Unable to sign in with Google right now. Please try again later.",
        )

@router.post("/facebook")
async def facebook_auth(request: FacebookAuthRequest):
    try:
        result = AuthService.facebook_auth(request.idToken)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AuthConfigError:
        raise HTTPException(
            status_code=503,
            detail="Unable to verify your Facebook account right now. Please try again later.",
        )
    except Exception as e:
        print(f"[auth] facebook-auth error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="Unable to sign in with Facebook right now. Please try again later.",
        )

@router.post("/feedback")
async def submit_feedback(request: FeedbackRequest, http_request: Request):
    ip = http_request.client.host if http_request.client else "unknown"
    if not _feedback_allow(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many feedback submissions. Please try again later.",
        )
    try:
        result = AuthService.submit_feedback(request.name, request.email, request.message)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except EmailDeliveryError:
        raise HTTPException(
            status_code=503,
            detail="Feedback email could not be sent. Please try again later.",
        )
    except Exception as e:
        print(f"[auth] feedback error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="Something went wrong. Please try again.",
        )

@router.post("/rating")
async def submit_rating(request: RatingRequest, http_request: Request):
    ip = http_request.client.host if http_request.client else "unknown"
    if not _feedback_allow(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many submissions. Please try again later.",
        )
    try:
        result = AuthService.submit_rating(request.rating, request.comment)
        return {"success": True, "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except EmailDeliveryError:
        raise HTTPException(
            status_code=503,
            detail="Rating email could not be sent. Please try again later.",
        )
    except Exception as e:
        print(f"[auth] rating error: {type(e).__name__}")
        raise HTTPException(
            status_code=500,
            detail="Something went wrong. Please try again.",
        )

@router.get("/profile/{uid}")
async def get_profile(uid: str, current_user: dict = Depends(get_current_user)):
    if uid != current_user["uid"]:
        raise HTTPException(status_code=404, detail="Profile not found")
    profile = AuthService.get_profile(uid)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"success": True, "data": profile}

@router.put("/profile/{uid}")
async def update_profile(uid: str, data: ProfileUpdate, current_user: dict = Depends(get_current_user)):
    if uid != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Cannot modify another user's profile")
    try:
        AuthService.update_profile(uid, data.model_dump(exclude_none=True))
        return {"success": True, "message": "Profile updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))