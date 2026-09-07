"""The model chain: graceful degradation and real failover.

Groq's free tier is metered at 8,000 tokens/minute *per model* and this agent
sends ~3,850 tokens of tool schema per request, so HTTP 429 is an expected
operating condition, not an exceptional one. These tests pin the behaviours
that depend on it: a chain with a missing key still starts, a rate-limited
primary hands the turn to the next model without the owner seeing an error,
and an exhausted chain waits exactly as long as the provider asks.
"""

from __future__ import annotations

import importlib

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kirana import config


@pytest.fixture(autouse=True)
def _restore_config():
    """These tests reload a module-level config; put it back for everyone else."""
    yield
    importlib.reload(config)
    from kirana.agent import harness
    importlib.reload(harness)


def _reload_config(monkeypatch, **env):
    """Re-read config.py under a given environment.

    `config` calls `load_dotenv()` at import, so reloading it re-reads the
    developer's real `.env` off disk and quietly undoes the env we just set up.
    That made these tests pass on a machine with no `.env` and fail on one with
    it — the worst kind of flake. Stub the loader so the environment under test
    is exactly what this function declares, nothing more.
    """
    # Patch it on `dotenv` itself: reloading config re-executes
    # `from dotenv import load_dotenv`, which would rebind a stub set on
    # the config module and silently restore the real loader.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for k in ("GROQ_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
              "ANTHROPIC_API_KEY", "KIRANA_MODELS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(config)


# ----------------------------------------------------------- chain resolution

def test_full_chain_when_every_key_is_present(monkeypatch):
    cfg = _reload_config(monkeypatch, GROQ_API_KEY="g", GOOGLE_API_KEY="gg",
                         OPENAI_API_KEY="o")
    assert cfg.MODEL_CHAIN == ["groq:openai/gpt-oss-120b", "groq:openai/gpt-oss-20b",
                               "google:gemini-3.6-flash", "openai:gpt-4o-mini"]
    assert cfg.SKIPPED_MODELS == []


def test_missing_fallback_key_degrades_instead_of_crashing(monkeypatch):
    """The free configuration: Groq + Gemini, no paid key. Must run, still chained."""
    cfg = _reload_config(monkeypatch, GROQ_API_KEY="g", GOOGLE_API_KEY="gg")
    assert cfg.MODEL_CHAIN == ["groq:openai/gpt-oss-120b", "groq:openai/gpt-oss-20b",
                               "google:gemini-3.6-flash"]
    assert "OPENAI_API_KEY" in cfg.SKIPPED_MODELS[0]
    cfg.TELEGRAM_BOT_TOKEN = "t"
    cfg.check_startup_config()          # does not raise


def test_no_keys_at_all_fails_fast_with_an_actionable_message(monkeypatch):
    cfg = _reload_config(monkeypatch)
    cfg.TELEGRAM_BOT_TOKEN = "t"
    with pytest.raises(SystemExit) as e:
        cfg.check_startup_config()
    assert "GROQ_API_KEY" in str(e.value)          # tells you exactly what to get


def test_chain_is_configurable(monkeypatch):
    cfg = _reload_config(monkeypatch, GROQ_API_KEY="g", ANTHROPIC_API_KEY="a",
                         KIRANA_MODELS="anthropic:claude-sonnet-4-5,groq:openai/gpt-oss-20b")
    assert cfg.MODEL_CHAIN == ["anthropic:claude-sonnet-4-5", "groq:openai/gpt-oss-20b"]


# -------------------------------------------------------------------- failover

def test_rate_limited_primary_fails_over_to_the_next_model():
    """A Groq 429 mid-conversation must not reach the shop owner."""
    calls = {"primary": 0, "fallback": 0}

    def rate_limited(messages, info: AgentInfo):
        calls["primary"] += 1
        raise ModelHTTPError(status_code=429, model_name="groq",
                             body={"error": "rate_limit_exceeded"})

    def healthy(messages, info: AgentInfo):
        calls["fallback"] += 1
        return ModelResponse(parts=[TextPart("Bill closed. ₹437.")])

    agent = Agent(model=FallbackModel(FunctionModel(rate_limited),
                                      FunctionModel(healthy)))
    result = agent.run_sync("done")

    assert calls == {"primary": 1, "fallback": 1}
    assert "437" in result.output          # the owner just gets their answer


def test_failover_also_covers_a_provider_outage():
    """500s are the same class of failure as a 429 — same handling."""
    def down(messages, info: AgentInfo):
        raise ModelHTTPError(status_code=503, model_name="groq", body="unavailable")

    def healthy(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(model=FallbackModel(FunctionModel(down), FunctionModel(healthy)))
    assert agent.run_sync("stock?").output == "ok"


def test_a_single_model_chain_needs_no_fallback_wrapper(monkeypatch):
    """One model means one model — no needless wrapper."""
    _reload_config(monkeypatch, GROQ_API_KEY="g",
                   KIRANA_MODELS="groq:openai/gpt-oss-120b")
    from kirana.agent import harness
    importlib.reload(harness)
    model = harness.build_model()
    assert not isinstance(model, FallbackModel)
    assert model.model_name == "openai/gpt-oss-120b"


def test_provider_side_retries_are_disabled_so_failover_can_happen(monkeypatch):
    """The bug this guards against cost an evening of debugging.

    The Groq and OpenAI SDKs retry a 429 themselves with backoff before
    raising. That swallows the error, so FallbackModel never fires and the bot
    just hangs. Retrying is the chain's decision, not the SDK's.
    """
    _reload_config(monkeypatch, GROQ_API_KEY="g", OPENAI_API_KEY="o")
    from kirana.agent import harness
    importlib.reload(harness)
    for link in harness.build_model().models:
        client = getattr(link, "client", None)
        if client is not None and hasattr(client, "max_retries"):
            assert client.max_retries == 0, link.model_name


# --------------------------------------------- waiting out an exhausted chain

def test_wait_honours_the_longest_window_the_provider_quotes():
    """Groq's 429 body says when its window rolls over — trust it, don't guess."""
    from pydantic_ai.exceptions import FallbackExceptionGroup
    from kirana.agent.harness import rate_limit_wait_seconds

    group = FallbackExceptionGroup("All models from FallbackModel failed", [
        ModelHTTPError(429, "openai/gpt-oss-120b",
                       body={"error": {"message": "... Please try again in 5.295s."}}),
        ModelHTTPError(429, "openai/gpt-oss-20b",
                       body={"error": {"message": "... Please try again in 22.13s."}}),
    ])
    assert rate_limit_wait_seconds(group) == pytest.approx(22.63, abs=0.01)


def test_wait_is_capped_so_the_owner_is_never_left_hanging():
    from kirana.agent.harness import MAX_RATE_LIMIT_WAIT, rate_limit_wait_seconds
    exc = ModelHTTPError(429, "groq",
                         body={"error": {"message": "try again in 600s."}})
    assert rate_limit_wait_seconds(exc) == MAX_RATE_LIMIT_WAIT


def test_a_429_without_a_stated_delay_still_waits():
    from kirana.agent.harness import rate_limit_wait_seconds
    assert rate_limit_wait_seconds(ModelHTTPError(429, "groq", body="slow down")) > 0


def test_provider_overload_backs_off_and_retries():
    """Gemini answers 503 'experiencing high demand' under load — transient."""
    from kirana.agent.harness import rate_limit_wait_seconds
    exc = ModelHTTPError(503, "gemini-3.6-flash",
                         body={"error": {"message": "high demand", "status": "UNAVAILABLE"}})
    first = rate_limit_wait_seconds(exc, attempt=0)
    later = rate_limit_wait_seconds(exc, attempt=2)
    assert first is not None and later > first        # backs off


def test_a_throttled_and_an_overloaded_provider_together_take_the_longer_wait():
    from pydantic_ai.exceptions import FallbackExceptionGroup
    from kirana.agent.harness import rate_limit_wait_seconds
    group = FallbackExceptionGroup("All models from FallbackModel failed", [
        ModelHTTPError(429, "groq",
                       body={"error": {"message": "... try again in 26.8s."}}),
        ModelHTTPError(503, "gemini-3.6-flash", body={"error": {"message": "busy"}}),
    ])
    assert rate_limit_wait_seconds(group) == pytest.approx(27.3, abs=0.01)


def test_non_rate_limit_failures_are_not_waited_on():
    """A 404 or a bug must surface immediately, not sleep and retry."""
    from kirana.agent.harness import rate_limit_wait_seconds
    assert rate_limit_wait_seconds(ValueError("boom")) is None
    assert rate_limit_wait_seconds(
        ModelHTTPError(404, "groq", body="model_not_found")) is None
    assert rate_limit_wait_seconds(
        ModelHTTPError(401, "groq", body="bad key")) is None


def test_multi_model_chain_builds_a_fallback(monkeypatch):
    _reload_config(monkeypatch, GROQ_API_KEY="g", OPENAI_API_KEY="o")
    from kirana.agent import harness
    importlib.reload(harness)
    assert isinstance(harness.build_model(), FallbackModel)
