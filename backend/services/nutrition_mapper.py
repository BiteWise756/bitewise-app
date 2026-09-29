import os
import csv
import re
from typing import Dict, Any, Optional, List, Tuple

DATASET_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "dataset",
    "comprehensive_foods_usda.csv",
)

UNKNOWN = "unknown"


def _stem_match(word: str, term: str) -> bool:
    """True when word is the term or a plural form of it."""
    return (
        word == term
        or (term.endswith("y") and word == term[:-1] + "ies")
        or (not term.endswith("s") and word == term + "s")
    )

# COCO food class → preferred USDA food search term
CLASS_TO_SEARCH = {
    "apple": "apple",
    "banana": "banana",
    "orange": "orange",
    "broccoli": "broccoli",
    "carrot": "carrot",
    "sandwich": "sandwich",
    "pizza": "pizza",
    "hot dog": "hot dog",
    "donut": "donut",
    "cake": "cake",
}


class NutritionMapper:
    _cache = None

    @classmethod
    def get_dataset_path(cls) -> str:
        return os.environ.get("USDA_DATASET_PATH", DATASET_PATH)

    @classmethod
    def _load_dataset(cls) -> List[Dict[str, str]]:
        if cls._cache is not None:
            return cls._cache
        path = cls.get_dataset_path()
        if not os.path.exists(path):
            print(f"WARNING: USDA dataset not found at {path}")
            cls._cache = []
            return cls._cache
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        cls._cache = rows
        return rows

    @classmethod
    def find_food(cls, term: str) -> Optional[Dict[str, Any]]:
        rows = cls._load_dataset()
        if not rows:
            return None

        term_lower = term.lower().strip()
        # USDA names use plural nouns ("Strawberries, raw") while SigLIP
        # labels are singular ("strawberry").
        terms = [term_lower]
        if term_lower.endswith("y"):
            terms.append(term_lower[:-1] + "ies")
        elif not term_lower.endswith("s"):
            terms.append(term_lower + "s")

        # Rows describing a different product (beverage mixes, powders, shakes,
        # desserts) must never beat the plain food itself.
        junk_words = {
            "mix", "powder", "flavor", "flavored", "flavour", "flavoured",
            "syrup", "beverage", "drink", "candy", "shake", "sundae",
            "dessert", "pie", "cake", "cookie", "filled", "twists",
        }
        junk_in_term = {w for w in junk_words if w in term_lower}

        candidates = []

        for row in rows:
            name = row.get("food_name", "").strip()
            if not name:
                continue
            name_lower = name.lower()
            data_type = row.get("data_type", "")
            is_legacy = data_type in ("SR Legacy", "Foundation")
            food_cat = (row.get("food_category", "") or "").lower()

            if not any(name_lower.startswith(t) for t in terms):
                continue

            is_restaurant = "restaurant" in food_cat
            is_raw = re.search(r"\braw\b", name_lower) is not None

            score = 100
            if is_raw and is_legacy:
                score = 0
            elif is_raw:
                score = 10
            elif is_legacy:
                score = 20

            for w in junk_words - junk_in_term:
                if w in name_lower:
                    score += 300
                    break

            if is_restaurant:
                score += 50

            foundation_penalty = 0 if data_type == "Foundation" else 1
            candidates.append((score, foundation_penalty, len(name), name, row))

        if not candidates:
            for row in rows:
                name = row.get("food_name", "").strip()
                if not name:
                    continue
                name_lower = name.lower()
                data_type = row.get("data_type", "")
                is_legacy = data_type in ("SR Legacy", "Foundation")
                food_cat = (row.get("food_category", "") or "").lower()
                if not any(t in name_lower for t in terms):
                    continue
                is_restaurant = "restaurant" in food_cat
                first_food_word = re.split(r"[\s,';]+", name_lower)[0]
                is_raw = re.search(r"\braw\b", name_lower) is not None
                score = 30
                if is_raw and is_legacy:
                    score = 5
                elif any(_stem_match(first_food_word, t) for t in terms) and is_legacy:
                    score = 8
                elif any(_stem_match(first_food_word, t) for t in terms):
                    score = 15
                elif is_legacy:
                    score = 25
                for w in junk_words - junk_in_term:
                    if w in name_lower:
                        score += 300
                        break
                if is_restaurant:
                    score += 40
                foundation_penalty = 0 if data_type == "Foundation" else 1
                candidates.append((score, foundation_penalty, len(name), name, row))

        if not candidates:
            return None

        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        best = candidates[0][4]

        return cls._row_to_nutrition(best)

    @classmethod
    def _row_to_nutrition(cls, row: Dict[str, str]) -> Dict[str, Any]:
        def _float(val: str) -> float:
            try:
                return float(val) if val.strip() else 0.0
            except (ValueError, TypeError):
                return 0.0

        return {
            "foodName": row.get("food_name", UNKNOWN),
            "foodCategory": row.get("food_category", ""),
            "dataType": row.get("data_type", ""),
            "calories": _float(row.get("calories", "0")),
            "protein": _float(row.get("protein_g", "0")),
            "fat": _float(row.get("fat_g", "0")),
            "carbs": _float(row.get("carbs_g", "0")),
            "fiber": _float(row.get("fiber_g", "0")),
            "sugar": _float(row.get("sugar_g", "0")),
            "sodium": _float(row.get("sodium_mg", "0")),
            "servingSize": row.get("serving_size", ""),
            "servingUnit": row.get("serving_unit", ""),
            "healthScore": int(_float(row.get("health_score", "50"))),
        }

    @classmethod
    def get_nutrition_for_class(cls, class_name: str) -> Dict[str, Any]:
        """USDA FoodData Central first, local USDA-derived CSV as fallback."""
        from services.usda_service import USDAFoodDataService
        usda_result = USDAFoodDataService.get_nutrition_for_food(class_name)
        if usda_result is not None:
            return usda_result
        term = CLASS_TO_SEARCH.get(class_name, class_name)
        result = cls.find_food(term)
        if result is not None:
            result["source"] = "local_csv"
            return result
        return {
            "foodName": class_name,
            "foodCategory": UNKNOWN,
            "dataType": UNKNOWN,
            "calories": UNKNOWN,
            "protein": UNKNOWN,
            "fat": UNKNOWN,
            "carbs": UNKNOWN,
            "fiber": UNKNOWN,
            "sugar": UNKNOWN,
            "sodium": UNKNOWN,
            "servingSize": UNKNOWN,
            "servingUnit": UNKNOWN,
            "healthScore": UNKNOWN,
        }

    @classmethod
    def is_available(cls) -> bool:
        return os.path.exists(cls.get_dataset_path())

    @classmethod
    def clear_cache(cls):
        cls._cache = None
