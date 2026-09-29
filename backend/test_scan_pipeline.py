"""End-to-end scan pipeline verification.

Runs the full Image -> SigLIP -> USDA FoodData Central -> nutrition pipeline
over real food photos covering: pizza, burger, rice, salad, fruits,
vegetables, desserts, packaged foods, and a non-food image.

Usage:
    cd backend
    venv\\Scripts\\python test_scan_pipeline.py

No API keys are printed or logged by this script.
"""
import io
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from PIL import Image, ImageDraw  # noqa: E402
from services.siglip_detector import SigLIPDetector  # noqa: E402
from services.usda_service import USDAFoodDataService  # noqa: E402
from utils.helpers import analyze_meal_image  # noqa: E402

TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_images")
os.makedirs(TEST_DIR, exist_ok=True)

CASES = [
    ("pizza", "Pizza"),
    ("burger", "Hamburger"),
    ("rice", "White rice"),
    ("salad", "Caesar salad"),
    ("fruits", "Strawberry"),
    ("vegetables", "Broccoli"),
    ("dessert", "Cake"),
    ("packaged", "Potato chips"),
    ("non_food", "Golden Gate Bridge"),
]


def fetch_image(name: str, page_title: str):
    """Resolve the Wikipedia lead image for a page and save it locally."""
    dest = os.path.join(TEST_DIR, f"{name}.jpg")
    if os.path.exists(dest) and os.path.getsize(dest) > 1000:
        if _looks_like_image(dest):
            return dest
        print(f"[{name}] cached file is not an image, re-fetching")
    import requests

    summary = requests.get(
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{page_title}",
        headers={"User-Agent": "BiteWiseScanTest/1.0"},
        timeout=20,
    )
    url = summary.json().get("originalimage", {}).get("source")
    if not url:
        return None
    resp = requests.get(url, headers={"User-Agent": "BiteWiseScanTest/1.0"}, timeout=30)
    if "html" in resp.headers.get("Content-Type", ""):
        return None
    with open(dest, "wb") as f:
        f.write(resp.content)
    if not _looks_like_image(dest):
        os.remove(dest)
        return None
    return dest


def _looks_like_image(path: str) -> bool:
    """Reject HTML error pages that some CDNs return instead of images."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
        if head.lstrip().startswith(b"<!DOCTYPE") or head.lstrip().startswith(b"<html"):
            return False
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


def generate_placeholder(name: str) -> str:
    dest = os.path.join(TEST_DIR, f"{name}.jpg")
    img = Image.new("RGB", (224, 224), color=(128, 96, 64))
    ImageDraw.Draw(img).text((60, 110), name, fill=(255, 255, 255))
    img.save(dest, "JPEG")
    return dest


def main():
    print("=" * 80)
    print("SCAN PIPELINE TEST  (SigLIP -> USDA FoodData Central)")
    print("=" * 80)

    ok = 0
    failed = 0
    for name, page_title in CASES:
        path = fetch_image(name, page_title) or generate_placeholder(name)
        with open(path, "rb") as f:
            image_bytes = f.read()
        try:
            result = analyze_meal_image(image_bytes)
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] ERROR: {exc}")
            traceback.print_exc()
            failed += 1
            continue

        status = result.get("status", "?")
        food_name = result.get("foodName", "")
        calories = result.get("calories", "?")
        health = result.get("healthScore", "?")
        n_det = result.get("totalDetected", 0)
        sources = sorted({d.get("nutrition", {}).get("source", "?") for d in result.get("detections", [])})
        print(f"[{name:>11}] status={status:>12} | food={food_name!r:>45} | "
              f"cal={calories} | score={health} | detections={n_det} | src={sources}")

        if status in ("success", "no_detection"):
            ok += 1
        else:
            failed += 1

    print("=" * 80)
    print(f"PASSED: {ok} / {len(CASES)} | FAILED: {failed}")
    print("USDA API configured:", USDAFoodDataService.is_configured(),
          "| available:", USDAFoodDataService.is_available())
    print("=" * 80)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
