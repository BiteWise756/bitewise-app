"""Payment routes for NutriLens AI with Paymob integration."""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from models.schemas import (
    PaymentCreateRequest,
    PaymentCreateResponse,
    PaymentStatusResponse,
    Plan,
    WebhookPayload,
)
from services.payment_service import (
    PaymentError,
    PaymentVerificationError,
    PaymentStatus,
    _utcnow,
    create_payment,
    get_payment_status,
    get_user_subscription,
    list_plans,
    process_paymob_webhook,
    process_webhook,
)
from firebase.config import get_db
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("/plans", response_model=list[Plan])
async def get_plans():
    """Get all available premium plans (public endpoint)."""
    return list_plans()


@router.post("/create", response_model=PaymentCreateResponse)
async def create_payment_endpoint(
    request: PaymentCreateRequest,
    user: dict = Depends(get_current_user),
):
    """Create a payment for the authenticated user.

    User must be authenticated. Plan amount/currency comes from server-side PLANS.
    Returns Paymob checkout URL for redirect.
    """
    user_id = user.get("uid")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user token",
        )

    # Get user email for billing
    user_doc = get_db().collection("users").document(user_id).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}
    user_email = user_data.get("email", "")
    user_phone = user_data.get("phone", "")

    try:
        payment = await create_payment(
            user_id=user_id,
            plan_id=request.plan_id,
            user_email=user_email,
            user_phone=user_phone,
        )
        return payment
    except PaymentError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"[payments] Create payment error: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create payment",
        )


@router.post("/create-checkout", response_model=PaymentCreateResponse)
async def create_checkout_endpoint(
    request: PaymentCreateRequest,
    user: dict = Depends(get_current_user),
):
    """Create a Paymob checkout session for the authenticated user (Intention API).

    Frontend calls this with {plan_id: "yearly"|"monthly"} and receives a
    Paymob Unified Checkout URL. Amount/currency are server-side only.

    Uses the same create_payment logic but is the preferred endpoint for the
    new Premium.html flow. Keeps /payments/create for backwards compatibility.
    """
    logger.info(f"[Paymob] Creating checkout for uid={user.get('uid')} plan={request.plan_id}")
    user_id = user.get("uid")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user token",
        )

    user_doc = get_db().collection("users").document(user_id).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}
    user_email = user_data.get("email", "")
    user_phone = user_data.get("phone", "")

    try:
        payment = await create_payment(
            user_id=user_id,
            plan_id=request.plan_id,
            user_email=user_email,
            user_phone=user_phone,
        )
        logger.info(f"[Paymob] Checkout URL generated for payment {payment.payment_id}")
        return payment
    except PaymentError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"[Paymob] Create checkout error: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create checkout",
        )


@router.get("/subscription/status")
async def get_subscription_status(user: dict = Depends(get_current_user)):
    """Get current user's subscription status."""
    user_id = user.get("uid")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user token",
        )

    subscription = get_user_subscription(user_id)
    if not subscription:
        return {
            "has_subscription": False,
            "premium": False,
            "plan_id": None,
            "status": "inactive",
        }

    return {
        "has_subscription": True,
        "premium": subscription.status == "ACTIVE",
        "plan_id": subscription.plan_id,
        "status": subscription.status,
        "current_period_start": subscription.current_period_start,
        "current_period_end": subscription.current_period_end,
    }


@router.get("/{payment_id}/status", response_model=PaymentStatusResponse)
async def get_payment_status_endpoint(
    payment_id: str,
    user: dict = Depends(get_current_user),
):
    """Get payment status for polling (user can only access their own payments)."""
    user_id = user.get("uid")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user token",
        )

    payment = get_payment_status(payment_id)
    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    # Verify ownership
    payment_doc = get_db().collection("payments").document(payment_id).get()
    if payment_doc.exists:
        payment_data = payment_doc.to_dict()
        if payment_data.get("user_id") != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to view this payment",
            )

    return payment


@router.post("/paymob/callback")
async def paymob_callback(request: Request):
    """Paymob Intention callback endpoint (new Unified Checkout).

    Verifies HMAC (SHA-512 on obj), validates payment, activates premium.
    This is the preferred endpoint for the new Intention API (notification_url).
    Keeps /payments/webhook for backwards compatibility.
    """
    try:
        raw_body = await request.body()
        payload = json.loads(raw_body) if raw_body else {}
        # Paymob sends HMAC in query string ?hmac=..., not in JSON body
        query_hmac = request.query_params.get("hmac")
        if query_hmac and "hmac" not in payload:
            payload["hmac"] = query_hmac
        logger.info(f"[Paymob] Callback received: {payload.get('type', 'unknown')}")
        result = await process_paymob_webhook(payload)
        logger.info(f"[Paymob] Callback HMAC verification successful, payment {result.get('activated')}")
        return JSONResponse(content=result)
    except PaymentVerificationError as e:
        logger.warning(f"[Paymob] Callback HMAC verification failed: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except json.JSONDecodeError:
        logger.error("[Paymob] Callback: Invalid JSON")
        return JSONResponse(
            content={"success": False, "error": "Invalid JSON"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.error(f"[Paymob] Callback error: {type(e).__name__}: {e}")
        return JSONResponse(
            content={"success": False, "error": "Callback processing failed"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@router.post("/webhook")
async def paymob_webhook(request: Request):
    """Paymob Transaction Processed Callback endpoint (legacy).

    This is called by Paymob server-to-server when payment status changes.
    We verify HMAC, validate payment details, and activate subscription if paid.
    Kept for backwards compatibility with old Paymob flow.
    """
    try:
        # Get raw body for HMAC verification
        raw_body = await request.body()
        payload = json.loads(raw_body) if raw_body else {}
        # Paymob may send HMAC in query string ?hmac=..., handle as fallback
        query_hmac = request.query_params.get("hmac")
        if query_hmac and "hmac" not in payload:
            payload["hmac"] = query_hmac

        logger.info(f"[payments] Paymob webhook received: {payload.get('type', 'unknown')}")

        # Process through Paymob-specific handler
        result = await process_paymob_webhook(payload)

        return JSONResponse(content=result)

    except PaymentVerificationError as e:
        logger.warning(f"[payments] Paymob webhook verification failed: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except json.JSONDecodeError:
        logger.error("[payments] Paymob webhook: Invalid JSON")
        return JSONResponse(
            content={"success": False, "error": "Invalid JSON"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.error(f"[payments] Paymob webhook error: {type(e).__name__}: {e}")
        return JSONResponse(
            content={"success": False, "error": "Webhook processing failed"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@router.post("/webhook/generic")
async def generic_webhook(request: Request):
    """Generic payment webhook endpoint for testing/other providers.

    Expects WebhookPayload with:
    - payment_id
    - provider
    - provider_transaction_id
    - status
    - amount_cents
    - currency
    - raw_payload (for signature verification)
    """
    try:
        raw_body = await request.body()
        payload_dict = json.loads(raw_body)

        # Verify this is a valid WebhookPayload structure
        required_fields = ["payment_id", "provider", "status"]
        for field in required_fields:
            if field not in payload_dict:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Missing required field: {field}",
                )

        payload = WebhookPayload(**payload_dict)
        result = process_webhook(payload)

        return JSONResponse(content=result)

    except PaymentVerificationError as e:
        logger.warning(f"[payments] Generic webhook verification failed: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except json.JSONDecodeError:
        logger.error("[payments] Generic webhook: Invalid JSON")
        return JSONResponse(
            content={"success": False, "error": "Invalid JSON"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.error(f"[payments] Generic webhook error: {type(e).__name__}: {e}")
        return JSONResponse(
            content={"success": False, "error": "Webhook processing failed"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@router.post("/{payment_id}/cancel")
async def cancel_payment(
    payment_id: str,
    user: dict = Depends(get_current_user),
):
    """Cancel a pending payment (user can only cancel their own)."""
    user_id = user.get("uid")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user token",
        )

    payment_doc = get_db().collection("payments").document(payment_id).get()
    if not payment_doc.exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    payment_data = payment_doc.to_dict()
    if payment_data.get("user_id") != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to cancel this payment",
        )

    # Only allow cancelling PENDING payments
    if payment_data.get("status") != PaymentStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel payment with status: {payment_data.get('status')}",
        )

    # Update status to CANCELLED
    get_db().collection("payments").document(payment_id).update({
        "status": PaymentStatus.CANCELLED.value,
        "cancelled_at": _utcnow(),
    })

    return {"success": True, "message": "Payment cancelled"}