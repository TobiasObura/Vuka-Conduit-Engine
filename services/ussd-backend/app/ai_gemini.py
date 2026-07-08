"""
Shared Gemini client used by all four AI modules (ai_language, ai_risk,
ai_corridors, ai_treasury).

Goes through the Replit AI Integrations proxy rather than calling
generativelanguage.googleapis.com directly:
  {AI_INTEGRATIONS_GEMINI_BASE_URL}/models/{model}:generateContent?key={key}
Note: no /v1beta/ prefix on the path -- the Replit proxy strips it internally.

SIMULATION_MODE_GEMINI is the single source of truth for whether calls here
are real. Every module that uses this client checks it explicitly and falls
back to a clearly-logged simulation rather than guessing or fabricating a
response when it's True.
"""
import logging
import os

import requests

logger = logging.getLogger("vuka.ai_gemini")

AI_INTEGRATIONS_GEMINI_BASE_URL = os.environ.get("AI_INTEGRATIONS_GEMINI_BASE_URL")
AI_INTEGRATIONS_GEMINI_API_KEY = os.environ.get("AI_INTEGRATIONS_GEMINI_API_KEY")

SIMULATION_MODE_GEMINI = not (AI_INTEGRATIONS_GEMINI_BASE_URL and AI_INTEGRATIONS_GEMINI_API_KEY)

if SIMULATION_MODE_GEMINI:
    logger.warning(
        "ai_gemini: SIMULATION MODE -- AI_INTEGRATIONS_GEMINI_BASE_URL / "
        "AI_INTEGRATIONS_GEMINI_API_KEY not set. All four AI modules will use their "
        "clearly-logged simulated fallbacks."
    )

DEFAULT_MODEL = "gemini-2.0-flash"


class GeminiError(Exception):
    pass


def generate_content(prompt: str, system_instruction: str = None, model: str = DEFAULT_MODEL,
                      response_mime_type: str = None, temperature: float = 0.3) -> str:
    """Raises GeminiError on any failure or if simulation mode is active --
    callers are expected to catch this and use their own simulated fallback
    rather than this module guessing on their behalf."""
    if SIMULATION_MODE_GEMINI:
        raise GeminiError("Gemini not configured (simulation mode)")

    url = f"{AI_INTEGRATIONS_GEMINI_BASE_URL}/models/{model}:generateContent?key={AI_INTEGRATIONS_GEMINI_API_KEY}"

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    if response_mime_type:
        body["generationConfig"]["responseMimeType"] = response_mime_type

    try:
        resp = requests.post(url, json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise GeminiError(f"No candidates in Gemini response: {data}")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            raise GeminiError(f"Empty text in Gemini response: {data}")
        return text
    except requests.RequestException as e:
        raise GeminiError(f"Gemini request failed: {e}") from e
