# Deployment — keeping the bot alive through the review, for ₹0

The reviewers drive the bot themselves, so it has to answer from submission
until they're done. Long polling means **no public URL is required** — but the
free hosts that cost nothing are all *web* service tiers, so the app also
serves a small `/` health page (`src/kirana/health.py`) that binds `$PORT` when
the host sets one. That single endpoint is what makes free hosting work.

Everything below is free with **no credit card**.

---

## Option A — Hugging Face Spaces (recommended)

Free, no card, and a Docker Space runs your image as-is.

1. huggingface.co → sign up → **New Space**
   - SDK: **Docker** → *Blank*
   - Visibility: **Public** is fine (your secrets go in Settings, not the repo)
2. Push this project into the Space repo, and add one line to `README.md` at
   the Space root so HF knows which port to expose:
   ```
   ---
   title: Kirana Ops Agent
   sdk: docker
   app_port: 7860
   ---
   ```
3. **Settings → Variables and secrets**, add as *secrets*:
   ```
   TELEGRAM_BOT_TOKEN=...
   GROQ_API_KEY=...
   OPENAI_API_KEY=...        # optional — omit to run at zero cost
   PORT=7860
   ```
4. It builds and starts. Open the Space URL — you should see the JSON status
   page. Message the bot on Telegram to confirm.

**Keep it awake.** Free Spaces pause after a stretch with no traffic. Create a
free monitor at [uptimerobot.com](https://uptimerobot.com) (no card) pointed at
your Space URL, interval 5 minutes. That's what the health page is for.

**Storage caveat:** a free Space's disk is ephemeral — a rebuild resets the
store to seed data. That's *fine for a review*, and arguably good (reviewers
get a clean shop). Say so in your README so it reads as a decision, not a bug.

## Option B — Render free web service

Also free, no card.

1. Push to GitHub → render.com → **New → Web Service** → connect the repo
2. Runtime **Docker**; it reads the `Dockerfile`
3. **Environment** → add `TELEGRAM_BOT_TOKEN` and `GROQ_API_KEY` (plus
   `OPENAI_API_KEY` if you want the fallback). Render sets `PORT` itself.
4. Free instances sleep after ~15 minutes idle — point UptimeRobot at the
   Render URL the same way.

## Option C — your laptop (backup only)

```bash
python -m src.kirana.main
```

Zero setup, but the bot dies when the lid closes. Acceptable only if you're
around to restart it; don't rely on it for a multi-day review window.

---

## The model chain

`KIRANA_MODELS` is a comma-separated list, best first. A model whose key is
missing is dropped at startup with a warning, so the same config works with one
key or three.

| Model | Cost | Free-tier ceiling | Role |
|---|---|---|---|
| `groq:llama-3.3-70b-versatile` | **free**, no card | 6,000 tokens/min, 1,000 req/day | **Primary.** Very fast. This agent sends ~4,300 tokens of tool schema per request, so a long multi-item bill can trip the per-minute quota. |
| `openai:gpt-4o-mini` | paid, ~₹0.40 per demo run | — | **Fallback.** Picks up a turn Groq rate-limited, conversation intact. Optional. |

```bash
# default — free primary, paid safety net
KIRANA_MODELS=groq:llama-3.3-70b-versatile,openai:gpt-4o-mini

# strictly zero cost — just leave OPENAI_API_KEY blank
KIRANA_MODELS=groq:llama-3.3-70b-versatile
```

**Running Groq-only?** Everything works. When the per-minute quota trips, that
turn waits instead of failing over, so keep demo bills to three or four items
and pause a beat between messages. Nothing breaks — it just slows.

Keys: [console.groq.com/keys](https://console.groq.com/keys) (free, no card) ·
[platform.openai.com/api-keys](https://platform.openai.com/api-keys) (paid)

## Health checks

```bash
curl https://<your-space-or-render-url>/     # status, model chain, bill count

sqlite3 data/kirana.db "SELECT COUNT(*) FROM bills WHERE status='finalized';"
sqlite3 data/kirana.db "SELECT key, value FROM preferences;"
```

## Before you submit

- [ ] Bot handle written into the README (replacing `@YOUR_BOT_HANDLE`)
- [ ] `.env` not committed — `git log --all --full-history -- .env` returns nothing
- [ ] Deployed instance answering, uptime monitor pinging it
- [ ] `pytest -q` green on a clean clone (71 tests)
- [ ] Repo **private**, with `Aswath363`, `akshaiP`, `ashwanthnebula` invited
