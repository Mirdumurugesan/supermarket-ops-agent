"""The control loop itself, driven by a scripted model — no API key, no network.

These tests replace the model with `FunctionModel`, which lets us assert on the
part of the system we actually own: that the tool surface is wired correctly,
that tool results flow back into the loop, that refusals reach the model as
text it must deal with, and that `/new` clears the conversation while the
store's durable memory survives.

What is deliberately NOT tested here is whether a real LLM picks the right
tool — that is the provider's job, and asserting on it would be testing
Gemini, not this codebase.
"""

from __future__ import annotations

import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (ModelMessage, ModelResponse, TextPart,
                                  ToolCallPart)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from kirana.agent import tools as tool_registry
from kirana.agent.harness import AgentManager, _build_tools
from kirana.services import memory_service


def _agent(script) -> Agent:
    """An agent with our real tools but a scripted model."""
    return Agent(model=FunctionModel(script), tools=_build_tools(),
                 instructions="test harness")


def _last_tool_result(messages: list[ModelMessage], tool_name: str) -> str:
    for m in messages:
        for part in m.parts:
            if getattr(part, "tool_name", None) == tool_name and hasattr(part, "content"):
                return str(part.content)
    raise AssertionError(f"no result for {tool_name}")


# --------------------------------------------------------------- tool wiring

def test_every_tool_reaches_the_model_with_its_schema(db):
    """The model must see all 29 capabilities, with descriptions intact."""
    seen = {}

    def script(messages, info: AgentInfo):
        seen.update({t.name: t for t in info.function_tools})
        return ModelResponse(parts=[TextPart("ok")])

    _agent(script).run_sync("hello")

    assert len(seen) == len(tool_registry.ALL_TOOLS) == 29
    assert "search_products" in seen and "finalize_bill" in seen
    # optional parameters survived the adapter — this is the regression that
    # a type-hint-derived schema would silently break
    add = seen["add_product"].parameters_json_schema
    assert set(add["required"]) == {"name", "gst_rate", "cost_price", "sell_price"}
    assert add["properties"]["mrp"]["description"]


def test_model_can_call_a_tool_and_receives_real_store_data(db):
    """Observe → act → feed result back: the loop's core."""
    calls = []

    def script(messages, info: AgentInfo):
        if not calls:
            calls.append("search")
            return ModelResponse(parts=[
                ToolCallPart("search_products", {"query": "maggi"})])
        return ModelResponse(parts=[TextPart("found it")])

    result = _agent(script).run_sync("how much maggi is left?")

    payload = json.loads(_last_tool_result(result.all_messages(), "search_products"))
    assert payload[0]["name"].startswith("Maggi")     # grounded in the DB, not invented
    assert payload[0]["qty"] == 6
    assert result.output == "found it"


def test_a_multi_step_bill_chains_tools_in_one_turn(db):
    """The 'bill 4 maggi, UPI' chain: start → add → pay → finalize."""
    step = {"n": 0}

    def script(messages, info: AgentInfo):
        step["n"] += 1
        n = step["n"]
        if n == 1:
            return ModelResponse(parts=[ToolCallPart("start_bill", {})])
        if n == 2:
            bill = json.loads(_last_tool_result(messages, "start_bill"))
            return ModelResponse(parts=[ToolCallPart(
                "add_bill_item", {"bill_id": bill["id"], "product_id": 1, "qty": 4})])
        if n == 3:
            bill = json.loads(_last_tool_result(messages, "start_bill"))
            return ModelResponse(parts=[ToolCallPart(
                "set_payment_mode", {"bill_id": bill["id"], "mode": "upi"})])
        if n == 4:
            bill = json.loads(_last_tool_result(messages, "start_bill"))
            return ModelResponse(parts=[ToolCallPart(
                "finalize_bill", {"bill_id": bill["id"]})])
        return ModelResponse(parts=[TextPart("Bill closed.")])

    result = _agent(script).run_sync("bill 4 maggi, upi")
    final = json.loads(_last_tool_result(result.all_messages(), "finalize_bill"))

    assert final["status"] == "finalized"
    assert final["grand_total"] == 56.0
    from kirana.services import inventory_service
    assert inventory_service.get_product(1)["qty"] == 2      # stock actually moved


def test_a_refusal_reaches_the_model_as_text_it_must_handle(db):
    """The model cannot route around a guardrail — it only sees the reason."""
    refusals = []

    def script(messages, info: AgentInfo):
        if not refusals:
            refusals.append(1)
            return ModelResponse(parts=[ToolCallPart(
                "khata_record_payment", {"customer": "Ghost", "amount": 100})])
        text = _last_tool_result(messages, "khata_record_payment")
        assert text.startswith("REFUSED:")
        return ModelResponse(parts=[TextPart("No khata for Ghost.")])

    result = _agent(script).run_sync("Ghost paid 100")
    assert "No khata" in result.output


def test_oversell_refusal_through_the_full_loop(db):
    """Six Maggi in stock; the model asks for ten and is told why not."""
    seen = []

    def script(messages, info: AgentInfo):
        if not seen:
            seen.append(1)
            return ModelResponse(parts=[ToolCallPart("start_bill", {})])
        if len(seen) == 1:
            seen.append(2)
            bill = json.loads(_last_tool_result(messages, "start_bill"))
            return ModelResponse(parts=[ToolCallPart(
                "add_bill_item", {"bill_id": bill["id"], "product_id": 1, "qty": 10})])
        text = _last_tool_result(messages, "add_bill_item")
        assert "REFUSED" in text and "6" in text
        return ModelResponse(parts=[TextPart("Only 6 left.")])

    assert "Only 6" in _agent(script).run_sync("bill 10 maggi").output


# ------------------------------------------------------- sessions and memory

@pytest.mark.asyncio
async def test_new_clears_conversation_but_not_durable_memory(db, monkeypatch):
    """The requirement: memory lives outside the context window."""
    def script(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart("ok")])

    manager = AgentManager()
    monkeypatch.setattr(manager, "_agent", _agent(script))

    await manager.handle_message(chat_id=7, update_id=1, text="hello")
    memory_service.set_preference("default_payment_mode", "upi")
    assert manager._session(7).history                    # conversation exists

    await manager.new_chat(7)
    assert manager._session(7).history == []              # context window emptied
    assert memory_service.get_preferences()["default_payment_mode"] == "upi"


@pytest.mark.asyncio
async def test_each_chat_keeps_its_own_conversation(db, monkeypatch):
    def script(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart("ok")])

    manager = AgentManager()
    monkeypatch.setattr(manager, "_agent", _agent(script))

    await manager.handle_message(chat_id=1, update_id=1, text="first")
    await manager.handle_message(chat_id=2, update_id=2, text="second")
    await manager.new_chat(1)

    assert manager._session(1).history == []
    assert manager._session(2).history                    # chat 2 untouched
