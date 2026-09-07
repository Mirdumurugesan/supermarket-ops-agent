# Deployment — keeping the bot alive through the review, for ₹0

The reviewers drive the bot themselves, so it has to answer from submission
until they're done. Long polling means **no public URL is required** — but the
free hosts that cost nothing are all *web* service tiers, so the app also
serves a small `/` health page (`src/kirana/health.py`) that binds `$PORT` when
the host sets one. That single endpoint is what makes free hosting work.

Everything below is free with **no credit card**.

---

## Option A — Render free web service (what this bot runs on)

1. Push to GitHub → [render.com](https://render.com) → **New → Web Service** →
   connect the repo.
2. Language **Docker** — it reads the `Dockerfile`. Instance type **Free**.
3. **Environment** → add:
   ```
   TELEGRAM_BOT_TOKEN=...
   GROQ_API_KEY=...
   GOOGLE_API_KEY=...        # free, no card — this is what catches Groq's 429s
   OPENAI_API_KEY=           # optional, paid; blank is fine
   ```
   Render sets `PORT` itself — don't set it.
4. Deploy. Open the Render URL: you should see the JSON status page listing the
   resolved model chain. Then message the bot on Telegram.

**Keep it awake.** A free instance sleeps after ~15 minutes with no traffic,
and a sleeping bot doesn't poll Telegram. Create a free monitor at
[uptimerobot.com](https://uptimerobot.com) (no card) → **HTTP(s)** → your
Render URL → interval 5 minutes. That's what the health page is for.

**Storage caveat:** the free instance's disk is ephemeral — a redeploy resets
the store to seed data. That's fine for a review, and arguably good (reviewers
get a clean shop). It's noted here so it reads as a decision, not a surprise.

## Option B — Hugging Face Spaces

Free, no card, and a Docker Space runs the image as-is.

1. huggingface.co → **New Space** → SDK **Docker** → *Blank*. Public is fine;
   secrets go in Settings, not the repo.
2. Push the project into the Space repo and add a header to the Space's root
   `README.md` so HF knows which port to expose:
   ```
   ---
   title: Kirana Ops Agent
   sdk: docker
   app_port: 7860
   ---
   ```
3. **Settings → Variables and secrets** → the same four keys as above, plus
   `PORT=7860`.
4. Same UptimeRobot monitor, pointed at the Space URL.

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
| `groq:openai/gpt-oss-120b` | **free**, no card | 8,000 tokens/min | **Primary.** Strong tool-calling, fastest replies. This agent sends ~3,850 tokens of schema per request, so a long bill trips the per-minute quota routinely. |
| `groq:openai/gpt-oss-20b` | **free**, no card | its own 8,000/min | **Second.** Groq meters *per model*, so a 429 on the 120B rolls here and stays free. |
| `google:gemini-3.6-flash` | **free**, no card | 1M tokens/min | **Safety net.** Far wider window; catches whatever gets past both Groq models. Occasionally answers 503 under load, which the harness backs off and retries. |
| `openai:gpt-4o-mini` | paid, ~₹0.40 per demo run | — | **Optional last resort.** Not in the default chain. |

```bash
# default — three free models, no credit card anywhere
KIRANA_MODELS=groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b,google:gemini-3.6-flash
```

**Running Groq-only?** Everything works. When both Groq models trip their
quota, the turn waits out the window Groq quotes rather than failing over — so
keep demo bills to three or four items and pause a beat between messages.
Nothing breaks; it just slows.

Both Groq and Google retire model IDs periodically, and a retired ID surfaces
as a 404 at the first message. List what your keys can actually use:

```bash
curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"
curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GOOGLE_API_KEY"
```

Keys: [console.groq.com/keys](https://console.groq.com/keys) ·
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) — both free,
no card.

## Health checks

```bash
curl https://<your-render-url>/     # status, resolved model chain, bill count

sqlite3 data/kirana.db "SELECT COUNT(*) FROM bills WHERE status='finalized';"
sqlite3 data/kirana.db "SELECT key, value FROM preferences;"
```
