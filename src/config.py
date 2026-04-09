"""
Configuration for the hotel AI email agent.
All settings can be overridden via environment variables or .env file.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Execution Mode ────────────────────────────────────────────────────────────
# "human"      → Agent plans, then waits for human approval before writing to PMS
# "autonomous" → Agent plans and executes immediately (risky requests still escalate)
APPROVAL_MODE: str = os.getenv("APPROVAL_MODE", "autonomous")

# ── Mock "Today" ──────────────────────────────────────────────────────────────
# Injected into the system prompt so the LLM can reason about cancellation windows,
# "next week", etc. In production this would be datetime.date.today().
MOCK_CURRENT_DATE: str = os.getenv("MOCK_CURRENT_DATE", "2025-04-18")

# ── Data ──────────────────────────────────────────────────────────────────────
DATA_PATH: Path = Path(__file__).parent.parent / "data" / "mock_hotel_data.json"

# ── LLM ───────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "claude-haiku-4-5")
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2048"))
AGENT_MAX_ITERATIONS: int = int(os.getenv("AGENT_MAX_ITERATIONS", "10"))