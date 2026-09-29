"""USDA FoodData Central integration for the scan pipeline.

Replaces hardcoded/local nutrition values with live USDA FoodData Central
lookups keyed on the foods detected by the SigLIP model.

Security notes:
  - The API key is read ONLY from the USDA_API_KEY environment variable
    (backend/.env on local dev, Railway env vars in production). It is never
    hardcoded, never logged, and never sent to the frontend.
  - All USDA API calls happen server-side.

Pipeline position:
    SigLIP label -> build_search_queries() -> FDC /foods/search -> best match
    -> parse foodNutrients -> nutrition dict (normalized per 100 g).

Fallbacks:
  - Invalid key, rate limit, network failure, or no matching food: this module
    returns None and logs the reason (never the key). The caller
    (services.nutrition_mapper) then falls back to the local USDA-derived CSV.
"""
import os
import re
import time
from typing import Dict, Any, List, Optional

import requests

API_BASE = "https://api.nal.usda.gov/fdc/v1"

# Nutrient IDs used by FDC (https://fdc.nal.usda.gov/faq.html)
NUTRIENT_IDS = {
    "calories": [1008],                    # Energy, kcal
    "protein": [1003],                     # Protein
    "fat": [1004],                         # Total lipid (fat)
    "carbs": [1005],                       # Carbohydrate, by difference
    "fiber": [1079],                       # Fiber, total dietary
    "sugar": [2000, 1063],                 # Total sugars / Sugars, total NLEA
    "sodium": [1093],
    "cholesterol": [1253],
    "saturatedFat": [1258],                # Fatty acids, total saturated
    "vitaminA": [1106, 1104],              # RAE preferred, IU fallback
    "vitaminC": [1162],
    "vitaminD": [1114, 1110, 1086],        # D2+D3, D2, D3
    "vitaminE": [1109, 1213],              # Added / alpha-tocopherol
    "vitaminK": [1185, 1184],              # Phylloquinone variants
    "calcium": [1087],
    "iron": [1089],
    "potassium": [1092],
    "magnesium": [1090],
    "zinc": [1095],
    "vitaminB6": [1175],
    "vitaminB12": [1178],
    "folate": [1177],
}

# Preferred non-branded data types: their nutrient values are per 100 g and
# are the most reliable source for generic foods.
PREFERRED_DATA_TYPES = ["SR Legacy", "Foundation", "Survey (FNDDS)"]

# Words that carry no nutritional information and just muddy a USDA query.
FILLER_WORDS = {
    "with", "and", "of", "in", "on", "a", "an", "the", "style",
    "flavored", "flavoured", "seasoned", "served",
}

REQUEST_TIMEOUT = 15
CACHE_TTL_SECONDS = 24 * 60 * 60  # FDC free tier: 30 req/min, 1000/day

# Extra description words that usually mean the food is NOT the generic
# version the scan pipeline wants (only penalized when not part of the query).
# Includes compound-food suffixes ("Rice crackers" for query "rice") so the
# bare staple wins over snacks/dishes made from it.
QUALIFIER_PENALTY = {
    "dessert", "sauce", "dressing", "flour", "mix", "dry", "flakes",
    "unprepared", "uncooked", "canned", "soup", "syrup", "margarine",
    "breadcrumbs", "snacks", "croutons", "powder", "granules", "rolls",
    "sticks", "tenders", "nuggets", "bites", "puffs", "krispies", "patties",
    "wraps", "bowls", "slices", "pieces", "strips", "frozen", "spinach",
    "slider", "meatless", "vegan", "vegetarian", "shelf-stable", "squash",
    "crackers", "cake", "paper", "pilaf", "patty", "pancake", "pie", "bread",
    "stew", "casserole", "cookie", "chips", "crisps", "noodles", "pudding",
    "candy", "dip", "spread", "cereal", "drink", "shake", "sundae",
    "milk", "juice", "water", "soda", "beer", "wine", "cocktail", "tea",
    "coffee", "latte", "cappuccino", "espresso", "smoothie", "lemonade",
    "sauce", "salad",
}

# Query aliases for labels whose natural query pulls poor generic matches
# (e.g. "pizza" ranks appetizer variants above plain "Pizza, cheese").
QUERY_ALIASES: Dict[str, List[str]] = {
    "pizza": ["cheese pizza"],
    "rice": ["white rice"],
}

# Generic USDA entries use "Not Further Specified" phrasing; those are the
# canonical generic match for a scan, so they get a head-noun bonus instead.
GENERIC_HEAD_WORDS = {"nfs", "nfsm"}

# Query-leading adjectives/preparation words. When a SigLIP label starts with
# one of these (e.g. "white rice"), an additional "noun first" query variant
# ("rice white") is emitted because USDA descriptions put the noun first
# ("Rice, white, long-grain, ...").
MODIFIER_WORDS = {
    "white", "brown", "black", "green", "red", "yellow", "dark", "raw",
    "cooked", "fried", "steamed", "grilled", "roasted", "boiled", "baked",
    "sweet", "fresh", "frozen", "canned", "instant", "plain", "whole",
    "ground", "sliced", "shredded", "mashed", "diced", "chopped", "pickled",
    "dried", "smoked", "salted", "unsalted", "refined", "enriched", "glazed",
}


def _log(message: str) -> None:
    print(f"[USDA] {message}")


def _stem(word: str) -> str:
    """Crude singular/plural normalization for token comparison.

    USDA descriptions use plural nouns ("Strawberries, raw") while SigLIP
    labels are singular ("strawberry"), so "berries" must match "berry".
    """
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


class USDAFoodDataService:
    _cache: Dict[str, Any] = {}
    _cache_ts: Dict[str, float] = {}
    _service_unavailable = False

    # ---- configuration -------------------------------------------------

    @classmethod
    def get_api_key(cls) -> str:
        return (os.environ.get("USDA_API_KEY") or "").strip()

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls.get_api_key())

    @classmethod
    def is_available(cls) -> bool:
        return cls.is_configured() and not cls._service_unavailable

    # ---- query building -------------------------------------------------

    @classmethod
    def build_search_queries(cls, label: str) -> List[str]:
        """Convert a SigLIP food label into ordered USDA search queries.

        The full cleaned label is tried first. For compound labels the
        head noun is prioritized (USDA descriptions put the noun first), and
        labels that begin with a modifier word (colors, preparation methods)
        also get a noun-first variant. Progressively shorter prefixes are
        emitted last, followed by any curated aliases.
        """
        cleaned = label.lower().strip()
        words = [w for w in re.findall(r"[a-z0-9]+", cleaned) if w not in FILLER_WORDS]
        queries: List[str] = []
        if words:
            queries.append(" ".join(words))
        if len(words) >= 2:
            if words[0] in MODIFIER_WORDS:
                reversed_q = " ".join(reversed(words))
                if reversed_q not in queries:
                    queries.append(reversed_q)
            head = words[-1]
            if head not in queries:
                queries.append(head)
        for n in range(len(words) - 1, 0, -1):
            q = " ".join(words[:n])
            if q and q not in queries:
                queries.append(q)
        for alias in QUERY_ALIASES.get(cleaned, []):
            if alias not in queries:
                queries.append(alias)
        return queries or [cleaned]

    # ---- FDC API ---------------------------------------------------------

    @classmethod
    def _request_search(cls, query: str, data_types: List[str]) -> Optional[dict]:
        params = {
            "query": query,
            "pageSize": 20,
            "pageNumber": 1,
            "api_key": cls.get_api_key(),
            "requireAllWords": "false",
        }
        if data_types:
            params["dataType"] = ",".join(data_types)

        for attempt in range(3):
            try:
                resp = requests.get(f"{API_BASE}/foods/search", params=params, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as e:
                _log(f"request failed for query '{query}': {e}")
                return None

            if resp.status_code == 401:
                _log("authentication failed (invalid or missing USDA_API_KEY). Falling back to local data.")
                cls._service_unavailable = True
                return None
            if resp.status_code == 403:
                _log("access denied by USDA. Falling back to local data.")
                cls._service_unavailable = True
                return None
            if resp.status_code in (429, 400) and attempt < 2:
                # FDC's nginx frontend rejects bursts with 400/429 on the free
                # tier, so back off and retry before falling back.
                _log(f"HTTP {resp.status_code} for query '{query}', retry {attempt + 1}/2...")
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code in (429, 400):
                _log(f"HTTP {resp.status_code} for query '{query}' (rate limit?). Falling back to local data.")
                return None
            if resp.status_code >= 500:
                _log(f"USDA server error {resp.status_code} for query '{query}'. Falling back to local data.")
                return None
            if resp.status_code != 200:
                _log(f"unexpected HTTP {resp.status_code} for query '{query}'. Falling back to local data.")
                return None
            try:
                return resp.json()
            except ValueError:
                _log(f"non-JSON response for query '{query}'. Falling back to local data.")
                return None
        return None

    @classmethod
    def search(cls, query: str) -> List[dict]:
        """Search FDC, preferring reliable non-branded data types.

        Logs the query terms for debugging (never the API key).
        """
        payload = cls._request_search(query, PREFERRED_DATA_TYPES)
        if payload is None:
            return []
        foods = payload.get("foods") or []
        if foods:
            _log(f"search '{query}' -> {len(foods)} candidate(s), top: "
                 f"{foods[0].get('description')} ({foods[0].get('dataType')})")
            return foods

        # No non-branded result: try a broad search (includes Branded).
        payload = cls._request_search(query, [])
        if payload is None:
            return []
        foods = payload.get("foods") or []
        if foods:
            _log(f"broad search '{query}' -> {len(foods)} candidate(s), top: "
                 f"{foods[0].get('description')} ({foods[0].get('dataType')})")
        else:
            _log(f"no USDA results for query '{query}'")
        return foods

    # ---- match selection -------------------------------------------------

    @classmethod
    def select_best_match(cls, foods: List[dict], query: str) -> Optional[Dict[str, Any]]:
        """Pick the most relevant USDA food for the query.

        Returns {"food": ..., "score": ...} or None. Combines the FDC
        relevance score with data-type preference and exactness signals:
          - description starts on a query token (strong signal),
          - consecutive token run from the description start,
          - description ends on a query token or a "NFS" generic marker,
          - penalties for qualifier words ("dessert pizza" for query "pizza"),
            ALL-CAPS brand descriptions, and brand parentheticals.
        """
        query_lower = query.lower().strip()
        q_token_list = [t for t in re.findall(r"[a-z0-9]+", query_lower) if len(t) > 1]
        q_tokens = set(q_token_list)
        q_stems = set(_stem(t) for t in q_token_list)
        reversed_tokens = list(reversed(q_token_list))
        type_bonus = {"SR Legacy": 350, "Foundation": 350, "Survey (FNDDS)": 300}

        best, best_score = None, float("-inf")
        for food in foods:
            desc = (food.get("description") or "").lower()
            data_type = food.get("dataType") or ""
            desc_tokens = [t for t in re.findall(r"[a-z0-9]+", desc) if len(t) > 1]
            desc_stems = [_stem(t) for t in desc_tokens]

            score = float(food.get("score") or 0) / 20.0
            score += type_bonus.get(data_type, 0)

            if q_tokens and desc_tokens:
                overlap = sum(1 for t in q_tokens if t in desc)
                score += 150.0 * (overlap / len(q_tokens))

                # description starts on a query token (plural-aware)
                if desc_stems[0] in q_stems:
                    score += 400

                # consecutive run of query tokens from the description start
                # (checked in both directions because USDA puts the noun first)
                best_prefix = 0
                for ref in (q_token_list, reversed_tokens):
                    prefix = 0
                    for dt, qt in zip(desc_tokens, ref):
                        if _stem(dt) == _stem(qt):
                            prefix += 1
                        else:
                            break
                    best_prefix = max(best_prefix, prefix)
                score += 100 * min(best_prefix, 3)

                # description ends on a query token (the "head" noun) or a
                # "not further specified" generic marker
                if _stem(desc_tokens[-1]) in q_stems:
                    score += 150
                elif desc_tokens[-1] in GENERIC_HEAD_WORDS:
                    score += 100

            for extra in desc_tokens:
                if extra not in q_tokens and extra in QUALIFIER_PENALTY:
                    score -= 250
                    break

            if desc.isupper():
                score -= 250  # all-caps branded descriptions (e.g. "PIZZA HUT ...")
            elif "(" in desc or ")" in desc:
                score -= 100  # brand-specific entries (e.g. "Hamburger (McDonalds)")

            if "restaurant" in desc:
                score -= 100

            category = (food.get("foodCategory") or "").lower()
            if "restaurant" in category:
                score -= 100
            if "fast food" in category:
                score -= 80

            score -= min(len(desc) * 0.02, 40)  # tie-break toward short generic names

            if score > best_score:
                best, best_score = food, score
        if best is None:
            return None
        return {"food": best, "score": best_score}

    # ---- nutrient parsing -------------------------------------------------

    @classmethod
    def _nutrient_value(cls, food: dict, nutrient_ids: List[int]) -> Optional[float]:
        for nutrient in food.get("foodNutrients") or []:
            if nutrient.get("nutrientId") in nutrient_ids:
                try:
                    return float(nutrient.get("value") or 0)
                except (TypeError, ValueError):
                    return None
        return None

    @classmethod
    def parse_food(cls, food: dict, query: str) -> Dict[str, Any]:
        """Convert a FDC food object into the scan pipeline nutrition dict.

        Nutrient values are normalized to per 100 g so the aggregation and
        health score behave identically to the local USDA-derived dataset.
        """
        data_type = food.get("dataType") or ""
        scale = 1.0
        serving_size = food.get("servingSize")
        if data_type == "Branded" and serving_size:
            try:
                scale = 100.0 / float(serving_size)
            except (TypeError, ValueError):
                scale = 1.0

        def scaled(nutrient_ids):
            value = cls._nutrient_value(food, nutrient_ids)
            return round(value * scale, 1) if value is not None else None

        values = {
            key: scaled(ids) for key, ids in NUTRIENT_IDS.items()
        }

        # FDC records omit nutrients that are effectively absent (e.g. fiber
        # in eggs). Core values must be numeric so consumers can float() them;
        # the local CSV fallback already defaults missing values to 0.0.
        for core_key in ("calories", "protein", "fat", "carbs", "fiber", "sugar", "sodium"):
            if values[core_key] is None:
                values[core_key] = 0.0

        from utils.helpers import generate_health_score

        nutrition: Dict[str, Any] = {
            "foodName": food.get("description") or query,
            "foodCategory": food.get("foodCategory") or "",
            "dataType": data_type,
            "calories": values["calories"],
            "protein": values["protein"],
            "fat": values["fat"],
            "carbs": values["carbs"],
            "fiber": values["fiber"],
            "sugar": values["sugar"],
            "sodium": values["sodium"],
            "servingSize": "100",
            "servingUnit": "g",
            "servingDescription": food.get("householdServingFullText") or "",
            "source": "usda",
            "fdcId": food.get("fdcId"),
            "brandOwner": food.get("brandOwner") or "",
        }

        micronutrients = {}
        for key in ("vitaminA", "vitaminC", "vitaminD", "vitaminE", "vitaminK",
                    "calcium", "iron", "potassium", "magnesium", "zinc",
                    "vitaminB6", "vitaminB12", "folate", "cholesterol",
                    "saturatedFat"):
            if values.get(key) is not None:
                micronutrients[key] = values[key]
        nutrition["micronutrients"] = micronutrients

        nutrition["healthScore"] = generate_health_score(
            calories=values["calories"] or 0.0,
            protein=values["protein"] or 0.0,
            fat=values["fat"] or 0.0,
            carbs=values["carbs"] or 0.0,
            fiber=values["fiber"] or 0.0,
            sugar=values["sugar"] or 0.0,
            sodium=values["sodium"] or 0.0,
        )
        return nutrition

    # ---- public lookup ----------------------------------------------------

    @classmethod
    def get_nutrition_for_food(cls, class_name: str) -> Optional[Dict[str, Any]]:
        """Full USDA lookup for one detected food. Returns None on any failure.

        Evaluates every query variant and keeps the best-scoring match (later
        variants get a small penalty so the full label is preferred when the
        matches are comparable). Early-exits once a confident match is found.
        """
        if not cls.is_available():
            if not cls.is_configured():
                _log("USDA_API_KEY not set in backend environment; using local fallback.")
            return None

        cache_key = class_name.lower().strip()
        if cache_key in cls._cache:
            return cls._cache[cache_key]

        queries = cls.build_search_queries(class_name)
        best_match, best_score = None, float("-inf")
        for variant_index, query in enumerate(queries):
            foods = cls.search(query)
            if not foods:
                time.sleep(0.4)  # avoid burst rejection on the free tier
                continue
            match = cls.select_best_match(foods, query)
            if match is None:
                time.sleep(0.4)
                continue
            adjusted = match["score"] - 60 * variant_index
            if adjusted > best_score:
                best_match, best_score = match["food"], adjusted
            if best_score >= 1100:
                break  # confident generic match; stop spending API calls

        result = None
        if best_match is not None:
            result = cls.parse_food(best_match, cache_key)
            _log(f"matched '{cache_key}' -> '{best_match.get('description')}' "
                 f"(fdcId={best_match.get('fdcId')}, dataType={best_match.get('dataType')})")
        else:
            _log(f"no USDA match for '{cache_key}' after {len(queries)} query variant(s): "
                 + " | ".join(f"'{q}'" for q in queries))

        cls._cache[cache_key] = result
        cls._cache_ts[cache_key] = time.time()
        return result

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache = {}
        cls._cache_ts = {}
        cls._service_unavailable = False
