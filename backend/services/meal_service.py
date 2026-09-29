import traceback
from firebase_admin import firestore
from google.cloud.firestore import FieldFilter
from fastapi import HTTPException
from firebase.config import get_db, get_bucket
from utils.helpers import analyze_meal_image, analyze_menu_image, analyze_pantry_image, analyze_basket_image, analyze_product_image
from typing import Dict, Any, List
import uuid
from datetime import datetime, timezone

class MealService:
    @staticmethod
    def analyze_meal(user_id: str, image_bytes: bytes, filename: str) -> Dict[str, Any]:
        result = analyze_meal_image(image_bytes)
        result["userId"] = user_id

        db = get_db()
        meal_ref = db.collection("meal_history").document()
        doc_data = {
            "userId": user_id,
            "foodName": result.get("foodName", "unknown"),
            "calories": result.get("calories", 0) if isinstance(result.get("calories"), (int, float)) else 0,
            "protein": result.get("protein", 0) if isinstance(result.get("protein"), (int, float)) else 0,
            "fat": result.get("fat", 0) if isinstance(result.get("fat"), (int, float)) else 0,
            "carbs": result.get("carbs", 0) if isinstance(result.get("carbs"), (int, float)) else 0,
            "fiber": result.get("fiber", 0) if isinstance(result.get("fiber"), (int, float)) else 0,
            "sugar": result.get("sugar", 0) if isinstance(result.get("sugar"), (int, float)) else 0,
            "sodium": result.get("sodium", 0) if isinstance(result.get("sodium"), (int, float)) else 0,
            "healthScore": result.get("healthScore", 0),
            "status": result.get("status", "success"),
            "timestamp": firestore.SERVER_TIMESTAMP,
            "imageUrl": None,
        }
        meal_ref.set(doc_data)

        result["mealId"] = meal_ref.id
        return result

    @staticmethod
    def analyze_menu(user_id: str, image_bytes: bytes, filename: str) -> Dict[str, Any]:
        return analyze_menu_image(image_bytes)

    @staticmethod
    def analyze_pantry(user_id: str, image_bytes: bytes, filename: str) -> Dict[str, Any]:
        return analyze_pantry_image(image_bytes)

    @staticmethod
    def analyze_basket(user_id: str, image_bytes: bytes, filename: str) -> Dict[str, Any]:
        return analyze_basket_image(image_bytes)

    @staticmethod
    def analyze_product(user_id: str, image_bytes: bytes, filename: str) -> Dict[str, Any]:
        return analyze_product_image(image_bytes)

    @staticmethod
    def get_history(user_id: str, search: str = "", filter_type: str = "", page: int = 1, limit: int = 20) -> Dict[str, Any]:
        db = get_db()
        try:
            query = db.collection("meal_history").where(filter=FieldFilter("userId", "==", user_id))
            docs = list(query.stream())

            def _sort_key(doc):
                data = doc.to_dict()
                if not data:
                    return datetime.min.replace(tzinfo=timezone.utc)
                ts = data.get("timestamp")
                if isinstance(ts, datetime):
                    return ts
                return datetime.min.replace(tzinfo=timezone.utc)

            docs.sort(key=_sort_key, reverse=True)

            start = (page - 1) * limit
            page_docs = docs[start:start + limit]

        except Exception as e:
            tb = traceback.format_exc()
            print(f"[MealService] Firestore query failed: {e}\n{tb}")
            return {"items": [], "page": page, "limit": limit, "total": 0}

        items = []
        for doc in page_docs:
            try:
                data = doc.to_dict()
                if not data:
                    continue
                data["id"] = doc.id
                ts = data.get("timestamp")
                if ts and hasattr(ts, "isoformat"):
                    data["timestamp"] = ts.isoformat()
                if search and search.lower() not in (data.get("foodName") or "").lower():
                    continue
                items.append(data)
            except Exception as e:
                tb = traceback.format_exc()
                print(f"[MealService] Skipping malformed meal doc {doc.id}: {e}\n{tb}")
                continue
        return {"items": items, "page": page, "limit": limit, "total": len(items)}

    @staticmethod
    def delete_history(meal_id: str) -> bool:
        db = get_db()
        db.collection("meal_history").document(meal_id).delete()
        return True

    @staticmethod
    def delete_history_owned(meal_id: str, owner_uid: str) -> bool:
        """Delete a meal only if it belongs to the authenticated user."""
        db = get_db()
        doc = db.collection("meal_history").document(meal_id).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Meal not found")
        if (doc.to_dict() or {}).get("userId") != owner_uid:
            raise HTTPException(status_code=403, detail="Cannot delete another user's meal")
        doc.reference.delete()
        return True

    @staticmethod
    def get_healthy_alternatives() -> List[Dict[str, Any]]:
        return [
            {"name": "Quinoa Bowl", "calories": 220, "benefits": ["Complete protein", "High fiber", "Gluten-free"], "image": "https://images.unsplash.com/photo-1505576399279-565b52d4ac71?w=400"},
            {"name": "Grilled Salmon", "calories": 280, "benefits": ["Omega-3 fatty acids", "High protein", "Vitamin D"], "image": "https://images.unsplash.com/photo-1467003909585-2f8a72700288?w=400"},
            {"name": "Sweet Potato", "calories": 130, "benefits": ["Rich in Vitamin A", "High fiber", "Antioxidants"], "image": "https://images.unsplash.com/photo-1596097635121-14b63b7a0c19?w=400"},
            {"name": "Avocado Toast", "calories": 195, "benefits": ["Healthy fats", "Fiber", "Potassium"], "image": "https://images.unsplash.com/photo-1541519227354-08fa5d50c44d?w=400"},
            {"name": "Greek Yogurt", "calories": 100, "benefits": ["Probiotics", "High protein", "Calcium"], "image": "https://images.unsplash.com/photo-1488477181946-6428a0291777?w=400"},
            {"name": "Mixed Berries", "calories": 85, "benefits": ["Antioxidants", "Vitamin C", "Low calorie"], "image": "https://images.unsplash.com/photo-1563746098251-d35aef196e83?w=400"},
            {"name": "Almonds", "calories": 160, "benefits": ["Healthy fats", "Vitamin E", "Magnesium"], "image": "https://images.unsplash.com/photo-1508061253366-f7da1583aebc?w=400"},
            {"name": "Green Tea", "calories": 0, "benefits": ["Antioxidants", "Caffeine", "May boost metabolism"], "image": "https://images.unsplash.com/photo-1556679343-c7306c1976bc?w=400"},
        ]

    @staticmethod
    def get_healthy_swaps() -> List[Dict[str, Any]]:
        return [
            {"original": "White Rice", "alternative": "Cauliflower Rice", "caloriesSaved": 150, "benefits": ["Low carb", "More vitamins", "Lower calories"]},
            {"original": "White Bread", "alternative": "Whole Grain Bread", "caloriesSaved": 20, "benefits": ["More fiber", "More nutrients", "Lower GI"]},
            {"original": "Potato Chips", "alternative": "Kale Chips", "caloriesSaved": 100, "benefits": ["More vitamins", "Less fat", "More fiber"]},
            {"original": "Soda", "alternative": "Sparkling Water with Lemon", "caloriesSaved": 150, "benefits": ["Zero sugar", "Hydrating", "No artificial sweeteners"]},
            {"original": "Ice Cream", "alternative": "Frozen Greek Yogurt", "caloriesSaved": 120, "benefits": ["More protein", "Probiotics", "Less sugar"]},
            {"original": "Mayonnaise", "alternative": "Greek Yogurt or Avocado", "caloriesSaved": 80, "benefits": ["More protein", "Healthy fats", "Less processed"]},
            {"original": "Pasta", "alternative": "Zucchini Noodles", "caloriesSaved": 180, "benefits": ["Low carb", "More vitamins", "Gluten-free"]},
            {"original": "Candy Bar", "alternative": "Dark Chocolate (70%+)", "caloriesSaved": 50, "benefits": ["Antioxidants", "Less sugar", "More satisfying"]},
            {"original": "Sour Cream", "alternative": "Plain Greek Yogurt", "caloriesSaved": 60, "benefits": ["More protein", "Probiotics", "Less fat"]},
            {"original": "Beef Burger", "alternative": "Turkey or Black Bean Burger", "caloriesSaved": 120, "benefits": ["Less saturated fat", "More fiber", "Lower calories"]},
        ]
