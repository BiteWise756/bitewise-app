import os
import sys

# Ensure backend/ is on sys.path so internal imports work when
# uvicorn is invoked from the project root (Railway-style).
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from dotenv import load_dotenv

from firebase.config import init_firebase
from routes import (
    auth_router, meals_router, nutrition_router, chat_router,
    shopping_router, favorites_router, challenges_router,
    doctor_router, users_router, support_router, payments_router,
    unsplash_router,
)

load_dotenv()

FRONTEND_DIR = os.environ.get(
    "FRONTEND_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
)

# Allowed CORS origins. Defaults to permissive (comma-separated env var to override).
# "null" covers local file:// pages (browsers send Origin: null for them).
_default_origins = ["*", "null"]
_origins_env = os.environ.get("CORS_ORIGINS", "").strip()
_allowed_origins = (
    [o.strip() for o in _origins_env.split(",") if o.strip()]
    if _origins_env else _default_origins
)

app = FastAPI(
    title="NutriLens AI",
    description="AI-Powered Nutrition Assistant",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    """Initialize Firebase. Failure is non-fatal — DB-backed routes return 503."""
    try:
        init_firebase()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] Firebase initialization failed: {e}")
    print("NutriLens AI Backend Started")
    _start_siglip_warmup()
    _start_support_poller()


def _start_siglip_warmup():
    """Preload the SigLIP model in the background (download + label encoding).

    Without this, the first scan request downloads the ~800MB model
    synchronously, which blocks the server past Railway's request/healthcheck
    timeouts and triggers a restart loop. Warming up asynchronously keeps
    /health responsive; scans wait on the load lock until warmup finishes.
    """
    import threading

    def _warm():
        try:
            from services.siglip_detector import SigLIPDetector
            SigLIPDetector.load_model()
            print("[startup] SigLIP model loaded and warmed up")
        except Exception as e:  # noqa: BLE001
            print(f"[startup] SigLIP warmup failed: {e} (will retry on first scan)")

    threading.Thread(target=_warm, daemon=True).start()


def _start_support_poller():
    """Start the customer-support inbox poller (env-gated daemon thread).

    Enabled only when SUPPORT_POLL_ENABLED is true AND Gmail support
    credentials exist. The support inbox requires a dedicated refresh token
    with gmail.modify scope (GMAIL_SUPPORT_REFRESH_TOKEN). The OTP token
    (GMAIL_REFRESH_TOKEN) is gmail.send-only and cannot be used for reading.
    """
    import os
    import threading

    enabled = os.getenv("SUPPORT_POLL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        print("[startup] Support poller disabled (SUPPORT_POLL_ENABLED not true)")
        return
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    support_refresh_token = os.getenv("GMAIL_SUPPORT_REFRESH_TOKEN", "").strip()
    if not (client_id and client_secret and support_refresh_token):
        missing = []
        if not client_id:
            missing.append("GMAIL_CLIENT_ID")
        if not client_secret:
            missing.append("GMAIL_CLIENT_SECRET")
        if not support_refresh_token:
            missing.append("GMAIL_SUPPORT_REFRESH_TOKEN (must have gmail.modify scope)")
        print("[startup] Support poller not started: " + ", ".join(missing) + " missing")
        return

    interval = int(os.getenv("SUPPORT_POLL_INTERVAL_SECONDS", "60").strip() or "60")

    def _loop():
        from services.support_service import poll_loop
        try:
            poll_loop(interval_seconds=interval)
        except Exception as e:  # noqa: BLE001
            print(f"[startup] Support poller stopped: {type(e).__name__}: {e}")

    threading.Thread(target=_loop, daemon=True).start()
    print(f"[startup] Support poller started (interval={interval}s)")


app.include_router(auth_router)
app.include_router(meals_router)
app.include_router(nutrition_router)
app.include_router(chat_router)
app.include_router(shopping_router)
app.include_router(favorites_router)
app.include_router(challenges_router)
app.include_router(doctor_router)
app.include_router(users_router)
app.include_router(support_router)
app.include_router(payments_router)
app.include_router(unsplash_router)


@app.get("/")
async def root():
    """Redirect the site root to the sign-in page."""
    return RedirectResponse(url="/app/Sign.html", status_code=307)


@app.get("/health")
async def health():
    from firebase.config import firebase_app
    return {
        "status": "healthy",
        "firebase": "connected" if firebase_app else "not_initialized",
    }


# ---------- Static frontend hosting ----------
# Mount the frontend directory (if present) so a single backend deployment
# can serve both API and HTML. The catch-all below serves index.html / SPA routes.
if os.path.isdir(FRONTEND_DIR):
    app.mount(
        "/static",
        StaticFiles(directory=FRONTEND_DIR),
        name="static",
    )

    @app.get("/app/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve frontend HTML files under /app/. Falls back to index.html."""
        if full_path and not full_path.endswith(".html"):
            candidate = os.path.join(FRONTEND_DIR, full_path + ".html")
            if os.path.isfile(candidate):
                return FileResponse(candidate)
        file_path = os.path.join(FRONTEND_DIR, full_path)
        if full_path and os.path.isfile(file_path):
            return FileResponse(file_path)
        index = os.path.join(FRONTEND_DIR, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        return JSONResponse({"detail": "Frontend not found"}, status_code=404)

    # Root-level frontend routes so the premium page served at "/" resolves
    # its relative links (auth_routing.js, sibling .html pages, models data)
    # the same way it does under /app/. Sibling pages only; never exposes
    # backend/ or other directories (single path segment + basename guard).
    @app.get("/{page}.html")
    async def serve_root_html(page: str):
        name = os.path.basename(page + ".html")
        file_path = os.path.join(FRONTEND_DIR, name)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.get("/auth_routing.js")
    async def serve_root_auth_routing():
        file_path = os.path.join(FRONTEND_DIR, "auth_routing.js")
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.get("/models/models_data.json")
    async def serve_root_models_data():
        file_path = os.path.join(FRONTEND_DIR, "models", "models_data.json")
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return JSONResponse({"detail": "Not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
