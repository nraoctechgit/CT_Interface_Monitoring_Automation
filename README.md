# Mirth Agent — agentic incident resolution for Mirth Connect

A runnable Python tool that uses the Claude API (with tool-use) to **investigate**
a live Mirth Connect / NextGen Connect instance, **diagnose** the incident against
an 11-layer failure taxonomy, and produce a **remediation runbook** — optionally
applying a small set of *safe* operational actions with your confirmation.

It is the executable counterpart to the failure-modes reference: instead of you
reading the table and mapping symptoms to mitigations by hand, the agent pulls the
real signals and does that mapping, grounded in the same taxonomy.

## Install

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your values
set -a && source .env && set +a
```

You need an `ANTHROPIC_API_KEY` and a `CLAUDE_MODEL` you have access to. To connect
to a real engine, set `MIRTH_URL` / `MIRTH_USER` / `MIRTH_PASS` (and optionally
`MIRTH_LOG_PATH`). No Mirth server? Use `--mock`.

## Use

```bash
# Diagnose a symptom. The agent auto-collects channel states, queues, engine
# stats and logs, then prints a diagnosis + runbook. Read-only by default.
python mirth_agent.py triage "engine frozen, OOM in the log around 02:10"

# Try it with zero setup against built-in realistic fake data:
python mirth_agent.py triage "a destination queue keeps climbing" --mock

# Include a log file (or pipe one in):
python mirth_agent.py triage "TLS errors on the HTTPS listener" --logs mirth.log
kubectl logs mc-0 | python mirth_agent.py triage "partner can't connect" --logs -

# Continuously watch; auto-triage when a channel isn't STARTED or a queue crosses
# the threshold. Only triages each new anomaly once.
python mirth_agent.py watch --interval 60 --queue-threshold 1000

# Let the agent apply whitelisted safe actions — it asks before each one:
python mirth_agent.py triage "lab channel stopped overnight" --apply
```

## How the agent works

Each `triage` runs a tool-use loop against Claude:

1. **Investigate.** The model decides which signals it needs and calls the
   read-only tools — `get_channel_states`, `get_destination_queues`,
   `get_engine_stats`, `tail_mirth_log`, `get_message_store_stats`. Results are fed
   back in until it has enough evidence.
2. **Report.** It calls `report_findings` once with a structured verdict:
   severity, taxonomy classification, ranked root-cause hypotheses (each with the
   evidence and the fastest confirming check), an immediate + durable runbook, and
   monitoring to add so the failure can't recur silently.

The full 11-layer taxonomy and the five dominant-failure patterns are injected into
the system prompt, so recommendations track the reference document's mitigations
rather than generic advice.

## Safety model

- **Read-only by default.** Without `--apply` the tool never changes anything.
- **Small, safe action whitelist.** Only `start_channel` and `redeploy_channel`
  can be executed, and only after you type `y` for each. These are operational, not
  destructive.
- **Everything else is recommend-only.** Heap changes, DB migration off Derby,
  pruning/retention, cert renewal, config edits — the agent puts these in the
  runbook for a human. It will not run them.
- Treat the output as guidance. Verify against your version, database, connector
  mix, and change process before acting in production.

## Adapting to your environment

- Mirth REST JSON shapes vary slightly by version; parsing in `MirthRestCollector`
  is defensive but check `get_channel_states` / `get_engine_stats` against your
  build (`/api/channels/statuses`, `/api/system/stats`, `/api/system/info`).
- Add signals by writing a new method on `Collector`, a matching entry in `TOOLS`,
  and a line in `READONLY_TOOLS`. The agent will start using it automatically.
- Point `watch` at your alerting by replacing the print calls with a webhook.
