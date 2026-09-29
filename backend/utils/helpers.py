from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from services.siglip_detector import SigLIPDetector
from services.nutrition_mapper import NutritionMapper
from services.gemini_service import (
    generate_nutrition_plan as _gemini_generate_plan,
    generate_chat_reply as _gemini_chat,
    get_mood_recommendations as _gemini_mood,
    generate_budget_plan as _gemini_budget,
)

UNKNOWN = "unknown"

# ====== FIRESTORE QUERY HELPER ======

def stream_sorted(query, order_field: str = "timestamp", desc: bool = True, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Fetch a Firestore query WITHOUT requiring a composite index.

    Firestore demands a composite index for `where(x) + order_by(y)`. To avoid
    forcing the user to create indexes manually, we stream the filtered result
    (single-field index only) and sort in application code.

    Returns a list of dicts with the document id added as 'id' (if not present)
    and the order_field converted to ISO string when it is a datetime.
    """
    try:
        docs = list(query.stream())
    except Exception as e:
        print(f"[stream_sorted] query failed: {e}")
        return []

    def _sort_key(doc):
        data = doc.to_dict() or {}
        ts = data.get(order_field)
        if isinstance(ts, datetime):
            return ts
        return datetime.min.replace(tzinfo=timezone.utc)

    docs.sort(key=_sort_key, reverse=desc)
    if limit is not None:
        docs = docs[:limit]

    results = []
    for doc in docs:
        data = doc.to_dict()
        if data is None:
            continue
        data.setdefault("id", doc.id)
        ts = data.get(order_field)
        if isinstance(ts, datetime):
            data[order_field] = ts.isoformat()
        results.append(data)
    return results


# ====== GENERIC UTILITIES ======

def generate_health_score(calories: float, protein: float, fat: float, carbs: float, fiber: float = 0, sugar: float = 0, sodium: float = 0) -> int:
    score = 100
    if calories > 500: score -= 15
    if calories > 800: score -= 20
    if fat > 30: score -= 10
    if fat > 50: score -= 15
    if sugar > 25: score -= 15
    if sugar > 40: score -= 10
    if sodium > 1000: score -= 10
    if sodium > 2000: score -= 10
    if fiber > 5: score += 10
    if protein > 20: score += 5
    if carbs > 70: score -= 10
    return max(0, min(100, score))

def calculate_bmi(weight_kg: float, height_cm: float) -> float:
    if height_cm <= 0: return 0
    height_m = height_cm / 100
    return round(weight_kg / (height_m ** 2), 1)

def get_bmi_category(bmi: float) -> str:
    if bmi < 18.5: return "Underweight"
    if bmi < 25: return "Normal"
    if bmi < 30: return "Overweight"
    return "Obese"

def calculate_calories_burned(weight_kg: float, activity: str, duration_minutes: int) -> float:
    mets = {
        "sedentary": 1.3, "light": 3.0, "moderate": 5.0,
        "vigorous": 7.0, "running": 9.0, "cycling": 6.0,
        "swimming": 7.0, "walking": 3.5, "yoga": 2.5, "gym": 5.5
    }
    met = mets.get(activity.lower(), 3.0)
    return round(met * 3.5 * weight_kg / 200 * duration_minutes, 1)

# ====== SIGLIP-POWERED IMAGE ANALYSIS ======

def _aggregate_nutrition(detected_foods: List[Dict[str, Any]]) -> Dict[str, Any]:
    cal = 0.0
    pro = 0.0
    fat = 0.0
    carb = 0.0
    fib = 0.0
    sug = 0.0
    sod = 0.0
    known_count = 0
    for f in detected_foods:
        nut = f.get("nutrition", {})
        if nut.get("calories") != UNKNOWN:
            cal += float(nut["calories"])
            pro += float(nut.get("protein", 0))
            fat += float(nut.get("fat", 0))
            carb += float(nut.get("carbs", 0))
            fib += float(nut.get("fiber", 0))
            sug += float(nut.get("sugar", 0))
            sod += float(nut.get("sodium", 0))
            known_count += 1
    if known_count == 0:
        return {"calories": UNKNOWN, "protein": UNKNOWN, "fat": UNKNOWN, "carbs": UNKNOWN, "fiber": UNKNOWN, "sugar": UNKNOWN, "sodium": UNKNOWN}
    hs = generate_health_score(cal, pro, fat, carb, fib, sug, sod)
    return {"calories": round(cal, 1), "protein": round(pro, 1), "fat": round(fat, 1), "carbs": round(carb, 1), "fiber": round(fib, 1), "sugar": round(sug, 1), "sodium": round(sod, 1), "healthScore": hs}


def analyze_meal_image(image_bytes: bytes) -> Dict[str, Any]:
    detect_result = SigLIPDetector.detect(image_bytes)

    if detect_result["status"] in ("model_not_found", "no_transformers"):
        return {"status": "error", "message": detect_result["message"], "detections": [], "foodName": UNKNOWN}

    if detect_result["status"] == "error":
        return {"status": "error", "message": detect_result["message"], "detections": [], "foodName": UNKNOWN}

    food_detections = detect_result.get("foodDetections", [])
    if not food_detections:
        return {
            "status": "no_detection",
            "message": "No food detected in the image. Try a different angle or closer shot.",
            "detections": [],
            "totalDetected": detect_result.get("totalDetected", 0),
            "topPredictions": detect_result.get("topPredictions", []),
            "foodName": UNKNOWN,
            "calories": UNKNOWN,
            "protein": UNKNOWN,
            "fat": UNKNOWN,
            "carbs": UNKNOWN,
            "fiber": UNKNOWN,
            "sugar": UNKNOWN,
            "sodium": UNKNOWN,
            "healthScore": 0,
            "healthyTips": [],
            "betterAlternatives": [],
        }

    enriched = []
    for d in food_detections:
        nutrition = NutritionMapper.get_nutrition_for_class(d["className"])
        enriched.append({
            "className": d["className"],
            "confidence": d["confidence"],
            "isFood": d["isFood"],
            "nutrition": nutrition,
        })

    food_names = [e["className"] for e in enriched]
    nutrition = _aggregate_nutrition(enriched)

    return {
        "status": "success",
        "foodName": ", ".join(food_names),
        "detections": enriched,
        "totalDetected": detect_result.get("totalDetected", 0),
        "totalFoodItems": detect_result.get("totalFoodItems", len(food_detections)),
        "topPredictions": detect_result.get("topPredictions", []),
        **nutrition,
        "healthyTips": ["Based on what we detected, add more colorful vegetables for variety."] if nutrition.get("calories") != UNKNOWN else [],
        "betterAlternatives": ["Try pairing with a side of greens or extra vegetables."] if nutrition.get("calories") != UNKNOWN else [],
    }


def _analyze_image_common(image_bytes: bytes) -> Dict[str, Any]:
    detect_result = SigLIPDetector.detect(image_bytes)
    if detect_result["status"] in ("model_not_found", "no_transformers"):
        return {"status": "error", "message": detect_result["message"]}
    return detect_result


def analyze_menu_image(image_bytes: bytes) -> Dict[str, Any]:
    detect_result = SigLIPDetector.detect(image_bytes)
    if detect_result["status"] != "success":
        return {"restaurantName": UNKNOWN, "items": [], "healthiestOptions": [], "suggestions": ["Could not analyze the menu image."]}
    food_detections = detect_result.get("foodDetections", [])
    items = []
    for d in food_detections:
        nutrition = NutritionMapper.get_nutrition_for_class(d["className"])
        hs = nutrition.get("healthScore", 50) if nutrition.get("calories") != UNKNOWN else 50
        items.append({"name": d["className"], "confidence": d["confidence"], "calories": nutrition.get("calories", UNKNOWN), "healthScore": hs})
    sorted_items = sorted(items, key=lambda x: x["calories"] if isinstance(x["calories"], (int, float)) else 9999)
    return {
        "restaurantName": "Detected Foods",
        "items": items,
        "healthiestOptions": [i["name"] for i in sorted_items[:3]] if sorted_items else [],
        "suggestions": ["Choose grilled over fried options", "Ask for dressings on the side", "Opt for water or unsweetened beverages"],
    }


def analyze_pantry_image(image_bytes: bytes) -> Dict[str, Any]:
    detect_result = SigLIPDetector.detect(image_bytes)
    if detect_result["status"] != "success":
        return {"ingredients": [], "recipes": []}
    ingredients = list(set(d["className"] for d in detect_result.get("foodDetections", [])))
    if not ingredients:
        return {"ingredients": [], "recipes": []}
    return {
        "ingredients": ingredients,
        "recipes": [{"name": f"{' & '.join(ingredients[:3])} Meal", "ingredients": ingredients, "instructions": f"1. Prepare all {', '.join(ingredients)}.\n2. Cook and combine.\n3. Season to taste and serve.", "prepTime": "Varies", "difficulty": "Easy"}],
    }


def analyze_basket_image(image_bytes: bytes) -> Dict[str, Any]:
    detect_result = SigLIPDetector.detect(image_bytes)
    if detect_result["status"] != "success":
        return {"healthyItems": [], "unhealthyItems": [], "healthScore": 0, "suggestions": []}
    detections = detect_result.get("detections", [])
    healthy = []
    unhealthy = []
    for d in detections:
        if d.get("isFood"):
            nutrition = NutritionMapper.get_nutrition_for_class(d["className"])
            if nutrition.get("calories") != UNKNOWN:
                cal = float(nutrition["calories"])
                sugar = float(nutrition.get("sugar", 0))
                fat = float(nutrition.get("fat", 0))
                if cal < 150 and sugar < 10 and fat < 5:
                    healthy.append(d["className"])
                else:
                    unhealthy.append(d["className"])
            else:
                healthy.append(d["className"])
    total = len(healthy) + len(unhealthy)
    hs = int((len(healthy) / total) * 100) if total > 0 else 50
    return {"healthyItems": healthy, "unhealthyItems": unhealthy, "healthScore": hs, "suggestions": ["Add more leafy greens to your basket", "Choose whole grain options", "Include more colorful vegetables"]}


def analyze_product_image(image_bytes: bytes) -> Dict[str, Any]:
    detect_result = SigLIPDetector.detect(image_bytes)
    if detect_result["status"] != "success":
        return {"productName": UNKNOWN, "ingredients": [], "nutritionFacts": {}, "warnings": [], "healthScore": 0}
    food_detections = detect_result.get("foodDetections", [])
    if not food_detections:
        return {"productName": UNKNOWN, "ingredients": [], "nutritionFacts": {}, "warnings": ["No food product detected"], "healthScore": 0}
    d = food_detections[0]
    nutrition = NutritionMapper.get_nutrition_for_class(d["className"])
    nf = {}
    hs = 0
    if nutrition.get("calories") != UNKNOWN:
        nf = {"calories": float(nutrition["calories"]), "protein": float(nutrition.get("protein", 0)), "carbs": float(nutrition.get("carbs", 0)), "fat": float(nutrition.get("fat", 0)), "sugar": float(nutrition.get("sugar", 0)), "fiber": float(nutrition.get("fiber", 0)), "sodium": float(nutrition.get("sodium", 0))}
        hs = int(nutrition.get("healthScore", 50))
    return {"productName": d["className"], "ingredients": [d["className"]], "nutritionFacts": nf, "warnings": [], "healthScore": hs}


# ====== GEMINI-POWERED FUNCTIONS ======

def generate_nutrition_plan(calorie_target: float, preferences: List[str] = None) -> Dict[str, Any]:
    try:
        return _gemini_generate_plan(calorie_target, preferences)
    except Exception as e:
        print(f"Gemini plan generation failed: {e}")
    return {"breakfast": [], "lunch": [], "dinner": [], "snacks": [], "totalCalories": 0, "waterGoal": 2000, "protein": 0, "carbs": 0, "fat": 0, "tips": ["AI nutrition plan is currently unavailable. Check that GEMINI_API_KEY is set in your environment."]}

def generate_chat_reply(message: str) -> str:
    try:
        return _gemini_chat(message)
    except Exception as e:
        print(f"Gemini chat failed: {e}")
    return "I'm your nutrition assistant. The AI model is not connected right now. Please check your Gemini API configuration."

def get_mood_recommendations(mood: str) -> List[Dict[str, Any]]:
    try:
        return _gemini_mood(mood)
    except Exception as e:
        print(f"Gemini mood recommendations failed: {e}")
    return []

def generate_budget_plan(budget: float) -> Dict[str, Any]:
    try:
        return _gemini_budget(budget)
    except Exception as e:
        print(f"Gemini budget plan failed: {e}")
    return {"budget": budget, "dailyPlan": {}, "dailyCost": 0, "weeklyCost": 0, "daysCovered": 0, "groceryList": [], "savingsTips": []}
