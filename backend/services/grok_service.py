import os
import json
import base64
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv
from utils.helpers import generate_health_score

load_dotenv()

GROK_API_KEY = os.getenv("GROK_API_KEY")
GROK_MODEL = os.getenv("GROK_MODEL", "grok-beta")
GROK_VISION_MODEL = os.getenv("GROK_VISION_MODEL", "grok-vision-beta")
GROK_BASE_URL = "https://api.x.ai/v1"

_grok_client = None

def _get_client():
    global _grok_client
    if _grok_client is not None:
        return _grok_client
    if not GROK_API_KEY or GROK_API_KEY == "your-grok-api-key":
        print("WARNING: GROK_API_KEY not set. Using fallback mock data.")
        return None
    try:
        from openai import OpenAI
        _grok_client = OpenAI(api_key=GROK_API_KEY, base_url=GROK_BASE_URL)
        print(f"Grok client initialized (model: {GROK_MODEL})")
        return _grok_client
    except Exception as e:
        print(f"Grok initialization error: {e}")
        return None

def _is_available():
    return _get_client() is not None

def _image_to_data_url(image_bytes: bytes) -> str:
    if image_bytes[:4] == b"\x89PNG":
        mime = "image/png"
    elif image_bytes[:2] == b"\xFF\xD8":
        mime = "image/jpeg"
    elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"

def _safe_json_parse(text: str) -> Optional[Dict]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
            return None

SYSTEM_PROMPT_MEAL = """You are a professional nutritionist AI. Analyze the food image and return JSON only (no markdown):
{
  "foodName": "string",
  "calories": float,
  "protein": float,
  "fat": float,
  "carbs": float,
  "fiber": float,
  "sugar": float,
  "sodium": float,
  "healthScore": int (0-100),
  "healthyTips": ["string"],
  "betterAlternatives": ["string"]
}
Be accurate and specific. Estimate portion sizes visually."""

SYSTEM_PROMPT_MENU = """You are a menu analyst. Analyze the restaurant menu image and return JSON only:
{
  "restaurantName": "string or null",
  "items": [{"name": "string", "calories": int, "healthScore": int}],
  "healthiestOptions": ["string"],
  "suggestions": ["string"]
}
Rate health scores 0-100 for each menu item."""

SYSTEM_PROMPT_PANTRY = """You are a chef AI. Analyze the fridge/pantry image, detect ingredients, and return JSON only:
{
  "ingredients": ["string"],
  "recipes": [{"name": "string", "ingredients": ["string"], "instructions": "string", "prepTime": "string", "difficulty": "Easy|Medium|Hard"}]
}
Suggest 1-2 practical recipes using detected ingredients."""

SYSTEM_PROMPT_BASKET = """You are a grocery health analyst. Analyze the grocery basket image and return JSON only:
{
  "healthyItems": ["string"],
  "unhealthyItems": ["string"],
  "healthScore": int (0-100),
  "suggestions": ["string"]
}
Categorize visible food items and score the basket."""

SYSTEM_PROMPT_PRODUCT = """You are a food label analyst. Analyze the product package image and return JSON only:
{
  "productName": "string",
  "ingredients": ["string"],
  "nutritionFacts": {"calories": float, "protein": float, "carbs": float, "fat": float, "sugar": float, "fiber": float, "sodium": float},
  "warnings": ["string"],
  "healthScore": int (0-100)
}
Read the nutrition label carefully."""

SYSTEM_PROMPT_PLAN = """You are a nutritionist creating personalized meal plans. Return JSON only:
{
  "breakfast": {"name": "string", "calories": int, "protein": float, "carbs": float, "fat": float},
  "lunch": {"name": "string", "calories": int, "protein": float, "carbs": float, "fat": float},
  "dinner": {"name": "string", "calories": int, "protein": float, "carbs": float, "fat": float},
  "snacks": [{"name": "string", "calories": int, "protein": float, "carbs": float, "fat": float}],
  "totalCalories": float,
  "waterGoal": int,
  "protein": float,
  "carbs": float,
  "fat": float,
  "tips": ["string"]
}
Create varied, realistic meals. Total calories should be close to the target."""

SYSTEM_PROMPT_CHAT = """You are NutriLens AI, a friendly expert nutrition coach. Give concise, practical, science-based nutrition advice. Keep responses to 3-5 sentences. Be encouraging and specific."""

SYSTEM_PROMPT_MOOD = """You are a mood-food specialist. Recommend 4 specific meals for the given mood. Return JSON only:
{
  "mood": "string",
  "recommendations": [{"name": "string", "calories": int, "reason": "string", "mealType": "string"}]
}
Each meal should have a clear nutritional reason for helping that mood."""

SYSTEM_PROMPT_BUDGET = """You are a budget meal planner. Create a daily meal plan within budget. Return JSON only:
{
  "budget": float,
  "dailyPlan": {"breakfast": [{"name": "string", "cost": float, "calories": int, "ingredients": ["string"]}],
    "lunch": [{"name": "string", "cost": float, "calories": int, "ingredients": ["string"]}],
    "dinner": [{"name": "string", "cost": float, "calories": int, "ingredients": ["string"]}],
    "snacks": [{"name": "string", "cost": float, "calories": int, "ingredients": ["string"]}]},
  "dailyCost": float,
  "weeklyCost": float,
  "groceryList": ["string"],
  "savingsTips": ["string"]
}
Realistic prices. Budget in USD."""

def _call_grok(prompt: str, image_bytes: Optional[bytes] = None, system_prompt: Optional[str] = None) -> str:
    client = _get_client()
    if not client:
        return ""

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    if image_bytes:
        data_url = _image_to_data_url(image_bytes)
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        messages.append({"role": "user", "content": content})
        model = GROK_VISION_MODEL
    else:
        messages.append({"role": "user", "content": prompt})
        model = GROK_MODEL

    try:
        response = client.chat.completions.create(model=model, messages=messages)
        return response.choices[0].message.content or ""
    except Exception as e:
        print(f"Grok API call failed: {e}")
        return ""

# ====== IMAGE ANALYSIS FUNCTIONS ======
# Fall back to SigLIP-powered helpers when Grok is unavailable

def analyze_meal_image(image_bytes: bytes) -> Dict[str, Any]:
    if not _is_available():
        from utils.helpers import analyze_meal_image as _siglip_meal
        return _siglip_meal(image_bytes)

    result = _call_grok("Analyze this food image in detail. Identify all visible foods, estimate portions, and provide complete nutritional data.", image_bytes, SYSTEM_PROMPT_MEAL)
    parsed = _safe_json_parse(result)
    if parsed and "foodName" in parsed:
        parsed.setdefault("fiber", 0)
        parsed.setdefault("sugar", 0)
        parsed.setdefault("sodium", 0)
        parsed.setdefault("healthyTips", [])
        parsed.setdefault("betterAlternatives", [])
        return parsed

    from utils.helpers import analyze_meal_image as _siglip_meal
    return _siglip_meal(image_bytes)


def analyze_menu_image(image_bytes: bytes) -> Dict[str, Any]:
    if not _is_available():
        from utils.helpers import analyze_menu_image as _siglip_menu
        return _siglip_menu(image_bytes)

    result = _call_grok("Analyze this restaurant menu. Identify all menu items, estimate calories, and rate their healthiness.", image_bytes, SYSTEM_PROMPT_MENU)
    parsed = _safe_json_parse(result)
    if parsed and "items" in parsed:
        return parsed

    from utils.helpers import analyze_menu_image as _siglip_menu
    return _siglip_menu(image_bytes)


def analyze_pantry_image(image_bytes: bytes) -> Dict[str, Any]:
    if not _is_available():
        from utils.helpers import analyze_pantry_image as _siglip_pantry
        return _siglip_pantry(image_bytes)

    result = _call_grok("Detect all visible ingredients in this fridge or pantry. Then suggest practical recipes.", image_bytes, SYSTEM_PROMPT_PANTRY)
    parsed = _safe_json_parse(result)
    if parsed and "ingredients" in parsed:
        return parsed

    from utils.helpers import analyze_pantry_image as _siglip_pantry
    return _siglip_pantry(image_bytes)


def analyze_basket_image(image_bytes: bytes) -> Dict[str, Any]:
    if not _is_available():
        from utils.helpers import analyze_basket_image as _siglip_basket
        return _siglip_basket(image_bytes)

    result = _call_grok("Analyze this grocery basket. Identify healthy and unhealthy items and score the overall healthiness.", image_bytes, SYSTEM_PROMPT_BASKET)
    parsed = _safe_json_parse(result)
    if parsed and "healthScore" in parsed:
        return parsed

    from utils.helpers import analyze_basket_image as _siglip_basket
    return _siglip_basket(image_bytes)


def analyze_product_image(image_bytes: bytes) -> Dict[str, Any]:
    if not _is_available():
        from utils.helpers import analyze_product_image as _siglip_product
        return _siglip_product(image_bytes)

    result = _call_grok("Read and analyze this food product label. Identify ingredients, nutrition facts, and health warnings.", image_bytes, SYSTEM_PROMPT_PRODUCT)
    parsed = _safe_json_parse(result)
    if parsed and "productName" in parsed:
        return parsed

    from utils.helpers import analyze_product_image as _siglip_product
    return _siglip_product(image_bytes)


# ====== TEXT GENERATION FUNCTIONS ======

def generate_nutrition_plan(calorie_target: float, preferences: List[str] = None) -> Dict[str, Any]:
    if not _is_available():
        from utils.helpers import generate_nutrition_plan as _siglip_plan
        return _siglip_plan(calorie_target, preferences)

    prefs_str = f"Consider these preferences: {', '.join(preferences)}." if preferences else ""
    prompt = f"Create a daily meal plan with approximately {calorie_target} calories. {prefs_str} Include breakfast, lunch, dinner, and snacks with realistic nutritional values."
    result = _call_grok(prompt, system_prompt=SYSTEM_PROMPT_PLAN)
    parsed = _safe_json_parse(result)
    if parsed and "breakfast" in parsed:
        parsed.setdefault("tips", [])
        return parsed

    from utils.helpers import generate_nutrition_plan as _siglip_plan
    return _siglip_plan(calorie_target, preferences)


def generate_chat_reply(message: str) -> str:
    if not _is_available():
        from utils.helpers import generate_chat_reply as _siglip_chat
        return _siglip_chat(message)

    prompt = f"The user asks: {message}\n\nProvide a helpful, friendly, and science-based nutrition response."
    result = _call_grok(prompt, system_prompt=SYSTEM_PROMPT_CHAT)
    if result:
        return result.strip()

    from utils.helpers import generate_chat_reply as _siglip_chat
    return _siglip_chat(message)


def get_mood_recommendations(mood: str) -> List[Dict[str, Any]]:
    if not _is_available():
        from utils.helpers import get_mood_recommendations as _siglip_mood
        return _siglip_mood(mood)

    prompt = f"The user is feeling {mood}. Recommend 4 meals that would help improve or complement this mood."
    result = _call_grok(prompt, system_prompt=SYSTEM_PROMPT_MOOD)
    parsed = _safe_json_parse(result)
    if parsed and "recommendations" in parsed:
        return parsed["recommendations"]

    from utils.helpers import get_mood_recommendations as _siglip_mood
    return _siglip_mood(mood)


def generate_budget_plan(budget: float) -> Dict[str, Any]:
    if not _is_available():
        from utils.helpers import generate_budget_plan as _siglip_budget
        return _siglip_budget(budget)

    prompt = f"Create a healthy daily meal plan with a weekly grocery budget of ${budget}. Include breakfast, lunch, dinner, and snacks with realistic costs."
    result = _call_grok(prompt, system_prompt=SYSTEM_PROMPT_BUDGET)
    parsed = _safe_json_parse(result)
    if parsed and "dailyPlan" in parsed:
        return parsed

    from utils.helpers import generate_budget_plan as _siglip_budget
    return _siglip_budget(budget)
