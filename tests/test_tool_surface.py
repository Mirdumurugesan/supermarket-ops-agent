"""The tool surface itself: schemas are well-formed and refusals propagate.

Schema shape matters more than it looks. A schema derived from type hints marks
every field required, which would force the model to invent values for optional
parameters like MRP — so every tool declares explicit JSON Schema, and these
tests hold that line as the project moves between harnesses and providers.
"""

from __future__ import annotations

import asyncio

import pytest

from kirana.agent import tools as T
from kirana.services import billing_service as billing


def call(tool_obj, args=None):
    return asyncio.run(tool_obj.handler(args or {}))


def text(result) -> str:
    return result["content"][0]["text"]


# ------------------------------------------------------------------- schemas

def test_every_tool_has_valid_object_schema():
    for t in T.ALL_TOOLS:
        s = t.schema
        assert s["type"] == "object", t.name
        assert isinstance(s["properties"], dict), t.name
        assert isinstance(s["required"], list), t.name
        # required fields must actually exist in properties
        assert set(s["required"]) <= set(s["properties"]), t.name


def test_optional_parameters_are_not_marked_required():
    """Regression guard: a type-hint-derived schema would require all of these."""
    add = next(t for t in T.ALL_TOOLS if t.name == "add_product")
    assert set(add.schema["required"]) == {
        "name", "gst_rate", "cost_price", "sell_price"}
    for optional in ("mrp", "brand", "aliases", "is_loose", "qty", "allow_below_cost"):
        assert optional in add.schema["properties"]
        assert optional not in add.schema["required"]

    recv = next(t for t in T.ALL_TOOLS if t.name == "receive_stock")
    assert set(recv.schema["required"]) == {"product_id", "qty"}


def test_every_parameter_documents_itself():
    """Tool-call accuracy depends on these far more than on the system prompt."""
    for t in T.ALL_TOOLS:
        for pname, pschema in t.schema["properties"].items():
            assert pschema.get("description"), f"{t.name}.{pname} has no description"


def test_tool_names_are_unique_and_descriptions_substantial():
    names = [t.name for t in T.ALL_TOOLS]
    assert len(names) == len(set(names))
    for t in T.ALL_TOOLS:
        assert len(t.description) > 40, t.name


def test_registry_is_the_single_source_of_truth():
    """Both the tool list and the name index come from one registration pass."""
    assert T.ALL_TOOLS == T._REGISTRY
    assert set(T.TOOLS_BY_NAME) == {t.name for t in T.ALL_TOOLS}


def test_unknown_tool_name_is_refused_not_crashed():
    import asyncio
    out = asyncio.run(T.invoke("no_such_tool", {}))
    assert out.startswith("REFUSED:")


# ------------------------------------------------------------- tool behaviour

def test_search_grounds_the_model_in_real_catalog_data(db):
    T.current_chat_id.set(1)
    out = text(call(T.search_products, {"query": "maggi"}))
    assert "Maggi" in out and "1902" in out          # name and HSN come from the DB


def test_refusals_reach_the_model_as_errors(db):
    T.current_chat_id.set(1)
    res = call(T.khata_record_payment, {"customer": "Nobody", "amount": 50})
    assert res.get("is_error") is True
    assert text(res).startswith("REFUSED:")


def test_oversell_refusal_through_the_tool_layer(db):
    T.current_chat_id.set(1)
    T.current_update_id.set("u1")
    bill = billing.create_draft(1)
    res = call(T.add_bill_item, {"bill_id": bill["id"], "product_id": 1, "qty": 99})
    assert res.get("is_error") is True
    assert "stock" in text(res).lower()


def test_full_bill_flow_through_tools_only(db):
    """The chain the model actually runs for 'bill 4 maggi, UPI'."""
    T.current_chat_id.set(42)
    T.current_update_id.set("u-flow")

    import json
    bill = json.loads(text(call(T.start_bill)))
    call(T.add_bill_item, {"bill_id": bill["id"], "product_id": 1, "qty": 4})
    call(T.set_payment_mode, {"bill_id": bill["id"], "mode": "upi"})
    final = json.loads(text(call(T.finalize_bill, {"bill_id": bill["id"]})))

    assert final["status"] == "finalized"
    assert final["grand_total"] == 56.0
    # a redelivered finalize is a no-op
    again = json.loads(text(call(T.finalize_bill, {"bill_id": bill["id"]})))
    assert again["invoice_no"] == final["invoice_no"]


def test_chat_scoping_keeps_drafts_separate(db):
    import json
    T.current_chat_id.set(100)
    call(T.start_bill)
    T.current_chat_id.set(200)
    other = text(call(T.get_current_bill))
    assert other == "null"                            # chat 200 sees no draft
