"""SigLIP-powered image understanding — comprehensive food recognition.

Replaces the previous YOLO object-detection pipeline (services/detector.py).

Key differences from the YOLO implementation:
  - YOLO was an object detector: it output bounding boxes + class ids from a
    fixed COCO-trained label set (models/yolov8n.pt).
  - SigLIP is a zero-shot image-text model: it embeds the image and the text of
    candidate food labels, then returns image-text similarity logits. There are
    no bounding boxes, so each label above the confidence threshold is reported
    as a "detection".
  - Preprocessing is done by the SigLIP processor (resize + center-crop +
    mean/std normalization at 224x224), which is exactly the input format
    SigLIP was trained with. YOLO used letterboxed 640x640 tensors — that
    preprocessing no longer applies.
  - The model weights are downloaded from the Hugging Face hub on first use and
    cached, instead of shipping a local weights file (yolov8n.pt was removed).
    The download uses the Python standard library (urllib) rather than the
    huggingface_hub client because some networks/proxies reject the hub client
    with 401 errors while plain HTTPS works fine.

Inference pipeline (zero-shot food recognition via embeddings):
  1. At model load, every food label is encoded once into its SigLIP text
     embedding (the "label database").
  2. Per request, only the image is encoded (fast); image-text similarity is
     image_embedding @ text_embeddings^T scaled by the model's logit scale
     plus its scalar logit bias, exactly matching the model's native logits.
  3. Per-label sigmoid(logit) yields a match probability that is independent of
     the label-set size — softmax would dilute every probability as more labels
     are added, making scores meaningless with 200+ labels.
  4. The top label is accepted only when it clears CONFIDENCE_THRESHOLD and
     beats the runner-up by CONFIDENCE_MARGIN. Otherwise the image is reported
     with zero detections (helpers translate that into a clear
     "no_detection" response instead of a wrong food prediction).
  5. Top predictions, similarity scores, and the final decision are logged for
     debugging.
"""
import io
import os
import shutil
import threading
import time
import urllib.request
from typing import Dict, Any, List, Tuple

from PIL import Image

# Canonical food labels. Names are chosen to double as USDA search terms so the
# nutrition mapper can look them up directly. The set is intentionally broad:
# fruits, vegetables, proteins, dishes from many cuisines, desserts, drinks,
# and packaged foods. Near-duplicate labels are avoided because they split the
# image-text similarity mass (e.g. "strawberry" + "strawberries" would both
# spike on one photo and break the margin check). Extra labels can be appended
# at runtime via the SIGLIP_EXTRA_LABELS env var (comma-separated).
FOOD_LABELS: List[str] = [
    # ---- Fruits ----
    "apple", "banana", "orange", "tangerine", "grapefruit", "lemon", "lime",
    "grapes", "strawberry", "blueberries", "raspberries", "blackberries",
    "cherries", "peach", "nectarine", "apricot", "plum", "pear", "fig",
    "dates", "mango", "papaya", "pineapple", "kiwi", "watermelon",
    "cantaloupe", "honeydew melon", "pomegranate", "cranberries", "coconut",
    "avocado", "persimmon", "lychee", "dragon fruit", "guava",
    "passion fruit", "star fruit", "jackfruit", "durian", "plantain",
    "raisins", "prunes", "dried apricots",
    # ---- Vegetables ----
    "broccoli", "cauliflower", "cabbage", "brussels sprouts", "carrot",
    "sweet potato", "potato", "yam", "beet", "radish", "turnip", "parsnip",
    "onion", "garlic", "ginger", "celery", "spinach", "kale", "lettuce",
    "arugula", "mixed salad greens", "cucumber", "tomato", "cherry tomatoes",
    "bell pepper", "chili pepper", "jalapeno pepper", "mushrooms",
    "zucchini", "eggplant", "pumpkin", "butternut squash", "acorn squash",
    "green beans", "snow peas", "peas", "corn", "asparagus", "artichoke",
    "okra", "leek", "scallion", "fennel", "bok choy", "swiss chard",
    "seaweed", "olives",
    # ---- Proteins: meat, poultry, seafood, eggs ----
    "chicken breast", "roasted chicken", "fried chicken", "grilled chicken",
    "chicken wings", "chicken nuggets", "turkey", "duck", "steak",
    "beef", "ground beef", "meatballs", "meatloaf", "ribs", "pulled pork",
    "pork chop", "bacon", "ham", "sausage", "chorizo", "salami", "pepperoni",
    "hot dog", "lamb chops", "salmon", "grilled salmon", "smoked salmon",
    "tuna", "canned tuna", "shrimp", "grilled shrimp", "crab", "lobster",
    "oysters", "mussels", "calamari", "fish and chips", "grilled fish",
    "fish sticks", "eggs", "scrambled eggs", "fried eggs", "boiled eggs",
    "omelet", "egg benedict", "quiche", "frittata",
    # ---- Dairy, cheese, and substitutes ----
    "milk", "chocolate milk", "almond milk", "soy milk", "cream",
    "sour cream", "whipped cream", "butter", "margarine", "cheese",
    "cheddar cheese", "mozzarella", "swiss cheese", "parmesan", "feta",
    "brie", "gouda", "provolone", "blue cheese", "goat cheese",
    "cream cheese", "cottage cheese", "ricotta", "yogurt", "greek yogurt",
    "fruit yogurt", "kefir",
    # ---- Nuts, seeds, legumes, plant proteins ----
    "almonds", "walnuts", "pecans", "cashews", "pistachios", "peanuts",
    "peanut butter", "almond butter", "hazelnuts", "macadamia nuts",
    "sunflower seeds", "pumpkin seeds", "chia seeds", "flax seeds",
    "sesame seeds", "tahini", "beans", "black beans", "kidney beans",
    "pinto beans", "chickpeas", "lentils", "edamame", "tofu", "tempeh",
    "hummus",
    # ---- Grains, breads, and breakfast ----
    "rice", "brown rice", "fried rice", "quinoa", "couscous", "barley",
    "oats", "oatmeal", "grits", "polenta", "cereal", "granola",
    "granola bar", "bread", "white bread", "whole wheat bread", "baguette",
    "sourdough bread", "rye bread", "pumpernickel", "cornbread", "biscuit",
    "scone", "croissant", "bagel", "english muffin", "muffin",
    "banana bread", "garlic bread", "focaccia", "ciabatta", "tortilla",
    "pita bread", "naan", "crackers", "pretzels", "popcorn", "potato chips",
    "tortilla chips", "corn chips", "pita chips", "cheese puffs",
    "pancakes", "waffles", "french toast", "crepes",
    # ---- Dishes and cuisines ----
    "pizza", "hamburger", "cheeseburger", "sandwich",
    "grilled cheese sandwich", "panini", "quesadilla", "taco", "burrito",
    "enchilada", "fajita", "nachos", "chili con carne", "lasagna",
    "spaghetti", "fettuccine alfredo", "penne pasta", "macaroni and cheese",
    "ravioli", "gnocchi", "tortellini", "risotto", "paella", "biryani",
    "curry", "butter chicken", "chicken tikka masala", "samosa", "pho",
    "ramen", "udon noodles", "pad thai", "spring rolls", "egg rolls",
    "dumplings", "wontons", "dim sum", "sushi", "sushi roll", "sashimi",
    "tempura", "teriyaki chicken", "stir fry", "kung pao chicken",
    "sweet and sour pork", "chow mein", "lo mein", "potstickers",
    "bao bun", "gyoza", "bibimbap", "kimchi", "miso soup", "tom yum",
    "satay", "kebab", "shawarma", "falafel", "tabbouleh", "dolma", "gyro",
    "souvlaki", "shepherd's pie", "baked potato", "mashed potatoes",
    "french fries", "hash browns", "potato salad", "coleslaw",
    "caesar salad", "greek salad", "caprese salad", "fruit salad",
    "garden salad", "quinoa salad", "beef stew", "beef soup",
    "chicken soup", "chicken noodle soup", "tomato soup",
    "cream of mushroom soup", "vegetable soup", "lentil soup",
    "minestrone", "congee", "porridge", "tater tots", "onion rings",
    "mozzarella sticks", "veggie burger",
    # ---- Desserts and sweets ----
    "cake", "chocolate cake", "cheesecake", "cupcake", "brownie", "cookie",
    "chocolate chip cookie", "macaron", "donut", "danish", "pastry", "pie",
    "apple pie", "pumpkin pie", "pecan pie", "tart", "lemon tart",
    "custard", "pudding", "mousse", "tiramisu", "baklava", "churro",
    "cannoli", "flan", "creme brulee", "ice cream", "gelato",
    "frozen yogurt", "sorbet", "popsicle", "candy", "chocolate bar",
    "dark chocolate", "lollipop", "gummy bears", "jelly beans",
    "marshmallow", "caramel", "fudge", "truffle", "nougat", "marzipan",
    "jam", "jelly", "marmalade", "honey", "maple syrup",
    # ---- Beverages ----
    "coffee", "cappuccino", "latte", "espresso", "tea", "green tea",
    "black tea", "herbal tea", "iced tea", "orange juice", "apple juice",
    "grape juice", "cranberry juice", "lemonade", "cola", "root beer",
    "energy drink", "sports drink", "milkshake", "smoothie",
    "hot chocolate", "boba tea",
    # ---- Packaged and convenience foods ----
    "instant noodles", "canned soup", "canned beans", "canned corn",
    "canned vegetables", "frozen pizza", "frozen vegetables",
    "beef jerky", "dried fruit", "trail mix", "protein bar", "energy bar",
]

# One more prompt template: full phrases match SigLIP's alt-text training data
# better than bare label words.
PROMPT_TEMPLATE = "a photo of {label}"

# Files required to load the SigLIP model + processor locally.
MODEL_FILES: List[str] = [
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "spiece.model",
]

# Config defaults (overridable via env)
MODEL_NAME = "google/siglip-base-patch16-224"
# sigmoid(logit) is the per-label match probability, so scores stay in [0, 1]
# regardless of how many labels are in the set. Two criteria guard against
# false positives (photos of mountains, animals, rooms, etc. score near 0 and
# stay well below real-food scores):
#   - the top label must clear CONFIDENCE_THRESHOLD, and
#   - it must beat the runner-up by at least CONFIDENCE_MARGIN.
# Calibrated on the model's native logits (logit_scale * cosine + logit_bias,
# see detect()): clear food photos score >= ~0.056 with top-2 gaps >= ~0.037,
# while non-food photos top out at ~0.019 with gaps of ~0.007.
CONFIDENCE_THRESHOLD = 0.05
CONFIDENCE_MARGIN = 0.03
MAX_DETECTIONS = 10
TOP_K_LOG = 5


def _model_cache_dir() -> str:
    base = os.environ.get("SIGLIP_CACHE_DIR")
    if not base:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "siglip_models")
    return os.path.join(base, MODEL_NAME.replace("/", "__"))


class SigLIPDetector:
    _model = None
    _processor = None
    _text_cache: Tuple[Tuple[str, ...], Any] = None
    _load_lock = threading.Lock()

    @classmethod
    def get_model_name(cls) -> str:
        return os.environ.get("SIGLIP_MODEL", MODEL_NAME)

    @classmethod
    def get_confidence_threshold(cls) -> float:
        return float(os.environ.get("SIGLIP_CONFIDENCE_THRESHOLD", str(CONFIDENCE_THRESHOLD)))

    @classmethod
    def get_confidence_margin(cls) -> float:
        return float(os.environ.get("SIGLIP_CONFIDENCE_MARGIN", str(CONFIDENCE_MARGIN)))

    @classmethod
    def get_max_detections(cls) -> int:
        return int(os.environ.get("SIGLIP_MAX_DETECTIONS", str(MAX_DETECTIONS)))

    @classmethod
    def get_labels(cls) -> List[str]:
        """Dynamic label set: curated base list + any SIGLIP_EXTRA_LABELS."""
        labels = list(FOOD_LABELS)
        extra = os.environ.get("SIGLIP_EXTRA_LABELS", "")
        for item in extra.split(","):
            item = item.strip()
            if item and item not in labels:
                labels.append(item)
        return labels

    @classmethod
    def _build_prompts(cls, labels: List[str]) -> List[str]:
        """Text prompts sent to SigLIP: full phrases beat bare words here."""
        return [PROMPT_TEMPLATE.format(label=label) for label in labels]

    @classmethod
    def is_available(cls) -> bool:
        try:
            import transformers  # noqa: F401
            return True
        except ImportError:
            return False

    @classmethod
    def _is_valid_safetensors(cls, path: str) -> bool:
        """Check if a .safetensors file has a complete header and data."""
        try:
            size = os.path.getsize(path)
            if size < 8:
                return False
            with open(path, "rb") as f:
                header_len = int.from_bytes(f.read(8), "little")
                if header_len <= 0 or header_len > 100 * 1024 * 1024:
                    return False
                header = f.read(header_len)
                if len(header) != header_len:
                    return False
                import json
                header_json = json.loads(header)
                # Validate data_offsets cover the file
                max_offset = 0
                for v in header_json.values():
                    if isinstance(v, dict) and "data_offsets" in v:
                        try:
                            end = int(v["data_offsets"][1])
                            if end > max_offset:
                                max_offset = end
                        except Exception:
                            pass
                # File must be exactly header + max_offset, or at least that large
                if max_offset > 0 and size < 8 + header_len + max_offset:
                    return False
                if size < 8 + header_len:
                    return False
            return True
        except Exception:
            return False

    @classmethod
    def _is_valid_file(cls, path: str, filename: str) -> bool:
        """Validate cached file — for safetensors use header check, else size>0."""
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        if filename == "model.safetensors":
            return cls._is_valid_safetensors(path)
        # For other files, ensure they are valid JSON/text and not truncated
        try:
            if filename.endswith(".json"):
                import json
                with open(path, "r", encoding="utf-8") as f:
                    json.load(f)
            return True
        except Exception:
            return False

    @classmethod
    def _download_if_missing(cls) -> str:
        """Download the SigLIP model files to a local cache dir via plain HTTPS.

        Differs from the YOLO setup, where the weights file was committed to the
        repo (models/yolov8n.pt). SigLIP weights are too large to ship with the
        repo, so they are fetched once per environment and cached on disk.

        Handles corrupted/partial downloads (Railway ephemeral FS, concurrent
        workers, healthcheck kills) by validating existing files and using
        atomic .part writes with file locking.
        """
        cache_dir = _model_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        for filename in MODEL_FILES:
            dest = os.path.join(cache_dir, filename)
            # Validate existing file — if corrupted, delete and redownload
            if os.path.exists(dest):
                if cls._is_valid_file(dest, filename):
                    continue
                print(f"[SigLIP] cached {filename} is corrupted/incomplete (size={os.path.getsize(dest)}), removing and redownloading")
                try:
                    os.remove(dest)
                except Exception:
                    pass
            # Also clean up any stale .part from previous interrupted download
            tmp = dest + ".part"
            lock = dest + ".lock"
            # Simple file lock for concurrent workers (Railway may run 2+ workers)
            lock_fd = None
            try:
                # Try to acquire lock (non-blocking)
                try:
                    lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                except FileExistsError:
                    # Another worker is downloading — wait for it
                    for _ in range(300):  # up to 300s
                        time.sleep(1)
                        if os.path.exists(dest) and cls._is_valid_file(dest, filename):
                            break
                        if not os.path.exists(lock):
                            break
                    if os.path.exists(dest) and cls._is_valid_file(dest, filename):
                        continue
                    # If still no valid file, try to acquire lock again
                    try:
                        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    except FileExistsError:
                        continue
                url = f"https://huggingface.co/{MODEL_NAME}/resolve/main/{filename}"
                request = urllib.request.Request(url, headers={"User-Agent": "BiteWise/1.0 (SigLIP food scanner)"})
                try:
                    with urllib.request.urlopen(request, timeout=600) as resp, open(tmp, "wb") as out:
                        # If server provides Content-Length, we can verify
                        expected = resp.headers.get("Content-Length")
                        shutil.copyfileobj(resp, out)
                        out.flush()
                        os.fsync(out.fileno())
                    # Verify downloaded file
                    if not cls._is_valid_file(tmp, filename):
                        raise RuntimeError(f"Downloaded {filename} is still invalid after download")
                    if expected:
                        try:
                            if os.path.getsize(tmp) != int(expected):
                                raise RuntimeError(f"Size mismatch for {filename}: got {os.path.getsize(tmp)}, expected {expected}")
                        except ValueError:
                            pass
                    os.replace(tmp, dest)
                    print(f"[SigLIP] downloaded {filename} ({os.path.getsize(dest)} bytes)")
                except Exception as e:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
                    raise RuntimeError(f"Failed to download SigLIP file '{filename}' from Hugging Face: {e}")
            finally:
                if lock_fd is not None:
                    try:
                        os.close(lock_fd)
                        os.remove(lock)
                    except Exception:
                        pass
        return cache_dir

    @classmethod
    def load_model(cls):
        if cls._model is not None:
            return cls._model, cls._processor
        with cls._load_lock:
            if cls._model is not None:
                return cls._model, cls._processor
            import torch
            from transformers import AutoModel, AutoProcessor
            # Constrain CPU threads to the container's vCPU count (Railway caps
            # memory at 1GB; oversized thread pools add memory pressure).
            torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
            # Try loading, and if safetensors is corrupted, delete and redownload once
            for attempt in range(2):
                try:
                    cache_dir = cls._download_if_missing()
                    processor = AutoProcessor.from_pretrained(cache_dir)
                    model = AutoModel.from_pretrained(cache_dir)
                    model.eval()
                    cls._model = model
                    cls._processor = processor
                    # Pre-encode the food label prompts once; per-request inference then
                    # only encodes the image (a ~10x latency win with 200+ labels).
                    with torch.no_grad():
                        cls._text_cache = cls._load_or_encode_labels(model, processor, cache_dir)
                    if attempt > 0:
                        print(f"[SigLIP] model loaded successfully after redownload (attempt {attempt+1})")
                    return cls._model, cls._processor
                except Exception as e:
                    msg = str(e)
                    is_corrupted = any(x in msg for x in ["incomplete metadata", "file not fully covered", "deserializing header", "safetensors", "truncated"])
                    if is_corrupted and attempt == 0:
                        print(f"[SigLIP] detected corrupted cache ({e}), clearing and redownloading...")
                        # Remove corrupted files
                        try:
                            cache_dir = _model_cache_dir()
                            for fn in MODEL_FILES:
                                p = os.path.join(cache_dir, fn)
                                if os.path.exists(p):
                                    # Only remove safetensors and related if corrupted
                                    if fn == "model.safetensors" or not cls._is_valid_file(p, fn):
                                        try:
                                            os.remove(p)
                                            print(f"[SigLIP] removed corrupted {fn}")
                                        except Exception:
                                            pass
                                # Also clean .part and .lock
                                for suffix in [".part", ".lock"]:
                                    pp = p + suffix
                                    if os.path.exists(pp):
                                        try:
                                            os.remove(pp)
                                        except Exception:
                                            pass
                        except Exception as ce:
                            print(f"[SigLIP] failed to clean corrupted cache: {ce}")
                        continue
                    raise

    @classmethod
    def _load_or_encode_labels(cls, model, processor, cache_dir: str) -> Tuple[Tuple[str, ...], Any]:
        """Load cached label embeddings from disk, or encode (and cache) them.

        Encoding 408 prompts takes ~35s on CPU; on ephemeral containers that
        window stalls /health and gets the instance killed. Persisting the
        embeddings (e.g. on a mounted volume) makes warmup fast and restart-safe.
        """
        import torch
        labels = cls.get_labels()
        emb_path = os.path.join(cache_dir, "text_embeddings.pt")
        if os.path.exists(emb_path):
            try:
                state = torch.load(emb_path, weights_only=True, map_location="cpu")
                if tuple(state["labels"]) == tuple(labels):
                    return tuple(labels), state["emb"]
            except Exception as e:  # noqa: BLE001
                print(f"[SigLIP] embedding cache unreadable ({e}); re-encoding")
        cache = cls._encode_labels(model, processor, labels)
        try:
            torch.save({"labels": tuple(labels), "emb": cache[1]}, emb_path)
        except Exception as e:  # noqa: BLE001
            print(f"[SigLIP] could not persist embedding cache ({e})")
        return cache

    @classmethod
    def _encode_labels(cls, model, processor, labels: List[str] = None) -> Tuple[Tuple[str, ...], Any]:
        """L2-normalized text embeddings for every label (the label database).

        Prompts are encoded in chunks with small sleeps between them so the
        worker thread releases the GIL periodically — a single long forward
        pass would stall /health probes long enough for the platform to kill
        the instance.
        """
        if labels is None:
            labels = cls.get_labels()
        import torch
        prompts = cls._build_prompts(labels)
        chunk_size = 64
        embs = []
        with torch.no_grad():
            for start in range(0, len(prompts), chunk_size):
                chunk = prompts[start:start + chunk_size]
                inputs = processor(text=chunk, padding="max_length", return_tensors="pt")
                chunk_emb = model.get_text_features(**inputs)
                chunk_emb = chunk_emb / chunk_emb.norm(dim=-1, keepdim=True)
                embs.append(chunk_emb)
                time.sleep(0.1)
        return tuple(labels), torch.cat(embs, dim=0)

    @classmethod
    def _get_text_embeddings(cls, model, processor):
        """Return cached text embeddings, re-encoding only if the label set changed."""
        labels = cls.get_labels()
        if cls._text_cache is None or cls._text_cache[0] != tuple(labels):
            cls._text_cache = cls._encode_labels(model, processor, labels)
        return cls._text_cache[1]

    @classmethod
    def detect(cls, image_bytes: bytes) -> Dict[str, Any]:
        if not cls.is_available():
            return {
                "status": "no_transformers",
                "detections": [],
                "message": "transformers package is not available. Install transformers to use the SigLIP model.",
            }
        if not image_bytes or len(image_bytes) < 32:
            return {"status": "error", "detections": [], "message": "Invalid image: file is empty or too small."}
        try:
            model, processor = cls.load_model()
        except Exception as e:
            return {
                "status": "model_not_found",
                "detections": [],
                "message": f"Failed to load SigLIP model '{cls.get_model_name()}': {e}",
            }
        try:
            import torch

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

            labels = cls.get_labels()

            # SigLIP preprocessing: the processor applies resize + center-crop +
            # mean/std normalization to the exact 224x224 format the model
            # expects. (Unlike YOLO, there is no letterboxing or 640x640 resize.)
            image_inputs = processor(images=image, return_tensors="pt")

            with torch.no_grad():
                image_emb = model.get_image_features(**image_inputs)
                image_emb = image_emb / image_emb.norm(dim=-1, keepdim=True)
                text_emb = cls._get_text_embeddings(model, processor)
                # Image-text similarity: sigmoid(logit_scale * cos_sim +
                # logit_bias) is the per-label match probability, independent
                # of label-set size (softmax would dilute scores as labels are
                # added, so it is not used here). The scalar logit_bias is
                # part of the model's native logits; skipping it saturates
                # every probability near 1.0 and breaks the margin check.
                logits = model.logit_scale.exp() * (image_emb @ text_emb.T) + model.logit_bias
            probs = torch.sigmoid(logits[0]).cpu().numpy().astype(float)

            threshold = cls.get_confidence_threshold()
            margin = cls.get_confidence_margin()
            max_det = cls.get_max_detections()
            top_k_log = TOP_K_LOG

            order = probs.argsort()[::-1]

            # Debug logging: top predicted labels with their similarity scores.
            top_bits = ", ".join(
                f"{labels[int(i)]}={probs[int(i)]:.3f}" for i in order[:top_k_log]
            )
            print(f"[SigLIP] top-{top_k_log} predictions: {top_bits}")

            # The top label must clear both the absolute threshold and the
            # margin over the runner-up before anything is reported; otherwise
            # this is a clear "no food" outcome rather than a wrong guess.
            top1_idx, top2_idx = int(order[0]), int(order[1])
            top1, top2 = float(probs[top1_idx]), float(probs[top2_idx])
            if top1 < threshold or (top1 - top2) < margin:
                print(
                    f"[SigLIP] no food detected (top1={labels[top1_idx]}={top1:.3f} "
                    f"< threshold={threshold} or margin={top1 - top2:.3f} < {margin})"
                )
                return {
                    "status": "success",
                    "detections": [],
                    "foodDetections": [],
                    "totalDetected": 0,
                    "totalFoodItems": 0,
                    "topPredictions": [
                        {"className": labels[int(i)], "confidence": round(float(probs[int(i)]), 3)}
                        for i in order[:top_k_log]
                    ],
                }

            # Additional labels must clear the absolute threshold AND stay
            # within the margin band of the winner. Without the band,
            # semantically-near duplicates (e.g. "ice cream" vs "gelato")
            # would be reported as separate foods and double-count nutrition.
            detections = []
            for i in order:
                idx = int(i)
                prob = float(probs[idx])
                if prob >= threshold and prob >= top1 - margin:
                    detections.append({
                        "classId": idx,
                        "className": labels[idx],
                        "confidence": round(prob, 3),
                        "isFood": True,
                    })
            detections = detections[:max_det]

            print(
                f"[SigLIP] selected {len(detections)} detection(s): "
                + ", ".join(f"{d['className']}={d['confidence']}" for d in detections)
            )

            return {
                "status": "success",
                "detections": detections,
                "foodDetections": detections,
                "totalDetected": len(detections),
                "totalFoodItems": len(detections),
                "topPredictions": [
                    {"className": labels[int(i)], "confidence": round(float(probs[int(i)]), 3)}
                    for i in order[:top_k_log]
                ],
            }
        except Exception as e:
            return {
                "status": "error",
                "detections": [],
                "message": str(e),
            }

    @classmethod
    def unload(cls):
        cls._model = None
        cls._processor = None
        cls._text_cache = None
