# Stakeholder Demo Script — Agentic Mirth Connect Resolver

**Audience:** senior / management stakeholders
**Duration:** ~12–15 min live (or a 6–8 min recorded tutorial)
**What you are proving:** an AI agent can (1) *diagnose* a Mirth Connect incident the way a senior SRE would, in a clean console, and (2) *actually recover* a down engine — safely, with a human in the loop.

> One line for the room: *"This turns a 2 a.m. outage that used to take an engineer an hour of log-diving into a five-minute, guided recovery — and it never touches production without asking."*

---

## 0. The two things you will show

| # | Capability | Tool | The "wow" |
|---|------------|------|-----------|
| A | **Diagnose** — investigate a live incident and produce a ranked, grounded runbook | `mirth_console.py` (web UI) | Watch the agent gather evidence in real time, then render an executive-grade diagnosis |
| B | **Fix** — recover a crashed engine end-to-end | `mirth_oom_agent.py` | It reads the real crash, right-sizes the heap, rewrites the config, and restarts the service — asking permission at each step |

Part A tells the story in a polished UI. Part B proves it does real work on the real machine.

---

## 1. Before you present (prep — do this once, ~5 min)

Open a normal PowerShell in the project folder and run the preflight:

```powershell
run_demo.bat check
```

You want **all green**. Then, so the "before" state is dramatic and honest, make sure the machine is in the broken state (undersized heap, service down). If you (or a rehearsal) already fixed it, reset it:

```powershell
run_demo.bat reset        # restores -Xmx256m and stops the service (asks for admin)
```

**Rehearse the fix once** the day before (see Part B) so you know the service comes back up cleanly on your box, then `reset` again for the real thing.

**Have ready on screen:**
- One PowerShell window in the project folder (normal, not admin).
- Browser closed (the script opens it for you).
- Optional: a single slide titled *"Before: 2 a.m. pager. After: guided recovery."*

**Honesty note (keep the room's trust):** the console's default **Mock environment** is a *representative* production incident (rich, reliable, safe to show). Say so plainly — "this is a representative incident so the demo is repeatable." Part B, by contrast, runs against the **real Mirth install on this machine** — that's where they see it do real work.

---

## 2. Open (30–45 sec) — frame the pain

> "Mirth Connect is the integration engine moving HL7 and lab messages between our systems. When it has a bad night — a memory blow-up, a queue backing up, a channel silently stopping — someone gets paged, opens a dozen log files, and pattern-matches from memory. It's slow, it's error-prone, and the knowledge lives in a few people's heads.
>
> We built an AI agent that does that first hour of work — the investigation and the recovery plan — and, when we let it, the recovery itself. Let me show you."

---

## 3. Part A — The AI Operations Console (5–6 min)

### Launch it
```powershell
run_demo.bat
```
This prints the current (broken) server state, starts the console, and opens **http://localhost:8000**.

### Scene A1 — the dashboard (45 sec)
Let the page load. Talk over it, left to right:

> "This is the operations console. On the left, live environment health — heap, garbage-collection pauses, disk, the database, channel states. Notice it's already flagging problems in red and amber: heap at 95%, a 42-second GC pause, and it's calling out that this instance is running on **Derby**, which isn't meant for production. On the right is the failure taxonomy the agent reasons against — eleven layers, from the JVM all the way up to monitoring blind spots. This isn't generic AI advice; every conclusion is grounded in this reference."

Point to the **stopped channel in red** ("Lab Results Inbound") and the **47,000-message queue** in amber.

### Scene A2 — run a diagnosis (2–3 min) — the centerpiece
In the center, the scenario is preselected (*"Engine frozen — OOM at 02:10"*). Click **Run diagnosis**.

> "I'm giving it a one-line symptom, exactly what an on-call engineer would get from an alert. Now watch — it's investigating."

Narrate the **live investigation timeline** as steps appear:

> "It's deciding what evidence it needs and pulling it: channel states… the destination queues… the JVM vitals… it's tailing the actual log for the out-of-memory error… and sizing the message store. This is the agent doing the log-diving for us, in seconds."

When the **diagnosis renders**, walk the panels top to bottom:

> "Here's the result. **Critical**, and it's flagged as a *dominant* real-incident pattern — one of the failure modes we see most. It classifies the incident across multiple layers with confidence scores. Then ranked **root-cause hypotheses**, each with the evidence it found and how to confirm it. Then the runbook, split into **stop-the-bleeding** steps and the **durable fix** — with the actual commands. And finally the **monitoring to add** so this can't recur silently. That's a senior engineer's write-up, produced in under a minute."

**If a stakeholder asks "is it just making this up?"** — scroll the hypotheses and point at the evidence lines: "Every claim cites the signal it pulled — 9.1 million unpruned messages, the 47k queue, the timeout to the EHR endpoint. It's reasoning from data, not guessing."

### Scene A3 — optional flourish (30 sec)
Pick a different scenario from the dropdown (e.g. *"Lab channel stopped overnight"*) and run it, to show it adapts:

> "Different symptom, different investigation path, different runbook — same rigor."

---

## 4. Part B — Watch it actually fix a crash (4–5 min)

Transition:

> "Diagnosis is half the value. The other half is doing something about it. This next part runs against the **real Mirth Connect installed on this machine**, which is currently down."

### Scene B1 — show it's really broken (30 sec)
The `run_demo.bat` output already showed it, or re-show:
```powershell
run_demo.bat check
```
> "The server's max heap is set to **256 megabytes** on a **16-gigabyte** machine — far too small. The log has **over 160 out-of-memory errors**, and the Windows service is **stopped**. This is a real, down engine."

### Scene B2 — run the recovery agent (2–3 min)
```powershell
run_demo.bat fix
```
A blue **UAC prompt** appears — approve it.

> "It needs administrator rights because it's going to edit a protected config file and restart a Windows service — so it asks the operating system for permission, out in the open."

An elevated window opens and the agent runs. Narrate its steps:

> "It confirms both processes are down… confirms the out-of-memory error is the real root cause from the logs… reads the current heap — there's the 256 megabytes… checks the machine has 16 gigs of RAM… and computes a safe new heap: about **4 gigabytes** for the server, with heap-dump-on-OOM and fail-fast flags added so a future incident is diagnosable."

**The trust moment** — when it prompts `Proceed? [y/N]`:

> "Here's the important part. Before it changes anything, it stops and asks. It's not a runaway script — a human approves each action."

Type **`y`** to approve the heap edit, then **`y`** again to approve the service restart.

> "It backed up the original config with a timestamp first, rewrote the heap settings, and restarted the service."

### Scene B3 — show it's fixed (30 sec)
The script prints the **AFTER** state automatically:

> "And there it is — max heap now **4 gigabytes**, service **Running**. The engine is back. From a down system to a recovered one, with the agent doing the work and a human staying in control the whole time."

Close with the incident report the agent printed:

> "It also hands back a plain-language incident report — root cause, exactly what changed old-to-new, current status, and a prevention tip — ready to paste into the ticket."

---

## 5. The close (30–45 sec) — business value

> "So: faster recovery — minutes, not an hour of log-diving. The senior-engineer playbook is captured in software instead of in a few people's heads. It's safe by design — read-only until a human says go, every change backed up, nothing destructive automated. And it's grounded in a real failure taxonomy, so the advice is specific to Mirth, not generic.
>
> The natural next steps are wiring it to our alerting so it triages automatically, and pointing the console at live environments for a real-time operations view."

---

## 6. Q&A — likely questions and crisp answers

- **"Could it break production on its own?"** No. It's read-only by default; the only actions it can take are whitelisted (edit heap, restart), it backs up before editing, and it asks for confirmation at each step. Destructive changes (DB migration, pruning) are recommend-only — a human does those.
- **"Is it hallucinating fixes?"** Every hypothesis cites the evidence it pulled, and its recommendations are constrained to a documented 11-layer Mirth failure taxonomy that's injected into the model.
- **"What does it cost to run?"** A triage is a handful of API calls to Claude — cents per incident — against an hour of senior-engineer time.
- **"What if it's a problem it hasn't seen?"** It still investigates with the same tools and reports what it found; for anything outside the safe action set it produces a runbook for a human rather than acting.
- **"Does it need our data to leave the building?"** It sends the specific signals it gathers (log excerpts, stats) to the model to reason over. Scope that to your data-governance rules; the tool controls exactly which signals are collected.
- **"How hard to point at our real systems?"** The console already speaks Mirth's REST API; set the URL and credentials and flip it from Mock to Live. The recovery agent already targets this machine's real install.

---

## 7. Video tutorial — storyboard & how to record it

I can't produce a video file for you, but here is a shot-by-shot storyboard you (or anyone) can screen-record in one take in ~15 minutes. Target length **6–8 minutes**.

**Record with** any of: **Windows Game Bar** (`Win`+`G` → record — built in, zero setup), **Microsoft Clipchamp** (also built in, lets you trim + add captions), or **OBS Studio** (free, best quality). Record at 1080p, capture system audio + mic.

**Setup before you hit record:** run `run_demo.bat reset` so you start broken; close extra windows; set display scaling so text is readable; have this script on a second monitor or phone.

| # | Shot | On screen | Say (voiceover) | ~Time |
|---|------|-----------|-----------------|-------|
| 1 | Title | A slide: *Agentic Mirth Resolver* | The pain: 2 a.m. outage, manual log-diving (Section 2 script). | 0:30 |
| 2 | Launch | Terminal: `run_demo.bat` | "One command brings up the console." | 0:20 |
| 3 | Dashboard tour | Browser at localhost:8000 | Scene A1 narration — vitals, red/amber flags, taxonomy. | 0:45 |
| 4 | Run diagnosis | Click **Run diagnosis**; timeline animates | Scene A2 — narrate the live investigation. | 1:30 |
| 5 | Read the result | Scroll the diagnosis panels slowly | Scene A2 — severity, hypotheses+evidence, runbook, monitoring. | 1:15 |
| 6 | Transition | Terminal | "Now watch it fix a real down engine." | 0:15 |
| 7 | Show broken | `run_demo.bat check` | Scene B1 — 256m heap, 160+ OOMs, service stopped. | 0:30 |
| 8 | Run the fix | `run_demo.bat fix`, approve UAC | Scene B2 — narrate investigate → recommend → **the y/N approval moment**. | 1:30 |
| 9 | Show fixed | AFTER block: heap 4g, service Running | Scene B3 — "down to recovered, human in control." | 0:30 |
| 10 | Close | Value slide | Section 5 close. | 0:40 |

**Editing tips:** cut dead air while the agent thinks (or speed those clips 1.5–2x with a "investigating…" caption); zoom/crop to the diagnosis panel and the `[y/N]` prompt so they're legible; add lower-third captions for the key numbers (256m → 4g, service Running). Export 1080p MP4.

---

## 8. Reset & troubleshooting

| Situation | Do this |
|-----------|---------|
| Re-run the whole demo from scratch | `run_demo.bat reset` then `run_demo.bat` |
| Console won't load | check `%TEMP%\mirth_console.err`; confirm `run_demo.bat check` is all green |
| Stop the console after the demo | `run_demo.bat stop` |
| Service won't restart after fix | it's a real service — check the Mirth log; the original config is backed up as `mcservice.vmoptions.bak-<timestamp>` next to the original |
| No API key error | ensure `ANTHROPIC_API_KEY=` is set in `.env` (preflight checks this) |

**Golden rule for the live room:** rehearse the `fix` once beforehand so you know the service comes back cleanly on this machine, then `reset`. Never show a path you haven't walked.
