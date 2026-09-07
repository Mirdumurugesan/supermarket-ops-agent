"""Configuration from environment (.env supported via python-dotenv).

Model selection is a **chain**, not a single provider. The store, the tools and
the control loop are all provider-agnostic; which model serves a turn — and
which one catches it when the first fails — is configuration.

Why a chain rather than one model:

* Groq's free tier is fast but metered at 6,000 tokens/minute. This agent's
  tool surface is ~4,300 tokens of schema per request, and a multi-item bill is
  several round-trips, so a busy minute *will* return HTTP 429.
* A 429 is a `ModelHTTPError`, which is exactly what the harness's
  `FallbackModel` is configured to fall over on. The next model in the chain
  picks the turn up mid-conversation and the owner never sees an error.

A model whose API key is missing is dropped from the chain at startup with a
warning rather than crashing, so the same `.env` works whether you have one
key or three.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ------------------------------------------------------------------ model chain
#
# Comma-separated, best first. Format is "<provider>:<model>".
#   groq      free, no card, very fast — the primary. 8k tokens/min per model.
#   google    free, no card, 1M tokens/min — the safety net under Groq's quota.
#   openai    paid — optional last resort; dropped automatically if unset.
#
# The order is deliberate. Groq is fastest, so it serves every turn it can, but
# this agent sends ~3.85k tokens of schema per request against an 8k/min budget
# — roughly every second request 429s. Groq meters *per model*, so the second
# link is another Groq model with its own budget; Gemini's far larger window
# then catches anything that gets past both. All three are free.
#
# Model IDs get retired, and the failure is a 404 at request time rather than
# at startup — this bit twice while building (Groq moved the Llama models to
# enterprise; Google closed gemini-2.5-flash to new keys). Both providers name
# the replacement in the error body, so read it before guessing. To list what
# your own keys can actually use:
#   curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"
#   curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GOOGLE_API_KEY"
DEFAULT_CHAIN = ("groq:openai/gpt-oss-120b,"
                 "groq:openai/gpt-oss-20b,"
                 "google:gemini-3.6-flash,"
                 "openai:gpt-4o-mini")

MODEL_CHAIN_RAW = os.environ.get("KIRANA_MODELS", DEFAULT_CHAIN)

# Which env var each provider's SDK reads, so we can drop unusable links and
# tell the owner exactly which key is missing.
PROVIDER_KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshotai": "MOONSHOTAI_API_KEY",
    "huggingface": "HF_TOKEN",
    "xai": "XAI_API_KEY",
}


def _parse_chain(raw: str) -> list[str]:
    return [m.strip() for m in raw.split(",") if m.strip()]


def resolve_model_chain() -> tuple[list[str], list[str]]:
    """Split the configured chain into (usable, skipped-for-missing-key)."""
    usable, skipped = [], []
    for model_id in _parse_chain(MODEL_CHAIN_RAW):
        provider = model_id.split(":", 1)[0]
        key_env = PROVIDER_KEY_ENV.get(provider)
        if key_env and not os.environ.get(key_env):
            skipped.append(f"{model_id} (no {key_env})")
        else:
            usable.append(model_id)
    return usable, skipped


MODEL_CHAIN, SKIPPED_MODELS = resolve_model_chain()

# Bounds one owner message: a multi-item bill needs ~8 model round-trips, so
# this is generous, but it stops a confused turn from spinning on a free tier.
MAX_STEPS_PER_TURN = int(os.environ.get("KIRANA_MAX_STEPS", "25"))

DB_PATH = os.environ.get("KIRANA_DB_PATH", "data/kirana.db")

# Comma-separated Telegram user ids allowed to use the bot; empty = open (demo).
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
}


def check_startup_config() -> None:
    """Fail fast with an actionable message rather than mid-conversation."""
    problems = []
    if not TELEGRAM_BOT_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN is not set — get one from @BotFather.")

    if not MODEL_CHAIN:
        wanted = _parse_chain(MODEL_CHAIN_RAW)
        needed = sorted({
            PROVIDER_KEY_ENV.get(m.split(":", 1)[0], "?") for m in wanted
        })
        problems.append(
            "No usable model in KIRANA_MODELS="
            f"'{MODEL_CHAIN_RAW}'. Set at least one of: {', '.join(needed)}. "
            "GROQ_API_KEY is free from console.groq.com."
        )

    if problems:
        raise SystemExit("Configuration problem:\n  - " + "\n  - ".join(problems))

    if SKIPPED_MODELS:
        log.warning("model chain: skipping %s", "; ".join(SKIPPED_MODELS))
    log.info("model chain: %s", " → ".join(MODEL_CHAIN))
