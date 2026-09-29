import random
import string
import logging
import traceback
from datetime import datetime, timedelta, date
from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import firestore
from google.cloud.firestore import FieldFilter
from firebase.config import get_db
from models.schemas import DoctorNoteRequest
from utils.helpers import stream_sorted
from utils.auth import get_current_user, require_doctor, is_doctor
from pydantic import BaseModel

class RegisterRequest(BaseModel):
    email: str = None
    displayName: str = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("users_router")

router = APIRouter(prefix="/api/users", tags=["Users"])


def _get_db_safe():
    db = get_db()
    if db is None:
        logger.error("Firestore database not initialized — check Firebase credentials")
        raise HTTPException(status_code=503, detail="Database not available. Check server configuration.")
    return db


def _generate_display_id() -> str:
    return "USR-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))


def _resolve_user(user_id: str):
    """Resolve a Firebase UID or display_id to (doc_id, data) or None."""
    db = _get_db_safe()
    doc = db.collection("users").document(user_id).get()
    if doc.exists:
        return user_id, doc.to_dict()
    results = db.collection("users").where(filter=FieldFilter("display_id", "==", user_id)).limit(1).stream()
    for r in results:
        return r.id, r.to_dict()
    return None


def _require_own_or_doctor(user_id: str, current_user: dict):
    """Access control for user-scoped reads.

    - The authenticated user's own profile (by UID or display_id) is allowed.
    - Doctors may look up any user (existing patient/doctor workflow).
    - Everyone else gets a 404 so existence is not leaked.
    Returns (doc_id, data) of the resolved user.
    """
    if user_id == current_user["uid"]:
        doc = _get_db_safe().collection("users").document(user_id).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="User not found")
        return user_id, doc.to_dict()

    resolved = _resolve_user(user_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="User not found")
    doc_id, data = resolved
    if doc_id == current_user["uid"]:
        return doc_id, data
    if is_doctor(current_user["uid"]):
        return doc_id, data
    raise HTTPException(status_code=404, detail="User not found")


@router.post("/register")
async def register_user(body: RegisterRequest = None, current_user: dict = Depends(get_current_user)):
    """Bootstrap a profile document for the authenticated user.

    The document is created ONLY at the verified Firebase UID from the ID
    token. Arbitrary fake UIDs are never accepted; for the normal signup flow
    use POST /auth/signup.
    """
    try:
        db = _get_db_safe()
        uid = current_user["uid"]

        existing = db.collection("users").document(uid).get()
        if existing.exists:
            return {"success": True, "data": {"uid": uid, "already_exists": True}}

        display_id = _generate_display_id()
        while db.collection("users").where(filter=FieldFilter("display_id", "==", display_id)).limit(1).get():
            display_id = _generate_display_id()

        email = body.email if body and body.email else None
        displayName = body.displayName if body and body.displayName else None

        user_data = {
            "uid": uid,
            "display_id": display_id,
            "email": email or f"{display_id}@local.user",
            "displayName": displayName or f"User {display_id}",
            "accountType": "user",
            "emailVerified": True,
            "onboardingCompleted": False,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "age": None,
            "weight": None,
            "height": None,
            "goal": "maintain",
            "dietaryPreferences": [],
            "allergies": [],
        }
        db.collection("users").document(uid).set(user_data)
        logger.info("Bootstrapped profile for authenticated user %s", uid)
        return {"success": True, "data": {"uid": uid, "display_id": display_id, **user_data}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Register user failed: %s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")


@router.get("/{user_id}")
async def get_user(user_id: str, current_user: dict = Depends(get_current_user)):
    try:
        doc_id, user = _require_own_or_doctor(user_id, current_user)
        logger.info("User lookup by %s resolved to %s", user_id, doc_id)
        return {"success": True, "data": user}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_user failed for %s: %s\n%s", user_id, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error looking up user: {str(e)}")


@router.get("/{user_id}/progress")
async def get_user_progress(user_id: str, current_user: dict = Depends(get_current_user)):
    try:
        _, user = _require_own_or_doctor(user_id, current_user)
        db = _get_db_safe()
        logger.info("Fetching progress for user: %s", user_id)

        today = date.today().isoformat()
        now_dt = datetime.combine(date.today(), datetime.min.time())

        meals = []
        try:
            meals = stream_sorted(
                db.collection("meal_history").where(filter=FieldFilter("userId", "==", user["uid"])),
                order_field="timestamp", desc=True, limit=10,
            )
        except Exception as e:
            logger.warning("Failed to fetch meal_history for %s: %s", user_id, e)
        recent_meals = list(meals)

        today_meals = []
        total_cal = 0
        total_prot = 0
        total_carbs = 0
        total_fat = 0
        try:
            # Single-field filter only; apply timestamp range in app code.
            raw = list(db.collection("meal_history").where(filter=FieldFilter("userId", "==", user["uid"])).stream())
            for m in raw:
                d = m.to_dict() or {}
                ts = d.get("timestamp")
                if isinstance(ts, datetime) and ts < now_dt:
                    continue
                today_meals.append(d)
            total_cal = sum(m.get("calories", 0) or 0 for m in today_meals)
            total_prot = sum(m.get("protein", 0) or 0 for m in today_meals)
            total_carbs = sum(m.get("carbs", 0) or 0 for m in today_meals)
            total_fat = sum(m.get("fat", 0) or 0 for m in today_meals)
        except Exception as e:
            logger.warning("Failed to fetch today's meals for %s: %s", user_id, e)

        total_water = 0
        try:
            # Single-field filter only; apply date match in app code.
            water_logs = list(db.collection("water_logs").where(filter=FieldFilter("userId", "==", user["uid"])).stream())
            total_water = sum((w.to_dict() or {}).get("amount", 0) for w in water_logs if (w.to_dict() or {}).get("date") == today)
        except Exception as e:
            logger.warning("Failed to fetch water_logs for %s: %s", user_id, e)

        weight_data = []
        try:
            weight_logs = stream_sorted(
                db.collection("weight_logs").where(filter=FieldFilter("userId", "==", user["uid"])),
                order_field="date", desc=True, limit=10,
            )
            weight_data = [{"date": w.get("date"), "weight": w.get("weight")} for w in weight_logs]
        except Exception as e:
            logger.warning("Failed to fetch weight_logs for %s: %s", user_id, e)

        weight_val = user.get("weight", 70)
        height_val = user.get("height", 170)
        bmi = round(weight_val / ((height_val / 100) ** 2), 1) if height_val else 0

        nutrition_stats = {
            "totalCalories": round(total_cal, 1),
            "totalProtein": round(total_prot, 1),
            "totalCarbs": round(total_carbs, 1),
            "totalFat": round(total_fat, 1),
            "waterIntake": total_water,
            "bmi": bmi,
            "bmiCategory": "Underweight" if bmi < 18.5 else "Normal" if bmi < 25 else "Overweight" if bmi < 30 else "Obese",
            "weight": weight_val,
            "height": height_val,
            "goal": user.get("goal", "maintain"),
        }

        return {
            "success": True,
            "data": {
                "profile": user,
                "recentMeals": recent_meals,
                "nutritionStats": nutrition_stats,
                "weightHistory": weight_data,
                "waterIntake": {"today": total_water, "date": today},
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_user_progress failed for %s: %s\n%s", user_id, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error fetching progress: {str(e)}")


@router.get("/{user_id}/history")
async def get_user_history(user_id: str, current_user: dict = Depends(get_current_user)):
    try:
        _, user = _require_own_or_doctor(user_id, current_user)
        db = _get_db_safe()
        logger.info("Fetching history for user: %s", user_id)
        uid = user["uid"]

        meal_data = []
        try:
            meal_data = stream_sorted(
                db.collection("meal_history").where(filter=FieldFilter("userId", "==", uid)),
                order_field="timestamp", desc=True, limit=50,
            )
        except Exception as e:
            logger.warning("Failed to fetch meal_history for %s: %s", user_id, e)

        weight_data = []
        try:
            weight_logs = stream_sorted(
                db.collection("weight_logs").where(filter=FieldFilter("userId", "==", uid)),
                order_field="date", desc=True, limit=50,
            )
            weight_data = [{"date": w.get("date"), "weight": w.get("weight")} for w in weight_logs]
        except Exception as e:
            logger.warning("Failed to fetch weight_logs for %s: %s", user_id, e)

        water_data = []
        try:
            water_data = stream_sorted(
                db.collection("water_logs").where(filter=FieldFilter("userId", "==", uid)),
                order_field="timestamp", desc=True, limit=50,
            )
        except Exception as e:
            logger.warning("Failed to fetch water_logs for %s: %s", user_id, e)

        chat_data = []
        try:
            chat_data = stream_sorted(
                db.collection("ai_history").where(filter=FieldFilter("userId", "==", uid)),
                order_field="timestamp", desc=True, limit=50,
            )
        except Exception as e:
            logger.warning("Failed to fetch ai_history for %s: %s", user_id, e)

        note_data = []
        try:
            note_data = stream_sorted(
                db.collection("doctor_notes").where(filter=FieldFilter("userId", "==", uid)),
                order_field="createdAt", desc=True, limit=20,
            )
        except Exception as e:
            logger.warning("Failed to fetch doctor_notes for %s: %s", user_id, e)

        return {
            "success": True,
            "data": {
                "meals": meal_data,
                "weightHistory": weight_data,
                "waterIntake": water_data,
                "aiChatHistory": chat_data,
                "doctorNotes": note_data,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_user_history failed for %s: %s\n%s", user_id, e, traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error fetching history: {str(e)}")


@router.get("/{user_id}/ai-history")
async def get_ai_history(user_id: str, limit: int = 30, current_user: dict = Depends(get_current_user)):
    try:
        _, user = _require_own_or_doctor(user_id, current_user)
        db = _get_db_safe()
        results = stream_sorted(
            db.collection("ai_history").where(filter=FieldFilter("userId", "==", user["uid"])),
            order_field="timestamp", desc=True, limit=limit,
        )
        return {"success": True, "data": results}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_ai_history failed for %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail=f"Error fetching AI history: {str(e)}")


@router.post("/{user_id}/doctor-notes")
async def add_doctor_note(user_id: str, note: DoctorNoteRequest, current_user: dict = Depends(require_doctor)):
    """Create a doctor note for a patient.

    Only authenticated users with the doctor role may create notes. The
    doctorId is always taken from the verified ID token — a client-supplied
    doctorId never grants doctor privileges.
    """
    try:
        _, patient = _require_own_or_doctor(user_id, current_user)
        db = _get_db_safe()
        doc_ref = db.collection("doctor_notes").document()
        doc_ref.set({
            "id": doc_ref.id,
            "userId": patient["uid"],
            "doctorId": current_user["uid"],
            "note": note.note,
            "diagnosis": note.diagnosis,
            "prescription": note.prescription,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
        return {"success": True, "data": {"id": doc_ref.id, "userId": patient["uid"]}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("add_doctor_note failed for %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail=f"Error adding doctor note: {str(e)}")


@router.get("/{user_id}/doctor-notes")
async def get_doctor_notes(user_id: str, current_user: dict = Depends(get_current_user)):
    try:
        _, patient = _require_own_or_doctor(user_id, current_user)
        db = _get_db_safe()
        notes = stream_sorted(
            db.collection("doctor_notes").where(filter=FieldFilter("userId", "==", patient["uid"])),
            order_field="createdAt", desc=True, limit=20,
        )
        return {"success": True, "data": notes}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_doctor_notes failed for %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail=f"Error fetching doctor notes: {str(e)}")