from fastapi import APIRouter, Depends, HTTPException, Query
from services.nutrition_service import NutritionService
from utils.auth import get_current_user
from typing import Optional
import traceback

router = APIRouter(prefix="/nutrition", tags=["Nutrition"])


def _verified_uid(userId: Optional[str], current_user: dict) -> str:
    """Return the authenticated UID; reject a mismatched client-supplied one."""
    uid = current_user["uid"]
    if userId and userId != uid:
        raise HTTPException(status_code=403, detail="Cannot access another user's data")
    return uid


def _require_own(user_id: str, current_user: dict) -> str:
    """Return the user_id only for the authenticated user's own data."""
    if user_id != current_user["uid"]:
        raise HTTPException(status_code=404, detail="Data not found")
    return user_id


@router.get("/dashboard/{user_id}")
async def get_dashboard(user_id: str, current_user: dict = Depends(get_current_user)):
    try:
        _require_own(user_id, current_user)
        result = NutritionService.get_dashboard_stats(user_id)
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        err_msg = str(e)
        if "index" in err_msg.lower() and "requires" in err_msg.lower():
            detail = "Firestore index missing. Please create the required composite index for the query."
        else:
            detail = err_msg
        raise HTTPException(status_code=500, detail=detail)

@router.get("/progress/{user_id}")
async def get_progress(user_id: str, current_user: dict = Depends(get_current_user)):
    try:
        _require_own(user_id, current_user)
        result = NutritionService.get_progress_data(user_id)
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        err_msg = str(e)
        if "index" in err_msg.lower() and "requires" in err_msg.lower():
            detail = "Firestore index missing. Please create the required composite index for the query."
        else:
            detail = err_msg
        raise HTTPException(status_code=500, detail=detail)

@router.post("/plan")
async def generate_plan(userId: Optional[str] = None, calorieTarget: float = 2000, preferences: str = "", current_user: dict = Depends(get_current_user)):
    try:
        uid = _verified_uid(userId, current_user)
        prefs = [p.strip() for p in preferences.split(",") if p.strip()] if preferences else []
        result = NutritionService.generate_plan(uid, calorieTarget, prefs)
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/water")
async def log_water(userId: Optional[str] = None, amount: int = 250, current_user: dict = Depends(get_current_user)):
    try:
        uid = _verified_uid(userId, current_user)
        result = NutritionService.log_water(uid, amount)
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/water/{user_id}")
async def get_water(user_id: str, days: int = 7, current_user: dict = Depends(get_current_user)):
    try:
        _require_own(user_id, current_user)
        result = NutritionService.get_water_intake(user_id, days)
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/weight")
async def log_weight(userId: Optional[str] = None, weight: float = 0, date: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    try:
        uid = _verified_uid(userId, current_user)
        result = NutritionService.log_weight(uid, weight, date)
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/weight/{user_id}")
async def get_weight(user_id: str, days: int = 30, current_user: dict = Depends(get_current_user)):
    try:
        _require_own(user_id, current_user)
        result = NutritionService.get_weight_history(user_id, days)
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/report/{user_id}")
async def get_report(user_id: str, period: str = "weekly", current_user: dict = Depends(get_current_user)):
    try:
        _require_own(user_id, current_user)
        # Read-only: compute and return the report. Reports are no longer
        # written to Firestore from a GET request.
        result = NutritionService.generate_report(user_id, period, store=False)
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/mood")
async def get_mood_meals(mood: str = "happy"):
    try:
        result = NutritionService.get_mood_recommendations(mood)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/budget-plan")
async def get_budget_plan(budget: float):
    try:
        result = NutritionService.generate_budget_plan(budget)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))