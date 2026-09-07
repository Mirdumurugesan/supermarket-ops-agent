# Replay the project as a sequence of commits that shows progression.
#
# PowerShell twin of make_history.sh, for Windows without Git Bash.
# Run ONCE in a fresh copy of this folder, before pushing to GitHub:
#   .\scripts\make_history.ps1
#
# Read each commit before you push — you should be able to explain every
# line of it.

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (Test-Path .git) {
  Write-Host "A .git directory already exists here. Remove it first for a fresh history."
  exit 1
}

git init -q

git add .gitignore requirements.txt .env.example
git commit -qm @"
chore: project skeleton, requirements, env template
"@

git add src/kirana/__init__.py src/kirana/db/__init__.py src/kirana/db/schema.sql
git commit -qm @"
feat(db): schema with stock/khata/bill tables and CHECK constraints

Business invariants belong in the schema: qty >= 0 makes negative stock
unrepresentable, and a UNIQUE idempotency_key makes a double finalize
impossible regardless of what the application layer does.
"@

git add src/kirana/db/database.py
git commit -qm @"
feat(db): WAL + BEGIN IMMEDIATE transaction helper

Acquiring the write lock up front serializes concurrent finalizes instead
of letting them interleave.
"@

git add src/kirana/services/__init__.py src/kirana/services/gst.py src/kirana/services/errors.py
git commit -qm @"
feat(gst): Decimal GST engine with inclusive pricing and CGST/SGST split

Prices are GST-inclusive (Indian retail norm) so taxable value is backed
out. CGST rounds half-up, SGST takes the remainder, so the breakup always
reconciles to the line total exactly.
"@

git add tests/conftest.py tests/test_gst.py
git commit -qm @"
test(gst): tax breakup reconciles to the paisa across slabs
"@

git add src/kirana/services/inventory_service.py scripts/seed.py
git commit -qm @"
feat(inventory): alias search, stock receipt, pricing guardrails

Search matches colloquial names (atta, paruppu, surf) because that is what
the owner types. Below-cost and above-MRP pricing are refused here, not
hoped for in a prompt.
"@

git add src/kirana/services/khata_service.py
git commit -qm @"
feat(khata): credit ledger, settle-only-existing guardrail
"@

git add src/kirana/services/billing_service.py
git commit -qm @"
feat(billing): multi-turn drafts, atomic idempotent finalize

Stock is untouched while drafting. Finalize decrements with a guarded
UPDATE whose rowcount is checked, so an oversell rolls the whole bill back;
a retried finalize returns the existing bill without double-charging.
"@

git add tests/test_billing_hard_parts.py
git commit -qm @"
test(billing): oversell refused, partial failure rolls back, retries safe
"@

git add tests/test_concurrency.py
git commit -qm @"
test(concurrency): racing bills and sale-vs-stock-in keep books exact
"@

git add src/kirana/services/analytics_service.py
git commit -qm @"
feat(analytics): IST daily close, range report, velocity-based reorder

Date bucketing shifts UTC by +05:30 — a kirana closes its day on Indian
time, not UTC.
"@

git add src/kirana/services/memory_service.py
git commit -qm @"
feat(memory): durable owner preferences outside the context window
"@

git add src/kirana/docs_gen/__init__.py src/kirana/docs_gen/invoice_pdf.py
git commit -qm @"
feat(docs): GST tax invoice PDF with HSN, slab summary, round-off
"@

git add src/kirana/docs_gen/analysis_pptx.py
git commit -qm @"
feat(docs): analysis deck with real matplotlib charts
"@

git add tests/test_khata_and_memory.py tests/test_documents.py scripts/smoke_demo.py
git commit -qm @"
test: khata guardrails, durable memory, artifact generation

smoke_demo.py runs a full day of trading through the services with no LLM
in the loop - the store has to be correct on its own.
"@

git add src/kirana/agent/__init__.py src/kirana/agent/tools.py tests/test_tool_surface.py
git commit -qm @"
feat(agent): harness-neutral tool surface over the services

Deliberately no create_bill_from_text mega-tool: parsing human phrasing is
the model's job, the tools are the primitives it composes. One tool per
state transition means every call site is a place a rule is enforced.

A tool is a plain ToolSpec - name, description, JSON Schema, handler - with
no agent-framework types in it, so the harness stays replaceable.

Schemas are hand-written rather than derived from type hints: a derived
schema marks every field required, which would force the model to invent an
MRP on every add_product call.
"@

git add src/kirana/agent/system_prompt.py src/kirana/agent/harness.py src/kirana/config.py
git commit -qm @"
feat(agent): Pydantic AI harness, per-chat conversations, /new

Model-agnostic on purpose. The tool surface costs ~3.85k tokens of schema per
request and a multi-item bill is six round-trips, so a provider's tokens-per-
minute ceiling is a real design constraint rather than a footnote.

The prompt carries persona and orchestration habits only. Business rules
stay in the services, so the tests pass with the model removed.
"@

git add tests/test_model_chain.py
git commit -qm @"
feat(agent): free model chain, 429 is not fatal

On Groq's free tier (8k tokens/min per model) a busy minute returns 429, which for this
workload is a normal operating condition, not an exception. FallbackModel
hands the same turn to the next model with the conversation intact, so the
owner sees a slower reply instead of a dead shop.

A model whose key is missing is dropped from the chain at startup, so the
same .env runs on Groq alone at zero cost.
"@

git add tests/test_agent_loop.py pytest.ini
git commit -qm @"
test(agent): control loop driven by a scripted model

Asserts on what we own - tools reach the model with schemas intact, results
flow back, refusals arrive as text, /new clears context but not memory.
Whether the LLM picks the right tool is the provider's job, not ours.
"@

git add src/kirana/telegram/__init__.py src/kirana/telegram/bot.py src/kirana/main.py
git commit -qm @"
feat(telegram): long-polling bot with update dedup and file outbox
"@

git add src/kirana/telegram/formatting.py tests/test_formatting.py
git commit -qm @"
fix(telegram): render the model's Markdown instead of showing it

The model writes Markdown; Telegram renders none of it without a parse_mode,
so the owner was reading literal **Bill Finalized**.

Converted to Telegram's HTML mode rather than either Markdown mode: MarkdownV2
requires escaping fourteen characters including . - ( ), all of which appear in
every reply (Rs.144.00, Loose Sugar (2 kg), INV-20260907-001), and one missed
escape drops the whole message with a 400. Unrendered asterisks are ugly; a
dropped bill is a bug. Falls back to plain text if Telegram rejects the markup.
"@

git add src/kirana/health.py
git commit -qm @"
feat(health): status endpoint so free web-service tiers will host it

The bot is a worker with no web surface, but every free host expects a port
and idles anything with no traffic. One JSON page solves both.
"@

git add README.md docs/ARCHITECTURE.md
git commit -qm @"
docs: one-page README, architecture notes alongside

The brief asks for ~1 page. Depth that does not fit lives in
docs/ARCHITECTURE.md rather than being cut.
"@

git add Dockerfile docs/ scripts/make_history.sh scripts/make_history.ps1 .vscode/
git commit -qm @"
chore: Dockerfile, deployment guide, VS Code run configs
"@

git add -A
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) { git commit -qm "chore: remaining project files" }

Write-Host ""
Write-Host "Done. Review with:  git log --oneline"
Write-Host "Then:  git remote add origin https://github.com/<you>/supermarket-ops-agent.git"
Write-Host "       git branch -M main; git push -u origin main"
