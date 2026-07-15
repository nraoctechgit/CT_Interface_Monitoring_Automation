#!/usr/bin/env python3
"""
mirth_console.py — a polished web console for the Mirth Connect agentic resolver.

This is the visual front-end to mirth_agent.py. It reuses the SAME agent brain
(system prompt, failure taxonomy, investigation tools and the report_findings
schema) and renders the result as a clean, executive-friendly operations report
instead of terminal text.

    python mirth_console.py            # serve at http://localhost:8000
    python mirth_console.py --port 9000

What you get:
    • A live environment snapshot (channel health grid, engine vitals, queues).
    • A one-click "Run diagnosis" that streams the agent's investigation in real
      time (each signal it gathers appears as it happens) and then renders the
      structured diagnosis + remediation runbook.

The diagnosis is a REAL Claude triage (this is the "live" console). The data the
agent reads can come from the built-in mock environment (default — always rich,
no Mirth server needed) or a real Mirth Connect instance (toggle in the UI).

Config is read from .env (ANTHROPIC_API_KEY, CLAUDE_MODEL, MIRTH_URL, ...).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# ----------------------------------------------------------------------------
# Load .env before importing the agent (mirth_agent reads env at import time).
# Minimal parser: only picks up simple UPPER_CASE=value lines, strips inline
# comments and quotes, and ignores the dotted "database.url = ..." style keys.
# ----------------------------------------------------------------------------
def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    pat = re.compile(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = pat.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            # strip an unquoted inline comment
            if val and val[0] not in ("'", '"'):
                val = val.split("#", 1)[0].strip()
            val = val.strip().strip("'\"")
            os.environ.setdefault(key, val)


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from flask import Flask, Response, jsonify, render_template, request  # noqa: E402

# Reuse the agent's brain verbatim.
import mirth_agent as ma  # noqa: E402

app = Flask(__name__)

# A small library of ready-to-run scenarios for the demo drop-down.
SCENARIOS = [
    {
        "id": "oom",
        "title": "Engine frozen — OOM at 02:10",
        "symptom": "Engine frozen, a 41s GC pause then java.lang.OutOfMemoryError in the log around 02:10. A destination queue is climbing.",
    },
    {
        "id": "queue",
        "title": "Destination queue climbing",
        "symptom": "A destination queue keeps climbing and messages aren't being delivered to the downstream EHR.",
    },
    {
        "id": "stopped",
        "title": "Lab channel stopped overnight",
        "symptom": "The Lab Results Inbound channel stopped overnight and nobody was alerted.",
    },
    {
        "id": "tls",
        "title": "TLS errors on HTTPS listener",
        "symptom": "TLS handshake errors on the HTTPS listener; a partner reports they can't connect.",
    },
]


def _collector(mock: bool):
    """Build a data collector. Falls back to mock if a real Mirth isn't reachable."""
    if mock:
        return ma.MockCollector(), "mock"
    try:
        return ma.build_collector(False), "live"
    except Exception:
        # No reachable Mirth — degrade gracefully to the mock environment so the
        # console always has something to show.
        return ma.MockCollector(), "mock-fallback"


@app.route("/")
def index():
    return render_template(
        "index.html",
        model=ma.MODEL,
        scenarios=SCENARIOS,
        taxonomy=ma.TAXONOMY,
        dominant=ma.DOMINANT,
    )


@app.route("/api/snapshot")
def snapshot():
    """Current environment vitals — no LLM call, powers the health grid."""
    mock = request.args.get("mock", "true").lower() != "false"
    col, source = _collector(mock)
    try:
        channels = col.channel_states()
    except Exception as e:
        channels = [{"name": "(collector error)", "state": str(e)}]
    try:
        engine = col.engine_stats()
    except Exception as e:
        engine = {"error": str(e)}
    try:
        queues = col.destination_queues()
    except Exception:
        queues = []
    try:
        store = col.message_store_stats()
    except Exception:
        store = {}
    try:
        log = col.tail_log(12)
    except Exception:
        log = []
    return jsonify(
        source=source,
        channels=channels,
        engine=engine,
        queues=queues,
        message_store=store,
        log=log,
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _triage_stream(symptom: str, mock: bool):
    """Reproduces mirth_agent.Agent.triage as a generator that emits SSE events
    for each tool the agent calls, then the final structured findings."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        yield _sse("error", {"message": "ANTHROPIC_API_KEY is not set. Add it to .env and restart."})
        return

    col, source = _collector(mock)
    yield _sse("start", {"symptom": symptom, "source": source, "model": ma.MODEL})

    try:
        from anthropic import Anthropic
        client = Anthropic()
    except Exception as e:
        yield _sse("error", {"message": f"Could not initialise Anthropic client: {e}"})
        return

    user = (
        f"INCIDENT SYMPTOM:\n{symptom or '(none provided)'}\n\n"
        "Investigate with the tools, then call report_findings."
    )
    messages = [{"role": "user", "content": user}]

    # friendly labels for the timeline
    labels = {
        "get_channel_states": "Reading channel states",
        "get_destination_queues": "Inspecting destination queues",
        "get_engine_stats": "Checking JVM heap / GC / disk vitals",
        "tail_mirth_log": "Tailing mirth.log for evidence",
        "get_message_store_stats": "Sizing the message store",
    }

    nudges = 0
    try:
        for _ in range(10):
            # max_tokens generous enough that the large report_findings tool call
            # is never truncated mid-flight (which would strand an unanswered
            # tool_use block and 400 the next request).
            resp = client.messages.create(
                model=ma.MODEL, max_tokens=4096,
                system=ma.SYSTEM_PROMPT, tools=ma.TOOLS, messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            # surface any reasoning text the model emitted this turn
            thought = "".join(
                b.text for b in resp.content if getattr(b, "type", "") == "text"
            ).strip()
            if thought:
                yield _sse("thinking", {"text": thought})

            # Branch on whether the model actually emitted tool calls — NOT on
            # stop_reason. A max_tokens truncation can leave a tool_use block with
            # stop_reason != "tool_use"; every tool_use block still needs a matching
            # tool_result in the next message or the API rejects the conversation.
            tool_blocks = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]

            if not tool_blocks:
                # Ended with prose and no tool call. Nudge it once or twice to
                # deliver the structured findings, then give up gracefully.
                if nudges < 2:
                    nudges += 1
                    yield _sse("tool", {"name": "report_findings", "label": "Compiling the structured diagnosis"})
                    messages.append({"role": "user", "content": (
                        "You have not called report_findings yet. Based on the evidence "
                        "you have gathered, call report_findings now — exactly once — with "
                        "the complete structured diagnosis and remediation runbook.")})
                    continue
                yield _sse("findings", {"_unstructured": thought or "(model returned no findings)"})
                yield _sse("done", {})
                return

            tool_results = []
            findings = None
            for block in tool_blocks:
                if block.name == "report_findings":
                    findings = block.input
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": "findings received"})
                    continue
                handler = ma.READONLY_TOOLS.get(block.name)
                yield _sse("tool", {"name": block.name, "label": labels.get(block.name, block.name)})
                try:
                    result = handler(col, block.input or {}) if handler else {"error": "unknown tool"}
                except Exception as e:
                    result = {"error": str(e)}
                yield _sse("tool_result", {"name": block.name, "result": result})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result, default=str)})

            messages.append({"role": "user", "content": tool_results})
            if findings is not None:
                yield _sse("findings", findings)
                yield _sse("done", {})
                return

        yield _sse("findings", {"_unstructured": "Investigation did not converge within the turn limit."})
        yield _sse("done", {})
    except Exception as e:
        yield _sse("error", {"message": f"{type(e).__name__}: {e}"})


@app.route("/api/triage")
def triage():
    symptom = request.args.get("symptom", "").strip()
    mock = request.args.get("mock", "true").lower() != "false"
    return Response(
        _triage_stream(symptom, mock),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def main():
    p = argparse.ArgumentParser(description="Web console for the Mirth agentic resolver.")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("! ANTHROPIC_API_KEY not found (set it in .env). The UI will load, "
              "but live diagnosis will report an error until it's set.", file=sys.stderr)

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Mirth Operations Console  ->  {url}")
    print(f"  Model: {ma.MODEL}   (Ctrl-C to stop)\n")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
