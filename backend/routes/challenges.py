from fastapi import APIRouter, Depends, HTTPException, Query
from firebase_admin import firestore
from google.cloud.firestore import FieldFilter
from firebase.config import get_db
from utils.auth import get_current_user
from typing import Optional
from pydantic import BaseModel

class ChallengeJoin(BaseModel):
    challengeId: str

router = APIRouter(prefix="/challenges", tags=["Challenges"])


def _require_own(user_id: str, current_user: dict) -> str:
    """Joining/leaving/listing uses the authenticated UID only."""
    if user_id != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Cannot manage challenges for another user")
    return user_id


@router.get("/")
async def get_challenges():
    """Public challenges catalog - intentionally readable without auth."""
    try:
        db = get_db()
        docs = db.collection("challenges").stream()
        items = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            items.append(data)
        if not items:
            items = [
                {"id": "no-sugar", "name": "No Sugar", "description": "7 days challenge", "icon": "🚫", "days": 7},
                {"id": "drink-water", "name": "Drink Water", "description": "14 days challenge", "icon": "💧", "days": 14},
                {"id": "high-protein", "name": "High Protein", "description": "7 days challenge", "icon": "🥩", "days": 7},
                {"id": "eat-veggies", "name": "Eat Veggies", "description": "7 days challenge", "icon": "🥦", "days": 7},
            ]
        return {"success": True, "data": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/join/{user_id}")
async def join_challenge(user_id: str, body: ChallengeJoin, current_user: dict = Depends(get_current_user)):
    try:
        uid = _require_own(user_id, current_user)
        db = get_db()
        doc_ref = db.collection("user_challenges").document(f"{uid}_{body.challengeId}")
        doc_ref.set({
            "userId": uid,
            "challengeId": body.challengeId,
            "joinedAt": firestore.SERVER_TIMESTAMP,
            "completed": False,
        })
        return {"success": True, "message": "Challenge joined"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/join/{user_id}")
async def leave_challenge(user_id: str, current_user: dict = Depends(get_current_user), challengeId: str = Query(...)):
    try:
        uid = _require_own(user_id, current_user)
        db = get_db()
        doc_ref = db.collection("user_challenges").document(f"{uid}_{challengeId}")
        doc = doc_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Challenge not joined")
        doc_ref.delete()
        return {"success": True, "message": "Challenge left"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/joined/{user_id}")
async def get_joined_challenges(user_id: str, current_user: dict = Depends(get_current_user)):
    try:
        uid = _require_own(user_id, current_user)
        db = get_db()
        docs = db.collection("user_challenges").where(filter=FieldFilter("userId", "==", uid)).stream()
        joined = [doc.to_dict().get("challengeId") for doc in docs]
        return {"success": True, "data": joined}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))