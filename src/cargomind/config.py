"""Central settings for CargoMind. Everything is env-driven so judges can run
the whole system with zero keys (offline mode) or with real Google APIs."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(PACKAGE_DIR / ".env")  # adk web also reads this one

DATA_DIR = Path(os.getenv("CARGOMIND_DATA_DIR", ROOT_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("CARGOMIND_DB", DATA_DIR / "cargomind.db"))
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --- API keys (all optional) -------------------------------------------------
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
if GOOGLE_API_KEY and not os.getenv("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = GOOGLE_API_KEY
USE_VERTEX = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() in ("1", "true")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# --- Models (multi-model by design) ------------------------------------------
# Fast conversational / routing model
GEMINI_FAST = os.getenv("CARGOMIND_GEMINI_FAST", "gemini-3.5-flash")
# Deep reasoning model for planning + critic
GEMINI_PRO = os.getenv("CARGOMIND_GEMINI_PRO", "gemini-3.1-pro-preview")
# Multimodal vision model for proof-of-delivery inspection
GEMINI_VISION = os.getenv("CARGOMIND_GEMINI_VISION", GEMINI_FAST)
# Second vendor used as cross-provider fallback / independent critic (LiteLLM id)
CLAUDE_MODEL = os.getenv("CARGOMIND_CLAUDE_MODEL", "anthropic/claude-sonnet-5-5")
OPENAI_MODEL = os.getenv("CARGOMIND_OPENAI_MODEL", "openai/gpt-5-mini")

FORCE_OFFLINE = os.getenv("CARGOMIND_OFFLINE", "false").lower() in ("1", "true")

# --- Self-healing knobs -------------------------------------------------------
CHAOS_RATE = float(os.getenv("CARGOMIND_CHAOS", "0"))  # 0..1 fault injection
TOOL_MAX_RETRIES = int(os.getenv("CARGOMIND_TOOL_RETRIES", "2"))
BREAKER_THRESHOLD = int(os.getenv("CARGOMIND_BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN_S = float(os.getenv("CARGOMIND_BREAKER_COOLDOWN", "60"))
HTTP_TIMEOUT_S = float(os.getenv("CARGOMIND_HTTP_TIMEOUT", "8"))

APP_NAME = "cargomind"


def online_gemini() -> bool:
    return bool((GOOGLE_API_KEY or USE_VERTEX) and not FORCE_OFFLINE)


def mode_summary() -> dict:
    return {
        "gemini": online_gemini(),
        "claude": bool(ANTHROPIC_API_KEY and not FORCE_OFFLINE),
        "openai": bool(OPENAI_API_KEY and not FORCE_OFFLINE),
        "google_maps": bool(GOOGLE_MAPS_API_KEY and not FORCE_OFFLINE),
        "offline_fallback": True,
        "models": {"fast": GEMINI_FAST, "pro": GEMINI_PRO, "vision": GEMINI_VISION, "claude": CLAUDE_MODEL},
    }
