"""Configuration loader for cam_enforcer_bot.

Reads environment variables (with optional .env support) and provides sane
defaults. Every value is optional — nothing is hard-required at import time,
so the bot can start in a "dry" state and be configured live via slash
commands or a populated .env.

All values are read once at import. Restart the bot after editing .env.

Environment variables
---------------------
DISCORD_TOKEN      : str   — bot token. Empty by default.
WATCHED_CHANNEL_IDS: str   — comma-separated channel IDs to enforce at startup.
GRACE_SECONDS      : int   — seconds a user has to turn their camera on (default 30).
WARNING_SECONDS    : int   — seconds before kick to send a warning DM (default 10).
EXEMPT_ROLE_IDS    : str   — comma-separated role IDs that are never kicked.
LOG_CHANNEL_ID     : int   — text channel ID to post kick logs to (0 = disabled).
"""
import os
from typing import List, Optional

# Load .env if present (python-dotenv is in requirements.txt)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional; plain env vars still work


def _comma_ints(val: str | None) -> List[int]:
    """Parse a comma-separated string of ints into a list.

    Empty/None input returns an empty list. Malformed tokens are skipped
    rather than raising, so a stray comma never crashes startup.
    """
    if not val:
        return []
    return [int(x.strip()) for x in val.split(",") if x.strip().isdigit()]


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to `default` on empty/invalid input."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --- All optional. Empty by default; configure via .env or slash commands. ---
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")

WATCHED_CHANNEL_IDS: List[int] = _comma_ints(os.getenv("WATCHED_CHANNEL_IDS"))

# Timeouts — fully adjustable via env or per-guild slash commands.
GRACE_SECONDS: int = _env_int("GRACE_SECONDS", 30)
WARNING_SECONDS: int = _env_int("WARNING_SECONDS", 10)

EXEMPT_ROLE_IDS: List[int] = _comma_ints(os.getenv("EXEMPT_ROLE_IDS"))

LOG_CHANNEL_ID: Optional[int] = _env_int("LOG_CHANNEL_ID", 0) or None
