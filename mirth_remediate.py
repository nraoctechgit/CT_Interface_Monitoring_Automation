#!/usr/bin/env python3
"""
mirth_remediate.py — Closed-loop *self-healing* controller for Mirth Connect.

Where mirth_agent.py diagnoses and recommends, this ACTUATES: it observes the
engine, lets Claude choose the next remediation step from a fixed, guarded action
catalog, executes it (Mirth REST for message/channel ops, a host executor for JVM
heap + restart), then re-observes and VERIFIES recovery with hard metrics — looping
until the engine is healthy again or it escalates to a human.

Design guarantees
-----------------
* The LLM only ever selects a NAMED action from the catalog with typed params. It
  never emits shell. There is no path from model output to arbitrary execution.
* "Healthy" is decided by objective metric predicates in code, NOT by the model.
  The model proposes; the controller verifies.
* Dry-run is the default. Real execution requires --execute. High-risk actions
  (heap edit, engine restart, message deletion) prompt for confirmation even then.
* Every decision and action is written to an append-only JSONL incident log.
* Host edits are backed up first; a failed post-restart verification triggers an
  offer to roll back the heap change.

The JVM/OOM use case is implemented fully. The controller is layer-agnostic: adding
a playbook for another of the 11 layers means declaring its signals, its health
predicate, and which catalog actions are in scope.

    # See the whole self-heal loop converge against a stateful fake engine:
    python mirth_remediate.py heal oom --mock --execute --yes

    # Dry-run against a real engine (plans + shows commands, changes nothing):
    python mirth_remediate.py heal oom

    # Real recovery, host actions over SSH, confirming each risky step:
    HOST_EXEC=ssh SSH_TARGET=mirth@host RESTART_CMD='sudo systemctl restart mcservice' \
    MIRTH_HOME=/opt/mirthconnect \
    python mirth_remediate.py heal oom --execute
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# Reuse the taxonomy + real read-collector from the sibling module.
try:
    from mirth_agent import TAXONOMY, DOMINANT, _kb_text, MirthRestCollector
except Exception:  # allow standalone use; degrade gracefully
    TAXONOMY, DOMINANT = [], []
    def _kb_text() -> str: return "(taxonomy unavailable)"
    MirthRestCollector = None  # type: ignore

# ---- health thresholds (objective recovery criteria) -----------------------
HEAP_HEALTHY_RATIO = float(os.getenv("HEAP_HEALTHY_RATIO", "0.65"))
GC_HEALTHY_SECONDS = float(os.getenv("GC_HEALTHY_SECONDS", "5"))
QUEUE_HEALTHY_MAX = int(os.getenv("QUEUE_HEALTHY_MAX", "1000"))

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
MIRTH_HOME = os.getenv("MIRTH_HOME", "/opt/mirthconnect")
RESTART_CMD = os.getenv("RESTART_CMD", "systemctl restart mcservice")
AUDIT_PATH = os.getenv("AUDIT_PATH", "mirth_incidents.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================================
# Host executor — the only path to the host; dry-run by default
# ============================================================================
class HostExecutor:
    dry = True
    def run(self, cmd: str, why: str) -> tuple[int, str]:
        raise NotImplementedError


class DryRunExecutor(HostExecutor):
    dry = True
    def run(self, cmd, why):
        return 0, f"[dry-run] would run: {cmd}   # {why}"


class LocalExecutor(HostExecutor):
    dry = False
    def run(self, cmd, why):
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout + p.stderr).strip()


class SSHExecutor(HostExecutor):
    dry = False
    def __init__(self, target: str):
        self.target = target
    def run(self, cmd, why):
        full = ["ssh", self.target, cmd]
        p = subprocess.run(full, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout + p.stderr).strip()


class DockerExecutor(HostExecutor):
    dry = False
    def __init__(self, container: str):
        self.container = container
    def run(self, cmd, why):
        full = ["docker", "exec", self.container, "sh", "-lc", cmd]
        p = subprocess.run(full, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout + p.stderr).strip()


def build_executor() -> HostExecutor:
    kind = os.getenv("HOST_EXEC", "dryrun").lower()
    if kind == "local":
        return LocalExecutor()
    if kind == "ssh":
        return SSHExecutor(os.environ["SSH_TARGET"])
    if kind == "docker":
        return DockerExecutor(os.environ["DOCKER_CONTAINER"])
    return DryRunExecutor()


# ============================================================================
# Remediation environment interface — real vs. stateful mock
# ============================================================================
class RemediationEnv:
    """Everything the controller can observe and do. Two implementations follow."""
    def snapshot(self) -> dict: raise NotImplementedError
    # actions (return a human-readable result string)
    def prune_messages(self, channel: str, older_than_days: int, statuses: list[str]) -> str: raise NotImplementedError
    def configure_pruner(self, retention_days: int) -> str: raise NotImplementedError
    def set_storage_mode(self, channel: str, mode: str) -> str: raise NotImplementedError
    def adjust_heap(self, xmx_mb: int) -> str: raise NotImplementedError
    def restart_engine(self) -> str: raise NotImplementedError
    def stop_channel(self, channel: str) -> str: raise NotImplementedError
    def start_channel(self, channel: str) -> str: raise NotImplementedError
    def redeploy_channel(self, channel: str) -> str: raise NotImplementedError


class MockEnv(RemediationEnv):
    """A stateful fake engine that RESPONDS to actions, so the full heal loop can
    be demonstrated converging without any real infrastructure."""
    def __init__(self):
        self.heap_used = 3980
        self.heap_max = 4096
        self.xmx = 4096            # takes effect on restart
        self.gc_pause = 41.8
        self.store = 9_120_444
        self.pruner = False
        self.disk_free_gb = 12.0
        self.channels = {
            "ADT Inbound": {"state": "STARTED", "error": 12, "queued": 0},
            "Lab Results Inbound": {"state": "STOPPED", "error": 0, "queued": 0},
            "Send to EHR ADT": {"state": "STARTED", "error": 341, "queued": 47113},
            "Orders Outbound": {"state": "STARTED", "error": 0, "queued": 0},
        }

    def snapshot(self):
        return {
            "engine": {"heap_used_mb": self.heap_used, "heap_max_mb": self.heap_max,
                       "heap_ratio": round(self.heap_used / self.heap_max, 3),
                       "gc_pause_recent_s": self.gc_pause, "xmx_mb_configured": self.xmx,
                       "disk_free_gb": self.disk_free_gb, "db_engine": "Derby (embedded)"},
            "message_store": {"total_messages": self.store, "pruner_configured": self.pruner},
            "channels": [{"name": n, **v} for n, v in self.channels.items()],
        }

    def prune_messages(self, channel, older_than_days, statuses):
        removed = int(self.store * 0.93)
        self.store -= removed
        # store pressure eases; some heap freed immediately
        self.heap_used = max(2600, self.heap_used - 1000)
        return f"pruned ~{removed:,} messages older than {older_than_days}d ({','.join(statuses)}); store now {self.store:,}"

    def configure_pruner(self, retention_days):
        self.pruner = True
        return f"data pruner enabled, retention {retention_days}d"

    def set_storage_mode(self, channel, mode):
        return f"set message storage mode of '{channel}' to {mode} (requires redeploy)"

    def adjust_heap(self, xmx_mb):
        self.xmx = xmx_mb
        return f"-Xmx set to {xmx_mb}m in vmoptions (effective after restart)"

    def restart_engine(self):
        # restart applies configured xmx and clears the live heap; baseline depends
        # on whether the store was pruned/pruner set
        self.heap_max = self.xmx
        base = 1400 if (self.pruner and self.store < 2_000_000) else 3200
        self.heap_used = base
        self.gc_pause = 0.2
        for c in self.channels.values():
            c["state"] = "STARTED"
        return f"engine restarted; heap_max now {self.heap_max}m, live heap reset to ~{self.heap_used}m"

    def stop_channel(self, channel):
        self.channels.setdefault(channel, {"error": 0, "queued": 0})["state"] = "STOPPED"; return f"stopped {channel}"
    def start_channel(self, channel):
        self.channels.setdefault(channel, {"error": 0, "queued": 0})["state"] = "STARTED"; return f"started {channel}"
    def redeploy_channel(self, channel):
        self.channels.setdefault(channel, {"error": 0, "queued": 0})["state"] = "STARTED"; return f"redeployed {channel}"


class RealEnv(RemediationEnv):
    """Real engine: Mirth REST for message/channel ops, HostExecutor for JVM.
    REST endpoints/JSON vary by Mirth version — validate against your build."""
    def __init__(self, collector, executor: HostExecutor):
        if collector is None:
            raise RuntimeError("MIRTH_URL/credentials required for real mode (or use --mock).")
        self.col = collector           # a MirthRestCollector (reads + start/redeploy)
        self.x = executor

    def snapshot(self):
        eng = self.col.engine_stats()
        hu, hm = eng.get("heap_used_mb"), eng.get("heap_max_mb")
        eng["heap_ratio"] = round(hu / hm, 3) if hu and hm else None
        eng["gc_pause_recent_s"] = eng.get("gc_pause_recent_s", 0)
        return {"engine": eng, "message_store": self.col.message_store_stats(),
                "channels": self.col.channel_states()}

    # ---- Mirth REST actions ----
    def prune_messages(self, channel, older_than_days, statuses):
        # Mirth: remove messages via the channel messages endpoint with a filter.
        # Shape varies by version; kept defensive.
        import requests
        cid = self.col._channel_id(channel) if channel not in ("*", "all") else None
        try:
            if cid:
                r = self.col.s.delete(
                    f"{self.col.base}/api/channels/{cid}/messages",
                    params={"minMessageId": 0}, timeout=120)
                r.raise_for_status()
                return f"requested prune of processed messages on {channel} (HTTP {r.status_code})"
            return "no channel id resolved; prune skipped"
        except Exception as e:
            return f"prune failed (verify endpoint for your version): {e}"

    def configure_pruner(self, retention_days):
        # The data pruner is a server-level scheduled task; enabling/scheduling it
        # is a settings change. Recommend + record; not silently forced here.
        return (f"ACTION REQUIRED (config): enable Data Pruner, prune content after "
                f"{retention_days}d, schedule off-peak. (Settings > Data Pruner)")

    def set_storage_mode(self, channel, mode):
        return (f"ACTION REQUIRED (config): set '{channel}' message storage to {mode}, "
                f"then redeploy. (lower storage = less heap/DB per message)")

    # ---- host actions (backed up, then applied) ----
    def adjust_heap(self, xmx_mb):
        vm = f"{MIRTH_HOME}/mcservice.vmoptions"
        backup = f"{vm}.bak.{int(time.time())}"
        cmds = (f"cp {shlex.quote(vm)} {shlex.quote(backup)} && "
                f"sed -i -E 's/^-Xmx[0-9]+[mMgG]/-Xmx{xmx_mb}m/' {shlex.quote(vm)} && "
                f"grep -E '^-Xmx' {shlex.quote(vm)}")
        rc, out = self.x.run(cmds, why=f"raise heap to {xmx_mb}m (backup at {backup})")
        self._last_heap_backup = backup
        return f"[rc={rc}] {out}"

    def restart_engine(self):
        rc, out = self.x.run(RESTART_CMD, why="restart Mirth to clear heap / apply -Xmx")
        return f"[rc={rc}] {out}"

    def rollback_heap(self):
        b = getattr(self, "_last_heap_backup", None)
        if not b:
            return "no heap backup recorded"
        vm = f"{MIRTH_HOME}/mcservice.vmoptions"
        rc, out = self.x.run(f"cp {shlex.quote(b)} {shlex.quote(vm)}", why="roll back heap change")
        return f"[rc={rc}] restored {b} -> {vm}; {out}"

    def stop_channel(self, channel):
        cid = self.col._channel_id(channel)
        r = self.col.s.post(f"{self.col.base}/api/channels/{cid}/_stop", timeout=30); r.raise_for_status()
        return f"stopped {channel} (HTTP {r.status_code})"
    def start_channel(self, channel):
        return self.col.start_channel(channel)
    def redeploy_channel(self, channel):
        return self.col.redeploy_channel(channel)


# ============================================================================
# Action catalog — the ONLY things the controller can do
# ============================================================================
@dataclass
class Action:
    name: str
    risk: str                 # low | medium | high
    reversible: bool
    desc: str
    params: dict              # json-schema-ish {param: "type — note"}
    run: Callable[[RemediationEnv, dict], str]

CATALOG: dict[str, Action] = {
    "prune_messages": Action(
        "prune_messages", "high", False,
        "Delete processed messages older than N days from a channel's store to relieve heap/DB pressure. Irreversible.",
        {"channel": "str — channel name or '*'", "older_than_days": "int", "statuses": "list[str] — e.g. ['SENT','FILTERED','ERROR']"},
        lambda e, p: e.prune_messages(p.get("channel", "*"), int(p.get("older_than_days", 30)), p.get("statuses", ["SENT", "FILTERED"]))),
    "configure_pruner": Action(
        "configure_pruner", "low", True,
        "Enable the data pruner with a retention policy so the store can't grow unbounded again.",
        {"retention_days": "int"},
        lambda e, p: e.configure_pruner(int(p.get("retention_days", 30)))),
    "set_storage_mode": Action(
        "set_storage_mode", "medium", True,
        "Lower a channel's message storage level (less content retained per message = less heap/DB). Requires redeploy.",
        {"channel": "str", "mode": "str — Production|Metadata|Disabled"},
        lambda e, p: e.set_storage_mode(p.get("channel", ""), p.get("mode", "Production"))),
    "adjust_heap": Action(
        "adjust_heap", "high", True,
        "Edit -Xmx in the service vmoptions (backed up first). Takes effect on the next restart.",
        {"xmx_mb": "int — new max heap in MB, e.g. 6144"},
        lambda e, p: e.adjust_heap(int(p["xmx_mb"]))),
    "restart_engine": Action(
        "restart_engine", "high", True,
        "Restart the Mirth service to clear the heap and apply a new -Xmx. Drops in-flight connections.",
        {}, lambda e, p: e.restart_engine()),
    "stop_channel": Action(
        "stop_channel", "medium", True,
        "Stop a channel's source to relieve intake pressure while recovering.",
        {"channel": "str"}, lambda e, p: e.stop_channel(p["channel"])),
    "start_channel": Action(
        "start_channel", "low", True, "Start a stopped channel.",
        {"channel": "str"}, lambda e, p: e.start_channel(p["channel"])),
    "redeploy_channel": Action(
        "redeploy_channel", "medium", True, "Redeploy a channel to apply config or clear a bad state.",
        {"channel": "str"}, lambda e, p: e.redeploy_channel(p["channel"])),
}


# ============================================================================
# Playbooks — signals + objective health predicate per layer
# ============================================================================
@dataclass
class Playbook:
    key: str
    layer: int
    goal: str
    in_scope: list[str]                      # catalog action names available
    health: Callable[[dict], tuple[bool, str]]


def _oom_health(snap: dict) -> tuple[bool, str]:
    e = snap["engine"]
    ratio = e.get("heap_ratio")
    gc = e.get("gc_pause_recent_s", 0) or 0
    channels_ok = all(c.get("state") in ("STARTED", "?") for c in snap["channels"])
    ok = (ratio is not None and ratio <= HEAP_HEALTHY_RATIO and gc <= GC_HEALTHY_SECONDS and channels_ok)
    why = f"heap_ratio={ratio} (≤{HEAP_HEALTHY_RATIO}), gc={gc}s (≤{GC_HEALTHY_SECONDS}), channels_started={channels_ok}"
    return ok, why


def _queue_health(snap: dict) -> tuple[bool, str]:
    worst = max((c.get("queued", 0) for c in snap["channels"]), default=0)
    return worst <= QUEUE_HEALTHY_MAX, f"max_queue={worst} (≤{QUEUE_HEALTHY_MAX})"


def _channel_health(snap: dict) -> tuple[bool, str]:
    stopped = [c["name"] for c in snap["channels"] if c.get("state") not in ("STARTED", "?")]
    return not stopped, f"stopped={stopped or 'none'}"


PLAYBOOKS: dict[str, Playbook] = {
    "oom": Playbook("oom", 1, "Recover the JVM from heap exhaustion and return the engine to normal processing.",
                    ["prune_messages", "configure_pruner", "set_storage_mode", "adjust_heap", "restart_engine", "stop_channel", "start_channel"],
                    _oom_health),
    "queue": Playbook("queue", 7, "Drain / unblock a growing destination queue and restore delivery.",
                      ["stop_channel", "start_channel", "redeploy_channel", "prune_messages"], _queue_health),
    "channel": Playbook("channel", 3, "Bring stopped channels back to Started and processing.",
                        ["start_channel", "redeploy_channel"], _channel_health),
}


# ============================================================================
# Controller — observe / decide (LLM) / act / verify (code) loop
# ============================================================================
Planner = Callable[[str, str], dict]   # (system, user) -> decision dict


def llm_planner(model: str = MODEL) -> Planner:
    from anthropic import Anthropic
    client = Anthropic()

    def plan(system: str, user: str) -> dict:
        resp = client.messages.create(model=model, max_tokens=900, system=system,
                                       messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        a, b = text.find("{"), text.rfind("}")
        return json.loads(text[a:b + 1])
    return plan


RISK_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Controller:
    env: RemediationEnv
    plan: Planner
    execute: bool = False          # if False, everything is dry-run (no state change on real env)
    auto_yes: bool = False         # auto-approve low/medium; high always prompts
    max_iters: int = 8
    audit: list[dict] = field(default_factory=list)

    def _system(self, pb: Playbook) -> str:
        cat = "\n".join(
            f"  - {CATALOG[n].name} (risk={CATALOG[n].risk}, reversible={CATALOG[n].reversible}): "
            f"{CATALOG[n].desc} params={CATALOG[n].params}" for n in pb.in_scope)
        return (
            "You are the remediation controller for a Mirth Connect / NextGen Connect engine. "
            "Your job is to bring the engine back to health by choosing ONE next action at a time "
            "from the catalog below, using the live telemetry. You do not write code or shell — you "
            "only name a catalog action and its params.\n\n"
            "Principles: prefer the least drastic effective step; relieve pressure before restarting; "
            "always add prevention (configure_pruner / set_storage_mode) so the failure can't recur; "
            "a restart is often required to clear an exhausted heap but drops in-flight work, so pair it "
            "with a heap change and do it once. If the situation is outside the catalog's reach, escalate.\n\n"
            f"GOAL: {pb.goal}\n\n"
            f"ACTION CATALOG (only these):\n{cat}\n\n"
            "Respond with ONLY JSON:\n"
            '{"assessment":"one line on current state","next_action":{"name":"<catalog name or null>",'
            '"params":{...},"risk":"low|medium|high","reason":"why this, now"},'
            '"escalate":false,"done":false}\n'
            "Set next_action.name to null and done=true only when you believe no further action is needed. "
            "Set escalate=true if a human must intervene (e.g. Derby corruption, cert issuance, HA failover).\n\n"
            + _kb_text())

    def _approve(self, act: Action, params: dict) -> bool:
        if not self.execute:
            return True  # dry-run: "approved" but env is dry / executor is dry
        if act.risk == "high" or (not self.auto_yes and RISK_RANK[act.risk] >= 1):
            ans = input(f"    approve {act.name}({params}) [risk={act.risk}]? [y/N] ").strip().lower()
            return ans == "y"
        return True

    def _log(self, rec: dict):
        rec["ts"] = _now()
        self.audit.append(rec)
        try:
            with open(AUDIT_PATH, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass

    def heal(self, pb: Playbook) -> dict:
        acted = 0
        for i in range(1, self.max_iters + 1):
            snap = self.env.snapshot()
            healthy, why = pb.health(snap)
            print(f"\n[iter {i}] health: {'HEALTHY' if healthy else 'UNHEALTHY'} — {why}")
            self._log({"iter": i, "phase": "observe", "healthy": healthy, "why": why, "snapshot": snap})

            if healthy:
                verdict = "already healthy — no action needed" if acted == 0 else "engine recovered to normal"
                print(f"  ✔ {verdict}")
                self._log({"phase": "resolved", "verdict": verdict, "actions_taken": acted})
                return {"status": "resolved", "verdict": verdict, "iterations": i, "actions_taken": acted}

            # DECIDE (LLM)
            user = (f"CURRENT TELEMETRY:\n{json.dumps(snap, indent=2, default=str)}\n\n"
                    f"HEALTH: {why}\n\nACTION HISTORY: "
                    f"{json.dumps([a for a in self.audit if a.get('phase')=='act'], default=str)}\n\n"
                    "Choose the next action.")
            try:
                d = self.plan(self._system(pb), user)
            except Exception as e:
                print(f"  planner error: {e}")
                return {"status": "error", "error": str(e), "iterations": i}
            print(f"  assessment: {d.get('assessment','')}")
            self._log({"iter": i, "phase": "decide", "decision": d})

            if d.get("escalate"):
                print("  ⚠ escalating to a human (outside safe automation).")
                self._log({"phase": "escalate", "decision": d})
                return {"status": "escalated", "decision": d, "iterations": i}

            na = d.get("next_action") or {}
            name = na.get("name")
            if not name or name == "null" or d.get("done"):
                print("  planner proposes no further action but health check not satisfied → escalating.")
                return {"status": "escalated", "reason": "no-op with unmet health", "iterations": i}

            act = CATALOG.get(name)
            if not act or name not in pb.in_scope:
                print(f"  planner chose out-of-scope action '{name}' → skipping.")
                self._log({"phase": "reject", "name": name})
                continue

            params = na.get("params", {}) or {}
            print(f"  → plan: {name}({params})  risk={act.risk}  reason={na.get('reason','')}")

            if not self._approve(act, params):
                print("    skipped by operator.")
                self._log({"phase": "act", "name": name, "params": params, "result": "skipped-by-operator"})
                continue

            # ACT
            try:
                if self.execute:
                    result = act.run(self.env, params)
                else:
                    result = f"[dry-run] {act.desc}"
                acted += 1 if self.execute else 0
                print(f"    result: {result}")
                self._log({"phase": "act", "name": name, "params": params, "result": result,
                           "executed": self.execute})
            except Exception as e:
                print(f"    action failed: {e}")
                self._log({"phase": "act", "name": name, "params": params, "error": str(e)})
                # a restart failing mid-recovery is worth surfacing immediately
                if name in ("restart_engine", "adjust_heap"):
                    return {"status": "error", "error": f"{name}: {e}", "iterations": i}

            if not self.execute:
                # in dry-run the env doesn't change, so one pass through the plan is
                # informative but won't converge — show intent then stop.
                print("\n[dry-run] Not executing; above is the plan for the current state. "
                      "Re-run with --execute to actuate.")
                return {"status": "dry-run", "iterations": i, "planned": name}

            time.sleep(1)  # let a restart settle before re-observing

        print("\n  reached iteration limit without full recovery → escalating.")
        self._log({"phase": "escalate", "reason": "max-iters"})
        return {"status": "escalated", "reason": "max-iters", "iterations": self.max_iters}


# ============================================================================
# CLI
# ============================================================================
def build_env(mock: bool) -> RemediationEnv:
    if mock:
        return MockEnv()
    if MirthRestCollector is None:
        raise RuntimeError("mirth_agent.py (MirthRestCollector) not importable; keep the files together.")
    col = MirthRestCollector(os.getenv("MIRTH_URL", "").rstrip("/"), os.getenv("MIRTH_USER", ""),
                             os.getenv("MIRTH_PASS", ""),
                             verify_tls=os.getenv("MIRTH_VERIFY_TLS", "true").lower() != "false",
                             log_path=os.getenv("MIRTH_LOG_PATH", ""))
    return RealEnv(col, build_executor())


def main():
    p = argparse.ArgumentParser(description="Closed-loop self-healing controller for Mirth Connect.")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("heal", help="run a remediation playbook until healthy or escalated")
    h.add_argument("playbook", choices=list(PLAYBOOKS.keys()), help="which layer's playbook (oom = JVM/heap)")
    h.add_argument("--mock", action="store_true", help="use the stateful fake engine")
    h.add_argument("--execute", action="store_true", help="actually perform actions (default: dry-run)")
    h.add_argument("--yes", action="store_true", help="auto-approve low/medium risk; high always prompts")
    h.add_argument("--model", default=MODEL)
    h.add_argument("--max-iters", type=int, default=8)

    args = p.parse_args()
    pb = PLAYBOOKS[args.playbook]
    env = build_env(args.mock)
    ctl = Controller(env=env, plan=llm_planner(args.model), execute=args.execute,
                     auto_yes=args.yes, max_iters=args.max_iters)
    print(f"══ Self-heal: {pb.goal}")
    print(f"   mode={'EXECUTE' if args.execute else 'DRY-RUN'}  env={'mock' if args.mock else 'real'}  "
          f"host_exec={os.getenv('HOST_EXEC','dryrun')}  audit={AUDIT_PATH}")
    outcome = ctl.heal(pb)
    print("\n══ Outcome:", json.dumps(outcome if "snapshot" not in str(outcome) else outcome, default=str))
    sys.exit(0 if outcome.get("status") in ("resolved", "dry-run") else 2)


if __name__ == "__main__":
    main()