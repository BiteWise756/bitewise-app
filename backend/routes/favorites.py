from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import firestore
from google.cloud.firestore import FieldFilter
from firebase.config import get_db
from utils.auth import get_current_user
from typing import Optional
from pydantic import BaseModel

class FavoriteAdd(BaseModel):
    name: str
    type: str = "meal"
    score: Optional[int] = None
    meta: Optional[str] = None

router = APIRouter(prefix="/favorites", tags=["Favorites"])


def _get_owned_favorite(item_id: str, current_user: dict):
    """Fetch a favorite and verify ownership; returns the document."""
    db = get_db()
    doc = db.collection("favorites").document(item_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Favorite not found")
    if (doc.to_dict() or {}).get("userId") != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Cannot modify another user's favorite")
    return doc


@router.get("/{user_id}")
async def get_favorites(user_id: str, type: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    try:
        if user_id != current_user["uid"]:
            raise HTTPException(status_code=403, detail="Cannot access another user's favorites")
        db = get_db()
        query = db.collection("favorites").where(filter=FieldFilter("userId", "==", current_user["uid"]))
        if type:
            query = query.where(filter=FieldFilter("type", "==", type))
        docs = query.stream()
        items = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            items.append(data)
        return {"success": True, "data": items}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{user_id}")
async def add_favorite(user_id: str, body: FavoriteAdd, current_user: dict = Depends(get_current_user)):
    try:
        if user_id != current_user["uid"]:
            raise HTTPException(status_code=403, detail="Cannot add favorites for another user")
        db = get_db()
        doc_ref = db.collection("favorites").document()
        doc_ref.set({
            "userId": current_user["uid"],
            "name": body.name,
            "type": body.type,
            "score": body.score,
            "meta": body.meta or "",
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
        return {"success": True, "data": {"id": doc_ref.id, "name": body.name, "type": body.type, "score": body.score, "meta": body.meta or ""}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{item_id}")
async def delete_favorite(item_id: str, current_user: dict = Depends(get_current_user)):
    try:
        _get_owned_favorite(item_id, current_user)
        db = get_db()
        db.collection("favorites").document(item_id).delete()
        return {"success": True, "message": "Favorite deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))