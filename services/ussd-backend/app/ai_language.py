"""
6-language USSD translation: EN (source), FR, SW, HA, TW, RW.

EN and FR are hand-written and live directly in ussd_router.STRINGS -- they're
the two most common corridor languages and don't need a translation call.
This module handles the other four (Swahili, Hausa, Twi, Kinyarwanda) by
asking Gemini to translate the fixed set of menu strings, once per language,
cached in-process for the life of the process.

Deliberately does NOT ship hand-written fallback translations for SW/HA/TW/RW:
guessing at translations for a real financial product and presenting them as
correct would be worse than being honest that they're unavailable. When
Gemini isn't configured, callers get the English strings back with
simulated=True rather than invented text in the target language.
"""
import json
import logging

from . import ai_gemini
from .ussd_router import STRINGS as _BASE_STRINGS

logger = logging.getLogger("vuka.ai_language")

SUPPORTED_LANGUAGES = ["EN", "FR", "SW", "HA", "TW", "RW"]
_NATIVE_NAME = {
    "SW": "Swahili", "HA": "Hausa", "TW": "Twi", "RW": "Kinyarwanda",
}

_cache = {}  # language_code -> {"strings": dict, "simulated": bool}


def get_ussd_strings(language_code: str) -> dict:
    """Returns {"strings": {...same keys as ussd_router.STRINGS["EN"]...}, "simulated": bool}."""
    if language_code in ("EN", "FR"):
        return {"strings": _BASE_STRINGS[language_code], "simulated": False}

    if language_code not in _NATIVE_NAME:
        raise ValueError(f"Unsupported language: {language_code}")

    if language_code in _cache:
        return _cache[language_code]

    result = _translate_via_gemini(language_code)
    _cache[language_code] = result
    return result


def _translate_via_gemini(language_code: str) -> dict:
    base = _BASE_STRINGS["EN"]
    try:
        prompt = (
            f"Translate the following USSD menu strings from English into "
            f"{_NATIVE_NAME[language_code]}. Keep numbered menu options "
            f"(e.g. '1. Convert & Transact') in the same numbered format, "
            f"just translate the option text. Reply with ONLY a JSON object "
            f"with the exact same keys as the input, values being the "
            f"translated strings, no other commentary:\n\n{json.dumps(base)}"
        )
        raw = ai_gemini.generate_content(prompt, response_mime_type="application/json")
        translated = json.loads(raw)
        missing = set(base.keys()) - set(translated.keys())
        if missing:
            raise ValueError(f"Gemini translation missing keys: {missing}")
        logger.info("ai_language: translated USSD strings to %s via Gemini", language_code)
        return {"strings": translated, "simulated": False}
    except (ai_gemini.GeminiError, ValueError, json.JSONDecodeError):
        logger.warning(
            "ai_language: could not get a real %s translation -- falling back to English "
            "rather than guessing. Set AI_INTEGRATIONS_GEMINI_* to enable.",
            _NATIVE_NAME[language_code],
        )
        return {"strings": base, "simulated": True}
