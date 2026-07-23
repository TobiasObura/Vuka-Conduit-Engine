"""
Corridor intelligence resolver.

The 4 launch corridors (Uganda, Tanzania, Rwanda, Ghana) are Vuka's own
static config (config.CORRIDORS) -- always available, never touches Gemini.

Beyond that, EXPANSION_MARKETS below is a static, factual table of country ->
currency (real ISO 4217 codes) for other pan-African markets reachable via
the Ecobank network -- this is public, unchanging information and doesn't
need an AI call either. It's a starting subset, not the full claimed
35-country list (assembling and verifying all 35 against current Ecobank/
Thunes coverage is real due-diligence work, not something to fabricate here).

What Gemini adds on top, for expansion markets, is qualitative "corridor
intelligence" -- feasibility notes, typical use cases, things worth knowing
before enabling a corridor -- which is inherently a judgment call rather
than a hard fact, so it's the one part that's explicitly marked simulated
when Gemini isn't configured, rather than inventing regulatory commentary.
Results are cached in-process per country for the life of the process.
"""
import logging

from . import ai_gemini, config

logger = logging.getLogger("vuka.ai_corridors")

# Real ISO 4217 currency codes for a starting set of expansion markets reachable
# via Ecobank's pan-African network. Not exhaustive of all 35 claimed markets.
EXPANSION_MARKETS = {
    "NIGERIA": "NGN",
    "COTE D'IVOIRE": "XOF",
    "BURKINA FASO": "XOF",
    "GUINEA-BISSAU": "XOF",
    "DR CONGO": "CDF",
    "REPUBLIC OF CONGO": "XAF",
    "CENTRAL AFRICAN REPUBLIC": "XAF",
    "EQUATORIAL GUINEA": "XAF",
    "ZAMBIA": "ZMW",
    "MALAWI": "MWK",
    "SIERRA LEONE": "SLE",
    "LIBERIA": "LRD",
    "GUINEA": "GNF",
    "GAMBIA": "GMD",
    "CAPE VERDE": "CVE",
    "SOUTH SUDAN": "SSP",
    "BURUNDI": "BIF",
}

_cache = {}  # normalized country name -> resolved dict


def resolve_corridor(country_name: str) -> dict:
    key = country_name.strip().upper()

    for corridor_code, meta in config.CORRIDORS.items():
        if meta["country"].upper() == key:
            return {
                "tier": "core",
                "country": meta["country"],
                "currency": meta["currency"],
                "corridor_code": corridor_code,
                "notes": None,
                "simulated": False,
            }

    if key not in EXPANSION_MARKETS:
        return {"tier": "unknown", "country": country_name, "currency": None,
                 "corridor_code": None, "notes": None, "simulated": True,
                 "warning": "Not in Vuka's core corridors or known expansion markets list."}

    if key in _cache:
        return _cache[key]

    result = {
        "tier": "expansion",
        "country": key.title(),
        "currency": EXPANSION_MARKETS[key],
        "corridor_code": None,
        "notes": None,
        "simulated": True,
    }

    if not ai_gemini.SIMULATION_MODE_GEMINI:
        try:
            result["notes"] = _get_feasibility_notes(key, EXPANSION_MARKETS[key])
            result["simulated"] = False
        except ai_gemini.GeminiError:
            logger.warning("ai_corridors: Gemini feasibility lookup failed for %s", key)

    _cache[key] = result
    return result


def _get_feasibility_notes(country: str, currency: str) -> str:
    prompt = (
        f"In 2-3 sentences, give a pan-African remittance operator a brief, factual "
        f"overview of what's generally worth knowing about enabling a mobile-money "
        f"remittance payout corridor into {country.title()} (currency: {currency}) via "
        f"the Ecobank network -- e.g. typical mobile money providers, general regulatory "
        f"posture. Be concise and note if you're not certain of specifics rather than "
        f"guessing."
    )
    return ai_gemini.generate_content(prompt).strip()
