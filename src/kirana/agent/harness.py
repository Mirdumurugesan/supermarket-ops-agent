"""Agent harness — Pydantic AI, running on whichever provider is configured.

Why this harness
----------------
The brief asks for a modern agent harness with a real control loop
(observe → reason → act → feed the result back → continue), explicitly *not*
a node-per-command state machine. Pydantic AI gives exactly that loop, and one
property the others don't: it is **model-agnostic**, and it ships a
`FallbackModel` that turns a provider outage into a non-event.

That matters here for a concrete reason, not tidiness. This agent's tool
surface is ~4,300 tokens per request (29 tools with per-parameter descriptions)
and a multi-item bill is several round-trips. Groq is the primary because it is
free and very fast, but its free tier is metered at 6,000 tokens per minute, so
a busy minute returns HTTP 429. Pydantic AI raises that as `ModelHTTPError`,
`FallbackModel` catches it, and the next model in the chain finishes the turn
with the conversation intact. The owner sees a slightly slower reply instead of
an error — which is the difference between a store that stays open and one that
doesn't.

The chain is configuration (`KIRANA_MODELS`), and the boundary that makes it
cost nothing is `tools.ToolSpec`: the store knows about no provider at all.

The control loop
----------------
One `Agent` per process (tools and instructions are static); one *conversation*
per Telegram chat, held as a message list. Pydantic AI runs the loop: it calls
the model, executes any tool calls the model asks for — in parallel where they
are independent, which is most of a multi-item bill — feeds the results back,
and continues until the model produces a final answer. `usage_limits` bounds
the number of requests so a confused turn cannot spin.

`/new` clears a chat's message list. That is the whole conversation gone —
which is exactly what makes the memory requirement testable: the owner's
preferences are in SQLite, get re-read on the next turn, and still apply.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.tools import Tool
from pydantic_ai.usage import UsageLimits

from ..config import MAX_STEPS_PER_TURN, MODEL_CHAIN
from . import tools as tool_registry
from .system_prompt import build_system_prompt
from .tools import current_chat_id, current_update_id

log = logging.getLogger(__name__)


def _build_tools() -> list[Tool]:
    """Adapt our neutral ToolSpecs onto Pydantic AI.

    ``Tool.from_schema`` takes our hand-written JSON Schema verbatim, so the
    careful optional/required distinction and the per-parameter descriptions
    survive into the model's view of the tool.
    """
    adapted: list[Tool] = []
    for spec in tool_registry.ALL_TOOLS:
        async def run(_spec=spec, **kwargs) -> str:
            result = await _spec.handler(kwargs)
            return result["content"][0]["text"]

        adapted.append(
            Tool.from_schema(
                function=run,
                name=spec.name,
                description=spec.description,
                json_schema=spec.schema,
            )
        )
    return adapted


def _no_internal_retry(model_id: str):
    """Build one link of the chain with provider-side retries turned OFF.

    This is the subtle part. Both the Groq and OpenAI SDKs retry a 429
    themselves, with exponential backoff, *before* raising. That is sensible
    for a single-model app and exactly wrong here: the retry swallows the
    error, so `FallbackModel` never sees it and never fails over — the owner
    just watches the bot hang for two minutes while the SDK sleeps.

    Setting ``max_retries=0`` makes a 429 surface immediately, so the chain
    does its job: the next model (which has its own separate quota) picks the
    turn up in milliseconds. Retrying is the fallback chain's decision to
    make, not the SDK's.
    """
    provider_name, _, model_name = model_id.partition(":")

    if provider_name == "groq":
        from groq import AsyncGroq
        from pydantic_ai.models.groq import GroqModel
        from pydantic_ai.providers.groq import GroqProvider

        client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"], max_retries=0)
        return GroqModel(model_name, provider=GroqProvider(groq_client=client))

    if provider_name == "openai":
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=0)
        return OpenAIChatModel(model_name, provider=OpenAIProvider(openai_client=client))

    return model_id          # other providers: library defaults are fine


def build_model():
    """The configured chain as one model object.

    With a single entry this is just that model. With more, `FallbackModel`
    tries them in order and moves on when one raises `ModelAPIError` — which
    covers a rate-limited free tier (HTTP 429) as well as a provider outage.
    """
    if not MODEL_CHAIN:
        raise SystemExit("No usable model configured — see KIRANA_MODELS in .env.")
    links = [_no_internal_retry(m) for m in MODEL_CHAIN]
    if len(links) == 1:
        return links[0]
    return FallbackModel(links[0], *links[1:])


def build_agent() -> Agent:
    """One agent for the process. Instructions are rebuilt per run so a
    preference saved mid-conversation is visible on the very next turn."""
    return Agent(
        model=build_model(),
        tools=_build_tools(),
        instructions=build_system_prompt,   # callable → re-evaluated each run
        retries=2,
    )


@dataclass
class ChatSession:
    """One Telegram chat's conversation."""

    chat_id: int
    history: list[ModelMessage] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AgentManager:
    def __init__(self) -> None:
        self._agent = build_agent()
        self._sessions: dict[int, ChatSession] = {}
        log.info("agent ready — models: %s | tools: %d",
                 " → ".join(MODEL_CHAIN), len(tool_registry.ALL_TOOLS))

    def _session(self, chat_id: int) -> ChatSession:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = ChatSession(chat_id)
        return self._sessions[chat_id]

    async def new_chat(self, chat_id: int) -> None:
        """/new — drop the conversation. Durable memory (SQLite) is untouched."""
        self._session(chat_id).history.clear()
        log.info("chat %s: conversation cleared", chat_id)

    async def handle_message(self, chat_id: int, update_id: int, text: str) -> str:
        """Run one agent turn and return the reply for Telegram."""
        session = self._session(chat_id)
        async with session.lock:                       # strict per-chat ordering
            current_chat_id.set(chat_id)               # ambient tool context
            current_update_id.set(str(update_id))
            result = await self._agent.run(
                text,
                message_history=session.history,
                usage_limits=UsageLimits(request_limit=MAX_STEPS_PER_TURN),
            )
            session.history = result.all_messages()
            self._trim(session)
            return (result.output or "Done.").strip()

    @staticmethod
    def _trim(session: ChatSession, keep: int = 60) -> None:
        """Bound context growth. The store's state lives in SQLite, so an old
        conversation tail is not worth paying for on every request."""
        if len(session.history) > keep:
            session.history = session.history[-keep:]
