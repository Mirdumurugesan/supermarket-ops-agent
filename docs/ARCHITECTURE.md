# Architecture notes

Depth behind the one-page [README](../README.md): the harness decision and the
measurement that drove it, the full tool inventory, and how each of the brief's
nine hard parts is enforced.

```
Owner: 50 packets of Maggi came in, cost ₹12, MRP ₹14
Agent: ✅ Stock in — Maggi Noodles 70g: 6 → 56 packets. Cost ₹12, MRP ₹14.

Owner: make a bill: 2kg sugar, 1 aashirvaad atta 5kg, 4 maggi, 1 amul butter, UPI
Agent: Bill #12 — ₹465 (incl. GST ₹18.24). Say "done" to close it.

Owner: drop the butter, make it 6 maggi
Agent: Updated. Bill #12 — ₹431.

Owner: done
Agent: ✅ Invoice INV-20260904-003 · ₹431 · UPI. Want the PDF?
```

---

## 1. The harness, and why

**Pydantic AI**, with the store exposed as a set of hand-written JSON-Schema
tools.

| Requirement in the brief | How this harness meets it |
|---|---|
| "A real control loop: observe → reason → act → feed result back → continue" | Pydantic AI *is* that loop. It calls the model, executes the tool calls it asks for — in parallel where they're independent, which is most of a multi-item bill — feeds results back, and continues until the model answers. `UsageLimits(request_limit=...)` bounds a turn so a confused one can't spin. |
| "The model orchestrates, not a regex router" | Tool selection and argument extraction are the model's job. There is no intent classifier, no keyword table, no `if/elif` routing anywhere in this repo. |
| "You author the skills and tools" | Tools are `ToolSpec`s I own — name, description, JSON Schema, async handler — with no framework types in them. |
| "Ask a clarifying question when genuinely ambiguous" | Emerges from the model reasoning over tool results (`search_products` returning two attas), not from a hardcoded branch. |
| Memory across sessions | One conversation per Telegram chat; `/new` clears it. Durable memory is in SQLite and re-read into the instructions on every run, so it survives both `/new` and a restart. |
| Resilience under a free-tier quota | `FallbackModel` moves a rate-limited turn to the next model in the chain, conversation intact. |

**Explicitly not LangGraph.** A node-per-command state machine would re-encode
in graph edges exactly the routing the model should be doing. The brief calls
that out and I agree: the interesting failure mode of a store agent isn't
"which node do I go to", it's "did the books stay correct" — a data-layer
problem, not a graph problem.

**Why Pydantic AI over the Claude Agent SDK.** I built this on the Claude Agent
SDK first. It works well, and the swap touched ~120 lines because the tool
surface never depended on either. What decided it was a constraint I measured
rather than a preference:

This agent presents 29 tools with per-parameter descriptions — about **3,850
tokens of schema on every request**. A single "2kg sugar, 1 atta, 4 Maggi, UPI"
is six model round-trips, so one owner message costs roughly 25,000 tokens.
Groq's free tier is metered at 8,000 tokens per minute *per model*. That makes
HTTP 429 a *normal operating condition* for this workload, not an exception —
and a store that stops taking bills when the quota trips is not a store.

So the model is a **chain**, not a choice:

```bash
KIRANA_MODELS=groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b,google:gemini-3.6-flash
```

Groq serves every turn it can. When it returns 429, Pydantic AI raises
`ModelHTTPError` and `FallbackModel` hands the same turn — same conversation,
same tools, same draft bill — to the next model, which finishes it. The owner
sees a slightly slower reply instead of an error. `test_model_chain.py` proves
this with a primary that raises 429 and a fallback that answers.

Because Groq meters *per model*, the second link is a second Groq model with
its own budget — a free retry that costs one extra hop. Gemini's much wider
window then catches anything that gets past both.

Two failure modes had to be handled explicitly, and both fail silently if you
get them wrong:

- **Provider-side retries are disabled** (`max_retries=0` on each client). The
  Groq and OpenAI SDKs retry a 429 internally with backoff before raising,
  which swallows the error — `FallbackModel` never fires and the bot simply
  hangs for two minutes. Retrying is the chain's decision, not the SDK's.
- **An exhausted chain waits, but only for the right reasons.** A 429 sleeps
  exactly as long as the provider quotes ("try again in 5.295s"); a 503
  ("experiencing high demand") backs off exponentially; a 404 for a retired
  model id or a 401 for a bad key is fatal and surfaces immediately instead of
  sleeping three times first.

Three properties fall out of that design, and they're the ones I'd defend:

- **It degrades instead of breaking.** A model whose API key is absent is
  dropped from the chain at startup with a warning. With only the free
  `GROQ_API_KEY` set, the bot still runs — it just waits out a quota trip
  rather than failing over. Adding a key later changes no code.
- **The store knows about no provider at all.** `tools.py` imports no agent
  framework; `config.py` is the only file that names a vendor.
- **It costs nothing to run.** Every link in the default chain is free and
  needs no credit card. `openai:gpt-4o-mini` can be appended as a paid last
  resort (~₹0.40 for a full demo run), but nothing depends on it.

## 2. Architecture

```
Telegram (long polling)
   │  update_id → dedup at ingress (processed_updates)
   ▼
AgentManager ── one conversation per chat, asyncio.Lock per chat
   │            instructions ← durable preferences (SQLite), re-read each run
   ▼
Pydantic AI agent — 29 thin tools (harness-neutral ToolSpecs)
   │            model = Groq → OpenAI fallback chain (429 = expected, not fatal)
   │            tools = verbs; they cannot bypass a rule
   ▼
Service layer — inventory · billing · khata · analytics · memory
   │            every business rule enforced HERE
   ▼
SQLite (WAL) — CHECK constraints, UNIQUE idempotency keys, BEGIN IMMEDIATE
   │
   └──▶ docs_gen — ReportLab PDF invoice · python-pptx + matplotlib deck
                   → outbox table → delivered as Telegram documents
```

**The layering rule:** a business rule is enforced where the data changes.
The prompt describes *behaviour*; the schema and services enforce *invariants*.
Every test in `tests/` passes with the LLM removed entirely — that's the point.

### Skill & tool design

I designed the surface around the store's real capabilities, not around the
example sentences in the brief. Twenty-nine thin tools in six groups:

| Group | Tools | Design note |
|---|---|---|
| Inventory | `search_products`, `add_product`, `receive_stock`, `update_product`, `adjust_stock`, `stock_level`, `low_stock_report` | `search_products` is mandatory before anything else — it's how grounding is enforced. It matches colloquial aliases (`atta`, `paruppu`, `surf`) because that's what the owner actually types. There is no `delete_product` and no raw stock write: corrections go through `adjust_stock`, which logs the delta. |
| Billing | `start_bill`, `add_bill_item`, `set_bill_item_qty`, `set_payment_mode`, `finalize_bill`, `cancel_bill`, `get_bill`, `get_current_bill`, `latest_bill` | Deliberately *not* a `create_bill_from_text` mega-tool. Parsing "2kg sugar, 4 maggi, UPI" is the model's job; the tools are the primitives it composes. `set_bill_item_qty(qty=0)` is how "drop the butter" works. |
| Khata | `khata_add_credit`, `khata_record_payment`, `khata_balance`, `khata_statement`, `khata_all_balances` | Credit and payment are separate tools because they have different guardrails — you can create a customer by extending credit, but you cannot settle a khata that doesn't exist. |
| Analytics | `daily_summary`, `sales_report`, `reorder_suggestions` | `daily_summary` is the daily close; `sales_report` feeds the deck. All date bucketing is IST — a kirana closes its day on Indian time, not UTC. |
| Documents | `generate_invoice_pdf`, `generate_analysis_deck` | Tools generate the file and queue it in an `outbox` table; the Telegram layer drains the outbox after the turn. Keeps document delivery out of the agent loop. |
| Memory | `set_preference`, `get_preferences`, `delete_preference` | Free-form key/value. The model decides what's worth remembering. |

Tool granularity was the main design decision. Too coarse and the model stops
reasoning (a mega-tool hides the store's state from it); too fine and simple
requests burn ten round-trips. Landing on *one tool per state transition* means
a bill edit is one call, a whole multi-item bill is a natural chain, and every
call site is a place a rule can be enforced.

Two smaller decisions that mattered more than expected:

- **Hand-written JSON Schema, not schemas derived from type hints.** A derived
  schema marks *every* field required, so `add_product` would have forced the
  model to invent an MRP and a brand on every call. Each parameter also carries
  its own description — in practice tool-call accuracy tracks these far more
  closely than it tracks system-prompt wording. There's a test that fails if a
  parameter loses its description or an optional field drifts into `required`.
- **Chat scoping via contextvar, not a tool argument.** The billing tools never
  accept a `chat_id`; it's ambient. The model cannot address another chat's
  draft bill even if it tries.

## 3. The hard parts

**1. Grounding.** Prices, GST slabs and stock come only from the DB. The system
prompt forbids invention, but more importantly there is no path to a price that
doesn't go through `search_products` — the billing tools take `product_id`, not
a name and a price. The model *cannot* pass a made-up price into a bill.

**2. Oversell guard.** Two layers.
- The draft-time check in `add_item` gives a fast, friendly refusal.
- The real guarantee is at finalize: `UPDATE products SET qty = qty - :q WHERE id = :id AND qty >= :q`. A zero rowcount raises `OversellError` and rolls back the *entire* bill. Under it all, `qty REAL NOT NULL CHECK (qty >= 0)` in the schema — stock cannot go negative even if you bypass the services and write SQL by hand (there's a test that does exactly that).

**3. GST correctness.** `services/gst.py`, all `Decimal`, no floats.
Prices are GST-inclusive (Indian retail norm), so taxable value is backed out:
`taxable = line_total / (1 + rate)`. CGST rounds half-up to 2dp and SGST takes
the remainder, so `taxable + CGST + SGST == line_total` to the paisa, always —
including the odd-paisa cases where the tax doesn't halve evenly. The invoice
total rounds to the nearest rupee with an explicit round-off line. Slabs and
HSN codes are per-SKU *data*, so a GST council rate change is an `UPDATE`, not
a deploy.

**4. Multi-turn bills.** A bill is a `draft` row that accumulates items over any
number of messages. Stock is untouched while drafting. One open draft per chat
keeps "the bill" unambiguous when the owner says "make it 6 maggi" three
messages later.

**5. Idempotency.** Telegram delivers at-least-once. Three layers:
- Ingress: every `update_id` is inserted into `processed_updates` (PRIMARY KEY); a duplicate insert means "already seen", drop it.
- Tool: `finalize_bill` derives its key from the update id, and the key is `UNIQUE` on `bills`.
- Semantics: finalizing an already-finalized bill returns that bill unchanged. A retry never double-bills or double-decrements — tested both ways (same key, and different key).

**6. Concurrency.** WAL mode plus `BEGIN IMMEDIATE` gives real write
serialization; the whole finalize is one transaction. Two bills racing for the
last 6 packets of Maggi → exactly one wins, the other gets a clean
`OversellError`, and stock lands at exactly 2. A sale racing a stock-in
commutes correctly. Both are tested with real threads.

**7. Guardrails.** Enforced in the service layer, returned to the model as
`REFUSED: <reason>` so it can explain or ask:
- Can't sell below cost without an explicit `allow_below_cost` override.
- Can't price above MRP at all (it's illegal).
- Products are deactivated, never deleted; stock is adjusted, never wiped.
- Can't settle a khata that doesn't exist, or overshoot a balance.
- Packaged goods reject fractional quantities (2.5 packets of Maggi is not a thing); loose goods accept them.

**8. Real artifacts.** ReportLab generates a proper GST tax invoice — seller
block with GSTIN, per-line HSN/qty/rate/taxable/CGST/SGST, slab-wise tax
summary, round-off, amount in words. python-pptx + matplotlib generate an
8-slide analysis deck with genuine charts (daily revenue, top items, payment
mix, GST by slab) plus velocity-based reorder advice. Both are produced by the
agent's own tools and pushed to the chat as documents.

**9. Memory across sessions.** Preferences live in a `preferences` table,
outside the context window. They're injected into the system prompt when a
session starts, and the model updates them mid-conversation via
`set_preference`. `/new` disconnects the SDK session — fresh context — but the
next message still knows the shop's GSTIN and that you always pay by UPI.

## 4. Setup

```bash
git clone <this repo> && cd supermarket-ops-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in two keys (both free, no card)
python scripts/seed.py    # 25 real SKUs, 3 khata customers, shop defaults
python -m src.kirana.main
```

`.env`:

```
TELEGRAM_BOT_TOKEN=...     # @BotFather on Telegram — free
GROQ_API_KEY=...           # console.groq.com/keys — free, no credit card
GOOGLE_API_KEY=...         # aistudio.google.com/apikey — free, no credit card
OPENAI_API_KEY=            # optional paid last resort; blank is fine
KIRANA_MODELS=groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b,google:gemini-3.6-flash
```

Groq alone is enough to run everything. The Gemini key only changes what
happens when both Groq models trip their quota mid-bill: with it, the turn
finishes on the fallback; without it, the turn waits out the window Groq
quotes. Startup validates the config, drops unusable models from the chain with
a warning, and fails with an actionable message rather than dying on the
owner's first message.

**Docker:** `docker build -t kirana-agent . && docker run --env-file .env -v $(pwd)/data:/app/data kirana-agent`.

**Hosting:** the bot is a long-polling worker, but it also serves a small
health page when the host sets `$PORT`, so it runs on free *web service* tiers
(Hugging Face Spaces, Render) with no credit card. See
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

**Tests:** `pytest -q` → 115 tests, ~5 seconds, no API key and no network needed.

| File | Covers |
|---|---|
| `test_gst.py` | Inclusive-price back-out, CGST/SGST split, odd-paisa remainder, per-slab totals, round-off |
| `test_billing_hard_parts.py` | Oversell at draft and finalize, rollback on partial failure, idempotent retries, multi-turn edits, invoice numbering |
| `test_concurrency.py` | Two bills racing for the last units, sale vs stock-in, 10 threads with no lost updates |
| `test_khata_and_memory.py` | Khata guardrails, durable preferences across a simulated restart, pricing guardrails, analytics |
| `test_documents.py` | PDF and PPTX really generate, drafts get no invoice, empty periods don't crash |
| `test_tool_surface.py` | Schemas well-formed, optional fields not required, every parameter documented, refusals reach the model |
| `test_agent_loop.py` | The control loop itself, driven by a scripted model: all 29 tools reach the model with schemas intact, tool results flow back, a multi-step bill chains in one turn, refusals surface as text, `/new` clears context but not durable memory |
| `test_model_chain.py` | Failover: a 429 from the primary is picked up by the fallback mid-turn; a missing key degrades the chain instead of crashing; no keys fails fast with the exact variable to set |

## 5. Trade-offs

- **A model chain, not a model.** Coupling the store to one vendor's SDK would
  have made a rate-limit ceiling into a rewrite. The cost is one small adapter
  and a `FallbackModel`; the benefit is that a free tier's 429 is a slower
  reply instead of a dead shop. See §1.
- **SQLite over Postgres.** Single-shop, single-writer workload; WAL plus
  `BEGIN IMMEDIATE` gives the serialization the correctness argument needs, in
  one file with no ops burden. The schema is plain SQL and would port to
  Postgres largely unchanged if a second shop ever shared a database.
- **Long polling over webhooks.** No public URL, no TLS termination, works
  identically on a laptop and a cloud host. Throughput is irrelevant at one
  shop's message volume.
- **One draft bill per chat.** Ambiguity ("which bill?") costs the owner more
  than the flexibility of parallel drafts would gain.
- **Prices GST-inclusive.** Matches how Indian retail actually prices; the
  alternative (exclusive + add tax) is a bigger cognitive mismatch for the owner
  than the extra division costs us.

## 6. With more time

- Expiry/batch tracking with FEFO picking, which changes the decrement from
  "one row" to "oldest batch first" and makes the concurrency story harder.
- Voice-note orders (Whisper → the same tool chain).
- Scheduled weekly deck, auto-sent Monday morning.
- Barcode/photo → item identification.
- Postgres + a `store_id` column for multi-shop.

---

**Structure**

```
src/kirana/
  agent/       tools.py      ← 29 ToolSpecs; imports no agent framework
               harness.py    ← Pydantic AI adapter + per-chat conversations
               system_prompt.py
  services/    gst · inventory · billing · khata · analytics · memory
  db/          schema.sql · database.py   ← invariants live here as constraints
  docs_gen/    invoice_pdf.py · analysis_pptx.py
  telegram/    bot.py        ← transport only: dedup, outbox, /new
  health.py    tiny status endpoint so free web-service tiers will host it
tests/         115 tests, all green, no API key, no network
```
