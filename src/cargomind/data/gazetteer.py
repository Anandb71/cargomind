"""Offline gazetteer of Indian logistics hubs.

Used everywhere a city name has to be normalised (tool-argument repair, route
estimates when map APIs are unavailable, free-text parsing of operator requests).
Pure Python, no I/O.
"""
from __future__ import annotations

import difflib
import math
import re
from collections.abc import Sequence

__all__ = [
    "ALIASES",
    "CITIES",
    "CODE_ALIASES",
    "coords",
    "find_cities_in_text",
    "haversine_km",
    "resolve_city",
]

# Canonical city name -> (lat, lon), WGS84, city-centre coordinates.
CITIES: dict[str, tuple[float, float]] = {
    "Mumbai": (19.0760, 72.8777),
    "Delhi": (28.6139, 77.2090),
    "Bengaluru": (12.9716, 77.5946),
    "Chennai": (13.0827, 80.2707),
    "Hyderabad": (17.3850, 78.4867),
    "Pune": (18.5204, 73.8567),
    "Kolkata": (22.5726, 88.3639),
    "Ahmedabad": (23.0225, 72.5714),
    "Jaipur": (26.9124, 75.7873),
    "Kochi": (9.9312, 76.2673),
    "Surat": (21.1702, 72.8311),
    "Lucknow": (26.8467, 80.9462),
    "Nagpur": (21.1458, 79.0882),
    "Indore": (22.7196, 75.8577),
    "Coimbatore": (11.0168, 76.9558),
    "Visakhapatnam": (17.6868, 83.2185),
    "Gurugram": (28.4595, 77.0266),
    "Noida": (28.5355, 77.3910),
    "Bhubaneswar": (20.2961, 85.8245),
    "Guwahati": (26.1445, 91.7362),
}

# Historic / alternate spellings (lower-case key -> canonical). Matched case-insensitively,
# both by resolve_city() and in free text.
ALIASES: dict[str, str] = {
    "bangalore": "Bengaluru",
    "bengalooru": "Bengaluru",
    "bombay": "Mumbai",
    "navi mumbai": "Mumbai",
    "bhiwandi": "Mumbai",
    "madras": "Chennai",
    "calcutta": "Kolkata",
    "gurgaon": "Gurugram",
    "new delhi": "Delhi",
    "delhi ncr": "Delhi",
    "ncr": "Delhi",
    "vizag": "Visakhapatnam",
    "vishakhapatnam": "Visakhapatnam",
    "vishakapatnam": "Visakhapatnam",
    "visakapatnam": "Visakhapatnam",
    "cochin": "Kochi",
    "ernakulam": "Kochi",
    "secunderabad": "Hyderabad",
    "cyberabad": "Hyderabad",
    "poona": "Pune",
    "amdavad": "Ahmedabad",
    "kovai": "Coimbatore",
    "gauhati": "Guwahati",
    "greater noida": "Noida",
    "lakhnau": "Lucknow",
}

# IATA-style codes (also used in warehouse IDs like WH-BOM). resolve_city() accepts them
# in any case; find_cities_in_text() only matches them in UPPER case so that ordinary words
# ("del", "maa", "amd") in prose are not mistaken for cities.
CODE_ALIASES: dict[str, str] = {
    "blr": "Bengaluru",
    "bom": "Mumbai",
    "del": "Delhi",
    "maa": "Chennai",
    "hyd": "Hyderabad",
    "ccu": "Kolkata",
    "pnq": "Pune",
    "amd": "Ahmedabad",
    "jai": "Jaipur",
    "cok": "Kochi",
    "stv": "Surat",
    "lko": "Lucknow",
    "nag": "Nagpur",
    "idr": "Indore",
    "cjb": "Coimbatore",
    "vtz": "Visakhapatnam",
    "bbi": "Bhubaneswar",
    "gau": "Guwahati",
}

_CANON_LOWER: dict[str, str] = {c.lower(): c for c in CITIES}
_ALL_KEYS: dict[str, str] = {**_CANON_LOWER, **ALIASES, **CODE_ALIASES}
_SUFFIX_RE = re.compile(r"[,(].*$")  # "Mumbai, Maharashtra" / "Pune (MH)" -> head only
_WS_RE = re.compile(r"\s+")


def _clean(name: str) -> str:
    s = _SUFFIX_RE.sub("", str(name)).strip().lower()
    s = re.sub(r"\b(city|india|district)\b", "", s)
    s = s.replace(".", " ").replace("_", " ").replace("-", " ")
    return _WS_RE.sub(" ", s).strip()


def resolve_city(name: str | None) -> str | None:
    """Normalise a city name, alias, IATA code or near-miss typo to its canonical name.

    Examples: "bangalore" -> "Bengaluru", "BOM" -> "Mumbai", "New Delhi, India" -> "Delhi",
    "WH-BLR" -> "Bengaluru", "Hyderbad" -> "Hyderabad". Returns None when nothing plausible
    matches.
    """
    if name is None:
        return None
    raw = str(name).strip()
    if not raw:
        return None
    # Warehouse-style IDs: WH-BOM -> bom
    m = re.fullmatch(r"(?i)wh[-_ ]?([a-z]{3})", raw)
    if m:
        return CODE_ALIASES.get(m.group(1).lower())
    s = _clean(raw)
    if not s:
        return None
    if s in _ALL_KEYS:
        return _ALL_KEYS[s]
    nospace = s.replace(" ", "")
    for key, canon in _ALL_KEYS.items():
        if key.replace(" ", "") == nospace:
            return canon
    # Typo tolerance (only for names, never 3-letter codes, to avoid false positives).
    if len(s) >= 4:
        candidates = list(_CANON_LOWER) + list(ALIASES)
        close = difflib.get_close_matches(s, candidates, n=1, cutoff=0.8)
        if close:
            return _ALL_KEYS[close[0]]
    return None


def _build_text_patterns() -> tuple[re.Pattern[str], re.Pattern[str]]:
    names = sorted(list(_CANON_LOWER) + list(ALIASES), key=len, reverse=True)
    name_alt = "|".join(r"\s+".join(map(re.escape, n.split())) for n in names)
    names_re = re.compile(rf"(?<![A-Za-z0-9])(?:{name_alt})(?![A-Za-z0-9])", re.IGNORECASE)
    codes_alt = "|".join(sorted((c.upper() for c in CODE_ALIASES), key=len, reverse=True))
    codes_re = re.compile(rf"(?<![A-Za-z0-9])(?:{codes_alt})(?![A-Za-z0-9])")
    return names_re, codes_re


_NAMES_RE, _CODES_RE = _build_text_patterns()


def find_cities_in_text(text: str | None) -> list[str]:
    """Return canonical cities mentioned in free text, ordered by first appearance, deduplicated.

    Aliases count ("ship from Bombay to Bangalore" -> ["Mumbai", "Bengaluru"]). Matching is on word
    boundaries; upper-case airport codes (BLR, BOM, DEL, ...) are recognised too.
    """
    if not text:
        return []
    hits: list[tuple[int, str]] = []
    for m in _NAMES_RE.finditer(text):
        key = _WS_RE.sub(" ", m.group(0).lower())
        hits.append((m.start(), _ALL_KEYS[key]))
    for m in _CODES_RE.finditer(text):
        hits.append((m.start(), CODE_ALIASES[m.group(0).lower()]))
    hits.sort(key=lambda h: h[0])
    out: list[str] = []
    for _, city in hits:
        if city not in out:
            out.append(city)
    return out


def coords(place: str | Sequence[float]) -> tuple[float, float]:
    """Coordinates for a city name/alias or a (lat, lon) pair. Raises ValueError if unknown."""
    if isinstance(place, str):
        city = resolve_city(place)
        if city is None:
            raise ValueError(f"Unknown city: {place!r}")
        return CITIES[city]
    if isinstance(place, Sequence) and len(place) == 2:
        lat, lon = float(place[0]), float(place[1])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"Invalid coordinates: {place!r}")
        return lat, lon
    raise ValueError(f"Expected a city name or (lat, lon) pair, got {place!r}")


def haversine_km(a: str | Sequence[float], b: str | Sequence[float]) -> float:
    """Great-circle distance in km between two cities (names/aliases) or (lat, lon) pairs.

    Raises ValueError for unknown cities. Road distance is typically 1.2-1.35x this value.
    """
    lat1, lon1 = coords(a)
    lat2, lon2 = coords(b)
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(h)), 1)
