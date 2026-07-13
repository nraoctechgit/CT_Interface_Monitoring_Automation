#!/usr/bin/env python3
"""
mirth_agent_remediation.py — Agentic operations assistant for Mirth Connect / NextGen Connect.

The agent uses the Anthropic Claude API with tool-use to INVESTIGATE a running
Mirth instance (channel states, queue depths, engine/host stats, logs), DIAGNOSE
the incident against an 11-layer failure taxonomy, and produce a remediation
runbook. A small, whitelisted set of *safe* operational actions can be applied
with explicit human confirmation; anything destructive is recommend-only.

    # investigate a symptom, auto-collecting signals, no changes made
    python mirth_agent_remediation.py triage "engine frozen, OOM in the log around 02:10"

    # continuously watch and auto-triage when a channel stops or a queue grows
    python mirth_agent_remediation.py watch --interval 60

    # run against built-in fake data (no Mirth, no API key beyond triage call)
    python mirth_agent_remediation.py triage "queue climbing" --mock

    # allow the agent to apply whitelisted safe actions (asks before each)
    python mirth_agent_remediation.py triage "lab channel stopped" --apply

    # run fully offline with the deterministic analyzer (no API key)
    python mirth_agent_remediation.py triage "queue climbing" --mock --local

    # augment the diagnosis with similar past incidents (RAG, offline by default)
    python mirth_agent_remediation.py triage "queue climbing" --mock --local --rag

    # record a resolved incident so future triages can learn from it
    python mirth_agent_remediation.py feedback "lab channel stopped" \
        --root-cause "expired TLS cert on EHR listener" --fix "renewed cert; restarted channel" --mock

Config via environment (see .env.example):
    ANTHROPIC_API_KEY   required for the LLM calls
    CLAUDE_MODEL        model id you have access to (default: claude-sonnet-4-5)
    MIRTH_URL           e.g. https://mirth.internal:8443
    MIRTH_USER / MIRTH_PASS
    MIRTH_LOG_PATH      path to mirth.log for tailing (optional)
    MIRTH_VERIFY_TLS    "false" to skip cert verification (lab only)
    DATABASE_URL        optional SQLAlchemy URL for message-store stats
    RAG_STORE_PATH      JSONL case store for retrieval (default: mirth_cases.jsonl beside this file)
    RAG_EMBED_MODEL     optional sentence-transformers model id; unset = deterministic hashed-BoW (no deps)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


def _load_local_env() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path, override=False)
        return
    except Exception:
        pass

    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if "#" in value:
                # support inline comments after the value
                value = value.split("#", 1)[0].rstrip()
            if ";" in value and not value.startswith("\"") and not value.startswith("'"):
                # support optional comment delimiter too
                value = value.split(";", 1)[0].rstrip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_load_local_env()

# Box-drawing/arrow glyphs used by render() aren't in cp1252; make legacy
# Windows consoles print UTF-8 instead of crashing with UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

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
RAG_STORE_PATH = os.getenv("RAG_STORE_PATH", "")
RAG_EMBED_MODEL = os.getenv("RAG_EMBED_MODEL", "")

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
    "1. GATHER. Use the investigation tools to collect EVIDENCE before concluding — "
    "channel states, queue depths, engine/host stats, message-store size, and relevant "
    "log lines. Don't guess when you can look. Pull more than one signal; a single "
    "reading rarely tells the whole story.\n"
    "2. CORRELATE. Real Mirth incidents are usually a CAUSAL CHAIN, not one isolated "
    "fault (e.g. a dead destination → unbounded queue growth → unpruned store → heap "
    "exhaustion → OOM). Reconstruct that chain: name the trigger, the amplifier(s), and "
    "the final failure the engineer noticed. Cross-check signals against each other and "
    "against the log timeline.\n"
    "3. QUANTIFY. Every finding must cite the actual number that supports it (e.g. "
    "'heap 3890/4096 MB = 95%', 'queue 47,113', 'FDs 8120/8192'). Put these in `signals`. "
    "Convert raw counts into ratios/percentages and flag anything near a cliff.\n"
    "4. GROUND. Map the incident to the failure taxonomy below (possibly several layers "
    "at once) and prefer its EXACT mitigations. Note when the pattern matches a dominant "
    "real-incident failure. Briefly say what you RULED OUT and why, so the engineer "
    "trusts the diagnosis.\n"
    "5. PRIORITIZE. Separate `immediate` steps (stop the bleeding / restore service now) "
    "from `durable` steps (remove the root cause so it can't recur). Order each list most-"
    "urgent first, and give each step a way to VERIFY it worked.\n"
    "6. REPORT. When you have enough evidence, call report_findings exactly ONCE with the "
    "full diagnosis and runbook. Be concrete and terse — the reader is mid-incident. Set "
    "an honest overall `confidence` and state key uncertainties or missing signals.\n\n"
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
            "confidence": {"type": "integer", "description": "overall confidence in the diagnosis, 0-100"},
            "is_dominant": {"type": "boolean", "description": "matches one of the dominant real-incident failure patterns"},
            "signals": {"type": "array", "description": "the key quantified stats gathered, each citing the raw number and why it matters, e.g. 'heap 3890/4096 MB = 95% (near OOM)'.",
                "items": {"type": "object", "properties": {
                    "metric": {"type": "string"}, "value": {"type": "string"},
                    "assessment": {"type": "string", "enum": ["critical", "warning", "nominal"]},
                    "note": {"type": "string"}}}},
            "causal_chain": {"type": "array", "description": "the incident reconstructed as an ordered chain from trigger to observed failure, one link per element.",
                "items": {"type": "string"}},
            "classification": {"type": "array", "items": {"type": "object", "properties": {
                "layer": {"type": "integer"}, "layer_name": {"type": "string"},
                "failure_mode": {"type": "string"}, "confidence": {"type": "integer"}}}},
            "blast_radius": {"type": "array", "description": "channels / flows / interfaces impacted and how.",
                "items": {"type": "string"}},
            "hypotheses": {"type": "array", "items": {"type": "object", "properties": {
                "cause": {"type": "string"}, "likelihood": {"type": "string"},
                "evidence": {"type": "string"}, "confirm": {"type": "string"}}}},
            "ruled_out": {"type": "array", "description": "plausible causes considered and dismissed, with the reason.",
                "items": {"type": "string"}},
            "immediate": {"type": "array", "description": "stop-the-bleeding steps, most urgent first.",
                "items": {"type": "object", "properties": {
                    "step": {"type": "string"}, "detail": {"type": "string"},
                    "command": {"type": "string"}, "verify": {"type": "string", "description": "how to confirm this step worked"}}}},
            "durable": {"type": "array", "description": "root-cause fixes so the incident can't recur, most impactful first.",
                "items": {"type": "object", "properties": {
                    "step": {"type": "string"}, "detail": {"type": "string"},
                    "command": {"type": "string"}, "verify": {"type": "string", "description": "how to confirm this step worked"}}}},
            "monitoring": {"type": "array", "items": {"type": "string"}},
            "suggested_actions": {"type": "array", "items": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["start_channel", "redeploy_channel"]},
                "target": {"type": "string"}, "why": {"type": "string"}}}},
            "summary": {"type": "string"},
        },
        "required": ["severity", "confidence", "signals", "causal_chain", "classification", "hypotheses", "immediate", "durable", "summary"],
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
# Local rule-based analyzer — same diagnosis, no API key, fully deterministic
# ----------------------------------------------------------------------------
def _pct(used, total):
    if isinstance(used, (int, float)) and isinstance(total, (int, float)) and total:
        return round(100 * used / total)
    return None


def _num(x):
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return str(x)


@dataclass
class LocalAnalyzer:
    """Deterministic, offline twin of the LLM Agent. It calls the SAME Collector
    tools, applies thresholds + pattern rules grounded in the failure taxonomy, and
    emits the SAME findings dict that render() consumes — so it needs no
    ANTHROPIC_API_KEY, no network, and gives an instant, reproducible diagnosis."""
    collector: Collector
    verbose: bool = True

    # tunable thresholds
    HEAP_CRIT = 90; HEAP_WARN = 75
    DISK_CRIT = 5;  DISK_WARN = 15
    FD_CRIT = 95;   FD_WARN = 85
    GC_CRIT = 10.0; GC_WARN = 2.0
    CPU_CRIT = 90;  CPU_WARN = 70
    QUEUE_CRIT = 10_000; QUEUE_WARN = 1_000
    STORE_WARN = 1_000_000; STORE_AGE_WARN = 30

    def triage(self, symptom: str, logs: str = "", **_) -> dict:
        col = self.collector
        def safe(fn, default):
            try:
                return fn()
            except Exception:
                return default
        channels = safe(col.channel_states, [])
        queues = safe(col.destination_queues, [])
        eng = safe(col.engine_stats, {}) or {}
        store = safe(col.message_store_stats, {}) or {}
        log = safe(lambda: col.tail_log(200), []) + ([logs] if logs else [])
        logtext = "\n".join(log).lower()
        if self.verbose:
            print("  · gathered channels, queues, engine, store, log (local)", file=sys.stderr)

        signals: list[dict] = []
        classification: list[dict] = []
        hypotheses: list[dict] = []
        immediate: list[dict] = []
        durable: list[dict] = []
        monitoring: list[str] = []
        blast_radius: list[str] = []
        chain: list[str] = []
        ruled_out: list[str] = []
        actions: list[dict] = []
        dominant = False
        crit = 0

        def sig(metric, value, assessment, note=""):
            nonlocal crit
            signals.append({"metric": metric, "value": value, "assessment": assessment, "note": note})
            if assessment == "critical":
                crit += 1

        # ---- host / JVM (Layer 1) --------------------------------------------
        heap_pct = _pct(eng.get("heap_used_mb"), eng.get("heap_max_mb"))
        oom = "outofmemory" in logtext or "java heap space" in logtext
        if heap_pct is not None:
            a = "critical" if heap_pct >= self.HEAP_CRIT else "warning" if heap_pct >= self.HEAP_WARN else "nominal"
            sig("Heap", f"{eng.get('heap_used_mb')}/{eng.get('heap_max_mb')} MB ({heap_pct}%)", a,
                "near OOM" if a == "critical" else "")
        if (heap_pct is not None and heap_pct >= self.HEAP_WARN) or oom:
            classification.append({"layer": 1, "layer_name": "JVM & host",
                "failure_mode": "Out-of-memory (heap exhaustion)", "confidence": 90 if oom else 70})
            immediate.append({"step": "Relieve heap pressure",
                "detail": "Prune the message store and/or reduce in-flight retention, then restart the engine cleanly if it is wedged after the OOM.",
                "command": "", "verify": "heap settles well below -Xmx and GC pauses return to sub-second"})
            durable.append({"step": "Right-size the heap and stop holding payloads in memory",
                "detail": "Set -Xmx to the working set, use attachment handling for large messages, and fix any leaks in custom scripts.",
                "command": "", "verify": "steady-state heap has headroom under peak load"})

        gc = eng.get("gc_pause_recent_s")
        if isinstance(gc, (int, float)):
            a = "critical" if gc >= self.GC_CRIT else "warning" if gc >= self.GC_WARN else "nominal"
            sig("Recent GC pause", f"{gc}s", a, "engine effectively frozen" if a == "critical" else "")
            if a != "nominal":
                classification.append({"layer": 1, "layer_name": "JVM & host",
                    "failure_mode": "Excessive GC pauses / engine frozen", "confidence": 80})

        disk_pct = _pct(eng.get("disk_free_gb"), eng.get("disk_total_gb"))
        if disk_pct is not None:
            a = "critical" if disk_pct <= self.DISK_CRIT else "warning" if disk_pct <= self.DISK_WARN else "nominal"
            sig("Disk free", f"{eng.get('disk_free_gb')}/{eng.get('disk_total_gb')} GB ({disk_pct}%)", a,
                "host outage risk" if a == "critical" else "")
            if a != "nominal":
                classification.append({"layer": 1, "layer_name": "JVM & host",
                    "failure_mode": "Disk exhaustion on host", "confidence": 75})
                immediate.append({"step": "Reclaim disk", "detail": "Rotate/compress logs and clear temp before the host runs out.",
                    "command": "", "verify": "free space back above threshold"})
                monitoring.append("Alert on free-space thresholds across all Mirth volumes (store/logs/temp).")

        fd_pct = _pct(eng.get("open_file_descriptors"), eng.get("fd_limit"))
        if fd_pct is not None:
            a = "critical" if fd_pct >= self.FD_CRIT else "warning" if fd_pct >= self.FD_WARN else "nominal"
            sig("File descriptors", f"{eng.get('open_file_descriptors')}/{eng.get('fd_limit')} ({fd_pct}%)", a,
                "connector failures imminent" if a == "critical" else "")
            if a != "nominal":
                classification.append({"layer": 1, "layer_name": "JVM & host",
                    "failure_mode": "OS resource limits (fds, threads)", "confidence": 70})
                durable.append({"step": "Raise OS resource limits", "detail": "Increase max file descriptors / thread limits for the service account.",
                    "command": "ulimit -n 65535", "verify": "open-fd ratio stays comfortably below the limit under load"})

        cpu = eng.get("cpu_pct")
        if isinstance(cpu, (int, float)):
            a = "critical" if cpu >= self.CPU_CRIT else "warning" if cpu >= self.CPU_WARN else "nominal"
            sig("CPU", f"{cpu}%", a)

        # ---- database (Layer 2) ----------------------------------------------
        db = str(eng.get("db_engine") or "").lower()
        if "derby" in db:
            dominant = True
            sig("Backing DB", str(eng.get("db_engine")), "critical", "Derby is unsafe at production volume")
            classification.append({"layer": 2, "layer_name": "Database",
                "failure_mode": "Derby choking/corrupting in prod", "confidence": 85})
            durable.append({"step": "Migrate off Derby",
                "detail": "Move the backing store to PostgreSQL or SQL Server. Never run Derby in production.",
                "command": "", "verify": "engine runs on the external DB and passes a load test"})

        total_msgs = store.get("total_messages") or store.get("approx_total_messages")
        pruner = store.get("pruner_configured")
        age = store.get("oldest_message_days")
        store_bad = pruner is False or (isinstance(total_msgs, (int, float)) and total_msgs >= self.STORE_WARN) \
            or (isinstance(age, (int, float)) and age >= self.STORE_AGE_WARN)
        if total_msgs is not None or pruner is not None:
            note = "no retention policy" if pruner is False else ""
            a = "critical" if store_bad else "nominal"
            val = f"{_num(total_msgs)} msgs" + (f", oldest {age}d" if age is not None else "") + (", pruner=off" if pruner is False else "")
            sig("Message store", val, a, note)
        if store_bad:
            dominant = True
            classification.append({"layer": 2, "layer_name": "Database",
                "failure_mode": "Unbounded message-table growth", "confidence": 85})
            immediate.append({"step": "Prune the message store",
                "detail": "Purge old/processed messages to reclaim heap and DB space; this is the fastest way to relieve the memory driver.",
                "command": "", "verify": "message count and store size drop materially"})
            durable.append({"step": "Configure pruning + retention policy",
                "detail": "Set an age/count-based retention so the store cannot grow unbounded again.",
                "command": "", "verify": "pruner runs on schedule and store size stays flat"})

        # ---- channels (Layers 3 / 11) ----------------------------------------
        stopped = [c for c in channels if str(c.get("state", "")).upper() not in ("STARTED", "?", "")]
        if stopped:
            names = ", ".join(c.get("name", "?") for c in stopped)
            sig("Stopped channels", names, "critical" if len(stopped) else "warning", "not processing, and undetected")
            classification.append({"layer": 3, "layer_name": "Channel",
                "failure_mode": "Channel stops silently", "confidence": 80})
            for c in stopped:
                blast_radius.append(f"'{c.get('name')}' is {c.get('state')} — its interface is not processing")
                actions.append({"action": "start_channel", "target": c.get("name"),
                    "why": f"channel is {c.get('state')}; restart restores processing"})
            immediate.append({"step": "Restart stopped channels",
                "detail": f"Bring {names} back to STARTED (safe, restorative).",
                "command": "", "verify": "channel state reads STARTED and received count advances"})
            monitoring.append("Alert on any channel not in STARTED state.")

        # ---- queues + destinations (Layers 5 / 7) ----------------------------
        def qdepth(c):
            try:
                return int(c.get("queued", 0) or 0)
            except (TypeError, ValueError):
                return 0
        top = max(channels, key=qdepth, default=None)
        top_q = qdepth(top) if top else 0
        head_err = next((q.get("head_error") for q in queues if q.get("head_error")), "")
        if top_q:
            a = "critical" if top_q >= self.QUEUE_CRIT else "warning" if top_q >= self.QUEUE_WARN else "nominal"
            sig("Largest queue", f"{top.get('name')} queued {_num(top_q)}", a)
            if a != "nominal":
                classification.append({"layer": 5, "layer_name": "Destination connectors",
                    "failure_mode": "Unmonitored destination queue growth", "confidence": 80})
                dominant = dominant or a == "critical"
                blast_radius.append(f"'{top.get('name')}' backlog {_num(top_q)} — delivery delayed downstream")
                monitoring.append("Alert on destination queue depth + age.")
        endpoint_down = any(k in (head_err or "").lower() for k in ("timed out", "timeout", "refused", "unreachable"))
        if endpoint_down:
            sig("Head-of-queue error", head_err, "critical", "destination endpoint down")
            classification.append({"layer": 5, "layer_name": "Destination connectors",
                "failure_mode": "Connection refused / timeout", "confidence": 85})
            if "retry" in logtext or "attempt" in logtext:
                classification.append({"layer": 7, "layer_name": "Queue & delivery",
                    "failure_mode": "Retry storms against a down endpoint", "confidence": 75})
            immediate.append({"step": "Restore or isolate the failing destination",
                "detail": f"Investigate the endpoint in '{head_err}'. If it is down, pause/drain the queue to stop the retry storm; if transient, recovery flushes the backlog.",
                "command": "", "verify": "head-of-queue delivers or queue is safely drained/rerouted"})
            durable.append({"step": "Bound retries + enable Rotate Queue",
                "detail": "Add backoff/circuit-breaking on the destination and enable Rotate Queue so one stuck message doesn't block the rest.",
                "command": "", "verify": "a forced endpoint failure no longer blocks the whole queue"})

        # ---- error rates (Layer 11) ------------------------------------------
        erroring = [c for c in channels if qdepth(c) >= 0 and int(c.get("error", 0) or 0) > 0]
        if erroring:
            worst = max(erroring, key=lambda c: int(c.get("error", 0) or 0))
            if int(worst.get("error", 0)) > 0:
                sig("Highest error count", f"{worst.get('name')}: {_num(worst.get('error'))} errors", "warning")
                monitoring.append("Alert on error-status message rates per channel.")

        # ---- causal chain ----------------------------------------------------
        if endpoint_down:
            chain.append(f"Destination endpoint unreachable ({head_err.split(':')[0] if head_err else 'timeout'})")
        if top_q >= self.QUEUE_WARN:
            chain.append(f"Outbound queue grows to {_num(top_q)}")
        if store_bad:
            chain.append("Unpruned store retains everything")
        if heap_pct is not None and heap_pct >= self.HEAP_WARN:
            chain.append(f"Heap pressure ({heap_pct}%)")
        if isinstance(gc, (int, float)) and gc >= self.GC_WARN:
            chain.append(f"Long GC pause ({gc}s)")
        if oom:
            chain.append("OutOfMemoryError")

        # ---- hypotheses ------------------------------------------------------
        if oom or (heap_pct is not None and heap_pct >= self.HEAP_WARN):
            ev = []
            if heap_pct is not None: ev.append(f"heap {heap_pct}%")
            if store_bad: ev.append(f"store {_num(total_msgs)} msgs, pruner off")
            if top_q: ev.append(f"queue {_num(top_q)} held in memory")
            hypotheses.append({"cause": "Heap exhaustion driven by an unpruned store and an in-memory backlog",
                "likelihood": "high", "evidence": "; ".join(ev) or "heap near limit",
                "confirm": "compare live message-store size and queue depth against -Xmx; watch GC logs"})
        if endpoint_down:
            hypotheses.append({"cause": f"Destination endpoint down → retry storm → queue growth ({head_err})",
                "likelihood": "high", "evidence": f"head-of-queue '{head_err}', queue {_num(top_q)}",
                "confirm": "test TCP connectivity to the endpoint host:port from the Mirth host"})
        if "derby" in db:
            hypotheses.append({"cause": "Derby embedded DB unable to sustain production volume",
                "likelihood": "medium", "evidence": f"db_engine={eng.get('db_engine')}, {_num(total_msgs)} messages",
                "confirm": "check for Derby lock/corruption warnings in the log"})
        if stopped:
            hypotheses.append({"cause": "Channel stopped (possibly from ungraceful shutdown) and went unnoticed",
                "likelihood": "medium", "evidence": ", ".join(c.get("name", "?") for c in stopped),
                "confirm": "review log for the stop event and preceding errors"})

        # ---- ruled out (only when the signal is present and clean) -----------
        if isinstance(cpu, (int, float)) and cpu < self.CPU_CRIT:
            ruled_out.append(f"CPU saturation — CPU at {cpu}%, not the bottleneck")
        if "connection refused" not in logtext and "database unreachable" not in logtext and db and "derby" not in db:
            ruled_out.append("Database unreachability — no DB connectivity errors observed")
        if not any(k in logtext for k in ("ssl", "tls", "certificate", "handshake")):
            ruled_out.append("TLS / certificate faults — no handshake or cert errors in the log")

        # ---- roll-up ---------------------------------------------------------
        monitoring = list(dict.fromkeys(monitoring))  # dedupe, keep order
        severity = "critical" if (crit >= 1 and (oom or heap_pct and heap_pct >= self.HEAP_CRIT
                    or disk_pct is not None and disk_pct <= self.DISK_CRIT)) else \
                   "high" if crit >= 1 else "medium" if classification else "low"
        confidence = min(95, 55 + 7 * crit + 3 * len(hypotheses))
        if not classification:
            classification.append({"layer": 11, "layer_name": "Monitoring blind spots",
                "failure_mode": "No clear anomaly in collected signals", "confidence": 40})

        parts = []
        if endpoint_down: parts.append("a down destination is driving a growing queue")
        if store_bad: parts.append("an unpruned message store")
        if oom or (heap_pct and heap_pct >= self.HEAP_CRIT): parts.append("heap exhaustion / OOM")
        if stopped: parts.append(f"{len(stopped)} stopped channel(s)")
        summary = (f"[local] symptom: {symptom or 'n/a'}. " +
                   ("Chain: " + " → ".join(chain) + ". " if chain else "") +
                   ("Primary concerns: " + "; ".join(parts) + "." if parts else "No dominant anomaly detected."))

        return {"severity": severity, "confidence": confidence, "is_dominant": dominant,
                "signals": signals, "causal_chain": chain, "classification": classification,
                "blast_radius": blast_radius, "hypotheses": hypotheses, "ruled_out": ruled_out,
                "immediate": immediate, "durable": durable, "monitoring": monitoring,
                "suggested_actions": actions, "summary": summary}


# ----------------------------------------------------------------------------
# RAG layer — case memory that lets past resolutions ground new diagnoses
# ----------------------------------------------------------------------------
class RetrievalStore:
    """Optional retrieval-augmentation over past resolved incidents.

    Storage : a JSON-lines corpus (one case per line) on disk — no server, no deps.
    Embeddings: sentence-transformers IF RAG_EMBED_MODEL is set AND the package is
                installed; otherwise a deterministic hashed bag-of-words vector in
                pure Python (offline, no deps, no PHI leaves the host).
    Retrieval GROUNDS a diagnosis — it attaches similar past cases and, only on
    strong corroboration, nudges confidence a little. It never overrides a verdict.

    Everything here is stdlib except the guarded sentence-transformers import, so
    --local keeps working with zero extra dependencies.
    """

    def __init__(self, path: str = "", dim: int = 512):
        self.path = path or RAG_STORE_PATH or str(Path(__file__).with_name("mirth_cases.jsonl"))
        self.dim = dim
        self._records: list[dict] = []
        self._model = None
        self.mode = "hashed-bow (no deps)"
        self._init_embedder()
        self._load()

    def _init_embedder(self) -> None:
        if RAG_EMBED_MODEL:
            try:
                from sentence_transformers import SentenceTransformer  # guarded optional dep
                self._model = SentenceTransformer(RAG_EMBED_MODEL)
                self.mode = f"sentence-transformers:{RAG_EMBED_MODEL}"
            except Exception:
                self._model = None  # fall back silently to the pure-Python embedder

    def _load(self) -> None:
        p = Path(self.path)
        if not p.exists():
            return
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._records.append(json.loads(line))
                except Exception:
                    continue

    # --- embedding -------------------------------------------------------
    @staticmethod
    def _l2(v: list[float]) -> list[float]:
        import math
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        n = min(len(a), len(b))  # both are L2-normalised, so dot == cosine
        return sum(a[i] * b[i] for i in range(n))

    def _hashed_bow(self, text: str) -> list[float]:
        import hashlib, re
        vec = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            idx = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dim
            vec[idx] += 1.0
        return self._l2(vec)

    def _embed(self, text: str) -> list[float]:
        if self._model is not None:
            vec = self._model.encode([text])[0]
            return self._l2([float(x) for x in vec])
        return self._hashed_bow(text)

    @staticmethod
    def _case_text(symptom: str, findings: dict) -> str:
        parts = [symptom or ""]
        for s in findings.get("signals", []):
            parts.append(f"{s.get('metric')} {s.get('value')} {s.get('assessment')}")
        for c in findings.get("classification", []):
            parts.append(f"L{c.get('layer')} {c.get('layer_name')} {c.get('failure_mode')}")
        return " | ".join(p for p in parts if p)

    # --- corpus ops ------------------------------------------------------
    def add_case(self, symptom: str, findings: dict, root_cause: str = "", fix: str = "") -> dict:
        text = self._case_text(symptom, findings)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symptom": symptom, "root_cause": root_cause, "fix": fix,
            "severity": findings.get("severity"), "dominant": bool(findings.get("is_dominant")),
            "classification": findings.get("classification", []),
            "text": text, "embed_mode": self.mode, "vector": self._embed(text),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        self._records.append(rec)
        return rec

    def query(self, symptom: str, findings: dict, k: int = 3) -> list[dict]:
        if not self._records:
            return []
        q = self._embed(self._case_text(symptom, findings))
        scored = []
        for rec in self._records:
            # re-embed on mode mismatch so a later model swap doesn't break recall
            v = rec.get("vector") if rec.get("embed_mode") == self.mode else self._embed(rec.get("text", ""))
            scored.append((self._cosine(q, v), rec))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [{
            "similarity": round(score, 3), "when": rec.get("ts"),
            "symptom": rec.get("symptom"), "root_cause": rec.get("root_cause"),
            "fix": rec.get("fix"), "severity": rec.get("severity"),
        } for score, rec in scored[:k]]

    def augment(self, symptom: str, findings: dict, k: int = 3, min_sim: float = 0.35) -> dict:
        cases = [c for c in self.query(symptom, findings, k) if c["similarity"] >= min_sim]
        if not cases:
            return findings
        findings = dict(findings)
        findings["similar_cases"] = cases
        # gentle, non-overriding nudge only on strong corroboration
        if cases[0]["similarity"] >= 0.6 and findings.get("confidence") is not None:
            findings["confidence"] = min(98, int(findings["confidence"]) + 3)
        return findings


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
    conf = f.get("confidence")
    print("\n" + _c("━" * 68, "dim"))
    header = _c(f"  DIAGNOSIS   severity={sev.upper()}", "bold")
    if conf is not None:
        header += _c(f"   confidence={conf}%", "dim")
    if f.get("is_dominant"):
        header += "   " + _c("▲ dominant failure pattern", "warn")
    print(header)
    print(_c("━" * 68, "dim"))
    print(_c("  " + f.get("summary", ""), sev_col))

    if f.get("signals"):
        print(_c("\n  Key signals", "bold"))
        mark = {"critical": "crit", "warning": "warn", "nominal": "ok"}
        for s in f["signals"]:
            dot = _c("●", mark.get(s.get("assessment", ""), "dim"))
            line = f"    {dot} {s.get('metric','')}: {_c(s.get('value',''),'bold')}"
            if s.get("note"):
                line += _c(f"  — {s['note']}", "dim")
            print(line)

    if f.get("causal_chain"):
        print(_c("\n  Causal chain", "bold"))
        print("    " + _c("  →  ", "dim").join(_c(link, "cyan") for link in f["causal_chain"]))

    print(_c("\n  Classification", "bold"))
    for c in f.get("classification", []):
        print(f"    L{c.get('layer')} {c.get('layer_name','')} — {c.get('failure_mode','')}  "
              + _c(f"({c.get('confidence','?')}%)", "dim"))

    if f.get("blast_radius"):
        print(_c("\n  Blast radius", "bold"))
        for b in f["blast_radius"]:
            print(f"    • {b}")

    print(_c("\n  Root-cause hypotheses", "bold"))
    for h in f.get("hypotheses", []):
        print(f"    • {_c(h.get('cause',''),'cyan')}  [{h.get('likelihood','')}]")
        print(_c(f"        evidence: {h.get('evidence','')}", "dim"))
        print(_c(f"        confirm : {h.get('confirm','')}", "dim"))

    if f.get("ruled_out"):
        print(_c("\n  Ruled out", "bold"))
        for r in f["ruled_out"]:
            print(_c(f"    ✗ {r}", "dim"))

    if f.get("similar_cases"):
        print(_c("\n  Similar past incidents (RAG)", "bold"))
        for c in f["similar_cases"]:
            print(f"    ~{int(c.get('similarity', 0) * 100)}%  {_c(c.get('symptom',''),'cyan')}  "
                  + _c(f"({c.get('when','')})", "dim"))
            if c.get("root_cause"):
                print(_c(f"        root cause: {c['root_cause']}", "dim"))
            if c.get("fix"):
                print(_c(f"        fix       : {c['fix']}", "dim"))

    def steps(title, arr, col):
        print(_c(f"\n  {title}", col))
        for i, s in enumerate(arr, 1):
            print(f"    {i}. {_c(s.get('step',''),'bold')} — {s.get('detail','')}")
            if s.get("command"):
                print(_c(f"       $ {s['command']}", "cyan"))
            if s.get("verify"):
                print(_c(f"       ✓ verify: {s['verify']}", "dim"))

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


def build_engine(collector: Collector, args):
    """Pick the diagnostic engine: the deterministic local analyzer (--local)
    or the LLM Agent. Only the Agent needs an API key / the anthropic package."""
    if getattr(args, "local", False):
        return LocalAnalyzer(collector)
    return Agent(collector, model=args.model)


def maybe_augment(symptom: str, findings: dict, args) -> dict:
    """If --rag is set, attach similar past incidents from the case store.
    Never fails the triage — retrieval is best-effort grounding."""
    if not getattr(args, "rag", False) or "_unstructured" in findings:
        return findings
    try:
        store = RetrievalStore()
    except Exception as e:
        print(_c(f"  (RAG unavailable: {e})", "dim"), file=sys.stderr)
        return findings
    print(_c(f"  · RAG: {len(store._records)} case(s) indexed via {store.mode}", "dim"), file=sys.stderr)
    return store.augment(symptom, findings)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def cmd_triage(args):
    collector = build_collector(args.mock)
    engine = build_engine(collector, args)
    logs = ""
    if args.logs:
        logs = sys.stdin.read() if args.logs == "-" else open(args.logs).read()
    who = "the local rule-based analyzer (no API)" if args.local else args.model
    print(_c(f"Working the case with {who}…", "dim"), file=sys.stderr)
    findings = engine.triage(args.symptom, logs=logs)
    findings = maybe_augment(args.symptom, findings, args)
    render(findings)
    maybe_apply(collector, findings, args.apply)


def cmd_watch(args):
    collector = build_collector(args.mock)
    engine = build_engine(collector, args)
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
                findings = engine.triage(symptom)
                findings = maybe_augment(symptom, findings, args)
                render(findings)
                maybe_apply(collector, findings, args.apply)
            else:
                print(_c(f"  {time.strftime('%H:%M:%S')} nominal", "dim"))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")


def cmd_feedback(args):
    """Record a resolved incident into the RAG case store so future triages can
    recall it. The signal/classification snapshot is taken with the local analyzer,
    so recording feedback never needs an API key."""
    collector = build_collector(args.mock)
    findings = LocalAnalyzer(collector, verbose=False).triage(args.symptom)
    store = RetrievalStore()
    store.add_case(args.symptom, findings,
                   root_cause=getattr(args, "root_cause", "") or "",
                   fix=getattr(args, "fix", "") or "")
    print(_c(f"  recorded case → {store.path}", "ok"))
    print(_c(f"  embeddings: {store.mode};  corpus size: {len(store._records)} case(s)", "dim"))


def main():
    p = argparse.ArgumentParser(description="Agentic Mirth Connect triage using the Claude API.")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mock", action="store_true", help="use built-in fake data (no Mirth server)")
    common.add_argument("--local", action="store_true", help="use the built-in rule-based analyzer (no API key, no network)")
    common.add_argument("--rag", action="store_true", help="ground the diagnosis with similar past incidents from the case store")
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

    fb = sub.add_parser("feedback", parents=[common], help="record a resolved incident into the RAG case store")
    fb.add_argument("symptom", help="short description of the incident that was resolved")
    fb.add_argument("--root-cause", help="the confirmed root cause")
    fb.add_argument("--fix", help="what actually resolved it")
    fb.set_defaults(func=cmd_feedback)

    args = p.parse_args()
    try:
        args.func(args)
    except RuntimeError as e:
        print(_c(f"error: {e}", "crit")); sys.exit(1)


if __name__ == "__main__":
    main()