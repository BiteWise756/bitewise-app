"""Customer-support pipeline: inbox -> identify -> AI reply -> thread storage.

Data model (Firestore):

    support_conversations/{threadId}
        threadId, customerEmail, customerUid?, customerName?, subject,
        status, aiEnabled, aiHandled, handlerUid?, createdAt, updatedAt,
        lastActivityAt

    support_conversations/{threadId}/messages/{gmailMessageId}
        messageId, threadId, direction (in|out), kind (customer|ai|human),
        from, to, subject, body, senderEmail, receivedAt

Status lifecycle:
    NEW -> PROCESSING -> AI_REPLIED | NEEDS_HUMAN | ERROR
    HUMAN_HANDLED (staff take-over; AI disabled for that conversation)
    RESOLVED (staff closes the conversation)

Security rules:
- The user context handed to Gemini is an explicit whitelist built by
  safe_customer_profile()/build_support_context(). OTP codes, password
  fields, hashes and tokens are NEVER included.
- Replies are only sent to the original sender of the thread.
- Every inbound message is processed exactly once (dedupe by Gmail message id).
- When the AI is unavailable or unsure, the conversation escalates to
  NEEDS_HUMAN and no fabricated answer is ever sent.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.cloud.firestore import FieldFilter

from firebase.config import get_db
from services import gmail_inbox
from services.gemini_service import generate_support_reply
from utils.helpers import stream_sorted

# ---- statuses ----
STATUS_NEW = "NEW"
STATUS_PROCESSING = "PROCESSING"
STATUS_AI_REPLIED = "AI_REPLIED"
STATUS_NEEDS_HUMAN = "NEEDS_HUMAN"
STATUS_HUMAN_HANDLED = "HUMAN_HANDLED"
STATUS_RESOLVED = "RESOLVED"
STATUS_ERROR = "ERROR"
STATUS_PENDING_APPROVAL = "PENDING_APPROVAL"

VALID_STATUSES = {
    STATUS_NEW, STATUS_PROCESSING, STATUS_AI_REPLIED, STATUS_NEEDS_HUMAN,
    STATUS_HUMAN_HANDLED, STATUS_RESOLVED, STATUS_ERROR, STATUS_PENDING_APPROVAL,
}

# Topics that must never be auto-answered; escalate immediately to a human.
_ESCALATION_KEYWORDS = [
    "refund", "reimburse", "charge", "billing", "charged",
    "hack", "hacked", "stolen", "unauthorized", "compromised",
    "otp", "verification code", "password", "reset link",
    "lawyer", "lawsuit", "police", "court", "legal",
]

# Explicit whitelist of user-document fields that MAY appear in the AI context.
_CONTEXT_KEYS = [
    "displayName", "accountType", "premium",
    "goal", "age", "gender", "height", "weight", "calorieTarget",
    "dietaryProfile", "allergies", "mealPreferences",
]

_SENSITIVE_KEY_FRAGMENTS = ("otp", "password", "hash", "secret", "token")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conversations_col():
    return get_db().collection("support_conversations")


def safe_customer_profile(user: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelisted copy of a user document for AI/staff use.

    Explicitly drops any field whose name suggests a secret (otp, password,
    hash, token, secret), even if the user document schema changes later.
    """
    if not isinstance(user, dict):
        return {}
    safe = {}
    for key in _CONTEXT_KEYS:
        if key in user and user[key] is not None:
            value = user[key]
            if isinstance(value, (str, int, float, bool)):
                safe[key] = value
            elif isinstance(value, list) and all(isinstance(i, str) for i in value):
                safe[key] = value
    for key in list(user.keys()):
        if any(fragment in str(key).lower() for fragment in _SENSITIVE_KEY_FRAGMENTS):
            safe.pop(key, None)
    return safe


def build_support_context(customer: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-serializable context given to the Gemini support prompt.

    Only safe, whitelisted profile fields are included; OTP/security material
    is structurally impossible to include here.
    """
    if not customer:
        return {"customer": {}, "note": "Email does not match a registered BiteWise account."}
    context = {
        "customer": {
            "email": customer.get("email", ""),
            "name": customer.get("displayName", ""),
            "accountType": customer.get("accountType", "user"),
            "premium": bool(customer.get("premium", False)),
        }
    }
    extra = {}
    for key in ("goal", "age", "gender", "height", "weight", "calorieTarget",
                "dietaryProfile", "allergies", "mealPreferences"):
        if customer.get(key) not in (None, ""):
            extra[key] = customer[key]
    if extra:
        context["profile"] = extra
    return context


def identify_customer(email: str) -> Optional[Dict[str, Any]]:
    """Look up the registered user for an email. Returns the raw doc dict or None."""
    email = (email or "").strip().lower()
    if not email:
        return None
    try:
        docs = list(get_db().collection("users")
                    .where(filter=FieldFilter("email", "==", email)).limit(1).stream())
    except Exception as e:
        print(f"[support] identify_customer error: {type(e).__name__}")
        return None
    if not docs:
        return None
    data = docs[0].to_dict() or {}
    data.setdefault("email", email)
    data["uid"] = docs[0].id
    return data


def _triggers_escalation(message_text: str) -> bool:
    text = (message_text or "").lower()
    return any(kw in text for kw in _ESCALATION_KEYWORDS)


def get_conversation(thread_id: str) -> Optional[Dict[str, Any]]:
    if not thread_id:
        return None
    doc = _conversations_col().document(thread_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    data["threadId"] = doc.id
    return data


def _create_conversation(thread_id: str, subject: str, customer: Dict[str, Any],
                         customer_email: str) -> Dict[str, Any]:
    now = _now()
    data = {
        "threadId": thread_id,
        "customerEmail": customer_email,
        "customerUid": (customer or {}).get("uid"),
        "customerName": (customer or {}).get("displayName", ""),
        "subject": (subject or "(no subject)")[:300],
        "status": STATUS_NEW,
        "aiEnabled": True,
        "aiHandled": False,
        "handlerUid": None,
        "createdAt": now,
        "updatedAt": now,
        "lastActivityAt": now,
    }
    _conversations_col().document(thread_id).set(data)
    return data


def _store_message(thread_id: str, parsed: Dict[str, Any], direction: str,
                   kind: str, extra: Optional[Dict[str, Any]] = None) -> None:
    msg_id = parsed.get("messageId")
    if not msg_id:
        msg_id = f"{direction}-{int(_now().timestamp() * 1000)}"
    doc_data = {
        "messageId": msg_id,
        "threadId": thread_id,
        "direction": direction,
        "kind": kind,
        "from": parsed.get("fromHeader", ""),
        "to": parsed.get("toHeader", ""),
        "subject": parsed.get("subject", ""),
        "body": parsed.get("bodyText", ""),
        "senderEmail": parsed.get("senderEmail", ""),
        "receivedAt": _now(),
    }
    if extra:
        doc_data.update(extra)
    _conversations_col().document(thread_id).collection("messages").document(msg_id).set(doc_data)


def _already_processed(thread_id: str, message_id: str) -> bool:
    try:
        doc = (_conversations_col().document(thread_id)
               .collection("messages").document(message_id).get())
        return doc.exists
    except Exception:
        return False


def _set_conversation_status(thread_id: str, status: str, **fields) -> None:
    updates = {"status": status, "updatedAt": _now(), "lastActivityAt": _now()}
    updates.update(fields)
    _conversations_col().document(thread_id).update(updates)


def process_incoming_email(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Handle one parsed inbound email. Idempotent per Gmail message id."""
    thread_id = parsed.get("threadId") or parsed.get("messageId")
    message_id = parsed.get("messageId")
    if not thread_id or not message_id:
        return {"status": "skipped", "reason": "no thread/message id"}

    if _already_processed(thread_id, message_id):
        return {"status": "skipped", "reason": "duplicate"}

    customer_email = parsed.get("senderEmail") or ""
    if not customer_email:
        return {"status": "skipped", "reason": "no sender email"}

    conversation = get_conversation(thread_id)
    customer = identify_customer(customer_email)

    if conversation is None:
        conversation = _create_conversation(
            thread_id, parsed.get("subject", ""), customer, customer_email)
    else:
        _set_conversation_status(
            thread_id, conversation.get("status") or STATUS_NEW,
            customerEmail=customer_email,
        )

    # A human already owns this thread -> never auto-answer.
    if not conversation.get("aiEnabled", True) or conversation.get("status") == STATUS_HUMAN_HANDLED:
        _store_message(thread_id, parsed, "in", "customer")
        _set_conversation_status(thread_id, STATUS_NEEDS_HUMAN)
        return {"status": "escalated", "reason": "human-owned thread"}

    _store_message(thread_id, parsed, "in", "customer")
    _set_conversation_status(thread_id, STATUS_PROCESSING)

    message_text = parsed.get("bodyText", "").strip()
    if not message_text:
        _set_conversation_status(thread_id, STATUS_NEEDS_HUMAN)
        return {"status": "escalated", "reason": "empty body"}

    if _triggers_escalation(message_text):
        _set_conversation_status(thread_id, STATUS_NEEDS_HUMAN)
        return {"status": "escalated", "reason": "sensitive topic"}

    context = build_support_context(customer)
    try:
        ai_result = generate_support_reply(message_text, context)
    except Exception as e:
        print(f"[support] AI call error: {type(e).__name__}")
        ai_result = None

    if ai_result is None:
        # AI unavailable or produced unsafe output: escalate; never fake a reply.
        _set_conversation_status(thread_id, STATUS_NEEDS_HUMAN, aiHandled=False)
        return {"status": "escalated", "reason": "ai unavailable"}

    if ai_result.get("needs_human"):
        _store_message(thread_id, parsed, "in", "customer",
                       extra={"aiDraft": ai_result.get("reply", ""), "draftKind": "ai"})
        _set_conversation_status(thread_id, STATUS_NEEDS_HUMAN, aiHandled=False)
        return {"status": "escalated", "reason": "ai requested human"}

    # Safe AI reply generated — store as draft for human approval instead of auto-sending.
    _store_message(thread_id, parsed, "in", "customer",
                   extra={"aiDraft": ai_result["reply"], "draftKind": "ai", "pendingApproval": True})
    _set_conversation_status(thread_id, STATUS_PENDING_APPROVAL, aiHandled=False)
    return {"status": "pending_approval", "ai_draft": ai_result["reply"]}


def _reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}" if subject else "Re: Your BiteWise inquiry"


def poll_inbox(max_results: int = 20) -> Dict[str, Any]:
    """Fetch unread inbound mail and process each message exactly once."""
    try:
        emails = gmail_inbox.fetch_unread(max_results=max_results)
    except gmail_inbox.GmailInboxError as e:
        print(f"[support] poll failed: {e}")
        return {"status": "error", "processed": 0, "reason": str(e)}

    results = []
    for email in emails:
        try:
            results.append(process_incoming_email(email))
            if email.get("messageId"):
                gmail_inbox.mark_read(email["messageId"])
        except Exception as e:
            print(f"[support] process error: {type(e).__name__}")
            results.append({"status": "error", "reason": type(e).__name__})
    return {"status": "ok", "processed": len(results), "results": results}


def list_conversations(status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """List conversations, most recent activity first (no composite index)."""
    if status and status not in VALID_STATUSES:
        return []
    try:
        items = stream_sorted(_conversations_col(), order_field="lastActivityAt",
                              desc=True, limit=limit)
    except Exception as e:
        print(f"[support] list error: {type(e).__name__}")
        return []
    if status:
        items = [i for i in items if i.get("status") == status]
    for item in items:
        for key in ("createdAt", "updatedAt", "lastActivityAt"):
            if isinstance(item.get(key), datetime):
                item[key] = item[key].isoformat()
    return items


def get_conversation_detail(thread_id: str) -> Optional[Dict[str, Any]]:
    conversation = get_conversation(thread_id)
    if conversation is None:
        return None
    for key in ("createdAt", "updatedAt", "lastActivityAt"):
        if isinstance(conversation.get(key), datetime):
            conversation[key] = conversation[key].isoformat()
    try:
        docs = list(_conversations_col().document(thread_id)
                    .collection("messages").order_by("receivedAt").stream())
        messages = []
        for doc in docs:
            data = doc.to_dict() or {}
            if isinstance(data.get("receivedAt"), datetime):
                data["receivedAt"] = data["receivedAt"].isoformat()
            data.setdefault("id", doc.id)
            messages.append(data)
    except Exception as e:
        print(f"[support] messages error: {type(e).__name__}")
        messages = []
    conversation["messages"] = messages
    return conversation


def send_human_reply(thread_id: str, handler_uid: str, reply_text: str) -> Dict[str, Any]:
    conversation = get_conversation(thread_id)
    if conversation is None:
        raise ValueError("Conversation not found")
    reply_text = (reply_text or "").strip()
    if not reply_text:
        raise ValueError("Reply body is empty")

    sent = gmail_inbox.send_thread_reply(
        to_email=conversation["customerEmail"],
        subject=_reply_subject(conversation.get("subject", "")),
        body_text=reply_text,
        thread_id=thread_id,
    )
    _store_message(thread_id, {
        "messageId": sent.get("messageId"),
        "threadId": thread_id,
        "fromHeader": gmail_inbox.FIXED_SENDER,
        "toHeader": conversation["customerEmail"],
        "subject": _reply_subject(conversation.get("subject", "")),
        "bodyText": reply_text,
        "senderEmail": gmail_inbox.FIXED_SENDER,
    }, "out", "human")
    _set_conversation_status(thread_id, STATUS_HUMAN_HANDLED,
                             aiEnabled=False, handlerUid=handler_uid)
    return {"messageId": sent.get("messageId"), "status": STATUS_HUMAN_HANDLED}


def take_over(thread_id: str, handler_uid: str) -> Dict[str, Any]:
    conversation = get_conversation(thread_id)
    if conversation is None:
        raise ValueError("Conversation not found")
    _set_conversation_status(thread_id, STATUS_HUMAN_HANDLED,
                             aiEnabled=False, handlerUid=handler_uid)
    return {"status": STATUS_HUMAN_HANDLED}


def set_ai_enabled(thread_id: str, enabled: bool) -> Dict[str, Any]:
    conversation = get_conversation(thread_id)
    if conversation is None:
        raise ValueError("Conversation not found")
    status = STATUS_NEW if enabled else STATUS_HUMAN_HANDLED
    _set_conversation_status(thread_id, status, aiEnabled=enabled)
    return {"status": status, "aiEnabled": enabled}


def resolve_conversation(thread_id: str, handler_uid: str) -> Dict[str, Any]:
    conversation = get_conversation(thread_id)
    if conversation is None:
        raise ValueError("Conversation not found")
    _set_conversation_status(thread_id, STATUS_RESOLVED,
                             aiEnabled=False, handlerUid=handler_uid)
    return {"status": STATUS_RESOLVED}


def approve_ai_reply(thread_id: str, handler_uid: str, approve: bool, edited_reply: Optional[str] = None) -> Dict[str, Any]:
    """Approve or reject an AI-generated draft reply.
    
    If approve=True:
    - If edited_reply is provided, use it; otherwise use the stored AI draft.
    - Send the reply using send_thread_reply().
    - Preserve Gmail thread_id, In-Reply-To, and References headers.
    - Mark the original message as processed/read.
    - Update Firestore status to AI_REPLIED.
    - Record who approved it and when.
    
    If approve=False:
    - Do not send anything.
    - Remove/archive the AI draft.
    - Set status to HUMAN_HANDLED or REJECTED.
    - Do not lose the conversation history.
    """
    conversation = get_conversation(thread_id)
    if conversation is None:
        raise ValueError("Conversation not found")
    
    # Only allow approval if conversation is in PENDING_APPROVAL status
    current_status = conversation.get("status")
    if current_status != STATUS_PENDING_APPROVAL:
        return {"status": "error", "reason": f"Conversation not in PENDING_APPROVAL status (current: {current_status})"}
    
    # Find the pending AI draft message
    try:
        messages_ref = _conversations_col().document(thread_id).collection("messages")
        draft_docs = list(messages_ref.where(filter=FieldFilter("pendingApproval", "==", True)).limit(1).stream())
    except Exception as e:
        print(f"[support] approve_ai_reply: error finding draft: {type(e).__name__}")
        return {"status": "error", "reason": "Failed to retrieve AI draft"}
    
    if not draft_docs:
        return {"status": "error", "reason": "No pending AI draft found for this conversation"}
    
    draft_doc = draft_docs[0]
    draft_data = draft_doc.to_dict() or {}
    ai_draft = draft_data.get("aiDraft", "")
    original_message_id = draft_data.get("messageId", "")
    
    if not ai_draft:
        return {"status": "error", "reason": "AI draft is empty"}
    
    if not approve:
        # Reject: remove the draft flag, set status to HUMAN_HANDLED
        try:
            # Update the draft message to remove pendingApproval flag
            messages_ref.document(draft_doc.id).update({"pendingApproval": False})
            _set_conversation_status(thread_id, STATUS_HUMAN_HANDLED, 
                                     aiEnabled=False, handlerUid=handler_uid)
            return {"status": "rejected", "message": "AI draft rejected; conversation assigned to human"}
        except Exception as e:
            print(f"[support] approve_ai_reply reject error: {type(e).__name__}")
            return {"status": "error", "reason": "Failed to reject draft"}
    
    # Approve: send the reply
    reply_to_send = edited_reply if edited_reply is not None else ai_draft
    if not reply_to_send or not reply_to_send.strip():
        return {"status": "error", "reason": "Reply text is empty"}
    
    # Use the original message's messageIdHeader and referencesHeader for proper threading
    # We need to get the original customer message to extract headers
    original_msg_doc = messages_ref.document(original_message_id).get()
    message_id_header = ""
    references_header = ""
    if original_msg_doc.exists:
        orig_data = original_msg_doc.to_dict() or {}
        # The stored messageIdHeader might be in the extra field or the message itself
        message_id_header = orig_data.get("messageIdHeader", "")
        references_header = orig_data.get("referencesHeader", "")
    
    try:
        sent = gmail_inbox.send_thread_reply(
            to_email=conversation["customerEmail"],
            subject=_reply_subject(conversation.get("subject", "")),
            body_text=reply_to_send.strip(),
            thread_id=thread_id,
            message_id_header=message_id_header,
            references_header=references_header,
        )
    except Exception as e:
        print(f"[support] approve_ai_reply send error: {type(e).__name__}")
        return {"status": "error", "reason": "Gmail send failed"}
    
    # Store the sent message
    _store_message(thread_id, {
        "messageId": sent.get("messageId"),
        "threadId": thread_id,
        "fromHeader": gmail_inbox.FIXED_SENDER,
        "toHeader": conversation["customerEmail"],
        "subject": _reply_subject(conversation.get("subject", "")),
        "bodyText": reply_to_send.strip(),
        "senderEmail": gmail_inbox.FIXED_SENDER,
    }, "out", "ai", extra={"approvedBy": handler_uid, "approvedAt": _now(), "edited": edited_reply is not None})
    
    # Remove the pending approval flag from the draft message
    try:
        messages_ref.document(draft_doc.id).update({"pendingApproval": False})
    except Exception:
        pass  # Best effort
    
    # Update conversation status
    _set_conversation_status(thread_id, STATUS_AI_REPLIED, aiHandled=True, handlerUid=handler_uid)
    
    return {
        "status": "approved",
        "messageId": sent.get("messageId"),
        "threadId": thread_id,
        "edited": edited_reply is not None,
    }


def poll_loop(interval_seconds: int = 60, max_results: int = 20) -> None:
    """Background loop; intended for a daemon thread. Never raises."""
    import time
    while True:
        try:
            poll_inbox(max_results=max_results)
        except Exception as e:
            print(f"[support] poll loop error: {type(e).__name__}")
        time.sleep(max(10, int(interval_seconds)))
