from firebase_admin import firestore
from google.cloud.firestore import FieldFilter
from firebase.config import get_db
from utils.helpers import generate_nutrition_plan, calculate_bmi, calculate_calories_burned, get_mood_recommendations, generate_budget_plan, stream_sorted
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, date
import random

class NutritionService:
    @staticmethod
    def generate_plan(user_id: str, calorie_target: float = 2000, preferences: List[str] = None) -> Dict[str, Any]:
        plan_data = generate_nutrition_plan(calorie_target, preferences)
        today = date.today().isoformat()

        db = get_db()
        doc_ref = db.collection("nutrition_plans").document(f"{user_id}_{today}")
        doc_ref.set({
            "userId": user_id,
            "date": today,
            "meals": plan_data,
            "totalCalories": plan_data.get("totalCalories", sum(
                plan_data.get(m, {}).get("calories", 0) for m in ["breakfast", "lunch", "dinner"]
            ) + sum(s.get("calories", 0) for s in plan_data.get("snacks", []))),
            "waterGoal": plan_data.get("waterGoal", max(1500, int(calorie_target / 5))),
            "createdAt": firestore.SERVER_TIMESTAMP,
        })

        plan_data.setdefault("totalCalories", sum(
            plan_data.get(m, {}).get("calories", 0) for m in ["breakfast", "lunch", "dinner"]
        ) + sum(s.get("calories", 0) for s in plan_data.get("snacks", [])))
        plan_data.setdefault("waterGoal", max(1500, int(calorie_target / 5)))
        plan_data["date"] = today
        plan_data["id"] = doc_ref.id
        return plan_data

    @staticmethod
    def log_water(user_id: str, amount: int) -> Dict[str, Any]:
        db = get_db()
        today = date.today().isoformat()
        # Single-field filter only; apply date match in app code.
        logs_before = db.collection("water_logs").where(filter=FieldFilter("userId", "==", user_id)).stream()
        previous_total = sum(
            (log.to_dict() or {}).get("amount", 0)
            for log in logs_before
            if (log.to_dict() or {}).get("date") == today
        )
        log_ref = db.collection("water_logs").document()
        log_ref.set({
            "userId": user_id,
            "amount": amount,
            "date": today,
            "timestamp": firestore.SERVER_TIMESTAMP,
        })
        return {"total": previous_total + amount, "logged": amount, "date": today}

    @staticmethod
    def get_water_intake(user_id: str, days: int = 7) -> Dict[str, Any]:
        db = get_db()
        today = date.today()
        start_date = (today - timedelta(days=days)).isoformat()
        # Single-field filter only (no composite index); apply date range in app code.
        logs = db.collection("water_logs").where(filter=FieldFilter("userId", "==", user_id)).stream()
        daily_data = {}
        for log in logs:
            data = log.to_dict()
            if not data:
                continue
            d = data.get("date", today.isoformat())
            if d >= start_date:
                daily_data[d] = daily_data.get(d, 0) + (data.get("amount", 0) or 0)
        return {"daily": daily_data, "total_days": len(daily_data)}

    @staticmethod
    def log_weight(user_id: str, weight: float, log_date: str = None) -> Dict[str, Any]:
        db = get_db()
        if not log_date:
            log_date = date.today().isoformat()
        doc_ref = db.collection("weight_logs").document(f"{user_id}_{log_date}")
        doc_ref.set({
            "userId": user_id,
            "weight": weight,
            "date": log_date,
            "timestamp": firestore.SERVER_TIMESTAMP,
        })
        return {"weight": weight, "date": log_date}

    @staticmethod
    def get_weight_history(user_id: str, days: int = 30) -> Dict[str, Any]:
        db = get_db()
        today = date.today()
        start_date = (today - timedelta(days=days)).isoformat()
        # Single-field filter only (no composite index); apply date range + sort in app code.
        logs = db.collection("weight_logs").where(filter=FieldFilter("userId", "==", user_id)).stream()
        data = []
        for log in logs:
            d = log.to_dict()
            if d and "date" in d and "weight" in d and d["date"] >= start_date:
                data.append({"date": d["date"], "weight": d["weight"]})
        data.sort(key=lambda x: x["date"])
        return {"data": data, "total": len(data)}

    @staticmethod
    def get_dashboard_stats(user_id: str) -> Dict[str, Any]:
        db = get_db()
        user_doc = db.collection("users").document(user_id).get()
        user_data = user_doc.to_dict() if user_doc.exists else {}

        today = date.today().isoformat()
        total_water = 0
        try:
            # Single-field filter only; apply date match in app code.
            water_logs = db.collection("water_logs").where(filter=FieldFilter("userId", "==", user_id)).stream()
            for log in water_logs:
                data = log.to_dict() or {}
                if data.get("date", "") == today:
                    total_water += data.get("amount", 0) or 0
        except Exception:
            total_water = 0

        from datetime import datetime as dt
        today_start = dt.combine(date.today(), dt.min.time())
        today_meals = []
        total_calories = 0
        total_protein = 0
        total_carbs = 0
        total_fat = 0
        try:
            # Single-field filter only; apply timestamp range in app code.
            meal_logs = db.collection("meal_history").where(filter=FieldFilter("userId", "==", user_id)).stream()
            for m in meal_logs:
                data = m.to_dict()
                if not data:
                    continue
                ts = data.get("timestamp")
                if isinstance(ts, dt) and ts < today_start:
                    continue
                total_calories += data.get("calories", 0) or 0
                total_protein += data.get("protein", 0) or 0
                total_carbs += data.get("carbs", 0) or 0
                total_fat += data.get("fat", 0) or 0
                today_meals.append(data)
        except Exception:
            pass

        weight = user_data.get("weight", 70) or 70
        height = user_data.get("height", 170) or 170
        bmi = calculate_bmi(weight, height)
        calories_burned = calculate_calories_burned(weight, "moderate", 30)

        avg_health_score = 0
        try:
            last_meals = stream_sorted(
                db.collection("meal_history").where(filter=FieldFilter("userId", "==", user_id)),
                order_field="timestamp", desc=True, limit=7,
            )
            scores = [m.get("healthScore", 50) or 0 for m in last_meals]
            avg_health_score = round(sum(scores) / max(len(scores), 1))
        except Exception:
            avg_health_score = 0

        return {
            "totalCalories": round(total_calories, 1),
            "totalProtein": round(total_protein, 1),
            "totalCarbs": round(total_carbs, 1),
            "totalFat": round(total_fat, 1),
            "waterIntake": total_water,
            "bmi": bmi,
            "bmiCategory": "Underweight" if bmi < 18.5 else "Normal" if bmi < 25 else "Overweight" if bmi < 30 else "Obese",
            "caloriesBurned": calories_burned,
            "healthScore": avg_health_score,
            "mealsLogged": len(today_meals),
            "weight": weight,
            "height": height,
        }

    @staticmethod
    def get_progress_data(user_id: str) -> Dict[str, Any]:
        db = get_db()
        weight_data = NutritionService.get_weight_history(user_id, 30)
        water_data = NutritionService.get_water_intake(user_id, 7)

        calorie_data = {}
        try:
            meals = stream_sorted(
                db.collection("meal_history").where(filter=FieldFilter("userId", "==", user_id)),
                order_field="timestamp", desc=False, limit=30,
            )
            for data in meals:
                ts = data.get("timestamp")
                if ts:
                    # timestamp may now be an ISO string (helper normalizes it)
                    day = ts[:10]
                    calorie_data[day] = calorie_data.get(day, 0) + (data.get("calories", 0) or 0)
        except Exception:
            pass

        achievements = [
            {"name": "First Scan", "description": "Scan your first meal", "icon": "📸", "unlocked": False},
            {"name": "7-Day Streak", "description": "Log meals for 7 days in a row", "icon": "🔥", "unlocked": False},
            {"name": "Water Warrior", "description": "Hit water goal for 5 days", "icon": "💧", "unlocked": False},
            {"name": "Health Guru", "description": "Score 90+ on a meal", "icon": "🧠", "unlocked": False},
            {"name": "Meal Master", "description": "Scan 50 meals", "icon": "🏆", "unlocked": False},
            {"name": "Weight Watcher", "description": "Log weight 10 times", "icon": "⚖️", "unlocked": False},
        ]

        return {
            "weightHistory": weight_data.get("data", []),
            "calorieHistory": [{"date": k, "calories": v} for k, v in sorted(calorie_data.items())],
            "waterHistory": water_data.get("daily", {}),
            "achievements": achievements,
        }

    @staticmethod
    def generate_report(user_id: str, period: str = "weekly", store: bool = False) -> Dict[str, Any]:
        db = get_db()
        today = date.today()
        if period == "weekly":
            start_date = (today - timedelta(days=7)).isoformat()
        elif period == "monthly":
            start_date = (today - timedelta(days=30)).isoformat()
        else:
            start_date = (today - timedelta(days=7)).isoformat()

        from datetime import datetime as dt
        start_dt = dt.fromisoformat(start_date) if isinstance(start_date, str) else start_date
        total_calories = 0
        total_protein = 0
        total_carbs = 0
        total_fat = 0
        meal_count = 0
        health_scores = []
        try:
            # Single-field filter only; apply timestamp range in app code.
            meals = db.collection("meal_history").where(filter=FieldFilter("userId", "==", user_id)).stream()
            for m in meals:
                data = m.to_dict()
                if not data:
                    continue
                ts = data.get("timestamp")
                if isinstance(ts, dt) and ts < start_dt:
                    continue
                total_calories += data.get("calories", 0) or 0
                total_protein += data.get("protein", 0) or 0
                total_carbs += data.get("carbs", 0) or 0
                total_fat += data.get("fat", 0) or 0
                meal_count += 1
                health_scores.append(data.get("healthScore", 50) or 0)
        except Exception:
            pass

        water_data = NutritionService.get_water_intake(user_id, 7)
        total_water = sum(water_data.get("daily", {}).values())

        days_in_period = (today - date.fromisoformat(start_date) if isinstance(start_date, str) else today - start_date).days if period == "weekly" else ((today - date.fromisoformat(start_date) if isinstance(start_date, str) else today - start_date).days) or 7
        days_in_period = max(days_in_period, 1)
        report_data = {
            "period": period,
            "startDate": start_date if isinstance(start_date, str) else start_date.isoformat(),
            "endDate": today.isoformat(),
            "totalMeals": meal_count,
            "totalCalories": round(total_calories, 1),
            "avgDailyCalories": round(total_calories / days_in_period, 1) if meal_count > 0 else 0,
            "totalProtein": round(total_protein, 1),
            "totalCarbs": round(total_carbs, 1),
            "totalFat": round(total_fat, 1),
            "avgHealthScore": round(sum(health_scores) / len(health_scores), 1) if health_scores else 0,
            "totalWaterOz": round(total_water, 1),
            "avgWaterOz": round(total_water / days_in_period, 1),
        }

        if store:
            db.collection("reports").add({
                "userId": user_id,
                "period": period,
                "data": report_data,
                "createdAt": firestore.SERVER_TIMESTAMP,
            })

        return report_data

    @staticmethod
    def get_mood_recommendations(mood: str) -> List[Dict[str, Any]]:
        return get_mood_recommendations(mood)

    @staticmethod
    def generate_budget_plan(budget: float) -> Dict[str, Any]:
        return generate_budget_plan(budget)
