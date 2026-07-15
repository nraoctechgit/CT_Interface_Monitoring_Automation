#!/usr/bin/env python3
"""
mirth_oom_agent.py — Agentic diagnostic + recovery assistant for Mirth Connect.

Scenario it handles:
    Two Java processes — the "Mirth Connect Service Manager" (server service)
    and the "Mirth Connect Administrator Launcher" — both went down after the
    JVM ran out of heap (java.lang.OutOfMemoryError: Java heap space).

What the agent does (Claude drives the loop; this file only provides tools):
    1. Reads the log tails and confirms the OOM as the root cause.
    2. Reads the current -Xms/-Xmx from each process's .vmoptions file.
    3. Sizes a safe new heap against the machine's physical RAM.
    4. (with --apply) Backs up and rewrites the .vmoptions files.
    5. (with --apply) Restarts the server service and relaunches the launcher.
    6. Prints a plain-language incident summary.

Read-only tools always run. Mutating tools (edit config, restart) are gated:
without --apply they return a dry-run description; with --apply they still
require an interactive y/N confirmation unless --yes is also passed.

Usage:
    python mirth_oom_agent.py                 # analyze only (safe, read-only)
    python mirth_oom_agent.py --apply         # analyze + fix, prompt before each change
    python mirth_oom_agent.py --apply --yes   # analyze + fix, no prompts (automation)

Auth: set the ANTHROPIC_API_KEY environment variable to your key
(from https://console.anthropic.com/settings/keys). The SDK reads it
automatically. On Windows: `setx ANTHROPIC_API_KEY "sk-ant-..."` (then open
a new terminal), or `$env:ANTHROPIC_API_KEY = "sk-ant-..."` for one session.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

import anthropic
from anthropic import beta_tool

MODEL = "claude-opus-4-8"

# --------------------------------------------------------------------------- #
# Environment description. Edit these paths/names to match your install.
# Each "process" entry names a Windows service (for the server) OR an exe to
# launch (for the launcher), plus its .vmoptions file and log file.
# --------------------------------------------------------------------------- #
PROCESSES = {
    "service_manager": {
        "label": "Mirth Connect Service Manager (server service)",
        "kind": "service",                       # controlled via Windows service
        "service_name": "Mirth Connect Service",  # `sc query` name
        "vmoptions": r"C:\Program Files\Mirth Connect\mcservice.vmoptions",
        "log": r"C:\Program Files\Mirth Connect\logs\mirth.log",
    },
    "launcher": {
        "label": "Mirth Connect Administrator Launcher",
        "kind": "exe",                           # controlled by launching the exe
        "exe": r"C:\Program Files\Mirth Connect Administrator Launcher\launcher.exe",
        "vmoptions": r"C:\Program Files\Mirth Connect Administrator Launcher\launcher.vmoptions",
        "log": r"C:\Program Files\Mirth Connect Administrator Launcher\logs\launcher.log",
    },
}

# Runtime flags, set from argv in main(). Mutating tools read these.
APPLY = False   # actually perform mutating actions
ASSUME_YES = False  # skip the y/N confirmation prompt


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _run_ps(command: str) -> subprocess.CompletedProcess:
    """Run a PowerShell command and capture output."""
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=120,
    )


def _confirm(action: str) -> bool:
    """Gate a mutating action. Returns True if it should proceed."""
    if not APPLY:
        return False
    if ASSUME_YES:
        return True
    try:
        answer = input(f"\n  >>> CONFIRM: {action}\n      Proceed? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _proc(key: str) -> dict:
    if key not in PROCESSES:
        raise ValueError(f"Unknown process '{key}'. Valid keys: {list(PROCESSES)}")
    return PROCESSES[key]


def _load_env_file(path: str = ".env") -> None:
    """Load ANTHROPIC_API_KEY (and other simple KEY=VALUE lines) from a local
    .env if they aren't already set in the environment. Keeps the demo one-liner
    working without the operator having to export the key by hand."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.isfile(here):
        return
    with open(here, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
            if not line or line.startswith("#") or not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            if val and val[0] not in ("'", '"'):
                val = val.split("#", 1)[0].strip()
            os.environ.setdefault(key, val.strip("'\""))


# --------------------------------------------------------------------------- #
# READ-ONLY tools
# --------------------------------------------------------------------------- #
@beta_tool
def get_physical_ram_mb() -> str:
    """Return the machine's total physical RAM in megabytes. Use this to size the heap."""
    r = _run_ps("(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory")
    raw = (r.stdout or "").strip()
    if not raw.isdigit():
        return f"Could not read RAM (stderr: {r.stderr.strip()})"
    return f"{int(raw) // (1024 * 1024)} MB total physical RAM"


@beta_tool
def check_status(process_key: str) -> str:
    """Check whether a process is currently running.

    Args:
        process_key: One of "service_manager" or "launcher".
    """
    p = _proc(process_key)
    if p["kind"] == "service":
        r = _run_ps(f'Get-Service -Name "{p["service_name"]}" | '
                    f'Select-Object -ExpandProperty Status')
        status = (r.stdout or "").strip() or r.stderr.strip()
        return f'{p["label"]}: service status = {status or "NOT FOUND"}'
    # exe-based process: match by image name
    exe_name = os.path.basename(p["exe"])
    r = _run_ps(f'(Get-Process | Where-Object {{ $_.Path -eq "{p["exe"]}" }}).Count')
    count = (r.stdout or "").strip()
    running = count.isdigit() and int(count) > 0
    return f'{p["label"]}: {"RUNNING" if running else "NOT running"} ({exe_name})'


@beta_tool
def scan_log_for_oom(process_key: str, max_lines: int = 4000) -> str:
    """Scan the tail of a process's log file for OutOfMemoryError and related evidence.

    Args:
        process_key: One of "service_manager" or "launcher".
        max_lines: How many lines from the end of the log to scan.
    """
    p = _proc(process_key)
    path = p["log"]
    if not os.path.isfile(path):
        return f'Log not found: {path}'
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()[-max_lines:]

    pattern = re.compile(
        r"OutOfMemoryError|Java heap space|GC overhead limit|"
        r"unable to create.*thread|Metaspace|heapdump|"
        r"ExitOnOutOfMemoryError",
        re.IGNORECASE,
    )
    hits = [ln.rstrip() for ln in lines if pattern.search(ln)]
    if not hits:
        return f'{p["label"]}: no OOM evidence in last {max_lines} lines of {path}'
    sample = "\n".join(hits[-15:])
    return (f'{p["label"]}: found {len(hits)} OOM-related line(s) in {path}.\n'
            f'Most recent:\n{sample}')


@beta_tool
def read_heap_settings(process_key: str) -> str:
    """Read the current -Xms and -Xmx heap settings from a process's .vmoptions file.

    Args:
        process_key: One of "service_manager" or "launcher".
    """
    p = _proc(process_key)
    path = p["vmoptions"]
    if not os.path.isfile(path):
        return f'.vmoptions not found: {path}'
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    xms = re.search(r"-Xms(\d+)([mMgG])", content)
    xmx = re.search(r"-Xmx(\d+)([mMgG])", content)
    def norm(m):
        if not m:
            return "unset"
        val, unit = int(m.group(1)), m.group(2).lower()
        return f"{val * 1024 if unit == 'g' else val}m"
    return (f'{p["label"]} ({path}):\n'
            f'  -Xms = {norm(xms)}\n'
            f'  -Xmx = {norm(xmx)}\n'
            f'  ExitOnOutOfMemoryError present: {"-XX:+ExitOnOutOfMemoryError" in content}')


@beta_tool
def recommend_heap_size(total_ram_mb: int, process_key: str) -> str:
    """Compute a safe new -Xms/-Xmx for a process given the machine's RAM.

    Server gets up to ~50% of RAM (capped 4096m); the launcher is a client
    UI and gets a smaller share. Both leave headroom for the OS and each other.

    Args:
        total_ram_mb: Total physical RAM in MB (from get_physical_ram_mb).
        process_key: One of "service_manager" or "launcher".
    """
    p = _proc(process_key)
    if p["kind"] == "service":
        xmx = min(max(total_ram_mb // 2, 1024), 4096)
        xms = max(xmx // 4, 512)
    else:  # launcher / client UI
        xmx = min(max(total_ram_mb // 8, 512), 1536)
        xms = max(xmx // 4, 256)
    return (f'{p["label"]} recommendation:\n'
            f'  -Xms{xms}m\n'
            f'  -Xmx{xmx}m\n'
            f'  (also add -XX:+HeapDumpOnOutOfMemoryError and -XX:+ExitOnOutOfMemoryError)')


# --------------------------------------------------------------------------- #
# MUTATING tools (gated by --apply / --yes)
# --------------------------------------------------------------------------- #
@beta_tool
def update_heap_settings(process_key: str, xms_mb: int, xmx_mb: int) -> str:
    """Back up and rewrite a process's .vmoptions with new heap + OOM-safety flags.

    Args:
        process_key: One of "service_manager" or "launcher".
        xms_mb: New initial heap in MB (-Xms).
        xmx_mb: New max heap in MB (-Xmx).
    """
    p = _proc(process_key)
    path = p["vmoptions"]
    action = f'rewrite {path} with -Xms{xms_mb}m / -Xmx{xmx_mb}m'
    if not _confirm(action):
        return f'DRY RUN (no --apply/confirmation): would {action}'
    if not os.path.isfile(path):
        return f'Cannot edit — file not found: {path}'

    backup = f"{path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(path, backup)

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = [ln for ln in fh.read().splitlines()
                 if not re.match(r"\s*-Xm[sx]\d", ln)
                 and "HeapDumpOnOutOfMemoryError" not in ln
                 and "ExitOnOutOfMemoryError" not in ln]
    lines = [f"-Xms{xms_mb}m", f"-Xmx{xmx_mb}m",
             "-XX:+HeapDumpOnOutOfMemoryError",
             "-XX:+ExitOnOutOfMemoryError"] + lines
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return f'Updated {path} (backup: {backup}). New heap: -Xms{xms_mb}m -Xmx{xmx_mb}m'


@beta_tool
def restart_process(process_key: str) -> str:
    """Restart a process: stop+start the service, or relaunch the exe.

    Args:
        process_key: One of "service_manager" or "launcher".
    """
    p = _proc(process_key)
    if p["kind"] == "service":
        action = f'restart Windows service "{p["service_name"]}"'
        if not _confirm(action):
            return f'DRY RUN (no --apply/confirmation): would {action}'
        r = _run_ps(f'Restart-Service -Name "{p["service_name"]}" -Force -ErrorAction Stop; '
                    f'Start-Sleep -Seconds 3; '
                    f'(Get-Service -Name "{p["service_name"]}").Status')
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            return f'Restart FAILED: {r.stderr.strip() or out}'
        return f'{p["label"]} restarted. Service status now: {out}'
    # exe
    action = f'launch {p["exe"]}'
    if not _confirm(action):
        return f'DRY RUN (no --apply/confirmation): would {action}'
    if not os.path.isfile(p["exe"]):
        return f'Cannot launch — exe not found: {p["exe"]}'
    subprocess.Popen([p["exe"]], close_fds=True)
    return f'{p["label"]} launched.'


READONLY = [get_physical_ram_mb, check_status, scan_log_for_oom,
            read_heap_settings, recommend_heap_size]
MUTATING = [update_heap_settings, restart_process]

SYSTEM = """\
You are an SRE assistant handling a Mirth Connect outage. Two Java processes —
the Mirth Connect Service Manager (server service) and the Administrator
Launcher — both crashed with a JVM OutOfMemoryError.

Work the incident in this order, using the tools:
1. Confirm both processes are down (check_status).
2. Confirm the OOM is the root cause from the logs (scan_log_for_oom).
3. Read the current heap settings for both (read_heap_settings).
4. Get the machine RAM (get_physical_ram_mb) and size new heaps
   (recommend_heap_size) for both processes.
5. Apply the new heap settings (update_heap_settings) — server first.
6. Restart the server service, verify it comes up, then relaunch the launcher
   (restart_process).
7. Verify both are running again (check_status).

Mutating tools may return "DRY RUN" if changes aren't authorized — if so, do NOT
keep retrying them; report what you would have done and stop.

Finish with a short incident report: root cause, the exact old→new heap values
for each process, what you changed, current status, and one prevention tip."""


def main() -> int:
    global APPLY, ASSUME_YES
    ap = argparse.ArgumentParser(description="Agentic Mirth Connect OOM diagnosis & recovery.")
    ap.add_argument("--apply", action="store_true",
                    help="actually edit configs and restart processes (default: dry run)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the y/N confirmation before each mutating action")
    args = ap.parse_args()
    APPLY, ASSUME_YES = args.apply, args.yes

    mode = "APPLY (changes will be made)" if APPLY else "DRY RUN (read-only analysis)"
    print(f"=== Mirth Connect OOM agent — mode: {mode} ===\n")

    _load_env_file()  # pick up ANTHROPIC_API_KEY from a local .env if present
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "ERROR: no API key found.\n"
            "  Set the ANTHROPIC_API_KEY environment variable to your key\n"
            "  (get one at https://console.anthropic.com/settings/keys), then re-run:\n"
            '    $env:ANTHROPIC_API_KEY = "sk-ant-..."   # this terminal only\n'
            '    setx ANTHROPIC_API_KEY "sk-ant-..."     # persist (open a new terminal after)',
            file=sys.stderr,
        )
        return 1

    client = anthropic.Anthropic(api_key=api_key)
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=SYSTEM,
        tools=READONLY + MUTATING,
        messages=[{
            "role": "user",
            "content": "Both Mirth processes are down after a Java OutOfMemoryError. "
                       "Diagnose the root cause and recover them.",
        }],
    )

    for message in runner:
        for block in message.content:
            if block.type == "text" and block.text.strip():
                print(block.text)
            elif block.type == "tool_use":
                print(f"  [tool] {block.name}({block.input})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except anthropic.APIError as e:
        print(f"\nAnthropic API error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
