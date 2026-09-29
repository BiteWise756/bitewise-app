"""Unsplash API integration for dynamic food images.

Uses the official Unsplash API (https://unsplash.com/documentation) to search
for food photos based on meal names. Results are cached in-memory to avoid
repeated API calls for the same food.

Security notes:
  - The API key is read ONLY from the UNSPLASH_ACCESS_KEY environment variable.
  - All API calls happen server-side; the secret key is never exposed to the frontend.
  - The Access Key is safe for client exposure per Unsplash guidelines, but we
    keep it server-side for centralized caching and rate-limit management.

Caching:
  - Results are cached in-memory with a TTL of 24 hours.
  - Cache keys are normalized (lowercased, trimmed meal names).
  - In-memory cache resets on server restart (acceptable for this use case).

Fallback:
  - If Unsplash returns no results or fails, returns a sensible default food image URL.
"""
import os
import time
from typing import Optional, Dict, Any
from urllib.parse import quote

import requests

# Cache storage: { normalized_name: { "url": str, "attribution": dict, "ts": float } }
_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Default fallback image (generic food photo)
FALLBACK_IMAGE = "https://images.unsplash.com/photo-1498837167922-ddd27525d352?w=160&h=160&fit=crop&q=80"

# Unsplash API endpoint
UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"


def _get_access_key() -> Optional[str]:
    """Read the Unsplash Access Key from environment."""
    return os.getenv("UNSPLASH_ACCESS_KEY", "").strip() or None


def _normalize_name(name: str) -> str:
    """Normalize a food name for cache lookup."""
    return (name or "").strip().lower()


def search_food_image(food_name: str) -> Dict[str, Any]:
    """Search Unsplash for a food photo matching the given name.

    Args:
        food_name: The meal/food name to search for (e.g. "Pepperoni Pizza").

    Returns:
        dict with keys:
          - url (str): The photo URL from photo.urls.regular or fallback.
          - photographer (str): Photographer name for attribution.
          - photographer_url (str): Link to photographer's Unsplash profile.
          - unsplash_url (str): Link to the photo on Unsplash.
          - alt (str): Photo description or alt text.
          - source (str): "unsplash" or "fallback".
    """
    normalized = _normalize_name(food_name)

    # Check cache first
    if normalized in _cache:
        cached = _cache[normalized]
        if time.time() - cached["ts"] < CACHE_TTL_SECONDS:
            return cached["result"]
        # Cache expired, remove and re-fetch
        del _cache[normalized]

    # Check if API key is configured
    access_key = _get_access_key()
    if not access_key:
        return _fallback_result(food_name, "UNSPLASH_ACCESS_KEY not configured")

    # Search Unsplash
    try:
        response = requests.get(
            UNSPLASH_SEARCH_URL,
            params={
                "query": food_name,
                "per_page": 10,
                "orientation": "squarish",
                "content_filter": "high",
            },
            headers={
                "Authorization": f"Client-ID {access_key}",
            },
            timeout=5,
        )

        if response.status_code == 403:
            return _fallback_result(food_name, "Unsplash rate limit or invalid key")
        if response.status_code != 200:
            return _fallback_result(food_name, f"Unsplash API returned {response.status_code}")

        data = response.json()
        results = data.get("results", [])

        if not results:
            return _fallback_result(food_name, f"No Unsplash results for '{food_name}'")

        # Pick the first result (most relevant)
        photo = results[0]

        # Build result with attribution
        result = {
            "url": photo["urls"]["regular"],  # Good quality, not too large
            "photographer": photo["user"]["name"],
            "photographer_url": photo["user"]["links"]["html"],
            "unsplash_url": photo["links"]["html"],
            "alt": photo.get("alt_description") or food_name,
            "source": "unsplash",
        }

        # Cache the result
        _cache[normalized] = {
            "result": result,
            "ts": time.time(),
        }

        return result

    except requests.Timeout:
        return _fallback_result(food_name, "Unsplash API timeout")
    except requests.RequestException as e:
        return _fallback_result(food_name, f"Unsplash API error: {str(e)}")
    except (KeyError, IndexError) as e:
        return _fallback_result(food_name, f"Unsplash response parse error: {str(e)}")


def search_food_images_batch(food_names: list) -> Dict[str, Dict[str, Any]]:
    """Search Unsplash for multiple food names at once.

    Args:
        food_names: List of meal/food names to search for.

    Returns:
        Dict mapping each food name (lowercased) to its search result dict.
    """
    results = {}
    for name in food_names:
        normalized = _normalize_name(name)
        if normalized not in results:
            results[normalized] = search_food_image(name)
    return results


def _fallback_result(food_name: str, reason: str) -> Dict[str, Any]:
    """Return a fallback result when Unsplash fails."""
    return {
        "url": FALLBACK_IMAGE,
        "photographer": "",
        "photographer_url": "",
        "unsplash_url": "",
        "alt": food_name,
        "source": "fallback",
    }


def clear_cache():
    """Clear the in-memory cache (useful for testing)."""
    global _cache
    _cache = {}
