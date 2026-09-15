import json
import re
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

from flask import Flask, g, jsonify, request, render_template

DB_PATH = Path(__file__).parent / "tickets.db"
CLAUDE_BIN = shutil.which("claude") or "claude"
MISSION_DOC_ID = "sean-doyle"

app = Flask(__name__)


# ---- DogStatsD - talks to the host Agent already set up in Lab 3, no new
# dependency needed since the StatsD wire protocol is a one-line UDP packet.
# Port 8135, not the default 8125 - this Mac's Agent config was remapped by
# Enterprise IT's End User Device Monitoring setup (EITOE-4424).
class DogStatsD:
    def __init__(self, host="127.0.0.1", port=8135):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _send(self, payload):
        try:
            self.sock.sendto(payload.encode(), self.addr)
        except OSError:
            pass  # metrics are best-effort - never let a submit failure break a request

    def gauge(self, metric, value, tags=None):
        self._send(f"{metric}:{value}|g" + self._tag_suffix(tags))

    def increment(self, metric, value=1, tags=None):
        self._send(f"{metric}:{value}|c" + self._tag_suffix(tags))

    def timing(self, metric, ms, tags=None):
        self._send(f"{metric}:{ms}|ms" + self._tag_suffix(tags))

    @staticmethod
    def _tag_suffix(tags):
        return f"|#{','.join(tags)}" if tags else ""


statsd = DogStatsD()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            ref TEXT NOT NULL,
            area TEXT NOT NULL,
            difficulty TEXT,
            priority TEXT,
            link TEXT,
            issue TEXT,
            outcome TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    try:
        conn.execute("ALTER TABLE tickets ADD COLUMN priority TEXT")
    except sqlite3.OperationalError:
        pass  # already has it
    conn.execute(
        """CREATE TABLE IF NOT EXISTS mission_doc (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )"""
    )
    conn.commit()
    conn.close()
    print("[startup] database ready at", DB_PATH)


@app.route("/")
def index():
    return render_template("index.html")


# ---- ticket log ----

def report_db_metrics():
    """Custom metrics for the SQLite file - there's no server process for a
    normal Datadog DB integration to connect to, so the app reports on
    itself instead: row count and file size."""
    try:
        count = get_db().execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        statsd.gauge("ticket_log.tickets.count", count)
    except sqlite3.Error:
        pass
    try:
        statsd.gauge("ticket_log.db.size_bytes", DB_PATH.stat().st_size)
    except OSError:
        pass


@app.route("/api/tickets", methods=["GET"])
def list_tickets():
    db = get_db()
    rows = db.execute("SELECT * FROM tickets ORDER BY created_at DESC, id DESC").fetchall()
    report_db_metrics()
    return jsonify([dict(r) for r in rows])


@app.route("/api/tickets", methods=["POST"])
def create_ticket():
    data = request.get_json(force=True)
    ref = (data.get("ref") or "").strip()
    area = (data.get("area") or "").strip()
    if not ref or not area:
        return jsonify({"error": "ref and area are required"}), 400

    entry_id = str(int(time.time() * 1000))
    row = {
        "id": entry_id,
        "ref": ref,
        "area": area,
        "difficulty": (data.get("difficulty") or "").strip(),
        "priority": (data.get("priority") or "").strip(),
        "link": (data.get("link") or "").strip(),
        "issue": (data.get("issue") or "").strip(),
        "outcome": (data.get("outcome") or "").strip(),
        "notes": (data.get("notes") or "").strip(),
        "created_at": time.strftime("%Y-%m-%d"),
    }
    db = get_db()
    db.execute(
        """INSERT INTO tickets (id, ref, area, difficulty, priority, link, issue, outcome, notes, created_at)
           VALUES (:id, :ref, :area, :difficulty, :priority, :link, :issue, :outcome, :notes, :created_at)""",
        row,
    )
    db.commit()
    statsd.increment("ticket_log.tickets.logged", tags=[f"area:{area}"])
    report_db_metrics()
    return jsonify(row), 201


@app.route("/api/tickets/<ticket_id>", methods=["DELETE"])
def delete_ticket(ticket_id):
    db = get_db()
    db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    db.commit()
    report_db_metrics()
    return jsonify({"ok": True})


# ---- mission doc (onboarding tracker page) ----

@app.route("/api/mission", methods=["GET"])
def get_mission():
    db = get_db()
    row = db.execute("SELECT data FROM mission_doc WHERE id = ?", (MISSION_DOC_ID,)).fetchone()
    if row is None:
        return jsonify(None), 404
    return jsonify(json.loads(row["data"]))


@app.route("/api/mission", methods=["PUT"])
def set_mission():
    data = request.get_json(force=True)
    db = get_db()
    db.execute(
        """INSERT INTO mission_doc (id, data) VALUES (?, ?)
           ON CONFLICT(id) DO UPDATE SET data = excluded.data""",
        (MISSION_DOC_ID, json.dumps(data)),
    )
    db.commit()
    return jsonify({"ok": True})


# ---- Zendesk, via the local `claude` CLI (already authenticated) ----
# No Zendesk API token or Anthropic key needed here - the CLI uses your
# existing Claude Code login and the Zendesk MCP connection it already has.

ZENDESK_TICKET_ID_RE = re.compile(r"/tickets/(\d+)")

# Deliberately narrow: the subprocess can only call these specific read-only
# Zendesk tools, nothing else - ticket content is untrusted external text,
# so the blast radius of anything unexpected in it stays contained to "reads
# tickets," never a write/Bash/Edit.
FETCH_ALLOWED_TOOLS = "mcp__claude_ai_Zendesk_MCP__get_ticket mcp__claude_ai_Zendesk_MCP__list_ticket_comments"
QUEUE_ALLOWED_TOOLS = "mcp__claude_ai_Zendesk_MCP__list_my_tickets mcp__claude_ai_Zendesk_MCP__search_tickets"


class ClaudeCliError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


AUTH_HICCUP_MARKERS = ("authenticat", "authoriz")  # matches -ion/-ed/-e spellings


def run_claude_json(prompt, allowed_tools, model="claude-haiku-4-5-20251001",
                     max_attempts=2, per_attempt_timeout=90):
    """Run the claude CLI headlessly and parse a JSON object/array out of its
    reply. Retries on the claude.ai connector's occasional transient auth
    hiccup ("no interactive MCP authentication available", "Zendesk MCP
    server requires authentication", etc.) - a follow-up call has reliably
    succeeded in testing even though nothing about the request changed.
    Kept to 2 attempts x 90s (not 3 x 150s) so a genuinely broken connector
    fails in under 3 minutes instead of feeling like it hangs forever.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            proc = subprocess.run(
                [
                    CLAUDE_BIN, "-p", prompt,
                    "--output-format", "json",
                    "--model", model,
                    "--allowedTools", allowed_tools,
                    "--permission-mode", "bypassPermissions",
                ],
                capture_output=True, text=True, timeout=per_attempt_timeout,
            )
        except subprocess.TimeoutExpired:
            last_error = ClaudeCliError(f"claude CLI timed out after {per_attempt_timeout}s", 504)
            continue
        except FileNotFoundError:
            raise ClaudeCliError("claude CLI not found on PATH - is Claude Code installed?", 500)

        if proc.returncode != 0:
            last_error = ClaudeCliError(f"claude CLI failed: {proc.stderr.strip()[:300]}")
            continue

        try:
            outer = json.loads(proc.stdout)
            result_text = outer["result"].strip()
        except (json.JSONDecodeError, KeyError):
            result_text = ""

        if any(m in result_text.lower() for m in AUTH_HICCUP_MARKERS):
            last_error = ClaudeCliError(
                "The Zendesk connector needs a moment to re-authorize for headless calls "
                "(this clears on its own - try again in a few seconds, or ask Claude in an "
                "interactive session to run a Zendesk MCP call once to refresh it)."
            )
            if attempt < max_attempts:
                continue
            raise last_error

        # The model doesn't reliably follow "raw JSON only" - it sometimes wraps in
        # a markdown fence, adds a leading/trailing sentence, etc. Grab the
        # outermost {...} or [...] block rather than assuming the string is clean.
        match = re.search(r"[\{\[].*[\}\]]", result_text, re.DOTALL)
        if not match:
            Path("/tmp/claude-fetch-debug.log").write_text(
                f"attempt={attempt} RC={proc.returncode}\n--- STDOUT ---\n{proc.stdout}\n--- STDERR ---\n{proc.stderr}\n"
            )
            raise ClaudeCliError("claude CLI returned something unparseable - try again")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            Path("/tmp/claude-fetch-debug.log").write_text(
                f"attempt={attempt} RC={proc.returncode}\n--- STDOUT ---\n{proc.stdout}\n--- STDERR ---\n{proc.stderr}\n"
            )
            raise ClaudeCliError("claude CLI returned something unparseable - try again")

    raise last_error or ClaudeCliError("claude CLI failed for an unknown reason - try again")


@app.route("/api/zendesk/fetch", methods=["POST"])
def fetch_zendesk_ticket():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    match = ZENDESK_TICKET_ID_RE.search(url)
    if not url.startswith("https://") or not match:
        return jsonify({"error": "That doesn't look like a Zendesk ticket URL (expected .../tickets/<id>)"}), 400

    ticket_id = match.group(1)
    prompt = (
        f"Call get_ticket with ticket_id={ticket_id}, then list_ticket_comments with "
        f"ticket_id={ticket_id} and visibility=all. If either tool response contains an "
        f'error_message field, reply with exactly {{"restricted": true}} and nothing else. '
        f"Otherwise, write two short summaries for a support engineer's personal ticket "
        f"log:\n"
        f"1. \"problem\" - 2-3 sentences on what the customer reported or asked, in "
        f"their own terms.\n"
        f"2. \"outcome\" - 2-4 sentences, written from the assigned agent's point of "
        f"view (first person, e.g. \"Confirmed...\", \"Pointed them to...\"), covering "
        f"what the agent actually said or did in their replies to resolve or answer it. "
        f"If the ticket has no agent reply yet, set outcome to an empty string.\n"
        f"Reply with ONLY a raw JSON object, no markdown fences, no prose: "
        f'{{"ref": "{ticket_id}", "subject": "...", "status": "...", "priority": "...", '
        f'"problem": "...", "outcome": "..."}}'
    )

    start = time.monotonic()
    try:
        inner = run_claude_json(prompt, FETCH_ALLOWED_TOOLS)
    except ClaudeCliError as e:
        statsd.increment("ticket_log.zendesk_call", tags=["endpoint:fetch", "outcome:error"])
        statsd.timing("ticket_log.zendesk_call.duration", (time.monotonic() - start) * 1000, tags=["endpoint:fetch"])
        return jsonify({"error": str(e)}), e.status
    statsd.timing("ticket_log.zendesk_call.duration", (time.monotonic() - start) * 1000, tags=["endpoint:fetch"])

    if isinstance(inner, dict) and inner.get("restricted"):
        statsd.increment("ticket_log.zendesk_call", tags=["endpoint:fetch", "outcome:restricted"])
        return jsonify({"error": f"Ticket #{ticket_id} is AI-restricted (HIPAA/opt-out) - no automated fetch allowed"}), 403

    statsd.increment("ticket_log.zendesk_call", tags=["endpoint:fetch", "outcome:ok"])
    inner["link"] = url
    return jsonify(inner)


@app.route("/api/zendesk/queue", methods=["GET"])
def zendesk_queue():
    prompt = (
        "Call list_my_tickets to find my currently assigned tickets that are open or "
        "pending (not solved/closed). Limit to 10, most recently updated first. Reply "
        "with ONLY a raw JSON array, no markdown fences, no prose. Each item: "
        '{"id": <numeric ticket id>, "subject": "...", "status": "...", "link": '
        '"<the ticket\'s full agent-facing URL, e.g. https://datadog.zendesk.com/agent/'
        'tickets/<id>>"}. If the tool errors, reply with {"error": "<message>"}.'
    )
    start = time.monotonic()
    try:
        result = run_claude_json(prompt, QUEUE_ALLOWED_TOOLS)
    except ClaudeCliError as e:
        statsd.increment("ticket_log.zendesk_call", tags=["endpoint:queue", "outcome:error"])
        statsd.timing("ticket_log.zendesk_call.duration", (time.monotonic() - start) * 1000, tags=["endpoint:queue"])
        return jsonify({"error": str(e)}), e.status
    statsd.timing("ticket_log.zendesk_call.duration", (time.monotonic() - start) * 1000, tags=["endpoint:queue"])

    if isinstance(result, dict) and result.get("error"):
        statsd.increment("ticket_log.zendesk_call", tags=["endpoint:queue", "outcome:error"])
        return jsonify({"error": result["error"]}), 502
    statsd.increment("ticket_log.zendesk_call", tags=["endpoint:queue", "outcome:ok"])
    statsd.gauge("ticket_log.queue.size", len(result))
    return jsonify(result)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    init_db()
    print(f"Mission Control (local) running at http://127.0.0.1:8091  (claude: {CLAUDE_BIN})")
    app.run(host="127.0.0.1", port=8091, debug=False)
