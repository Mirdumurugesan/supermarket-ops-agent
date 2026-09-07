# Kirana Ops Agent

Run an entire Indian kirana store from a Telegram chat — receive stock, cut and
edit bills, run khata, close the day, generate GST invoices and analysis decks.
No web app, no admin panel, no forms. The chat *is* the product.

**Bot:** [@YOUR_BOT_HANDLE](https://t.me/YOUR_BOT_HANDLE) · **Demo video:** *(link)*

```
Owner: 50 packets of Maggi came in, cost ₹12, MRP ₹14
Agent: ✅ Maggi Noodles 70g: 6 → 56 packets.

Owner: bill: 2kg sugar, 1 aashirvaad atta, 4 maggi, 1 amul butter, UPI
Agent: Bill #12 — ₹465 (GST ₹18.24). Say "done" to close it.

Owner: drop the butter, make it 6 maggi
Agent: Updated. ₹437.

Owner: done
Agent: ✅ INV-20260906-003 · ₹437 · UPI. Want the PDF?
```

## The harness, and why

**Pydantic AI.** It gives the control loop the brief asks for — observe → reason
→ act → feed the result back → continue — with tool calls chained (and
parallelised) by the model, not by me. Explicitly not a LangGraph-style
node-per-command machine: that would re-encode in graph edges the routing the
model should be doing.

I built this on the Claude Agent SDK first and moved. The reason was measured,
not aesthetic: **29 tools with per-parameter descriptions cost ~4,300 tokens of
schema per request**, and a multi-item bill is six round-trips — roughly 30,000
tokens per owner message. Groq's free tier allows 6,000 tokens/minute, so HTTP
429 is a *normal operating condition* here. So the model is a chain, not a
choice:

```bash
KIRANA_MODELS=groq:llama-3.3-70b-versatile,openai:gpt-4o-mini
```

Groq serves every turn it can; on a 429, `FallbackModel` hands the same turn —
same conversation, same draft bill — to the next model, which finishes it. A
model with no API key is dropped at startup, so the bot runs on the free Groq
key alone. Pydantic AI was the harness that made the store provider-agnostic.

## The control loop

Telegram long-polls → the update id is recorded (at-least-once delivery, so
duplicates are dropped at ingress) → one conversation per chat, serialized by a
per-chat lock → the agent runs until the model answers, bounded by
`UsageLimits` → generated files are drained from an `outbox` table and sent as
documents. `/new` clears the conversation; the owner's preferences don't move,
because they live in SQLite and are re-read into the instructions every run.

## Skill & tool design

29 thin tools in six groups — inventory, billing, khata, analytics, documents,
memory — designed around the store's capabilities rather than the brief's
example sentences. Three decisions carry the design:

- **One tool per state transition, no mega-tool.** There is no
  `create_bill_from_text`. Parsing "2kg sugar, 4 maggi, UPI" is the model's job;
  the tools are the primitives it composes. Every call site is then a place a
  rule can be enforced.
- **Hand-written JSON Schema, not schemas derived from type hints.** A derived
  schema marks every field required, forcing the model to invent an MRP on
  every `add_product`. Each parameter carries its own description; tool-call
  accuracy tracks these more closely than prompt wording.
- **Chat scoping is ambient.** Billing tools never accept a `chat_id` — it
  rides on a contextvar, so one chat cannot touch another's draft bill.

## The hard parts

| | How |
|---|---|
| **Grounding** | Billing tools take `product_id`, never a name and price. There is no path to a price that doesn't come from `search_products`. |
| **Oversell guard** | Finalize decrements with `UPDATE … WHERE qty >= :q` and checks rowcount; a failure rolls back the whole bill. Under it, `CHECK (qty >= 0)` in the schema — raw SQL can't go negative either. |
| **GST correctness** | All `Decimal`. Inclusive prices, per-SKU HSN and slab, CGST rounded half-up with SGST taking the remainder so the breakup reconciles to the paisa. Rupee round-off shown as its own line. |
| **Multi-turn bills** | A draft row accumulates items across messages; stock moves only on finalize. |
| **Idempotency** | Three layers: `update_id` PRIMARY KEY at ingress, `UNIQUE` idempotency key on the bill, and finalize-of-finalized returns the same bill. |
| **Concurrency** | WAL + `BEGIN IMMEDIATE`; the whole finalize is one transaction. Two bills racing for the last 6 packets → one wins, one gets a clean refusal, stock lands at 2. |
| **Guardrails** | No sell-below-cost without an explicit override, no pricing above MRP, no deleting stock or products, no settling a khata that doesn't exist. All in the service layer, returned as `REFUSED: <reason>`. |
| **Real artifacts** | ReportLab GST invoice (HSN, CGST/SGST, slab summary, amount in words); python-pptx deck with real matplotlib charts. |
| **Memory** | Preferences in SQLite, outside the context window, re-injected each run. |

**The layering rule:** a business rule is enforced where the data changes. The
prompt carries persona and orchestration habits only — every test passes with
the LLM removed.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env          # TELEGRAM_BOT_TOKEN + GROQ_API_KEY (both free)
python scripts/seed.py        # 25 real SKUs
pytest -q                     # 71 passed, ~5s, no API key needed
python -m src.kirana.main
```

`docker build -t kirana-agent . && docker run --env-file .env -v $(pwd)/data:/app/data kirana-agent`

## More

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — full tool inventory, the harness
  migration in detail, trade-offs, what I'd do with more time
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — free 24/7 hosting, the model chain
- `scripts/smoke_demo.py` — a full day of trading through the services, no LLM

```
src/kirana/
  agent/     tools.py (29 ToolSpecs, no framework imports) · harness.py · system_prompt.py
  services/  gst · inventory · billing · khata · analytics · memory   ← rules live here
  db/        schema.sql · database.py                                 ← invariants as constraints
  docs_gen/  invoice_pdf.py · analysis_pptx.py
  telegram/  bot.py    health.py
tests/       71 tests — GST maths, oversell, idempotency, threaded concurrency,
             the agent loop, and model failover. No API key, no network.
```
