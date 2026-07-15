// Mirth Operations Console — front-end logic
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const isMock = () => $("mockToggle").checked;

// ---------- scenario picker ----------
const scenarioSel = $("scenario");
const symptomBox = $("symptom");
function syncSymptom() {
  const opt = scenarioSel.selectedOptions[0];
  if (scenarioSel.value === "custom") {
    if (symptomBox.dataset.wasScenario !== "false") symptomBox.value = "";
    symptomBox.dataset.wasScenario = "false";
    symptomBox.focus();
  } else {
    symptomBox.value = opt.dataset.symptom || "";
    symptomBox.dataset.wasScenario = "true";
  }
}
scenarioSel.addEventListener("change", syncSymptom);
syncSymptom();

// ---------- env toggle ----------
$("mockToggle").addEventListener("change", () => {
  $("envLabel").textContent = isMock() ? "Mock environment" : "Live Mirth";
  loadSnapshot();
});

// ---------- snapshot / health grid ----------
async function loadSnapshot() {
  try {
    const r = await fetch(`/api/snapshot?mock=${isMock()}`);
    const d = await r.json();
    renderSnapshot(d);
  } catch (e) {
    $("vitals").innerHTML = `<div class="err-box">Could not load environment: ${esc(e.message)}</div>`;
  }
}

function vital(label, value, sub, level, pct) {
  const cls = level ? ` is-${level}` : "";
  const bar = pct != null ? `<div class="bar${level ? " is-" + level : ""}"><span style="width:${Math.min(100, pct)}%"></span></div>` : "";
  return `<div class="vital${cls}"><div class="v-label">${esc(label)}</div><div class="v-value">${esc(value)}</div><div class="v-sub">${esc(sub || "")}</div>${bar}</div>`;
}

function renderSnapshot(d) {
  const e = d.engine || {};
  const src = { mock: "Mock environment", "mock-fallback": "Mock (no live Mirth)", live: "Live Mirth" }[d.source] || d.source;
  $("sourceBadge").textContent = src;

  const vitals = [];
  if (e.heap_used_mb != null && e.heap_max_mb) {
    const pct = Math.round((e.heap_used_mb / e.heap_max_mb) * 100);
    const lvl = pct >= 90 ? "crit" : pct >= 75 ? "warn" : null;
    vitals.push(vital("JVM heap", pct + "%", `${e.heap_used_mb} / ${e.heap_max_mb} MB`, lvl, pct));
  }
  if (e.gc_pause_recent_s != null) {
    const lvl = e.gc_pause_recent_s >= 5 ? "crit" : e.gc_pause_recent_s >= 1 ? "warn" : null;
    vitals.push(vital("Recent GC pause", e.gc_pause_recent_s + "s", "last full GC", lvl));
  }
  if (e.disk_free_gb != null) {
    const lvl = e.disk_free_gb < 5 ? "crit" : e.disk_free_gb < 20 ? "warn" : null;
    vitals.push(vital("Disk free", e.disk_free_gb + " GB", e.disk_total_gb ? `of ${e.disk_total_gb} GB` : "", lvl));
  }
  if (e.db_engine) {
    const derby = /derby/i.test(e.db_engine);
    vitals.push(vital("Database", derby ? "Derby" : esc(e.db_engine), derby ? "not for production" : "backing store", derby ? "warn" : null));
  }
  if (d.message_store && d.message_store.total_messages != null) {
    vitals.push(vital("Message store", Number(d.message_store.total_messages).toLocaleString(), d.message_store.pruner_configured ? "pruner on" : "no pruner", d.message_store.pruner_configured ? null : "warn"));
  }
  if (e.cpu_pct != null) vitals.push(vital("CPU", e.cpu_pct + "%", "host load", e.cpu_pct >= 85 ? "warn" : null, e.cpu_pct));
  $("vitals").innerHTML = vitals.join("") || `<div class="err-box">${esc(e.error || "No vitals available")}</div>`;

  const cls = (d.channels || []).map((c) => {
    const st = (c.state || "?").toUpperCase();
    const kind = st === "STARTED" ? "started" : st === "STOPPED" ? "stopped" : "other";
    const meta = [];
    if (c.error) meta.push(`<span class="err">${c.error} err</span>`);
    if (c.queued) meta.push(`<span class="q">${Number(c.queued).toLocaleString()} queued</span>`);
    if (c.sent != null) meta.push(`${Number(c.sent).toLocaleString()} sent`);
    return `<div class="chan"><span class="dot ${kind}"></span><span class="chan-name">${esc(c.name)}</span><span class="chan-meta">${meta.join("")}</span><span class="chan-state ${kind}">${esc(st)}</span></div>`;
  }).join("");
  $("channels").innerHTML = cls || '<div class="muted small">No channels reported.</div>';

  $("logTail").textContent = (d.log || []).join("\n") || "(no recent log lines)";
}

// ---------- run diagnosis (SSE stream) ----------
let es = null;
$("runBtn").addEventListener("click", runDiagnosis);

function runDiagnosis() {
  const symptom = symptomBox.value.trim();
  if (!symptom) { symptomBox.focus(); return; }
  if (es) es.close();

  const btn = $("runBtn");
  btn.disabled = true;
  btn.querySelector(".run-btn-label").textContent = "Diagnosing…";

  const tl = $("timeline");
  tl.innerHTML = "";
  $("timelineCard").classList.remove("hidden");
  $("timelineCard").classList.add("fade-in");
  const status = $("timelineStatus");
  status.className = "pulse-badge working";
  status.textContent = "working…";
  $("result").innerHTML = "";

  addStep("Connecting to the resolver…", "", false);

  es = new EventSource(`/api/triage?mock=${isMock()}&symptom=${encodeURIComponent(symptom)}`);

  es.addEventListener("start", (ev) => {
    const d = JSON.parse(ev.data);
    tl.innerHTML = "";
    addStep("Agent engaged", `${d.model} · reading the ${d.source === "live" ? "live Mirth" : "environment"}`, false);
  });
  es.addEventListener("thinking", (ev) => {
    const d = JSON.parse(ev.data);
    addStep("Reasoning", d.text, true);
  });
  es.addEventListener("tool", (ev) => {
    const d = JSON.parse(ev.data);
    addStep(d.label || d.name, "gathering signal…", false);
  });
  es.addEventListener("tool_result", (ev) => {
    const d = JSON.parse(ev.data);
    const last = tl.lastElementChild;
    if (last) { const det = last.querySelector(".t-detail"); if (det) det.textContent = summarize(d.name, d.result); }
  });
  es.addEventListener("findings", (ev) => {
    renderFindings(JSON.parse(ev.data));
  });
  es.addEventListener("error", (ev) => {
    let msg = "Connection interrupted.";
    try { if (ev.data) msg = JSON.parse(ev.data).message; } catch {}
    if (ev.data) $("result").innerHTML = `<div class="err-box"><b>Diagnosis failed:</b> ${esc(msg)}</div>`;
    finish();
  });
  es.addEventListener("done", finish);

  function finish() {
    if (es) { es.close(); es = null; }
    status.className = "pulse-badge done";
    status.textContent = "complete";
    btn.disabled = false;
    btn.querySelector(".run-btn-label").textContent = "Run diagnosis";
  }
}

function addStep(title, detail, think) {
  const li = document.createElement("li");
  if (think) li.className = "think";
  li.innerHTML = `<div class="t-title">${esc(title)}</div>${detail ? `<div class="t-detail">${esc(detail)}</div>` : ""}`;
  $("timeline").appendChild(li);
}

function summarize(name, r) {
  try {
    if (name === "get_channel_states" && Array.isArray(r)) {
      const stopped = r.filter((c) => (c.state || "").toUpperCase() !== "STARTED").length;
      return `${r.length} channels · ${stopped} not started`;
    }
    if (name === "get_destination_queues" && Array.isArray(r)) {
      const total = r.reduce((a, c) => a + (c.queued || 0), 0);
      return r.length ? `${r.length} queue(s), ${total.toLocaleString()} messages waiting` : "no backed-up queues";
    }
    if (name === "get_engine_stats") {
      const p = r.heap_max_mb ? Math.round((r.heap_used_mb / r.heap_max_mb) * 100) + "% heap" : "";
      return [p, r.db_engine, r.gc_pause_recent_s ? r.gc_pause_recent_s + "s GC" : ""].filter(Boolean).join(" · ");
    }
    if (name === "tail_mirth_log" && Array.isArray(r)) return `${r.length} log line(s) examined`;
    if (name === "get_message_store_stats") return r.total_messages ? `${Number(r.total_messages).toLocaleString()} messages stored` : (r.note || "checked");
  } catch {}
  return "done";
}

// ---------- render structured findings ----------
function renderFindings(f) {
  const box = $("result");
  if (f._unstructured) {
    box.innerHTML = `<div class="panel fade-in"><h3>Agent response</h3><div style="white-space:pre-wrap;font-size:13.5px;line-height:1.6">${esc(f._unstructured)}</div></div>`;
    return;
  }
  const sev = (f.severity || "medium").toLowerCase();
  const parts = [];

  parts.push(`<div class="diag-banner ${sev} fade-in"><div class="diag-top">
      <span class="sev-pill">${esc(sev.toUpperCase())}</span>
      ${f.is_dominant ? '<span class="dom-pill">▲ Dominant real-incident pattern</span>' : ""}
    </div><div class="diag-summary">${esc(f.summary || "")}</div></div>`);

  if ((f.classification || []).length) {
    parts.push(`<div class="panel fade-in"><h3>Classification</h3>${f.classification.map((c) => {
      const conf = c.confidence != null ? `<span class="conf"><span class="conf-bar"><span style="width:${c.confidence}%"></span></span>${c.confidence}%</span>` : "";
      return `<div class="class-row"><span class="layer-tag">L${esc(c.layer)}</span><span class="class-mode">${esc(c.failure_mode || "")}<br><span class="muted small">${esc(c.layer_name || "")}</span></span>${conf}</div>`;
    }).join("")}</div>`);
  }

  if ((f.hypotheses || []).length) {
    parts.push(`<div class="panel fade-in"><h3>Root-cause hypotheses</h3>${f.hypotheses.map((h) => {
      const lk = (h.likelihood || "").toLowerCase();
      const lkCls = lk.includes("high") ? "high" : lk.includes("low") ? "low" : "medium";
      return `<div class="hyp"><div class="hyp-head"><span class="hyp-cause">${esc(h.cause || "")}</span>${h.likelihood ? `<span class="like ${lkCls}">${esc(h.likelihood)}</span>` : ""}</div>
        ${h.evidence ? `<div class="hyp-line"><b>Evidence:</b> ${esc(h.evidence)}</div>` : ""}
        ${h.confirm ? `<div class="hyp-line"><b>Confirm:</b> ${esc(h.confirm)}</div>` : ""}</div>`;
    }).join("")}</div>`);
  }

  const stepsHtml = (arr, cls) => (arr || []).map((s) =>
    `<li><div class="step-title">${esc(s.step || "")}</div>${s.detail ? `<div class="step-detail">${esc(s.detail)}</div>` : ""}${s.command ? `<div class="step-cmd">${esc(s.command)}</div>` : ""}</li>`
  ).join("");

  const cols = [];
  if ((f.immediate || []).length) cols.push(`<div><h3 style="color:var(--crit)">Immediate — stop the bleeding</h3><ol class="steps immediate">${stepsHtml(f.immediate)}</ol></div>`);
  if ((f.durable || []).length) cols.push(`<div><h3 style="color:var(--ok)">Durable fix</h3><ol class="steps durable">${stepsHtml(f.durable)}</ol></div>`);
  if (cols.length) parts.push(`<div class="panel fade-in"><div class="two-col">${cols.join("")}</div></div>`);

  if ((f.suggested_actions || []).length) {
    parts.push(`<div class="panel actions-panel fade-in"><h3>Suggested safe actions</h3>${f.suggested_actions.map((a) =>
      `<div class="action-row"><span class="action-verb">${esc(a.action)}(${esc(a.target)})</span><span class="action-why">${esc(a.why || "")}</span></div>`
    ).join("")}<div class="actions-note">Whitelisted, reversible operations only — the agent asks before executing each one.</div></div>`);
  }

  if ((f.monitoring || []).length) {
    parts.push(`<div class="panel fade-in"><h3>Prevention · monitoring to add</h3><ul class="mon-list">${f.monitoring.map((m) => `<li>${esc(m)}</li>`).join("")}</ul></div>`);
  }

  box.innerHTML = parts.join("");
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// boot
loadSnapshot();
