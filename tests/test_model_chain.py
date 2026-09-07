"""The model chain: graceful degradation and real failover.

Groq's free tier is metered at 6,000 tokens/minute and this agent sends ~4,300
tokens of tool schema per request, so HTTP 429 is an expected operating
condition, not an exceptional one. These tests pin the two behaviours that
depend on it: a chain with a missing key still starts, and a rate-limited
primary hands the turn to the next model without the owner seeing an error.
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
    """Re-read config.py under a given environment."""
    for k in ("GROQ_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
              "ANTHROPIC_API_KEY", "KIRANA_MODELS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(config)


# ----------------------------------------------------------- chain resolution

def test_full_chain_when_every_key_is_present(monkeypatch):
    cfg = _reload_config(monkeypatch, GROQ_API_KEY="g", OPENAI_API_KEY="o")
    assert cfg.MODEL_CHAIN == ["groq:llama-3.3-70b-versatile", "openai:gpt-4o-mini"]
    assert cfg.SKIPPED_MODELS == []


def test_missing_fallback_key_degrades_instead_of_crashing(monkeypatch):
    """The common case: a student with only the free Groq key. Must still run."""
    cfg = _reload_config(monkeypatch, GROQ_API_KEY="g")
    assert cfg.MODEL_CHAIN == ["groq:llama-3.3-70b-versatile"]
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
                         KIRANA_MODELS="anthropic:claude-sonnet-4-5,groq:openai/gpt-oss-120b")
    assert cfg.MODEL_CHAIN == ["anthropic:claude-sonnet-4-5", "groq:openai/gpt-oss-120b"]


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
    """Groq-only is the zero-cost configuration and must stay simple."""
    _reload_config(monkeypatch, GROQ_API_KEY="g")
    from kirana.agent import harness
    importlib.reload(harness)
    assert harness.build_model() == "groq:llama-3.3-70b-versatile"


def test_multi_model_chain_builds_a_fallback(monkeypatch):
    _reload_config(monkeypatch, GROQ_API_KEY="g", OPENAI_API_KEY="o")
    from kirana.agent import harness
    importlib.reload(harness)
    assert isinstance(harness.build_model(), FallbackModel)
