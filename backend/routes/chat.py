from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import firestore
from firebase.config import get_db
from models.schemas import ChatMessage
from utils.helpers import generate_chat_reply
from utils.auth import get_current_user
from datetime import datetime, timezone

router = APIRouter(prefix="/chat", tags=["Chat"])

@router.post("/")
async def chat(message: ChatMessage, current_user: dict = Depends(get_current_user)):
    try:
        # The identity always comes from the verified ID token. The
        # client-supplied userId is never trusted.
        uid = current_user["uid"]
        if message.userId and message.userId != uid:
            raise HTTPException(status_code=403, detail="Cannot access another user's data")

        reply = generate_chat_reply(message.message)
        now = datetime.now(timezone.utc)

        try:
            db = get_db()
            db.collection("ai_history").add({
                "userId": uid,
                "role": "user",
                "content": message.message,
                "timestamp": now,
            })
            db.collection("ai_history").add({
                "userId": uid,
                "role": "assistant",
                "content": reply,
                "timestamp": now,
            })
        except Exception as store_err:
            print(f"Failed to store AI history: {store_err}")

        return {
            "success": True,
            "data": {
                "reply": reply,
                "timestamp": now.isoformat(),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))