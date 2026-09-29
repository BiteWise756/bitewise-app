import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from utils.auth import require_doctor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("doctor_ai")

router = APIRouter(prefix="/doctor", tags=["Doctor AI"])

class DoctorAnalysisRequest(BaseModel):
    text: str

DOCTOR_SYSTEM_PROMPT = (
    "You are a medical AI assistant for doctors. Your role is to analyze patient examination "
    "results and provide clinical insights.\n\n"
    "For each case, provide a structured analysis with these sections:\n"
    "1. **Possible Diagnosis** — What the patient may have. Use cautious language like "
    '"may indicate", "possible", "consider".\n'
    "2. **Explanation** — Brief description of the suspected condition.\n"
    "3. **Possible Causes** — Common etiologies and risk factors.\n"
    "4. **Recommended Next Steps** — Suggested diagnostic tests, referrals, or follow-ups.\n"
    "5. **Possible Treatment Approaches** — Common management strategies.\n"
    "6. **Possible Medications** — Commonly used medications (if applicable).\n\n"
    "IMPORTANT RULES:\n"
    "- Never claim 100% certainty. Use phrases like 'possible', 'may indicate', 'suggests', 'consider'.\n"
    '- Include this disclaimer: "This AI assistant provides medical suggestions and information only. '
    "It does not replace a licensed doctor's diagnosis. Doctors must verify all diagnoses and "
    'medications before making clinical decisions."\n'
    "- If you are not confident about the diagnosis, clearly state your uncertainty and recommend "
    "more tests or specialist consultation.\n"
    "- If you do not know the answer, respond with 'UNSURE:' followed by what information would help.\n"
    "- Do NOT provide absolute medical decisions.\n"
    "- Recommend that doctors verify all findings before prescribing."
)


@router.post("/analyze")
async def analyze_patient_case(request: DoctorAnalysisRequest, current_user: dict = Depends(require_doctor)):
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Patient examination results are required.")

    try:
        from services.gemini_service import _call_gemini

        result = _call_gemini(text, system_prompt=DOCTOR_SYSTEM_PROMPT, max_output_tokens=4096)

        if not result:
            return {
                "success": False,
                "error": "AI analysis is currently unavailable. Please try again later.",
                "unsure": True,
            }

        unsure = result.startswith("UNSURE:") or ("i don't know" in result.lower() and len(result) < 100)

        return {
            "success": True,
            "data": {
                "text": result,
                "unsure": unsure,
            },
        }

    except Exception as e:
        logger.error("Doctor AI analysis failed: %s", str(e))
        return {
            "success": False,
            "error": "AI analysis is temporarily unavailable. Please try again later.",
            "unsure": True,
        }


@router.get("/profile")
async def doctor_profile(uid: Optional[str] = None, current_user: dict = Depends(require_doctor)):
    """Own profile for the authenticated doctor (used by the dashboard)."""
    if uid and uid != current_user["uid"]:
        raise HTTPException(status_code=404, detail="Profile not found")
    from services.auth_service import AuthService
    profile = AuthService.get_profile(current_user["uid"])
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"success": True, "data": profile}


@router.get("/health")
async def doctor_health():
    from services.gemini_service import is_ready, GEMINI_MODEL
    ready = is_ready()
    return {
        "status": "configured" if ready else "unconfigured",
        "ai_provider": "gemini",
        "gemini_ready": ready,
        "model": GEMINI_MODEL,
    }
