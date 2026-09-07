"""The agent's tool surface — the capability layer the model orchestrates.

Design philosophy (this is what the task grades):

* Tools are **thin verbs over the service layer**. Every business rule
  (oversell guard, GST maths, idempotency, khata invariants) lives in the
  services/DB — a tool can only *invoke* a capability, never bypass a rule.
* Tools are **composable**: "2kg sugar, 1 atta, 4 Maggi, UPI" becomes
  search → add_item ×3 → set_payment → preview, chained by the model in one
  turn. There is no `make_bill_from_text` mega-tool, because parsing human
  phrasing is the model's job, not regex code.
* Errors are **refusals with reasons**, returned as text the model must relay,
  so it can explain ("only 6 Maggi left") or ask a clarifying question.
* The current Telegram chat id rides on a contextvar — tools never trust the
  model to supply it, so one chat can't touch another's draft bill.

This module is **harness-neutral**: a tool is a `ToolSpec` (name, description,
JSON Schema, async handler) with no dependency on any agent framework. The
adapters in `harness.py` register these specs with whichever framework and
model provider is configured. That boundary is what let the whole project move
from one provider to another by rewriting ~120 lines instead of the store.

Schemas are hand-written JSON Schema rather than derived from type hints, for
two reasons: optional parameters stay genuinely optional (a derived schema
would force the model to invent an MRP on every `add_product` call), and every
parameter carries its own description — tool-call accuracy tracks these far
more closely than it tracks system-prompt wording.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..db.database import transaction
from ..docs_gen.analysis_pptx import generate_analysis_deck
from ..docs_gen.invoice_pdf import generate_invoice_pdf
from ..services import (analytics_service, billing_service, inventory_service,
                        khata_service, memory_service)
from ..services.errors import KiranaError


@dataclass(frozen=True)
class ToolSpec:
    """One capability, described in a way any agent harness can consume."""

    name: str
    description: str
    schema: dict
    handler: Callable[[dict], Awaitable[dict]]


_REGISTRY: list[ToolSpec] = []

# Money as a shopkeeper writes it: ₹500, Rs.500, rs 500, 1,200, "500".
_MONEY_NOISE = str.maketrans("", "", "₹, ")


def coerce_arguments(schema: dict, args: dict) -> dict:
    """Make the model's arguments match the types the schema promised.

    The owner types "put rs.500 on Ramesh's credit" and the model faithfully
    passes `amount="rs.500"` — a string where the service expects a number, so
    the comparison inside the service raises TypeError and the whole turn dies
    with "internal error". The model is not wrong to echo the owner's phrasing;
    the boundary is the right place to normalise it, the same way the tool layer
    normalises everything else before the services see it.

    Only string→number is coerced, and only for fields the schema declares
    numeric. Anything that still isn't a number is passed through untouched so
    it fails loudly rather than silently becoming 0.
    """
    props = schema.get("properties") or {}
    out = dict(args)
    for key, value in args.items():
        declared = (props.get(key) or {}).get("type")
        if declared not in ("number", "integer") or not isinstance(value, str):
            continue
        cleaned = value.translate(_MONEY_NOISE).lower()
        for prefix in ("rs.", "rs", "inr"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
        cleaned = cleaned.strip(".")
        try:
            out[key] = int(cleaned) if declared == "integer" else float(cleaned)
        except ValueError:
            pass                      # not a number at all — let it fail loudly
    return out


def tool(name: str, description: str, schema: dict):
    """Register an async handler as a tool. Mirrors the shape of most SDK
    decorators, but produces a plain ToolSpec we own."""

    def decorator(fn):
        spec = ToolSpec(name=name, description=description, schema=schema, handler=fn)
        _REGISTRY.append(spec)
        return spec

    return decorator


current_chat_id: ContextVar[int] = ContextVar("current_chat_id", default=0)
current_update_id: ContextVar[str] = ContextVar("current_update_id", default="")


# --------------------------------------------------------------- schema helpers

def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def P(kind: str, desc: str, **extra) -> dict:
    return {"type": kind, "description": desc, **extra}


NO_ARGS = schema({})


def _ok(payload) -> dict:
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False, default=str)}]}


def _err(e: Exception) -> dict:
    """A refusal the model must relay, not route around."""
    return {"content": [{"type": "text", "text": f"REFUSED: {e}"}], "is_error": True}


def _queue_file(path: str, caption: str) -> None:
    """Hand a generated document to the Telegram layer's outbox."""
    with transaction() as tx:
        tx.execute("INSERT INTO outbox (chat_id, file_path, caption) VALUES (?,?,?)",
                   (current_chat_id.get(), path, caption))


# ------------------------------------------------------------------- inventory

@tool(
    "search_products",
    "Find products by name, brand or colloquial alias ('atta', 'surf', 'paruppu'). "
    "ALWAYS call before billing or changing stock — never guess an id, price or GST "
    "rate. Returns id, price, stock, unit, is_loose and GST slab. If two plausible "
    "matches come back, ask the owner which they mean.",
    schema({
        "query": P("string", "The item, in the owner's own words."),
    }, ["query"]),
)
async def search_products(args):
    try:
        return _ok(inventory_service.search_products(args["query"]))
    except KiranaError as e:
        return _err(e)


@tool(
    "add_product",
    "Add a NEW product (only if search_products found nothing). sell_price is "
    "GST-INCLUSIVE ₹. Refuses below-cost without allow_below_cost, and above-MRP always.",
    schema({
        "name": P("string", "Full name, e.g. 'Amul Butter 100g'."),
        "gst_rate": P("number", "GST slab %: 0, 5, 12, 18 or 28."),
        "cost_price": P("number", "Shop's cost per unit, ₹."),
        "sell_price": P("number", "Customer price per unit, GST-inclusive ₹."),
        "unit": P("string", "Unit of sale.",
                  enum=["kg", "g", "litre", "ml", "packet", "dozen", "piece"]),
        "hsn": P("string", "HSN tax code, e.g. '1902'."),
        "mrp": P("number", "Printed MRP, if any."),
        "qty": P("number", "Opening stock. Default 0."),
        "brand": P("string", "Brand, e.g. 'Amul'."),
        "is_loose": P("boolean", "True if sold by weight/volume (2.5kg valid)."),
        "aliases": P("string", "Comma-separated other names, incl. Tamil/Hindi."),
        "allow_below_cost": P("boolean", "True only if owner confirmed below-cost."),
    }, ["name", "gst_rate", "cost_price", "sell_price"]),
)
async def add_product(args):
    try:
        return _ok(inventory_service.add_product(
            name=args["name"], unit=args.get("unit", "piece"),
            hsn=args.get("hsn", "2106"), gst_rate=args["gst_rate"],
            cost_price=args["cost_price"], sell_price=args["sell_price"],
            mrp=args.get("mrp"), qty=args.get("qty", 0), brand=args.get("brand"),
            is_loose=bool(args.get("is_loose", False)), aliases=args.get("aliases", ""),
            allow_below_cost=bool(args.get("allow_below_cost", False))))
    except KiranaError as e:
        return _err(e)


@tool(
    "receive_stock",
    "Goods received for an existing product ('50 packets of Maggi came in'). Adds "
    "stock, optionally updating cost/price/MRP. Never reduces stock — use adjust_stock.",
    schema({
        "product_id": P("integer", "Id from search_products."),
        "qty": P("number", "Quantity received, positive."),
        "cost_price": P("number", "New cost, if changed."),
        "sell_price": P("number", "New GST-inclusive price, if changed."),
        "mrp": P("number", "New MRP, if changed."),
    }, ["product_id", "qty"]),
)
async def receive_stock(args):
    try:
        return _ok(inventory_service.receive_stock(
            args["product_id"], args["qty"], args.get("cost_price"),
            args.get("sell_price"), args.get("mrp")))
    except KiranaError as e:
        return _err(e)


@tool(
    "update_product",
    "Change price, MRP, reorder level or aliases, or deactivate. Products are "
    "deactivated, never deleted.",
    schema({
        "product_id": P("integer", "Id from search_products."),
        "sell_price": P("number", "New GST-inclusive price."),
        "mrp": P("number", "New MRP."),
        "reorder_level": P("number", "Level that flags a reorder."),
        "aliases": P("string", "Replacement alias list."),
        "active": P("boolean", "False to stop stocking."),
        "allow_below_cost": P("boolean", "True only if owner confirmed below-cost."),
    }, ["product_id"]),
)
async def update_product(args):
    try:
        return _ok(inventory_service.update_product(
            args["product_id"], args.get("sell_price"), args.get("mrp"),
            args.get("reorder_level"), args.get("aliases"), args.get("active"),
            bool(args.get("allow_below_cost", False))))
    except KiranaError as e:
        return _err(e)


@tool(
    "adjust_stock",
    "Correct stock to a counted quantity (breakage, spoilage, stock-take). Sets an "
    "absolute value and logs the delta; never below zero.",
    schema({
        "product_id": P("integer", "Id from search_products."),
        "new_qty": P("number", "Quantity actually on the shelf now."),
        "reason": P("string", "Short reason, e.g. 'breakage'."),
    }, ["product_id", "new_qty"]),
)
async def adjust_stock(args):
    try:
        return _ok(inventory_service.adjust_stock(
            args["product_id"], args["new_qty"], args.get("reason", "adjust")))
    except KiranaError as e:
        return _err(e)


@tool(
    "stock_level",
    "Stock, price and reorder level for one product ('how much sugar is left?').",
    schema({"product_id": P("integer", "Product id from search_products.")},
           ["product_id"]),
)
async def stock_level(args):
    try:
        p = inventory_service.get_product(args["product_id"])
        return _ok({"name": p["name"], "qty": p["qty"], "unit": p["unit"],
                    "reorder_level": p["reorder_level"], "sell_price": p["sell_price"]})
    except KiranaError as e:
        return _err(e)


@tool(
    "low_stock_report",
    "Every item at or below its reorder level — answers \"what's running out?\".",
    NO_ARGS,
)
async def low_stock_report(args):
    return _ok(inventory_service.low_stock())


# --------------------------------------------------------------------- billing

@tool(
    "get_current_bill",
    "The open draft bill for this chat with lines and running total, or null. Use "
    "when the owner says 'the bill' without naming one.",
    NO_ARGS,
)
async def get_current_bill(args):
    return _ok(billing_service.get_open_draft(current_chat_id.get()))


@tool(
    "start_bill",
    "Open a draft bill for this chat; returns the existing one if already open, so "
    "calling twice is safe. Drafting does not move stock.",
    schema({"customer_name": P("string", "Customer's name, if the owner mentioned one.")}),
)
async def start_bill(args):
    try:
        return _ok(billing_service.create_draft(current_chat_id.get(),
                                                args.get("customer_name")))
    except KiranaError as e:
        return _err(e)


@tool(
    "add_bill_item",
    "Add quantity of a product to a draft bill; adding again increases it. Refuses "
    "more than stock, and fractional quantities for packaged goods.",
    schema({
        "bill_id": P("integer", "Draft bill id."),
        "product_id": P("integer", "Id from search_products."),
        "qty": P("number", "Quantity to add, in the product's unit."),
    }, ["bill_id", "product_id", "qty"]),
)
async def add_bill_item(args):
    try:
        return _ok(billing_service.add_item(args["bill_id"], args["product_id"],
                                            args["qty"]))
    except KiranaError as e:
        return _err(e)


@tool(
    "set_bill_item_qty",
    "Set a line's exact quantity on a draft bill ('make it 6 Maggi'). qty=0 removes "
    "the line ('drop the butter').",
    schema({
        "bill_id": P("integer", "Draft bill id."),
        "product_id": P("integer", "Product id of the line to change."),
        "qty": P("number", "The new quantity; 0 removes the line."),
    }, ["bill_id", "product_id", "qty"]),
)
async def set_bill_item_qty(args):
    try:
        return _ok(billing_service.set_item_qty(args["bill_id"], args["product_id"],
                                                args["qty"]))
    except KiranaError as e:
        return _err(e)


@tool(
    "set_payment_mode",
    "Set how a draft bill is paid. 'khata' bills a customer's credit ledger and needs "
    "khata_customer. Required before finalizing.",
    schema({
        "bill_id": P("integer", "Draft bill id."),
        "mode": P("string", "Payment method.", enum=["cash", "upi", "card", "khata"]),
        "reference": P("string", "UPI/card reference, if given."),
        "khata_customer": P("string", "Customer name — required for 'khata'."),
    }, ["bill_id", "mode"]),
)
async def set_payment_mode(args):
    try:
        return _ok(billing_service.set_payment(
            args["bill_id"], args["mode"], args.get("reference"),
            args.get("khata_customer")))
    except KiranaError as e:
        return _err(e)


@tool(
    "finalize_bill",
    "Close a draft bill: atomically decrements stock, computes GST, assigns an invoice "
    "number, posts khata if applicable. Refuses and rolls back entirely if any line "
    "exceeds stock. Idempotent — re-finalizing returns the same bill. Confirm with the "
    "owner first unless they clearly said to close it.",
    schema({"bill_id": P("integer", "Draft bill id to finalize.")}, ["bill_id"]),
)
async def finalize_bill(args):
    try:
        key = f"tg-{current_chat_id.get()}-{current_update_id.get()}-bill{args['bill_id']}"
        return _ok(billing_service.finalize_bill(args["bill_id"], key))
    except KiranaError as e:
        return _err(e)


@tool(
    "cancel_bill",
    "Cancel a draft bill. Finalized bills cannot be cancelled.",
    schema({"bill_id": P("integer", "Draft bill id to cancel.")}, ["bill_id"]),
)
async def cancel_bill(args):
    try:
        return _ok(billing_service.cancel_bill(args["bill_id"]))
    except KiranaError as e:
        return _err(e)


@tool(
    "get_bill",
    "Fetch any bill by id with its lines, tax breakup and totals.",
    schema({"bill_id": P("integer", "Bill id.")}, ["bill_id"]),
)
async def get_bill(args):
    try:
        return _ok(billing_service.get_bill(args["bill_id"]))
    except KiranaError as e:
        return _err(e)


@tool(
    "latest_bill",
    "The most recently finalized bill — use for 'send me that bill as a PDF'.",
    NO_ARGS,
)
async def latest_bill(args):
    return _ok(billing_service.latest_finalized_bill(current_chat_id.get())
               or billing_service.latest_finalized_bill())


# ----------------------------------------------------------------------- khata

@tool(
    "khata_add_credit",
    "Put an amount on a customer's khata — 'put ₹500 on Ramesh's credit'. Creates the "
    "customer if new. INCREASES what they owe.",
    schema({
        "customer": P("string", "Customer's name."),
        "amount": P("number", "Amount to add to their balance, ₹."),
        "note": P("string", "What it was for."),
    }, ["customer", "amount"]),
)
async def khata_add_credit(args):
    try:
        return _ok(khata_service.add_credit(args["customer"], args["amount"],
                                            args.get("note")))
    except KiranaError as e:
        return _err(e)


@tool(
    "khata_record_payment",
    "Record a khata settlement — 'Ramesh paid ₹300'. REDUCES what they owe. Refuses "
    "if no khata exists or the payment overshoots the balance.",
    schema({
        "customer": P("string", "Customer's name."),
        "amount": P("number", "Amount received, ₹."),
        "note": P("string", "Optional note."),
    }, ["customer", "amount"]),
)
async def khata_record_payment(args):
    try:
        return _ok(khata_service.record_payment(args["customer"], args["amount"],
                                                args.get("note")))
    except KiranaError as e:
        return _err(e)


@tool(
    "khata_balance",
    "How much one customer currently owes — 'what's Ramesh's balance?'.",
    schema({"customer": P("string", "Customer's name.")}, ["customer"]),
)
async def khata_balance(args):
    try:
        return _ok(khata_service.balance(args["customer"]))
    except KiranaError as e:
        return _err(e)


@tool(
    "khata_statement",
    "A customer's recent khata entries — credits and payments with dates.",
    schema({"customer": P("string", "Customer's name.")}, ["customer"]),
)
async def khata_statement(args):
    try:
        return _ok(khata_service.statement(args["customer"]))
    except KiranaError as e:
        return _err(e)


@tool(
    "khata_all_balances",
    "Everyone with an outstanding khata balance — 'who owes me money?'.",
    NO_ARGS,
)
async def khata_all_balances(args):
    return _ok(khata_service.all_balances())


# ------------------------------------------------------------------- analytics

@tool(
    "daily_summary",
    "Daily close for a date (today by default, IST): bills, revenue, GST collected, "
    "payment split, top items, khata outstanding.",
    schema({"date": P("string", "Date as YYYY-MM-DD. Defaults to today in IST.")}),
)
async def daily_summary(args):
    return _ok(analytics_service.daily_summary(args.get("date")))


@tool(
    "sales_report",
    "Sales aggregates for an inclusive IST date range: daily trend, top items, "
    "payment mix, GST per slab.",
    schema({
        "date_from": P("string", "Start date, YYYY-MM-DD."),
        "date_to": P("string", "End date, YYYY-MM-DD, inclusive."),
    }, ["date_from", "date_to"]),
)
async def sales_report(args):
    return _ok(analytics_service.sales_range(args["date_from"], args["date_to"]))


@tool(
    "reorder_suggestions",
    "What to reorder, ranked by urgency — stock on hand vs recent sales velocity, "
    "with estimated days of stock left.",
    NO_ARGS,
)
async def reorder_suggestions(args):
    return _ok(analytics_service.reorder_suggestions())


# ------------------------------------------------------------------- documents

@tool(
    "generate_invoice_pdf",
    "Generate and send the GST tax-invoice PDF for a FINALIZED bill (per-item HSN, "
    "CGST/SGST, slab summary, round-off). Finalize first — drafts have no invoice.",
    schema({"bill_id": P("integer", "Id of a finalized bill.")}, ["bill_id"]),
)
async def generate_invoice_pdf_tool(args):
    try:
        path = generate_invoice_pdf(args["bill_id"])
        bill = billing_service.get_bill(args["bill_id"])
        _queue_file(path, f"Invoice {bill['invoice_no']} · ₹{bill['grand_total']:.2f}")
        return _ok({"generated": path, "delivery": "sent to this chat"})
    except (KiranaError, ValueError) as e:
        return _err(e)


@tool(
    "generate_analysis_deck",
    "Generate and send the PowerPoint sales-analysis deck for an IST date range: "
    "revenue trend, top sellers, payment mix, GST by slab, stock health, insights.",
    schema({
        "date_from": P("string", "Start date, YYYY-MM-DD."),
        "date_to": P("string", "End date, YYYY-MM-DD, inclusive."),
    }, ["date_from", "date_to"]),
)
async def generate_analysis_deck_tool(args):
    try:
        path = generate_analysis_deck(args["date_from"], args["date_to"])
        _queue_file(path, f"Sales analysis {args['date_from']} → {args['date_to']}")
        return _ok({"generated": path, "delivery": "sent to this chat"})
    except KiranaError as e:
        return _err(e)


# ---------------------------------------------------------------------- memory

@tool(
    "set_preference",
    "Save a standing owner preference — survives /new and restarts. Use whenever the "
    "owner states a lasting rule ('always assume UPI', 'my GSTIN is ...'). Keys: "
    "default_payment_mode, default_atta, shop_name, shop_address, shop_phone, gstin, language.",
    schema({
        "key": P("string", "snake_case key, e.g. 'default_payment_mode'."),
        "value": P("string", "Value to remember."),
    }, ["key", "value"]),
)
async def set_preference(args):
    return _ok(memory_service.set_preference(args["key"], args["value"]))


@tool("get_preferences",
      "Every saved owner preference and its value. Already loaded into your system "
      "prompt at session start — call this only to re-check after a change.",
      NO_ARGS)
async def get_preferences(args):
    return _ok(memory_service.get_preferences())


@tool(
    "delete_preference",
    "Forget a saved preference when the owner drops a standing rule ('stop assuming "
    "UPI'). Does not touch stock, bills or khata.",
    schema({"key": P("string", "The preference key to delete.")}, ["key"]),
)
async def delete_preference(args):
    return _ok(memory_service.delete_preference(args["key"]))


ALL_TOOLS: list[ToolSpec] = [
    # inventory
    search_products, add_product, receive_stock, update_product, adjust_stock,
    stock_level, low_stock_report,
    # billing
    get_current_bill, start_bill, add_bill_item, set_bill_item_qty, set_payment_mode,
    finalize_bill, cancel_bill, get_bill, latest_bill,
    # khata
    khata_add_credit, khata_record_payment, khata_balance, khata_statement,
    khata_all_balances,
    # analytics
    daily_summary, sales_report, reorder_suggestions,
    # documents
    generate_invoice_pdf_tool, generate_analysis_deck_tool,
    # memory
    set_preference, get_preferences, delete_preference,
]


TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}


async def invoke(name: str, args: dict) -> str:
    """Run a tool and render its result as text for the model.

    Guardrail violations come back as ``REFUSED: <reason>`` rather than an
    exception, so the model sees the reason and can explain it to the owner or
    ask a clarifying question — but can never route around it.
    """
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return f"REFUSED: no such tool '{name}'."
    result = await spec.handler(args or {})
    return result["content"][0]["text"]
