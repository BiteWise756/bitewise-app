from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from services.meal_service import MealService
from utils.auth import get_current_user
from typing import Optional
import traceback

router = APIRouter(prefix="/meal", tags=["Meals"])


def _verified_uid(userId: Optional[str], current_user: dict) -> str:
    """Return the authenticated UID; reject a mismatched client-supplied one."""
    uid = current_user["uid"]
    if userId and userId != uid:
        raise HTTPException(status_code=403, detail="Cannot access another user's data")
    return uid


@router.post("/analyze")
async def analyze_meal(userId: Optional[str] = Form(None), image: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    try:
        contents = await image.read()
        uid = _verified_uid(userId, current_user)
        result = MealService.analyze_meal(uid, contents, image.filename or "image.jpg")
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/history")
async def get_history(userId: Optional[str] = None, search: str = "", page: int = 1, limit: int = 20, current_user: dict = Depends(get_current_user)):
    try:
        uid = _verified_uid(userId, current_user)
        result = MealService.get_history(uid, search, "", page, limit)
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[MealHistory] Unexpected error: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.delete("/history/{meal_id}")
async def delete_history(meal_id: str, current_user: dict = Depends(get_current_user)):
    try:
        MealService.delete_history_owned(meal_id, current_user["uid"])
        return {"success": True, "message": "Meal deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/alternatives")
async def get_alternatives():
    try:
        result = MealService.get_healthy_alternatives()
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/swaps")
async def get_swaps():
    try:
        result = MealService.get_healthy_swaps()
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/menu/analyze")
async def analyze_menu(userId: Optional[str] = Form(None), image: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    try:
        contents = await image.read()
        uid = _verified_uid(userId, current_user)
        result = MealService.analyze_menu(uid, contents, image.filename or "menu.jpg")
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/pantry/analyze")
async def analyze_pantry(userId: Optional[str] = Form(None), image: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    try:
        contents = await image.read()
        uid = _verified_uid(userId, current_user)
        result = MealService.analyze_pantry(uid, contents, image.filename or "pantry.jpg")
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/basket/analyze")
async def analyze_basket(userId: Optional[str] = Form(None), image: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    try:
        contents = await image.read()
        uid = _verified_uid(userId, current_user)
        result = MealService.analyze_basket(uid, contents, image.filename or "basket.jpg")
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/product/analyze")
async def analyze_product(userId: Optional[str] = Form(None), image: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    try:
        contents = await image.read()
        uid = _verified_uid(userId, current_user)
        result = MealService.analyze_product(uid, contents, image.filename or "product.jpg")
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))