from fastapi import APIRouter, Depends, HTTPException, Body
from firebase_admin import firestore
from firebase.config import get_db
from utils.auth import get_current_user
from typing import Optional
from pydantic import BaseModel
from google.cloud.firestore_v1.base_query import FieldFilter
from datetime import datetime, timezone

class ShoppingAdd(BaseModel):
    name: str
    amount: Optional[str] = None

class ShoppingUpdate(BaseModel):
    done: Optional[bool] = None
    name: Optional[str] = None
    amount: Optional[str] = None

router = APIRouter(prefix="/shopping", tags=["Shopping"])


def _require_own(user_id: str, current_user: dict) -> str:
    if user_id != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Cannot access another user's shopping list")
    return user_id


def _get_owned_item(item_id: str, current_user: dict):
    """Fetch a shopping item and verify ownership; returns the document."""
    db = get_db()
    doc = db.collection("shopping_lists").document(item_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Item not found")
    if (doc.to_dict() or {}).get("userId") != current_user["uid"]:
        raise HTTPException(status_code=403, detail="Cannot modify another user's item")
    return doc


@router.get("/{user_id}")
async def get_shopping_list(user_id: str, current_user: dict = Depends(get_current_user)):
    try:
        _require_own(user_id, current_user)
        db = get_db()
        # Filter only (no composite index needed); sort in app code by createdAt desc.
        docs = list(db.collection("shopping_lists").where(filter=FieldFilter("userId", "==", current_user["uid"])).stream())

        def _key(doc):
            data = doc.to_dict() or {}
            ts = data.get("createdAt")
            if isinstance(ts, datetime):
                return ts
            return datetime.min.replace(tzinfo=timezone.utc)

        docs.sort(key=_key, reverse=True)

        items = []
        for doc in docs:
            data = doc.to_dict()
            if not data:
                continue
            data["id"] = doc.id
            ts = data.get("createdAt")
            if ts and hasattr(ts, "isoformat"):
                data["createdAt"] = ts.isoformat()
            items.append(data)
        return {"success": True, "data": items}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{user_id}")
async def add_shopping_item(user_id: str, body: ShoppingAdd, current_user: dict = Depends(get_current_user)):
    try:
        _require_own(user_id, current_user)
        db = get_db()
        doc_ref = db.collection("shopping_lists").document()
        doc_ref.set({
            "userId": current_user["uid"],
            "name": body.name,
            "amount": body.amount or "",
            "done": False,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
        return {"success": True, "data": {"id": doc_ref.id, "name": body.name, "amount": body.amount or "", "done": False}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/{item_id}")
async def update_shopping_item(item_id: str, body: ShoppingUpdate, current_user: dict = Depends(get_current_user)):
    try:
        _get_owned_item(item_id, current_user)
        db = get_db()
        update_data = {k: v for k, v in body.model_dump().items() if v is not None}
        if update_data:
            db.collection("shopping_lists").document(item_id).update(update_data)
        return {"success": True, "message": "Item updated"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{item_id}")
async def delete_shopping_item(item_id: str, current_user: dict = Depends(get_current_user)):
    try:
        _get_owned_item(item_id, current_user)
        db = get_db()
        db.collection("shopping_lists").document(item_id).delete()
        return {"success": True, "message": "Item deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))