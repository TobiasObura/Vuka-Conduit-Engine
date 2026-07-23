"""
USSD translation across Vuka's pan-African language footprint.

EN and FR are hand-written and live directly in ussd_router.STRINGS -- the
two most common corridor languages, no translation call needed. Every other
language listed in ALL_LANGUAGES is translated by asking Gemini to translate
the fixed set of menu strings, once per language, cached in-process for the
life of the process.

Deliberately does NOT ship hand-written fallback translations for any
non-EN/FR language: guessing at translations for a real financial product
and presenting them as correct would be worse than being honest that they're
unavailable. When Gemini isn't configured (or a call fails), callers get the
English strings back with simulated=True rather than invented text in the
target language -- this is true whether there are 4 languages or 18.

Real constraint worth knowing: Arabic and Amharic use non-Latin scripts
(Arabic script, Ge'ez script respectively). Rendering those correctly over
USSD/SMS requires the carrier gateway and handset to support Unicode (UCS-2)
encoding rather than the default GSM-7 alphabet -- not universally supported
on older feature phones, and UCS-2 messages have a shorter per-segment
character limit. This isn't something ai_language.py can fix; it's a
handset/gateway capability question worth confirming per corridor before
relying on those two languages in production.
"""
import json
import logging

from . import ai_gemini
from .ussd_router import STRINGS as _BASE_STRINGS

logger = logging.getLogger("vuka.ai_language")

# (language_code, native/self-referential display name for the menu, full
# English name used in the Gemini translation prompt). Display names are
# kept ASCII-safe (no accents/diacritics) since USSD gateways commonly use
# the GSM-7 alphabet, which doesn't support them reliably.
ALL_LANGUAGES = [
    ("EN", "English", "English"),
    ("FR", "Francais", "French"),
    ("SW", "Kiswahili", "Swahili"),
    ("HA", "Hausa", "Hausa"),
    ("AM", "Amharic", "Amharic"),
    ("YO", "Yoruba", "Yoruba"),
    ("ZU", "Zulu", "Zulu"),
    ("PT", "Portugues", "Portuguese"),
    ("TW", "Twi", "Twi"),
    ("RW", "Kinyarwanda", "Kinyarwanda"),
    ("IG", "Igbo", "Igbo"),
    ("XH", "Xhosa", "Xhosa"),
    ("WO", "Wolof", "Wolof"),
    ("LN", "Lingala", "Lingala"),
    ("SO", "Somali", "Somali"),
    ("AR", "Arabic", "Arabic"),
    ("AF", "Afrikaans", "Afrikaans"),
    ("SN", "Shona", "Shona"),
]

SUPPORTED_LANGUAGES = [code for code, _, _ in ALL_LANGUAGES]
_NATIVE_NAME = {code: full_name for code, _, full_name in ALL_LANGUAGES}
_MENU_NAME = {code: menu_name for code, menu_name, _ in ALL_LANGUAGES}

# USSD menus are digit-driven, so the language list is paginated rather than
# a single long numbered list -- a real, common USSD pattern. Each page shows
# at most 8 language slots (digits 1-8), reserving 9 for "more" and 0 for
# "back" so the nav digits never collide with a language choice.
PAGE_SIZE = 8


def get_page(page: int) -> list:
    """Returns the slice of ALL_LANGUAGES for this page (list of (code, menu_name))."""
    start = page * PAGE_SIZE
    return [(code, name) for code, name, _ in ALL_LANGUAGES[start:start + PAGE_SIZE]]


def total_pages() -> int:
    return (len(ALL_LANGUAGES) + PAGE_SIZE - 1) // PAGE_SIZE


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
    full_name = _NATIVE_NAME[language_code]
    try:
        prompt = (
            f"Translate the following USSD menu strings from English into "
            f"{full_name}. Keep numbered menu options "
            f"(e.g. '1. Convert & Transact') in the same numbered format, "
            f"just translate the option text. IMPORTANT: some strings contain "
            f"placeholder tokens in curly braces, like {{name}} or {{currency}} -- "
            f"copy these tokens EXACTLY as they appear, character for character, "
            f"do not translate or alter anything inside the curly braces. Reply "
            f"with ONLY a JSON object with the exact same keys as the input, "
            f"values being the translated strings, no other commentary:\n\n{json.dumps(base)}"
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
            full_name,
        )
        return {"strings": base, "simulated": True}
