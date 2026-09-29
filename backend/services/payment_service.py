"""Payment / Subscription service for NutriLens AI with Paymob integration.

Architecture:
- Plans are defined server-side (not trusted from client)
- Payment records stored in Firestore with status tracking
- Premium activation ONLY happens after verified payment (server-side)
- Idempotent webhook processing with duplicate detection
- No payment secrets in frontend code
- Paymob integration: Auth token -> Create Intention -> Unified Checkout -> Webhook verification
"""

import os
import secrets
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum

import httpx
from fastapi import Request
from firebase_admin import firestore
from firebase.config import get_db
from models.schemas import Plan, PaymentCreateRequest, PaymentCreateResponse, PaymentStatusResponse, WebhookPayload, SubscriptionResponse


logger = logging.getLogger(__name__)


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REFUNDED = "REFUNDED"


class SubscriptionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    CANCELLED = "CANCELLED"
    PAST_DUE = "PAST_DUE"


class PaymentError(Exception):
    """Payment service errors that should be surfaced to the client."""
    pass


class PaymentVerificationError(Exception):
    """Payment verification failed - do not activate premium."""
    pass


class PaymobConfigError(Exception):
    """Paymob configuration missing or invalid."""
    pass


# ---- Plan definitions (server-side only - never trust client) ----
# Prices in EGP to match Premium.html (99 EGP/month, 199 EGP/year) and Paymob integration 5881793
PLANS: Dict[str, Plan] = {
    "monthly": Plan(
        id="monthly",
        name="Monthly Premium",
        price_cents=9900,  # 99.00 EGP
        currency="EGP",
        billing_period="monthly",
        description="Flexible month-to-month with full premium access",
        features=[
            "Unlimited AI meal & health scans",
            "Unlimited AI Nutrition Coach",
            "Unlock All Features",
        ],
    ),
    "yearly": Plan(
        id="yearly",
        name="Yearly Premium",
        price_cents=19900,  # 199.00 EGP/year
        currency="EGP",
        billing_period="yearly",
        description="Full year of premium features at a great price",
        features=[
            "Unlimited AI meal & health scans",
            "Unlimited AI Nutrition Coach",
            "Unlock All Features",
        ],
    ),
}


def _utcnow() -> datetime:
    """Naive UTC now. Firestore returns naive UTC datetimes, so we store naive."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _payment_id() -> str:
    """Generate a unique payment ID."""
    return "pay_" + secrets.token_urlsafe(16)


# ---- Paymob Configuration ----
def _get_paymob_config() -> Tuple[str, str, str, str, str, bool]:
    """Get Paymob configuration from environment.

    Supports both new (PAYMOB_SECRET_KEY/PAYMOB_PUBLIC_KEY) and legacy
    (PAYMOB_API_KEY/PAYMOB_IFRAME_ID) naming for backwards compatibility.
    Prefers new Intention API vars when present.

    Returns:
        (secret_key/public_or_api_key, hmac_secret, integration_id, public_key/iframe_id, webhook_base_url, sandbox)

    Raises:
        PaymobConfigError: If any required config is missing.
    """
    # New Intention API vars (preferred)
    secret_key = os.getenv("PAYMOB_SECRET_KEY", "").strip() or os.getenv("PAYMOB_API_KEY", "").strip()
    public_key = os.getenv("PAYMOB_PUBLIC_KEY", "").strip() or os.getenv("PAYMOB_IFRAME_ID", "").strip()
    hmac_secret = os.getenv("PAYMOB_HMAC_SECRET", "").strip()
    integration_id = os.getenv("PAYMOB_INTEGRATION_ID", "").strip()
    # PAYMOB_BASE_URL new, fallback to PAYMOB_WEBHOOK_BASE_URL or default
    base_url = os.getenv("PAYMOB_BASE_URL", "").strip() or os.getenv("PAYMOB_WEBHOOK_BASE_URL", "").strip()
    # For webhook base, prefer PAYMOB_WEBHOOK_BASE_URL, fallback to PAYMOB_BASE_URL
    webhook_base_url = os.getenv("PAYMOB_WEBHOOK_BASE_URL", "").strip() or os.getenv("PAYMOB_BASE_URL", "").strip()
    sandbox = os.getenv("PAYMOB_SANDBOX", "true").lower() == "true"

    missing = []
    if not secret_key:
        missing.append("PAYMOB_SECRET_KEY/PAYMOB_API_KEY")
    if not hmac_secret:
        missing.append("PAYMOB_HMAC_SECRET")
    if not integration_id:
        missing.append("PAYMOB_INTEGRATION_ID")
    if not public_key:
        missing.append("PAYMOB_PUBLIC_KEY/PAYMOB_IFRAME_ID")
    if not webhook_base_url:
        missing.append("PAYMOB_WEBHOOK_BASE_URL/PAYMOB_BASE_URL")

    # Enforce HTTPS for webhook URL in production
    if not sandbox and webhook_base_url and not webhook_base_url.startswith("https://"):
        raise PaymobConfigError("PAYMOB_WEBHOOK_BASE_URL must use HTTPS in production mode")

    if missing:
        raise PaymobConfigError(f"Missing Paymob configuration: {', '.join(missing)}")

    return secret_key, hmac_secret, integration_id, public_key, webhook_base_url, sandbox


def _paymob_base_url(sandbox: bool) -> str:
    """Get Paymob API base URL."""
    # Prefer PAYMOB_BASE_URL env, fallback to legacy paymobsolutions.com for backwards compat
    env_base = os.getenv("PAYMOB_BASE_URL", "").strip()
    if env_base:
        return env_base.rstrip("/")
    # For backwards compatibility with existing tests that expect paymobsolutions.com
    return "https://accept.paymobsolutions.com"


# ---- Paymob API Client ----
class PaymobClient:
    """Paymob API client for creating intentions and verifying webhooks.

    Supports both new Intention API (POST /api/intention/ with SECRET_KEY)
    and legacy flow (api/auth/tokens -> api/ecommerce/orders -> api/acceptance/payment_keys)
    for backwards compatibility. Prefers Intention API when PAYMOB_SECRET_KEY is set.
    """

    def __init__(self, api_key: str, hmac_secret: str, integration_id: str, iframe_id: str, webhook_base_url: str, sandbox: bool):
        self.api_key = api_key  # Secret key for Intention API (or legacy API key)
        self.hmac_secret = hmac_secret
        self.integration_id = integration_id
        self.iframe_id = iframe_id  # Public key for checkout URL
        self.webhook_base_url = webhook_base_url.rstrip("/")
        self.sandbox = sandbox
        self.base_url = _paymob_base_url(sandbox)
        self._auth_token: Optional[str] = None
        self._auth_token_expires: Optional[datetime] = None

    async def _get_auth_token(self) -> str:
        """Get or refresh Paymob auth token (legacy flow only)."""
        if self._auth_token and self._auth_token_expires and datetime.now() < self._auth_token_expires:
            return self._auth_token

        url = f"{self.base_url}/api/auth/tokens"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json={"api_key": self.api_key})
            response.raise_for_status()
            data = response.json()
            self._auth_token = data["token"]
            # Token typically valid for 1 hour, refresh 5 min early
            self._auth_token_expires = datetime.now() + timedelta(minutes=55)
            return self._auth_token

    async def _create_intention_new_api(
        self,
        amount_cents: int,
        currency: str,
        payment_id: str,
        user_id: str,
        user_email: str,
        user_phone: str = "",
        request: Request = None,
    ) -> Tuple[str, str]:
        """Create intention via new Paymob Intention API (POST /api/intention/)."""
        # Prepare billing data
        billing_data = {
            "first_name": "Customer",
            "last_name": "User",
            "email": user_email,
            "phone_number": user_phone or "+201000000000",
            "street": "N/A",
            "building": "N/A",
            "floor": "N/A",
            "apartment": "N/A",
            "city": "Cairo",
            "country": "EG",
            "state": "Cairo",
            "postal_code": "11511",
        }

        # Intention API payload
        # Determine redirection URL from request origin if available, otherwise use webhook_base_url
        # This ensures local testing uses localhost, production uses Railway
        # Definition is shared by both the new Intention API path and the legacy fallback path
        if request and hasattr(request, 'client') and request.client:
            # Get the actual origin from the HTTP request that initiated checkout
            host = request.client.host
            port = request.client.port
            scheme = 'https' if request.url.scheme == 'https' else 'http'
            # Build origin: scheme://host[:port]
            if (scheme == 'https' and port == 443) or (scheme == 'http' and port == 80):
                request_origin = f"{scheme}://{host}"
            else:
                request_origin = f"{scheme}://{host}:{port}"
            # Only allow known application origins to prevent open redirect
            allowed_origins = [
                'http://localhost:8000',
                'http://127.0.0.1:8000',
                'https://mybitewise.up.railway.app',
            ]
            if request_origin in allowed_origins:
                final_redirection_url = f"{request_origin}/app/bitewise-app(2).html"
            else:
                # Fall back to webhook_base_url for unknown origins
                final_redirection_url = f"{self.webhook_base_url.rstrip('/').split('/payments')[0]}/app/bitewise-app(2).html"
        else:
            # No request context, fall back to webhook_base_url
            final_redirection_url = f"{self.webhook_base_url.rstrip('/').split('/payments')[0]}/app/bitewise-app(2).html"

        payload = {
            "amount": amount_cents,
            "currency": currency,
            "payment_methods": [int(self.integration_id)],
            "items": [
                {
                    "name": f"NutriLens AI Premium - {payment_id}",
                    "amount": amount_cents,
                    "description": f"Premium subscription ({payment_id})",
                    "quantity": 1,
                }
            ],
            "billing_data": billing_data,
            "customer": {
                "first_name": "Customer",
                "last_name": "User",
                "email": user_email,
                "extras": {"payment_id": payment_id, "user_id": user_id},
            },
            "extras": {"payment_id": payment_id, "user_id": user_id},
            "special_reference": payment_id,
            "notification_url": f"{self.webhook_base_url.rstrip('/').split('/payments')[0]}/payments/paymob/callback",
            "redirection_url": final_redirection_url,
        }

        url = f"{self.base_url}/v1/intention/"
        headers = {"Authorization": f"Token {self.api_key}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            # Intention API returns client_secret and id
            client_secret = data.get("client_secret") or data.get("clientSecret") or data.get("token")
            order_id = str(data.get("id") or data.get("intention_order_id") or payment_id)
            if not client_secret:
                raise RuntimeError(f"Intention API did not return client_secret: {data}")
            return client_secret, order_id

    async def create_intention(
        self,
        amount_cents: int,
        currency: str,
        payment_id: str,
        user_id: str,
        user_email: str,
        user_phone: str = "",
        request: Request = None,
    ) -> Tuple[str, str]:
        """Create a Paymob intention and return (client_secret, order_id).

        Tries new Intention API first (SECRET_KEY), falls back to legacy
        (auth token + order + payment_keys) for backwards compatibility.

        Args:
            amount_cents: Amount in cents
            currency: Currency code (e.g., "USD", "EGP")
            payment_id: Our internal payment ID
            user_id: User ID for metadata
            user_email: User email for billing
            user_phone: User phone for billing (optional)

        Returns:
            (client_secret, order_id) - client_secret for redirect, order_id for tracking
        """
        # Try new Intention API if secret key looks like a Paymob secret (starts with sk_ or is long JWT)
        # The new SECRET_KEY is typically longer and may be a JWT, while old API_KEY is also a JWT
        # We will try new API first, and fallback to legacy on failure
        try:
            return await self._create_intention_new_api(
                amount_cents, currency, payment_id, user_id, user_email, user_phone
            )
        except Exception as e:
            # If new API fails (e.g., 401, 404, or missing secret), fallback to legacy for backwards compat
            # Check if error is due to endpoint not found or auth failure
            err_msg = str(e).lower()
            if "404" in err_msg or "not found" in err_msg or "intention" in err_msg:
                logger.warning(f"[Paymob] Intention API failed ({e}), falling back to legacy flow")
            else:
                # For other errors (e.g., invalid secret), still try legacy but log
                logger.warning(f"[Paymob] Intention API failed, trying legacy: {e}")

        # Fallback to legacy flow
        auth_token = await self._get_auth_token()

        # Prepare billing data (required by Paymob)
        billing_data = {
            "first_name": "Customer",
            "last_name": "User",
            "email": user_email,
            "phone_number": user_phone or "+201000000000",  # Required, use placeholder if missing
            "street": "N/A",
            "building": "N/A",
            "floor": "N/A",
            "apartment": "N/A",
            "city": "Cairo",
            "country": "EG",
            "state": "Cairo",
            "postal_code": "11511",
        }

        payload = {
            "amount": amount_cents,
            "currency": currency,
            "payment_methods": [int(self.integration_id)],
            "items": [
                {
                    "name": f"NutriLens AI Premium - {payment_id}",
                    "amount": amount_cents,
                    "description": f"Premium subscription ({payment_id})",
                    "quantity": 1,
                }
            ],
            "billing_data": billing_data,
            "extras": {
                "payment_id": payment_id,
                "user_id": user_id,
            },
        }

        url = f"{self.base_url}/api/ecommerce/orders"
        headers = {"Authorization": f"Token {auth_token}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        order_id = str(data["id"])

        # Now create payment key for the iframe
        payment_key_payload = {
            "source": {"identifier": "payment_key", "subtype": "payment_key"},
            "amount": amount_cents,
            "currency": currency,
            "order_id": order_id,
            "integration_id": self.integration_id,
            "billing_data": billing_data,
            "extras": {
                "payment_id": payment_id,
                "user_id": user_id,
            },
        }

        url = f"{self.base_url}/api/acceptance/payment_keys"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payment_key_payload, headers=headers)
            response.raise_for_status()
            payment_key_data = response.json()
            client_secret = payment_key_data["token"]

        return client_secret, order_id

    def verify_hmac(self, payload: Dict[str, Any], received_hmac: str) -> bool:
        """Verify Paymob HMAC signature.

        Paymob uses HMAC-SHA512 with the HMAC secret on the 'obj' field.
        """
        if not received_hmac:
            return False

        # Paymob HMAC is calculated on the 'obj' field
        obj = payload.get("obj", {})
        if not obj:
            return False

        # Sort keys for consistent serialization
        payload_bytes = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()

        expected_hmac = hmac.new(
            self.hmac_secret.encode(),
            payload_bytes,
            hashlib.sha512
        ).hexdigest()

        return hmac.compare_digest(expected_hmac, received_hmac)

    def get_checkout_url(self, client_secret: str, iframe_id: str) -> str:
        """Generate Paymob Unified Checkout URL."""
        return f"{self.base_url}/unifiedcheckout/?publicKey={iframe_id}&clientSecret={client_secret}"


# ---- Firestore helpers ----
def _payments_col():
    return get_db().collection("payments")


def _subscriptions_col():
    return get_db().collection("subscriptions")


# ---- Core payment operations ----
def get_plan(plan_id: str) -> Optional[Plan]:
    """Get a plan by ID (server-side only)."""
    return PLANS.get(plan_id)


def list_plans() -> List[Plan]:
    """List all available plans."""
    return list(PLANS.values())


async def create_payment(
    user_id: str,
    plan_id: str,
    user_email: str,
    user_phone: str = "",
    request: Request = None,
) -> PaymentCreateResponse:
    """Create a pending payment record for a user selecting a plan.

    Creates Paymob intention and returns Paymob checkout URL.
    Does NOT activate premium.

    Args:
        user_id: Authenticated user ID
        plan_id: Selected plan ID (must be in PLANS)
        user_email: User email for billing
        user_phone: User phone for billing (optional)

    Returns:
        PaymentCreateResponse with Paymob checkout URL
    """
    plan = get_plan(plan_id)
    if not plan:
        raise PaymentError(f"Invalid plan: {plan_id}")

    payment_id = _payment_id()
    expires_at = _utcnow() + timedelta(minutes=30)  # 30 min to complete payment

    # Try to create Paymob intention
    paymob_checkout_url = None
    paymob_order_id = None
    paymob_client_secret = None

    # Get Paymob config first (including sandbox mode)
    try:
        api_key, hmac_secret, integration_id, iframe_id, webhook_base_url, sandbox = _get_paymob_config()
    except PaymobConfigError as e:
        logger.warning(f"[payments] Paymob not configured: {e}")
        # In production (no sandbox), this is a hard failure - cannot create payments without Paymob
        # In sandbox/development, allow mock flow for testing
        sandbox = os.getenv("PAYMOB_SANDBOX", "true").lower() == "true"
        if not sandbox:
            raise PaymentError("Paymob is not configured. Please set all required Paymob environment variables.")
        # Return mock URL for development/testing when Paymob not configured (sandbox only)
        paymob_checkout_url = f"/Payment.html?payment_id={payment_id}"
    else:
        # Config successful - create Paymob intention
        try:
            paymob = PaymobClient(api_key, hmac_secret, integration_id, iframe_id, webhook_base_url, sandbox)

            # Create intention and get client_secret for checkout
            client_secret, order_id = await paymob.create_intention(
                amount_cents=plan.price_cents,
                currency=plan.currency,
                payment_id=payment_id,
                user_id=user_id,
                user_email=user_email,
                user_phone=user_phone,
                request=request,
            )

            paymob_client_secret = client_secret
            paymob_order_id = order_id
            paymob_checkout_url = paymob.get_checkout_url(client_secret, iframe_id)

            logger.info(f"[payments] Created Paymob intention for payment {payment_id}: order={order_id}")
        except Exception as e:
            # In sandbox, fallback to mock URL if Paymob fails (e.g., 401 due to test credentials)
            if sandbox:
                logger.warning(f"[payments] Paymob failed in sandbox, using mock checkout: {e}")
                paymob_checkout_url = f"/Payment.html?payment_id={payment_id}"
                paymob_order_id = None
                paymob_client_secret = None
            else:
                logger.error(f"[payments] Failed to create Paymob intention: {type(e).__name__}: {e}")
                raise PaymentError(f"Failed to initialize payment with provider: {e}")

    payment_doc = {
        "payment_id": payment_id,
        "user_id": user_id,
        "plan_id": plan_id,
        "amount_cents": plan.price_cents,
        "currency": plan.currency,
        "status": PaymentStatus.PENDING.value,
        "created_at": _utcnow(),
        "expires_at": expires_at,
        "paid_at": None,
        "provider_transaction_id": paymob_order_id,
        "provider": "paymob" if paymob_order_id else None,
        "paymob_order_id": paymob_order_id,
        "paymob_client_secret": paymob_client_secret,
        "webhook_processed_at": None,
        "webhook_signature_valid": None,
    }

    _payments_col().document(payment_id).set(payment_doc)

    return PaymentCreateResponse(
        payment_id=payment_id,
        payment_url=paymob_checkout_url,
        status=PaymentStatus.PENDING.value,
        plan=plan,
        expires_at=expires_at,
    )


def get_payment(payment_id: str) -> Optional[Dict[str, Any]]:
    """Get payment record by ID."""
    doc = _payments_col().document(payment_id).get()
    if doc.exists:
        return doc.to_dict()
    return None


def get_payment_status(payment_id: str) -> Optional[PaymentStatusResponse]:
    """Get payment status for polling/display."""
    payment = get_payment(payment_id)
    if not payment:
        return None
    # Only return payment_url for PENDING payments
    payment_url = None
    if payment.get("paymob_client_secret") and payment["status"] == PaymentStatus.PENDING.value:
        iframe_id = os.getenv("PAYMOB_IFRAME_ID", "").strip()
        if iframe_id:
            payment_url = f"https://accept.paymobsolutions.com/unifiedcheckout?publicKey={iframe_id}&clientSecret={payment.get('paymob_client_secret')}"
    return PaymentStatusResponse(
        payment_id=payment["payment_id"],
        status=payment["status"],
        plan_id=payment["plan_id"],
        amount_cents=payment["amount_cents"],
        currency=payment["currency"],
        created_at=payment["created_at"],
        paid_at=payment.get("paid_at"),
        provider_transaction_id=payment.get("provider_transaction_id"),
        payment_url=payment_url,
    )


def _verify_paymob_hmac(payload: Dict[str, Any], hmac_secret: str) -> bool:
    """Verify Paymob HMAC signature using SHA512.

    Paymob transaction callbacks (both legacy and Intention) use HMAC-SHA512
    over the concatenated string of 20 fields in exact documented order.
    See: https://developers.paymob.com/docs/hmac-calculation
    """
    received_hmac = payload.get("hmac")
    if not received_hmac:
        return False

    obj = payload.get("obj", {})
    if not obj:
        return False

    # Paymob HMAC: concatenated values of 20 fields in exact order
    # For missing/null values, use empty string; booleans as lowercase true/false
    def _get(obj_dict, key, default=""):
        val = obj_dict.get(key, default)
        if val is None:
            return ""
        if isinstance(val, bool):
            return str(val).lower()
        return str(val)

    order = obj.get("order", {}) or {}
    source_data = obj.get("source_data", {}) or {}

    concatenated = (
        _get(obj, "amount_cents") +
        _get(obj, "created_at") +
        _get(obj, "currency") +
        _get(obj, "error_occured") +
        _get(obj, "has_parent_transaction") +
        _get(obj, "id") +
        _get(obj, "integration_id") +
        _get(obj, "is_3d_secure") +
        _get(obj, "is_auth") +
        _get(obj, "is_capture") +
        _get(obj, "is_refunded") +
        _get(obj, "is_standalone_payment") +
        _get(obj, "is_voided") +
        _get(order, "id") +
        _get(obj, "owner") +
        _get(obj, "pending") +
        _get(source_data, "pan") +
        _get(source_data, "sub_type") +
        _get(source_data, "type") +
        _get(obj, "success")
    )

    expected_hmac = hmac.new(
        hmac_secret.encode(),
        concatenated.encode(),
        hashlib.sha512
    ).hexdigest()

    return hmac.compare_digest(expected_hmac, received_hmac)


def _activate_paid_subscription(payment_id: str) -> bool:
    """Activate premium subscription after VERIFIED payment.

    This is THE ONLY place that sets user.premium = True.
    Idempotent: safe to call multiple times for same payment_id.

    Returns True if subscription was activated (or already active).
    Returns False if payment not verified/paid.
    """
    payment = get_payment(payment_id)
    if not payment:
        raise PaymentVerificationError(f"Payment not found: {payment_id}")

    # Only activate if payment is verified PAID
    if payment["status"] != PaymentStatus.PAID.value:
        raise PaymentVerificationError(
            f"Payment {payment_id} not paid (status: {payment['status']})"
        )

    # Check if already processed (idempotency)
    if payment.get("webhook_processed_at"):
        # Already activated - return success without double-activating
        return True

    user_id = payment["user_id"]
    plan_id = payment["plan_id"]
    provider_txn_id = payment.get("provider_transaction_id")

    db = get_db()
    user_ref = db.collection("users").document(user_id)

    # Use a transaction for atomicity
    @firestore.transactional
    def _activate_txn(transaction):
        # transaction.get returns a generator that yields DocumentSnapshot(s)
        user_docs = list(transaction.get(user_ref))
        user_doc = user_docs[0] if user_docs else None
        if not user_doc or not user_doc.exists:
            raise PaymentVerificationError(f"User not found: {user_id}")

        user_data = user_doc.to_dict() or {}

        # Don't downgrade existing premium users
        if user_data.get("premium") is True:
            return  # Already premium, no change needed

        # Activate premium
        transaction.update(user_ref, {
            "premium": True,
            "subscription_plan": plan_id,
            "subscription_status": SubscriptionStatus.ACTIVE.value,
            "subscription_started_at": _utcnow(),
            "last_payment_id": payment_id,
            "last_payment_amount_cents": payment["amount_cents"],
            "last_payment_currency": payment["currency"],
            "last_payment_provider_transaction_id": provider_txn_id,
        })

    # Run transaction
    try:
        _activate_txn(db.transaction())
    except Exception as e:
        raise PaymentVerificationError(f"Failed to activate subscription: {e}")

    # Mark payment as processed (idempotency key)
    _payments_col().document(payment_id).update({
        "webhook_processed_at": _utcnow(),
        "subscription_activated": True,
    })

    # Create subscription record
    subscription_doc = {
        "user_id": user_id,
        "plan_id": plan_id,
        "status": SubscriptionStatus.ACTIVE.value,
        "current_period_start": _utcnow(),
        "current_period_end": _utcnow() + timedelta(days=30 if plan_id == "monthly" else 365),
        "payment_id": payment_id,
        "provider_subscription_id": provider_txn_id,
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }
    _subscriptions_col().document(payment_id).set(subscription_doc)

    return True


async def process_paymob_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Process Paymob transaction processed callback.

    This is called from the webhook endpoint with the raw Paymob payload.
    Verifies HMAC, validates payment, activates subscription if paid.

    Args:
        payload: Raw Paymob webhook JSON payload

    Returns:
        Dict with success status and details
    """
    try:
        # Get HMAC secret for verification
        hmac_secret = os.getenv("PAYMOB_HMAC_SECRET", "").strip()
        if not hmac_secret:
            raise PaymentVerificationError("PAYMOB_HMAC_SECRET not configured")

        # Verify HMAC signature
        if not _verify_paymob_hmac(payload, hmac_secret):
            logger.warning("[payments] Paymob webhook: Invalid HMAC signature")
            # Still try to extract payment_id for logging
            payment_id = None
            if "obj" in payload and "order" in payload["obj"]:
                payment_id = payload["obj"]["order"].get("merchant_order_id")
            if payment_id:
                _payments_col().document(payment_id).update({
                    "webhook_signature_valid": False,
                    "webhook_error": "Invalid HMAC signature",
                })
            raise PaymentVerificationError("Invalid Paymob HMAC signature")

        # Extract payment details from Paymob payload
        obj = payload["obj"]
        order = obj["order"]
        transaction = obj.get("transaction", {})

        payment_id = order.get("merchant_order_id")
        if not payment_id:
            raise PaymentVerificationError("Missing merchant_order_id in Paymob webhook")

        payment = get_payment(payment_id)
        if not payment:
            raise PaymentVerificationError(f"Payment not found: {payment_id}")

        # Idempotency: already processed
        if payment.get("webhook_processed_at"):
            logger.info(f"[payments] Paymob webhook already processed: {payment_id}")
            return {
                "success": True,
                "message": "Webhook already processed",
                "already_processed": True,
            }

        # Verify payment details match
        paymob_amount_cents = order.get("amount_cents")
        if paymob_amount_cents is not None and paymob_amount_cents != payment["amount_cents"]:
            _payments_col().document(payment_id).update({
                "webhook_signature_valid": True,
                "webhook_error": f"Amount mismatch: expected {payment['amount_cents']}, got {paymob_amount_cents}",
            })
            raise PaymentVerificationError(
                f"Amount mismatch: expected {payment['amount_cents']}, got {paymob_amount_cents}"
            )

        paymob_currency = order.get("currency", "USD")
        if paymob_currency != payment["currency"]:
            _payments_col().document(payment_id).update({
                "webhook_signature_valid": True,
                "webhook_error": f"Currency mismatch: expected {payment['currency']}, got {paymob_currency}",
            })
            raise PaymentVerificationError(
                f"Currency mismatch: expected {payment['currency']}, got {paymob_currency}"
            )

        # Store Paymob transaction ID
        paymob_transaction_id = transaction.get("id") or order.get("id")
        if paymob_transaction_id:
            _payments_col().document(payment_id).update({
                "provider_transaction_id": str(paymob_transaction_id),
            })

        # Determine payment status from Paymob response
        # Transaction success = paid, transaction declined/failed = failed, void = cancelled, expired = expired
        transaction_success = transaction.get("success", False)
        order_paid = order.get("paid", False)
        order_void = order.get("void", False)
        order_expired = order.get("expired", False)
        transaction_expired = transaction.get("expired", False)
        # Paymob Intention standalone success signals (for is_standalone_payment=true flow)
        obj_success = obj.get("success", False)
        obj_pending = obj.get("pending", True)
        obj_error = obj.get("error_occured", True)
        is_standalone_payment = obj.get("is_standalone_payment", False)

        if (transaction_success and order_paid) or (
            obj_success is True
            and obj_pending is False
            and obj_error is False
            and is_standalone_payment is True
        ):
            new_status = PaymentStatus.PAID.value
        elif order_void:
            new_status = PaymentStatus.CANCELLED.value
        elif order_expired or transaction_expired:
            new_status = PaymentStatus.EXPIRED.value
        elif transaction.get("error_occured") or (not transaction_success and not order_paid):
            new_status = PaymentStatus.FAILED.value
        else:
            # Unknown/pending state - don't activate
            new_status = "UNKNOWN"

        logger.info(f"[payments] Paymob webhook for {payment_id}: status={new_status}, transaction_id={paymob_transaction_id}")

        if new_status == PaymentStatus.PAID.value:
            _payments_col().document(payment_id).update({
                "status": PaymentStatus.PAID.value,
                "paid_at": _utcnow(),
                "provider": "paymob",
                "webhook_signature_valid": True,
            })
            # Activate subscription
            _activate_paid_subscription(payment_id)
            return {"success": True, "activated": True, "status": "paid"}

        elif new_status == PaymentStatus.FAILED.value:
            _payments_col().document(payment_id).update({
                "status": PaymentStatus.FAILED.value,
                "webhook_processed_at": _utcnow(),
                "webhook_signature_valid": True,
            })
            return {"success": True, "activated": False, "reason": "payment_failed"}

        elif new_status == PaymentStatus.CANCELLED.value:
            _payments_col().document(payment_id).update({
                "status": PaymentStatus.CANCELLED.value,
                "webhook_processed_at": _utcnow(),
                "webhook_signature_valid": True,
            })
            return {"success": True, "activated": False, "reason": "payment_cancelled"}

        elif new_status == PaymentStatus.EXPIRED.value:
            _payments_col().document(payment_id).update({
                "status": PaymentStatus.EXPIRED.value,
                "webhook_processed_at": _utcnow(),
                "webhook_signature_valid": True,
            })
            return {"success": True, "activated": False, "reason": "payment_expired"}

        else:
            # Unknown status - don't activate, mark for review
            _payments_col().document(payment_id).update({
                "webhook_processed_at": _utcnow(),
                "webhook_signature_valid": True,
                "webhook_error": f"Unknown Paymob status: transaction_success={transaction_success}, order_paid={order_paid}, order_void={order_void}",
            })
            return {"success": True, "activated": False, "reason": f"unknown_status_{new_status}"}

    except PaymentVerificationError:
        raise
    except Exception as e:
        logger.error(f"[payments] Paymob webhook processing error: {type(e).__name__}: {e}")
        raise PaymentVerificationError(f"Webhook processing failed: {e}")


def process_webhook(payload: WebhookPayload) -> Dict[str, Any]:
    """Process payment provider webhook (generic - for testing/other providers).

    Verifies:
    - Payment exists
    - Amount matches
    - Currency matches
    - Plan matches
    - Webhook signature (where supported)
    - Idempotency (duplicate webhook protection)

    Only activates premium if ALL checks pass.
    """
    payment = get_payment(payload.payment_id)
    if not payment:
        raise PaymentVerificationError(f"Payment not found: {payload.payment_id}")

    # Idempotency: already processed
    if payment.get("webhook_processed_at"):
        return {
            "success": True,
            "message": "Webhook already processed",
            "already_processed": True,
        }

    # Verify webhook signature if provided
    if payload.raw_payload and os.getenv("PAYMENT_WEBHOOK_SECRET"):
        import json as json_mod
        raw_bytes = json_mod.dumps(payload.raw_payload, separators=(",", ":")).encode()
        provider_signature = payload.raw_payload.get("signature") or payload.raw_payload.get("hmac")
        if provider_signature:
            if not _verify_webhook_signature(raw_bytes, provider_signature):
                _payments_col().document(payload.payment_id).update({
                    "webhook_signature_valid": False,
                    "webhook_error": "Invalid signature",
                })
                raise PaymentVerificationError("Invalid webhook signature")

    # Verify payment details match
    if payload.amount_cents is not None and payload.amount_cents != payment["amount_cents"]:
        raise PaymentVerificationError(f"Amount mismatch: expected {payment['amount_cents']}, got {payload.amount_cents}")

    if payload.currency and payload.currency != payment["currency"]:
        raise PaymentVerificationError(f"Currency mismatch: expected {payment['currency']}, got {payload.currency}")

    if payload.provider_transaction_id:
        _payments_col().document(payload.payment_id).update({
            "provider_transaction_id": payload.provider_transaction_id,
        })

    # Update payment status based on webhook
    new_status = payload.status.upper()
    if new_status in (PaymentStatus.PAID.value, "SUCCESS", "COMPLETED", "APPROVED"):
        _payments_col().document(payload.payment_id).update({
            "status": PaymentStatus.PAID.value,
            "paid_at": _utcnow(),
            "provider": payload.provider,
        })
        _activate_paid_subscription(payload.payment_id)
        return {"success": True, "activated": True}

    elif new_status in (PaymentStatus.FAILED.value, "DECLINED", "REJECTED"):
        _payments_col().document(payload.payment_id).update({
            "status": PaymentStatus.FAILED.value,
            "webhook_processed_at": _utcnow(),
        })
        return {"success": True, "activated": False, "reason": "payment_failed"}

    elif new_status in (PaymentStatus.CANCELLED.value, "CANCELED", "VOIDED"):
        _payments_col().document(payload.payment_id).update({
            "status": PaymentStatus.CANCELLED.value,
            "webhook_processed_at": _utcnow(),
        })
        return {"success": True, "activated": False, "reason": "payment_cancelled"}

    elif new_status in (PaymentStatus.EXPIRED.value, "EXPIRED"):
        _payments_col().document(payload.payment_id).update({
            "status": PaymentStatus.EXPIRED.value,
            "webhook_processed_at": _utcnow(),
        })
        return {"success": True, "activated": False, "reason": "payment_expired"}

    else:
        _payments_col().document(payload.payment_id).update({
            "webhook_processed_at": _utcnow(),
            "webhook_error": f"Unknown status: {new_status}",
        })
        return {"success": True, "activated": False, "reason": f"unknown_status_{new_status}"}


def _verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Verify HMAC signature from payment provider webhook (generic SHA256)."""
    if not signature:
        return False
    key = os.getenv("PAYMENT_WEBHOOK_SECRET", "").encode()
    if not key:
        return False
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def get_user_subscription(user_id: str) -> Optional[SubscriptionResponse]:
    """Get user's current subscription status."""
    try:
        docs = _subscriptions_col().where("user_id", "==", user_id).order_by("created_at", direction=firestore.Query.DESCENDING).limit(1).stream()
        for doc in docs:
            data = doc.to_dict()
            return SubscriptionResponse(**data)
    except Exception:
        # Index not ready (400 FailedPrecondition) — fallback without ordering
        try:
            docs = _subscriptions_col().where("user_id", "==", user_id).limit(10).stream()
            latest = None
            latest_time = None
            for doc in docs:
                data = doc.to_dict()
                created = data.get("created_at")
                if latest is None or (created and (latest_time is None or created > latest_time)):
                    latest = data
                    latest_time = created
                elif latest is None:
                    latest = data
            if latest:
                return SubscriptionResponse(**latest)
        except Exception:
            pass
    return None


def cancel_subscription(user_id: str) -> bool:
    """Cancel user's active subscription (premium remains until period end)."""
    docs = _subscriptions_col().where("user_id", "==", user_id).where("status", "==", SubscriptionStatus.ACTIVE.value).limit(1).stream()
    for doc in docs:
        doc.reference.update({
            "status": SubscriptionStatus.CANCELLED.value,
            "updated_at": _utcnow(),
        })
        # Note: We don't immediately set premium=False - that happens
        # when the billing period ends or via a separate cron job
        return True
    return False