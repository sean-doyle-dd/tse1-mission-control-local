# Mission Control (local, non-Docker)

Standalone local version of the full onboarding tracker (Mission Log,
Ground Control, Resources, Meet & Greets, Badges, Ticket Log) — SQLite
instead of Postgres/Docker, and Zendesk access goes through the local
`claude` CLI (already authenticated, already has the Zendesk MCP connected)
instead of a stored API token. No API keys stored anywhere in this app.

## Run it

```bash
cd ticket-log-local
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:8091**.

(If your browser can't reach a given port — some corporate security tools
intercept specific ports — change the port in the last two lines of
`app.py` and restart.)

Data lives in `tickets.db` (SQLite: the ticket log rows, plus a single
`mission_doc` row holding the Mission Log/Ground Control/etc. JSON blob).
Gitignore this file — it's your personal ticket history and onboarding
progress, not something to commit.

## Ticket Log: Fetch & summarize / My Queue

Both features shell out to `claude -p ...` on this machine — same as
running it in a terminal yourself, using your existing login and Zendesk
MCP connection:

```
claude -p "<prompt>" \
  --output-format json \
  --model claude-haiku-4-5-20251001 \
  --allowedTools "mcp__claude_ai_Zendesk_MCP__get_ticket mcp__claude_ai_Zendesk_MCP__list_ticket_comments" \
  --permission-mode bypassPermissions
```

`--allowedTools` restricts the subprocess to specific read-only Zendesk
tools per feature — no Bash/Edit/Write — since ticket content is untrusted
external text.

**Fetch & summarize** (paste a ticket URL) autofills three things:
- **Problem** — what the customer reported, in their own terms
- **Solved** — what *you* (the assigned agent) actually said/did in your
  replies, written in first person, so your ticket log reads like your own
  resolution notes rather than a re-summary of the customer's message
- **Priority** — shown as a colored pill on the saved card

**My Queue** pulls your currently assigned open/pending tickets via
`list_my_tickets` so you can jump straight to logging one with "Log this."

**Expect 20-50 seconds per call** (a real Claude agent invocation, not an
instant API call), and note each call counts against your normal Claude
usage. There's also a known intermittent hiccup where the Zendesk
connector's auth isn't picked up by a fresh headless invocation — the app
retries automatically (up to 3 attempts) when it sees that specific error.

If a ticket is AI-restricted (HIPAA/opt-out), fetch returns a 403 and
refuses to summarize it — same guardrail Zendesk MCP itself enforces.

## What's not included (vs. the Docker Mission Control app)

No APM tracing, no CSM vulnerability/posture scanning — those depend on
running as a container under the Datadog Agent. See
`tse1-tracker-docker/` for that version, and the "Non-Docker Version"
section of the Confluence write-up for why both exist.
