"""Staff-only customer-support API.

Every endpoint requires an authenticated user with the support-staff role
(doctor, admin account type, or an email in SUPPORT_STAFF_EMAILS) via
``require_support_staff``. The staff identity comes from the verified token;
the handler uid is never taken from the request body.
"""

from fastapi import APIRouter, Depends, HTTPException

from models.schemas import SupportAiToggleRequest, SupportApprovalRequest, SupportReplyRequest
from services import support_service
from utils.auth import require_support_staff

router = APIRouter(prefix="/support", tags=["Support"])


@router.get("/conversations")
async def list_conversations(status: str = None, limit: int = 100,
                             current_user: dict = Depends(require_support_staff)):
    try:
        items = support_service.list_conversations(status=status, limit=min(limit, 500))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Could not list conversations")
    return {"success": True, "data": {"conversations": items}}


@router.get("/conversations/{thread_id}")
async def get_conversation(thread_id: str,
                           current_user: dict = Depends(require_support_staff)):
    detail = support_service.get_conversation_detail(thread_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True, "data": detail}


@router.post("/conversations/{thread_id}/reply")
async def send_reply(thread_id: str, body: SupportReplyRequest,
                     current_user: dict = Depends(require_support_staff)):
    try:
        result = support_service.send_human_reply(
            thread_id, current_user["uid"], body.reply)
    except ValueError as e:
        raise HTTPException(status_code=404 if "not found" in str(e) else 400, detail=str(e))
    except Exception as e:
        print(f"[support-route] reply error: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Could not send reply")
    return {"success": True, "data": result}


@router.post("/conversations/{thread_id}/take-over")
async def take_over(thread_id: str,
                    current_user: dict = Depends(require_support_staff)):
    try:
        result = support_service.take_over(thread_id, current_user["uid"])
    except ValueError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True, "data": result}


@router.post("/conversations/{thread_id}/ai")
async def toggle_ai(thread_id: str, body: SupportAiToggleRequest,
                    current_user: dict = Depends(require_support_staff)):
    try:
        result = support_service.set_ai_enabled(thread_id, body.enabled)
    except ValueError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True, "data": result}


@router.post("/conversations/{thread_id}/resolve")
async def resolve(thread_id: str,
                  current_user: dict = Depends(require_support_staff)):
    try:
        result = support_service.resolve_conversation(thread_id, current_user["uid"])
    except ValueError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True, "data": result}


@router.post("/conversations/{thread_id}/approve-ai")
async def approve_ai(thread_id: str, body: SupportApprovalRequest,
                     current_user: dict = Depends(require_support_staff)):
    try:
        result = support_service.approve_ai_reply(
            thread_id, current_user["uid"], body.approve, body.edited_reply)
    except ValueError as e:
        raise HTTPException(status_code=404 if "not found" in str(e) else 400, detail=str(e))
    except Exception as e:
        print(f"[support-route] approve-ai error: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Could not process approval")
    
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("reason", "Approval failed"))
    
    return {"success": True, "data": result}


@router.post("/poll")
async def trigger_poll(current_user: dict = Depends(require_support_staff)):
    try:
        result = support_service.poll_inbox(max_results=20)
    except Exception as e:
        print(f"[support-route] poll error: {type(e).__name__}")
        raise HTTPException(status_code=500, detail="Poll failed")
    return {"success": result.get("status") == "ok", "data": result}
