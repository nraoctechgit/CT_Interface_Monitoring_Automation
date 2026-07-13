#!/usr/bin/env python3
"""
mirth_agent.py — Agentic operations assistant for Mirth Connect / NextGen Connect.

The agent uses the Anthropic Claude API with tool-use to INVESTIGATE a running
Mirth instance (channel states, queue depths, engine/host stats, logs), DIAGNOSE
the incident against an 11-layer failure taxonomy, and produce a remediation
runbook. A small, whitelisted set of *safe* operational actions can be applied
with explicit human confirmation; anything destructive is recommend-only.

    # investigate a symptom, auto-collecting signals, no changes made
    python mirth_agent.py triage "engine frozen, OOM in the log around 02:10"

    # continuously watch and auto-triage when a channel stops or a queue grows
    python mirth_agent.py watch --interval 60

    # run against built-in fake data (no Mirth, no API key beyond triage call)
    python mirth_agent.py triage "queue climbing" --mock

    # allow the agent to apply whitelisted safe actions (asks before each)
    python mirth_agent.py triage "lab channel stopped" --apply

Config via environment (see .env.example):
    ANTHROPIC_API_KEY   required for the LLM calls
    CLAUDE_MODEL        model id you have access to (default: claude-sonnet-4-5)
    MIRTH_URL           e.g. https://mirth.internal:8443
    MIRTH_USER / MIRTH_PASS
    MIRTH_LOG_PATH      path to mirth.log for tailing (optional)
    MIRTH_VERIFY_TLS    "false" to skip cert verification (lab only)
    DATABASE_URL        optional SQLAlchemy URL for message-store stats
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
MIRTH_URL = os.getenv("MIRTH_URL", "").rstrip("/")
MIRTH_USER = os.getenv("MIRTH_USER", "")
MIRTH_PASS = os.getenv("MIRTH_PASS", "")
MIRTH_LOG_PATH = os.getenv("MIRTH_LOG_PATH", "")
MIRTH_VERIFY_TLS = os.getenv("MIRTH_VERIFY_TLS", "true").lower() != "false"
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ----------------------------------------------------------------------------
# Failure taxonomy (transcribed from the reference document) — grounds the model
# ----------------------------------------------------------------------------
TAXONOMY: list[tuple[int, str, list[tuple[str, str]]]] = [
    (1, "JVM & host", [
        ("Out-of-memory (heap exhaustion)", "Right-size -Xmx; prune the message store; don't hold large messages/attachments in memory; fix leaks in custom scripts."),
        ("Excessive GC pauses / engine frozen", "Tune GC and heap; watch GC logs; reduce in-flight message retention."),
        ("Service won't start / crashes", "Verify supported JVM version; check for corrupt install; review mirth log + OS logs."),
        ("OS resource limits (fds, threads)", "Raise ulimit / max file descriptors and thread limits for the service account."),
        ("Disk exhaustion on host", "Separate + monitor volumes for store/logs/temp; rotate logs; alert on free space."),
    ]),
    (2, "Database", [
        ("Database unreachable", "Ensure network/DB availability; configure connection retry; alert on connectivity."),
        ("Connection-pool exhaustion", "Size the pool to load; close connections in custom code; monitor active connections."),
        ("Deadlocks / slow queries under load", "Index appropriately; reduce write contention; watch slow-query logs."),
        ("Unbounded message-table growth", "Configure pruning/purging with a retention policy."),
        ("Derby choking/corrupting in prod", "Migrate to PostgreSQL/SQL Server for real volume. Never run Derby in production."),
    ]),
    (3, "Channel", [
        ("Channel fails to deploy", "Resolve script compile errors, missing code-template deps, invalid channel XML before deploy."),
        ("Channel stops silently", "Monitor channel state; alert on any channel not Started."),
        ("Inconsistent state after ungraceful shutdown", "Redeploy affected channels; enable graceful shutdown; verify state after restart."),
        ("Channel loops / backpressure", "Avoid circular references; design chaining with clear termination + backpressure."),
        ("Wrong deploy order for dependents", "Deploy dependencies first; document the dependency graph."),
    ]),
    (4, "Source connectors", [
        ("MLLP: port conflict / connection refused", "Ensure the listen port is free + firewall-permitted; one listener per port."),
        ("MLLP: dropped connections mid-message", "Configure keep-alive + timeouts; handle partial reads; verify network stability."),
        ("MLLP: malformed framing", "Validate start/end block bytes; reject/quarantine non-conformant frames."),
        ("MLLP: clients not receiving ACKs", "Confirm ACK generation + response settings; check the sender awaits the ACK."),
        ("File reader: permission / locked files", "Grant read/move rights; use move-on-read to avoid locks."),
        ("File reader: partial-file reads", "Use file-age or done-file/rename patterns so only complete files are read."),
        ("File reader: same file reprocessed", "Move/delete after processing; track processed files reliably."),
        ("HTTP listener: TLS / auth / timeout", "Verify certs, protocols, ciphers; confirm credentials; tune timeouts."),
        ("DB reader: read flag not updating", "Run the post-process update/ack transactionally with the read."),
        ("DB reader: missed/overlapping polls", "Set poll interval above worst-case job duration; prevent overlapping runs."),
    ]),
    (5, "Destination connectors", [
        ("Connection refused / timeout", "Health-check endpoints; set sensible timeouts; alert on repeated failures."),
        ("TLS / certificate errors", "Track expiry; validate trust chains; automate renewal."),
        ("Negative ACK (AE/AR) from HL7 receiver", "Inspect the receiver's rejection reason; add response handling + error routing."),
        ("HTTP 4xx / 5xx responses", "Distinguish client vs server errors; retry only where safe; log response bodies."),
        ("DB insert / file-write failures", "Verify permissions + schema; handle and route persistent errors."),
        ("Unmonitored destination queue growth", "Monitor queue depth; alert on thresholds; investigate the head-of-queue failure."),
    ]),
    (6, "Transformation / processing", [
        ("Null/undefined on missing segments/fields", "Defensively check segment/field existence before access in scripts."),
        ("Bad E4X / XML navigation", "Validate XPath/E4X paths; test against representative + edge-case messages."),
        ("Encoding issues (UTF-8 / Latin-1 / Win-1252)", "Set correct inbound/outbound encoding; normalise on ingest."),
        ("Date parsing errors", "Standardise date formats; guard parsing with try/catch."),
        ("Messages silently filtered out", "Log filter decisions; never drop without an audit trail."),
        ("Data-type / parsing mismatch (non-conformant HL7/XML/JSON)", "Validate against the declared data type; quarantine non-conformant input."),
        ("Oversized messages / attachments", "Enforce + test size limits; use attachment handling, not in-memory payloads."),
    ]),
    (7, "Queue & delivery", [
        ("Head-of-line blocking (stuck message)", "Enable Rotate Queue so a failing message doesn't block those behind it."),
        ("Retry storms against a down endpoint", "Bounded retries with backoff; circuit-break on sustained failure."),
        ("Queue growth consuming memory/DB", "Monitor queue depth; cap + alert; drain or reroute when an endpoint is down."),
        ("Duplicate delivery after retries", "Make downstream delivery idempotent; track message IDs."),
        ("Queuing disabled blocks the source", "Balance queuing on/off per destination against throughput + backpressure."),
    ]),
    (8, "Config & deployment", [
        ("Wrong global/configuration-map values", "Externalise environment-specific values; validate on deploy."),
        ("Dev settings carried into prod", "Keep environment configs separate; review before promotion."),
        ("Missing resource libraries (custom JARs)", "Include resources in exports; verify classpath on target."),
        ("Version mismatch after upgrades", "Test upgrades in staging; check extension/version compatibility."),
        ("Incomplete backups/exports", "Export channels, code templates, resources + keystores together; test restores."),
    ]),
    (9, "Security & certificates", [
        ("Expired / untrusted TLS certificates", "Monitor expiry; automate renewal; keep truststore current."),
        ("Keystore / truststore misconfiguration", "Verify key/cert entries + paths; test after every change."),
        ("Mutual-TLS (client-auth) failures", "Confirm both ends present valid certs; align trust config."),
        ("Credential rotation breaking connectors", "Coordinate rotation with connector updates; stage + verify."),
        ("Admin lockout / HTTPS connect failure", "Keep admin creds + cert config recoverable; document break-glass."),
    ]),
    (10, "Clustering / HA", [
        ("Split-brain", "Use the supported HA/clustering configuration with proper coordination."),
        ("Shared-database contention", "Size + tune the shared DB for cluster load; monitor contention."),
        ("Uneven load distribution", "Balance channels/load across nodes; monitor per-node throughput."),
        ("Failover not triggering / false failover", "Test failover regularly; validate health checks + thresholds."),
        ("Single-node outage = full outage (no HA)", "Adopt the HA extension or external redundancy where uptime matters."),
    ]),
    (11, "Monitoring blind spots", [
        ("Stopped channel undetected", "Alert on any channel not in Started state."),
        ("Growing error counts undetected", "Alert on error-status message rates per channel."),
        ("Full disk with no notification", "Alert on free-space thresholds across all Mirth volumes."),
        ("Logs filling the disk", "Configure log rotation + retention."),
        ("Queue depth unmonitored", "Alert on destination queue depth + age."),
    ]),
]

DOMINANT = [
    "Memory exhaustion from an unpruned message store held in heap/DB",
    "Derby in production — corruption and choking under real volume",
    "Unmonitored destination queues growing unbounded while an endpoint is down",
    "Transformer exceptions on non-conformant messages, often silently",
    "Missing alerting — a stopped channel, rising errors, or a full disk with nobody notified",
]


def _kb_text() -> str:
    lines = []
    for n, name, modes in TAXONOMY:
        lines.append(f"[Layer {n} — {name}]")
        for mode, mit in modes:
            lines.append(f"  - {mode} => {mit}")
    dom = "\n".join(f"  {i+1}. {d}" for i, d in enumerate(DOMINANT))
    return "FAILURE TAXONOMY:\n" + "\n".join(lines) + "\n\nDOMINANT REAL-INCIDENT FAILURES:\n" + dom


SYSTEM_PROMPT = (
    "You are the diagnostic engine of a Mirth Connect / NextGen Connect operations "
    "console. You are helping an on-call engineer resolve a live incident.\n\n"
    "Work the case like a good SRE:\n"
    "1. Use the investigation tools to gather EVIDENCE before concluding. Pull channel "
    "states, queue depths, engine/host stats, and relevant log lines. Don't guess when "
    "you can look.\n"
    "2. Ground every judgement in the failure taxonomy below and prefer its exact "
    "mitigations.\n"
    "3. When you have enough evidence, call report_findings exactly once with the full "
    "diagnosis and runbook. Keep it concrete and terse.\n\n"
    "Only suggest an action in suggested_actions if it is one of the whitelisted safe "
    "operations (start_channel, redeploy_channel). Never suggest destructive or "
    "config-mutating steps as actions — put those in the runbook for a human to do.\n\n"
    + _kb_text()
)

# ----------------------------------------------------------------------------
# Signal collectors — the agent's "senses"
# ----------------------------------------------------------------------------
class Collector:
    """Interface the tools call. Real and mock implementations below."""

    def channel_states(self) -> list[dict]: raise NotImplementedError
    def destination_queues(self) -> list[dict]: raise NotImplementedError
    def engine_stats(self) -> dict: raise NotImplementedError
    def tail_log(self, lines: int = 80, contains: str = "") -> list[str]: raise NotImplementedError
    def message_store_stats(self) -> dict: raise NotImplementedError

    # remediation (only used with --apply, after human confirmation)
    def start_channel(self, channel: str) -> str: raise NotImplementedError
    def redeploy_channel(self, channel: str) -> str: raise NotImplementedError


class MockCollector(Collector):
    """Deterministic fake environment so the tool runs with no Mirth server.
    Seeded with a few realistic anomalies so a demo produces a real diagnosis."""

    def channel_states(self):
        return [
            {"name": "ADT Inbound", "state": "STARTED", "received": 148231, "sent": 148100, "error": 12, "queued": 0},
            {"name": "Lab Results Inbound", "state": "STOPPED", "received": 5521, "sent": 5521, "error": 0, "queued": 0},
            {"name": "Send to EHR ADT", "state": "STARTED", "received": 148100, "sent": 100987, "error": 341, "queued": 47113},
            {"name": "Orders Outbound", "state": "STARTED", "received": 22010, "sent": 22010, "error": 0, "queued": 0},
        ]

    def destination_queues(self):
        return [{"channel": "Send to EHR ADT", "destination": "EHR ADT Listener", "queued": 47113, "head_error": "Connect timed out: ehr-adt.internal:6661"}]

    def engine_stats(self):
        return {"heap_used_mb": 3890, "heap_max_mb": 4096, "gc_pause_recent_s": 41.8,
                "disk_free_gb": 3.1, "disk_total_gb": 200.0, "db_engine": "Derby (embedded)",
                "cpu_pct": 71, "open_file_descriptors": 8120, "fd_limit": 8192}

    def tail_log(self, lines=80, contains=""):
        log = [
            "02:08:44 INFO  Channel 'Send to EHR ADT' destination retry (attempt 118)",
            "02:09:02 WARN  Long GC pause detected: 41.8s (G1 Full GC)",
            "02:10:11 ERROR java.lang.OutOfMemoryError: Java heap space",
            "02:10:11 ERROR   at com.mirth.connect.donkey.server.channel.Channel.process",
            "02:10:12 WARN  Message store size 3.9G approaching heap limit",
            "23:41:07 INFO  Channel 'Lab Results Inbound' stopped (state=STOPPED)",
        ]
        if contains:
            log = [l for l in log if contains.lower() in l.lower()]
        return log[-lines:]

    def message_store_stats(self):
        return {"total_messages": 9_120_444, "oldest_message_days": 613, "pruner_configured": False,
                "note": "Derby embedded; no retention policy detected"}

    def start_channel(self, channel):
        return f"[mock] would POST /api/channels/{channel}/_start"

    def redeploy_channel(self, channel):
        return f"[mock] would POST /api/channels/{channel}/_deploy"


class MirthRestCollector(Collector):
    """Talks to a real Mirth Connect via its REST API. Endpoints/JSON shapes vary
    a little across Mirth versions; parsing here is defensive. Requires `requests`."""

    def __init__(self, base_url, user, password, verify_tls=True, log_path=""):
        import requests  # local import so --mock works without requests installed
        if not base_url:
            raise RuntimeError("MIRTH_URL not set. Use --mock to run without a server.")
        self.base = base_url
        self.log_path = log_path
        self.s = requests.Session()
        self.s.verify = verify_tls
        self.s.headers.update({"Accept": "application/json", "X-Requested-With": "mirth-agent"})
        r = self.s.post(f"{base_url}/api/users/_login",
                        data={"username": user, "password": password}, timeout=15)
        r.raise_for_status()

    def _get(self, path):
        r = self.s.get(f"{self.base}{path}", timeout=20)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _as_list(node, key):
        """Mirth JSON returns {key:{child:[...]}} or {key:{child:{...}}} or a bare list."""
        if isinstance(node, list):
            return node
        if isinstance(node, dict):
            inner = node.get(key, node)
            if isinstance(inner, dict):
                child = next(iter(inner.values()), [])
                return child if isinstance(child, list) else [child]
            if isinstance(inner, list):
                return inner
        return []

    def channel_states(self):
        data = self._get("/api/channels/statuses")
        out = []
        for st in self._as_list(data, "list"):
            stats = st.get("statistics", {}) or {}
            out.append({
                "name": st.get("name") or st.get("channelId", "?"),
                "state": st.get("state", "?"),
                "received": int(stats.get("RECEIVED", 0) or 0),
                "sent": int(stats.get("SENT", 0) or 0),
                "error": int(stats.get("ERROR", 0) or 0),
                "queued": int(stats.get("QUEUED", 0) or 0),
                "channelId": st.get("channelId"),
            })
        return out

    def destination_queues(self):
        return [{"channel": c["name"], "queued": c["queued"]} for c in self.channel_states() if c.get("queued", 0) > 0]

    def engine_stats(self):
        stats, info = {}, {}
        try:
            stats = self._get("/api/system/stats")
        except Exception as e:
            stats = {"error": f"stats unavailable: {e}"}
        try:
            info = self._get("/api/system/info")
        except Exception:
            pass
        free = stats.get("diskFreeBytes"); total = stats.get("diskTotalBytes")
        heap_used = stats.get("allocatedMemoryBytes"); heap_max = stats.get("maxMemoryBytes")
        gb = lambda b: round(b / 1e9, 2) if isinstance(b, (int, float)) else None
        mb = lambda b: round(b / 1e6) if isinstance(b, (int, float)) else None
        return {
            "heap_used_mb": mb(heap_used), "heap_max_mb": mb(heap_max),
            "disk_free_gb": gb(free), "disk_total_gb": gb(total),
            "cpu_pct": stats.get("cpuUsagePct"),
            "db_engine": info.get("dbName") or info.get("database"),
            "jvm": info.get("jvmVersion"), "mirth_version": info.get("version"),
            "raw": stats if "error" in stats else None,
        }

    def tail_log(self, lines=80, contains=""):
        if not self.log_path or not os.path.exists(self.log_path):
            return [f"(log tailing not configured; set MIRTH_LOG_PATH to a readable mirth.log)"]
        with open(self.log_path, "r", errors="replace") as f:
            data = f.readlines()
        if contains:
            data = [l for l in data if contains.lower() in l.lower()]
        return [l.rstrip("\n") for l in data[-lines:]]

    def message_store_stats(self):
        if not DATABASE_URL:
            return {"note": "DATABASE_URL not set; message-store introspection skipped"}
        try:
            from sqlalchemy import create_engine, text
        except Exception:
            return {"note": "sqlalchemy not installed; run: pip install sqlalchemy"}
        try:
            eng = create_engine(DATABASE_URL)
            with eng.connect() as c:
                # d_m<localChannelId> tables hold messages; count across them heuristically
                total = c.execute(text(
                    "SELECT COALESCE(SUM(n),0) FROM ("
                    "  SELECT reltuples::bigint AS n FROM pg_class "
                    "  WHERE relname LIKE 'd_m%'"
                    ") t")).scalar()
                return {"approx_total_messages": int(total or 0), "source": "pg_class estimate"}
        except Exception as e:
            return {"note": f"db introspection failed: {e}"}

    def _channel_id(self, name_or_id):
        for c in self.channel_states():
            if name_or_id in (c.get("name"), c.get("channelId")):
                return c.get("channelId") or c.get("name")
        return name_or_id

    def start_channel(self, channel):
        cid = self._channel_id(channel)
        r = self.s.post(f"{self.base}/api/channels/{cid}/_start", timeout=30)
        r.raise_for_status()
        return f"started channel {channel} (HTTP {r.status_code})"

    def redeploy_channel(self, channel):
        cid = self._channel_id(channel)
        r = self.s.post(f"{self.base}/api/channels/{cid}/_deploy", timeout=60)
        r.raise_for_status()
        return f"redeployed channel {channel} (HTTP {r.status_code})"


# ----------------------------------------------------------------------------
# Tool definitions exposed to Claude
# ----------------------------------------------------------------------------
TOOLS = [
    {"name": "get_channel_states", "description": "List every channel with state (STARTED/STOPPED/...) and message counts (received/sent/error/queued).", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_destination_queues", "description": "List destinations with a non-empty outbound queue and, where known, the head-of-queue error.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_engine_stats", "description": "JVM heap, recent GC pauses, disk free, CPU, file descriptors, backing DB engine.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "tail_mirth_log", "description": "Return the last N lines of mirth.log, optionally filtered by a substring.", "input_schema": {"type": "object", "properties": {"lines": {"type": "integer", "default": 80}, "contains": {"type": "string", "description": "case-insensitive substring filter, e.g. 'OutOfMemory' or a channel name"}}}},
    {"name": "get_message_store_stats", "description": "Size/age of the persisted message store and whether a pruner/retention policy is configured.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "report_findings", "description": "Deliver the final structured diagnosis and runbook. Call this exactly once when investigation is complete.", "input_schema": {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "is_dominant": {"type": "boolean", "description": "matches one of the dominant real-incident failure patterns"},
            "classification": {"type": "array", "items": {"type": "object", "properties": {
                "layer": {"type": "integer"}, "layer_name": {"type": "string"},
                "failure_mode": {"type": "string"}, "confidence": {"type": "integer"}}}},
            "hypotheses": {"type": "array", "items": {"type": "object", "properties": {
                "cause": {"type": "string"}, "likelihood": {"type": "string"},
                "evidence": {"type": "string"}, "confirm": {"type": "string"}}}},
            "immediate": {"type": "array", "items": {"type": "object", "properties": {
                "step": {"type": "string"}, "detail": {"type": "string"}, "command": {"type": "string"}}}},
            "durable": {"type": "array", "items": {"type": "object", "properties": {
                "step": {"type": "string"}, "detail": {"type": "string"}, "command": {"type": "string"}}}},
            "monitoring": {"type": "array", "items": {"type": "string"}},
            "suggested_actions": {"type": "array", "items": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["start_channel", "redeploy_channel"]},
                "target": {"type": "string"}, "why": {"type": "string"}}}},
            "summary": {"type": "string"},
        },
        "required": ["severity", "classification", "hypotheses", "immediate", "durable", "summary"],
    }},
]

READONLY_TOOLS: dict[str, Callable[[Collector, dict], Any]] = {
    "get_channel_states": lambda col, a: col.channel_states(),
    "get_destination_queues": lambda col, a: col.destination_queues(),
    "get_engine_stats": lambda col, a: col.engine_stats(),
    "tail_mirth_log": lambda col, a: col.tail_log(int(a.get("lines", 80)), a.get("contains", "")),
    "get_message_store_stats": lambda col, a: col.message_store_stats(),
}
ACTION_HANDLERS: dict[str, Callable[[Collector, str], str]] = {
    "start_channel": lambda col, target: col.start_channel(target),
    "redeploy_channel": lambda col, target: col.redeploy_channel(target),
}


# ----------------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------------
@dataclass
class Agent:
    collector: Collector
    model: str = MODEL
    verbose: bool = True
    _client: Any = field(default=None, repr=False)

    def __post_init__(self):
        try:
            from anthropic import Anthropic
        except Exception:
            raise RuntimeError("The 'anthropic' package is required: pip install anthropic")
        self._client = Anthropic()  # reads ANTHROPIC_API_KEY

    def triage(self, symptom: str, logs: str = "", max_turns: int = 8) -> dict:
        user = f"INCIDENT SYMPTOM:\n{symptom or '(none provided)'}"
        if logs:
            user += f"\n\nENGINEER-PASTED LOGS:\n{logs}"
        user += "\n\nInvestigate with the tools, then call report_findings."
        messages: list[dict] = [{"role": "user", "content": user}]

        for _ in range(max_turns):
            resp = self._client.messages.create(
                model=self.model, max_tokens=2048,
                system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                return {"_unstructured": text or "(model returned no findings)"}

            tool_results = []
            findings = None
            for block in resp.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                if block.name == "report_findings":
                    findings = block.input
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": "findings received"})
                    continue
                handler = READONLY_TOOLS.get(block.name)
                try:
                    result = handler(self.collector, block.input or {}) if handler else {"error": "unknown tool"}
                    if self.verbose:
                        print(f"  · gathered {block.name}", file=sys.stderr)
                except Exception as e:
                    result = {"error": str(e)}
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result, default=str)})

            messages.append({"role": "user", "content": tool_results})
            if findings is not None:
                return findings

        return {"_unstructured": "Investigation did not converge within the turn limit."}


# ----------------------------------------------------------------------------
# Rendering + remediation
# ----------------------------------------------------------------------------
C = {"crit": "\033[91m", "warn": "\033[93m", "ok": "\033[92m", "dim": "\033[90m",
     "cyan": "\033[96m", "bold": "\033[1m", "end": "\033[0m"}


def _c(s, k):
    return f"{C[k]}{s}{C['end']}" if sys.stdout.isatty() else s


def render(f: dict) -> None:
    if "_unstructured" in f:
        print(f["_unstructured"]); return
    sev = f.get("severity", "?")
    sev_col = {"critical": "crit", "high": "warn", "medium": "cyan", "low": "dim"}.get(sev, "dim")
    print("\n" + _c("━" * 68, "dim"))
    print(_c(f"  DIAGNOSIS   severity={sev.upper()}", "bold") +
          ("   " + _c("▲ dominant failure pattern", "warn") if f.get("is_dominant") else ""))
    print(_c("━" * 68, "dim"))
    print(_c("  " + f.get("summary", ""), sev_col))

    print(_c("\n  Classification", "bold"))
    for c in f.get("classification", []):
        print(f"    L{c.get('layer')} {c.get('layer_name','')} — {c.get('failure_mode','')}  "
              + _c(f"({c.get('confidence','?')}%)", "dim"))

    print(_c("\n  Root-cause hypotheses", "bold"))
    for h in f.get("hypotheses", []):
        print(f"    • {_c(h.get('cause',''),'cyan')}  [{h.get('likelihood','')}]")
        print(_c(f"        evidence: {h.get('evidence','')}", "dim"))
        print(_c(f"        confirm : {h.get('confirm','')}", "dim"))

    def steps(title, arr, col):
        print(_c(f"\n  {title}", col))
        for i, s in enumerate(arr, 1):
            print(f"    {i}. {_c(s.get('step',''),'bold')} — {s.get('detail','')}")
            if s.get("command"):
                print(_c(f"       $ {s['command']}", "cyan"))

    steps("Immediate — stop the bleeding", f.get("immediate", []), "crit")
    steps("Durable fix", f.get("durable", []), "ok")

    if f.get("monitoring"):
        print(_c("\n  Prevention / monitoring to add", "bold"))
        for m in f["monitoring"]:
            print(f"    • {m}")
    print(_c("━" * 68, "dim") + "\n")


def maybe_apply(collector: Collector, findings: dict, apply: bool) -> None:
    actions = findings.get("suggested_actions") or []
    if not actions:
        return
    print(_c("  Suggested safe actions", "bold"))
    for a in actions:
        print(f"    → {a.get('action')}({a.get('target')}) — {a.get('why','')}")
    if not apply:
        print(_c("  (run with --apply to execute, with confirmation)\n", "dim"))
        return
    for a in actions:
        handler = ACTION_HANDLERS.get(a.get("action", ""))
        if not handler:
            continue
        ans = input(_c(f"  Execute {a['action']}({a['target']})? [y/N] ", "warn")).strip().lower()
        if ans == "y":
            try:
                print(_c("    " + handler(collector, a["target"]), "ok"))
            except Exception as e:
                print(_c(f"    action failed: {e}", "crit"))
        else:
            print(_c("    skipped", "dim"))
    print()


def build_collector(mock: bool) -> Collector:
    if mock:
        return MockCollector()
    return MirthRestCollector(MIRTH_URL, MIRTH_USER, MIRTH_PASS,
                              verify_tls=MIRTH_VERIFY_TLS, log_path=MIRTH_LOG_PATH)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def cmd_triage(args):
    collector = build_collector(args.mock)
    agent = Agent(collector, model=args.model)
    logs = ""
    if args.logs:
        logs = sys.stdin.read() if args.logs == "-" else open(args.logs).read()
    print(_c(f"Working the case with {args.model}…", "dim"), file=sys.stderr)
    findings = agent.triage(args.symptom, logs=logs)
    render(findings)
    maybe_apply(collector, findings, args.apply)


def cmd_watch(args):
    collector = build_collector(args.mock)
    agent = Agent(collector, model=args.model)
    print(_c(f"Watching every {args.interval}s. Ctrl-C to stop.", "dim"))
    seen: set[str] = set()
    try:
        while True:
            anomalies = []
            for c in collector.channel_states():
                if c.get("state") not in ("STARTED", "?"):
                    anomalies.append(f"channel '{c['name']}' is {c['state']}")
                if c.get("queued", 0) >= args.queue_threshold:
                    anomalies.append(f"channel '{c['name']}' queue={c['queued']}")
            fresh = [a for a in anomalies if a not in seen]
            if fresh:
                seen.update(fresh)
                symptom = "Watcher detected: " + "; ".join(fresh)
                print(_c("\n! " + symptom, "warn"))
                findings = agent.triage(symptom)
                render(findings)
                maybe_apply(collector, findings, args.apply)
            else:
                print(_c(f"  {time.strftime('%H:%M:%S')} nominal", "dim"))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")


def main():
    p = argparse.ArgumentParser(description="Agentic Mirth Connect triage using the Claude API.")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mock", action="store_true", help="use built-in fake data (no Mirth server)")
    common.add_argument("--model", default=MODEL, help=f"Claude model id (default {MODEL})")
    common.add_argument("--apply", action="store_true", help="allow whitelisted safe actions (asks first)")

    t = sub.add_parser("triage", parents=[common], help="diagnose one incident")
    t.add_argument("symptom", help="short description of what's wrong")
    t.add_argument("--logs", help="path to a log file to include, or - for stdin")
    t.set_defaults(func=cmd_triage)

    w = sub.add_parser("watch", parents=[common], help="poll and auto-triage on anomalies")
    w.add_argument("--interval", type=int, default=60, help="seconds between polls")
    w.add_argument("--queue-threshold", type=int, default=1000, help="queue depth that triggers triage")
    w.set_defaults(func=cmd_watch)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        print(_c(f"error: {e}", "crit")); sys.exit(1)


if __name__ == "__main__":
    main()
