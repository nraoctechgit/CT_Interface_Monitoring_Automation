#!/usr/bin/env python3
"""
Unit tests for mirth_agent_remediation.py.

Everything here runs OFFLINE — no Anthropic API key, no Mirth server, no extra
packages. The LLM Agent is exercised through a fake client, so even the tool-use
loop is covered without a network call.

    python -m unittest test_mirth_agent_remediation -v
    python test_mirth_agent_remediation.py
"""
from __future__ import annotations

import io
import os
import sys
import json
import types
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mirth_agent_remediation as mar  # noqa: E402


# ----------------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------------
class HealthyCollector(mar.Collector):
    """A nominal environment — nothing wrong. Used to prove the analyzer does
    NOT cry wolf when signals are clean."""

    def channel_states(self):
        return [
            {"name": "ADT Inbound", "state": "STARTED", "received": 100, "sent": 100, "error": 0, "queued": 0},
            {"name": "Orders Outbound", "state": "STARTED", "received": 50, "sent": 50, "error": 0, "queued": 0},
        ]

    def destination_queues(self):
        return []

    def engine_stats(self):
        return {"heap_used_mb": 1000, "heap_max_mb": 4096, "gc_pause_recent_s": 0.2,
                "disk_free_gb": 150.0, "disk_total_gb": 200.0, "db_engine": "PostgreSQL",
                "cpu_pct": 20, "open_file_descriptors": 100, "fd_limit": 8192}

    def tail_log(self, lines=80, contains=""):
        log = ["10:00:00 INFO  All channels started", "10:01:00 INFO  Heartbeat ok"]
        if contains:
            log = [l for l in log if contains.lower() in l.lower()]
        return log[-lines:]

    def message_store_stats(self):
        return {"total_messages": 5000, "oldest_message_days": 5, "pruner_configured": True}

    def start_channel(self, channel):
        return f"started {channel}"

    def redeploy_channel(self, channel):
        return f"redeployed {channel}"


class ExplodingCollector(mar.Collector):
    """Every sense raises — proves the analyzer degrades gracefully."""

    def channel_states(self):
        raise RuntimeError("boom channels")

    def destination_queues(self):
        raise RuntimeError("boom queues")

    def engine_stats(self):
        raise RuntimeError("boom engine")

    def tail_log(self, lines=80, contains=""):
        raise RuntimeError("boom log")

    def message_store_stats(self):
        raise RuntimeError("boom store")

    def start_channel(self, channel):
        raise RuntimeError("no")

    def redeploy_channel(self, channel):
        raise RuntimeError("no")


def _fake_resp(content, stop_reason):
    return types.SimpleNamespace(content=content, stop_reason=stop_reason)


def _text_block(text):
    return types.SimpleNamespace(type="text", text=text)


def _tool_block(name, inp, block_id="tb"):
    return types.SimpleNamespace(type="tool_use", name=name, input=inp, id=block_id)


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _bare_agent(collector, responses):
    """Build an Agent WITHOUT running __post_init__ (so no anthropic import /
    API key needed), injecting a fake client."""
    agent = mar.Agent.__new__(mar.Agent)
    agent.collector = collector
    agent.model = "fake-model"
    agent.verbose = False
    agent._client = _FakeClient(responses)
    return agent


# ----------------------------------------------------------------------------
# Helpers: _pct, _num, _kb_text
# ----------------------------------------------------------------------------
class TestHelpers(unittest.TestCase):
    def test_pct_normal(self):
        self.assertEqual(mar._pct(50, 100), 50)
        self.assertEqual(mar._pct(1, 3), 33)

    def test_pct_guards(self):
        self.assertIsNone(mar._pct(None, 100))
        self.assertIsNone(mar._pct(5, None))
        self.assertIsNone(mar._pct(5, 0))          # zero total -> None, no ZeroDivision
        self.assertIsNone(mar._pct("x", 100))

    def test_num_formats_thousands(self):
        self.assertEqual(mar._num(9120444), "9,120,444")
        self.assertEqual(mar._num(0), "0")

    def test_num_non_numeric_passthrough(self):
        self.assertEqual(mar._num("abc"), "abc")
        self.assertEqual(mar._num(None), "None")

    def test_kb_text_has_taxonomy_and_layers(self):
        kb = mar._kb_text()
        self.assertIn("FAILURE TAXONOMY", kb)
        self.assertIn("Layer 1", kb)
        self.assertIn("DOMINANT REAL-INCIDENT FAILURES", kb)


# ----------------------------------------------------------------------------
# MockCollector
# ----------------------------------------------------------------------------
class TestMockCollector(unittest.TestCase):
    def setUp(self):
        self.c = mar.MockCollector()

    def test_channel_states_shape(self):
        chans = self.c.channel_states()
        self.assertTrue(any(c["state"] == "STOPPED" for c in chans))
        for c in chans:
            self.assertEqual(set(c) >= {"name", "state", "received", "sent", "error", "queued"}, True)

    def test_destination_queue_has_head_error(self):
        q = self.c.destination_queues()
        self.assertEqual(len(q), 1)
        self.assertIn("timed out", q[0]["head_error"].lower())

    def test_engine_stats_near_limits(self):
        e = self.c.engine_stats()
        self.assertGreater(e["heap_used_mb"] / e["heap_max_mb"], 0.9)
        self.assertIn("derby", e["db_engine"].lower())

    def test_tail_log_filter(self):
        only_oom = self.c.tail_log(contains="OutOfMemory")
        self.assertTrue(all("outofmemory" in l.lower() for l in only_oom))
        self.assertTrue(len(only_oom) >= 1)

    def test_tail_log_line_limit(self):
        self.assertEqual(len(self.c.tail_log(lines=2)), 2)

    def test_message_store_no_pruner(self):
        s = self.c.message_store_stats()
        self.assertFalse(s["pruner_configured"])

    def test_action_stubs_are_strings(self):
        self.assertIn("_start", self.c.start_channel("X"))
        self.assertIn("_deploy", self.c.redeploy_channel("X"))


# ----------------------------------------------------------------------------
# MirthRestCollector._as_list (pure, defensive parsing)
# ----------------------------------------------------------------------------
class TestAsList(unittest.TestCase):
    f = staticmethod(mar.MirthRestCollector._as_list)

    def test_bare_list(self):
        self.assertEqual(self.f([1, 2], "list"), [1, 2])

    def test_dict_key_to_list(self):
        node = {"list": {"channelStatus": [{"a": 1}, {"b": 2}]}}
        self.assertEqual(self.f(node, "list"), [{"a": 1}, {"b": 2}])

    def test_dict_key_to_single_dict(self):
        node = {"list": {"channelStatus": {"a": 1}}}
        self.assertEqual(self.f(node, "list"), [{"a": 1}])

    def test_key_maps_directly_to_list(self):
        node = {"list": [{"a": 1}]}
        self.assertEqual(self.f(node, "list"), [{"a": 1}])

    def test_empty_dict(self):
        self.assertEqual(self.f({}, "list"), [])

    def test_scalar_returns_empty(self):
        self.assertEqual(self.f("nope", "list"), [])
        self.assertEqual(self.f(7, "list"), [])


# ----------------------------------------------------------------------------
# LocalAnalyzer — the rule engine
# ----------------------------------------------------------------------------
class TestLocalAnalyzerMock(unittest.TestCase):
    """Against the seeded MockCollector this must produce the canonical critical
    multi-layer diagnosis."""

    @classmethod
    def setUpClass(cls):
        cls.f = mar.LocalAnalyzer(mar.MockCollector(), verbose=False).triage("engine frozen, OOM")

    def test_severity_and_dominant(self):
        self.assertEqual(self.f["severity"], "critical")
        self.assertTrue(self.f["is_dominant"])

    def test_confidence_capped(self):
        self.assertEqual(self.f["confidence"], 95)

    def test_signals_include_critical(self):
        assessments = {s["assessment"] for s in self.f["signals"]}
        self.assertIn("critical", assessments)
        metrics = {s["metric"] for s in self.f["signals"]}
        self.assertIn("Heap", metrics)
        self.assertIn("Backing DB", metrics)

    def test_causal_chain_reconstructed(self):
        chain = self.f["causal_chain"]
        self.assertGreaterEqual(len(chain), 4)
        self.assertTrue(any("OutOfMemoryError" in link for link in chain))

    def test_classification_spans_expected_layers(self):
        layers = {c["layer"] for c in self.f["classification"]}
        self.assertTrue({1, 2, 3, 5, 7}.issubset(layers))

    def test_suggested_action_for_stopped_channel(self):
        acts = self.f["suggested_actions"]
        self.assertTrue(any(a["action"] == "start_channel" and a["target"] == "Lab Results Inbound" for a in acts))

    def test_hypotheses_and_ruled_out_present(self):
        self.assertGreaterEqual(len(self.f["hypotheses"]), 3)
        self.assertTrue(len(self.f["ruled_out"]) >= 1)

    def test_immediate_and_durable_have_verify(self):
        for step in self.f["immediate"] + self.f["durable"]:
            self.assertIn("verify", step)

    def test_monitoring_deduped(self):
        mon = self.f["monitoring"]
        self.assertEqual(len(mon), len(set(mon)))

    def test_required_keys_present(self):
        for key in ("severity", "confidence", "signals", "causal_chain",
                    "classification", "hypotheses", "immediate", "durable", "summary"):
            self.assertIn(key, self.f)


class TestLocalAnalyzerHealthy(unittest.TestCase):
    """Clean signals must NOT be escalated."""

    @classmethod
    def setUpClass(cls):
        cls.f = mar.LocalAnalyzer(HealthyCollector(), verbose=False).triage("routine check")

    def test_low_severity(self):
        self.assertEqual(self.f["severity"], "low")

    def test_not_dominant(self):
        self.assertFalse(self.f["is_dominant"])

    def test_no_actions_no_chain(self):
        self.assertEqual(self.f["suggested_actions"], [])
        self.assertEqual(self.f["causal_chain"], [])

    def test_no_remediation_steps(self):
        self.assertEqual(self.f["immediate"], [])
        self.assertEqual(self.f["durable"], [])

    def test_all_signals_nominal(self):
        self.assertTrue(all(s["assessment"] == "nominal" for s in self.f["signals"]))

    def test_fallback_classification(self):
        self.assertEqual(len(self.f["classification"]), 1)
        self.assertEqual(self.f["classification"][0]["layer"], 11)

    def test_base_confidence(self):
        self.assertEqual(self.f["confidence"], 55)


class TestLocalAnalyzerRobustness(unittest.TestCase):
    def test_all_collectors_raise_no_crash(self):
        f = mar.LocalAnalyzer(ExplodingCollector(), verbose=False).triage("???")
        # No signals gathered, but it still returns a well-formed report.
        self.assertIn("severity", f)
        self.assertEqual(f["signals"], [])
        self.assertEqual(f["causal_chain"], [])
        self.assertEqual(f["classification"][0]["layer"], 11)


class TestLocalAnalyzerThresholds(unittest.TestCase):
    """Drive individual detectors with tailored engine stats."""

    def _analyze(self, engine=None, channels=None, queues=None, store=None, log=None):
        col = HealthyCollector()
        if engine is not None:
            col.engine_stats = lambda: engine
        if channels is not None:
            col.channel_states = lambda: channels
        if queues is not None:
            col.destination_queues = lambda: queues
        if store is not None:
            col.message_store_stats = lambda: store
        if log is not None:
            col.tail_log = lambda lines=80, contains="": log
        return mar.LocalAnalyzer(col, verbose=False).triage("t")

    def _sig(self, findings, metric):
        return next((s for s in findings["signals"] if s["metric"] == metric), None)

    def test_heap_warning_band(self):
        f = self._analyze(engine={"heap_used_mb": 3200, "heap_max_mb": 4096})
        self.assertEqual(self._sig(f, "Heap")["assessment"], "warning")  # 78%

    def test_heap_critical_band(self):
        f = self._analyze(engine={"heap_used_mb": 3900, "heap_max_mb": 4096})
        self.assertEqual(self._sig(f, "Heap")["assessment"], "critical")  # 95%

    def test_disk_critical(self):
        f = self._analyze(engine={"disk_free_gb": 2.0, "disk_total_gb": 200.0})
        self.assertEqual(self._sig(f, "Disk free")["assessment"], "critical")

    def test_fd_critical(self):
        f = self._analyze(engine={"open_file_descriptors": 8100, "fd_limit": 8192})
        self.assertEqual(self._sig(f, "File descriptors")["assessment"], "critical")

    def test_gc_critical(self):
        f = self._analyze(engine={"gc_pause_recent_s": 30.0})
        self.assertEqual(self._sig(f, "Recent GC pause")["assessment"], "critical")

    def test_derby_flagged_dominant(self):
        f = self._analyze(engine={"db_engine": "Derby (embedded)"})
        self.assertTrue(f["is_dominant"])
        self.assertTrue(any(c["layer"] == 2 for c in f["classification"]))

    def test_missing_engine_values_no_signal(self):
        f = self._analyze(engine={})   # nothing to read
        self.assertIsNone(self._sig(f, "Heap"))
        self.assertIsNone(self._sig(f, "Disk free"))

    def test_stopped_channel_detected(self):
        chans = [{"name": "Lab", "state": "STOPPED", "received": 1, "sent": 1, "error": 0, "queued": 0}]
        f = self._analyze(channels=chans)
        self.assertTrue(any(a["target"] == "Lab" for a in f["suggested_actions"]))
        self.assertTrue(any(c["layer"] == 3 for c in f["classification"]))

    def test_queue_warning_vs_critical(self):
        warn = self._analyze(channels=[{"name": "Q", "state": "STARTED", "queued": 2000, "error": 0}])
        crit = self._analyze(channels=[{"name": "Q", "state": "STARTED", "queued": 50000, "error": 0}])
        self.assertEqual(self._sig(warn, "Largest queue")["assessment"], "warning")
        self.assertEqual(self._sig(crit, "Largest queue")["assessment"], "critical")

    def test_endpoint_down_adds_retry_storm(self):
        f = self._analyze(
            channels=[{"name": "D", "state": "STARTED", "queued": 500, "error": 5}],
            queues=[{"channel": "D", "head_error": "Connection refused: host:1234"}],
            log=["ERROR retry attempt 42"],
        )
        layers = {c["layer"] for c in f["classification"]}
        self.assertIn(5, layers)
        self.assertIn(7, layers)   # retry storm because 'retry'/'attempt' in log


# ----------------------------------------------------------------------------
# RetrievalStore — RAG layer
# ----------------------------------------------------------------------------
class TestRetrievalStore(unittest.TestCase):
    def setUp(self):
        mar.RAG_EMBED_MODEL = ""  # force the pure-Python embedder
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        self.tmp.close()
        os.unlink(self.tmp.name)  # start with no file; store should cope
        self.path = self.tmp.name
        self.findings = mar.LocalAnalyzer(mar.MockCollector(), verbose=False).triage("oom")

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_default_mode_no_deps(self):
        s = mar.RetrievalStore(path=self.path)
        self.assertEqual(s.mode, "hashed-bow (no deps)")
        self.assertEqual(s._records, [])

    def test_hashed_bow_normalized_and_deterministic(self):
        s = mar.RetrievalStore(path=self.path)
        v1 = s._hashed_bow("out of memory heap derby queue")
        v2 = s._hashed_bow("out of memory heap derby queue")
        self.assertEqual(len(v1), s.dim)
        self.assertEqual(v1, v2)
        norm = sum(x * x for x in v1) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_cosine_self_is_one(self):
        s = mar.RetrievalStore(path=self.path)
        v = s._hashed_bow("heap oom queue")
        self.assertAlmostEqual(s._cosine(v, v), 1.0, places=6)

    def test_cosine_disjoint_is_zero(self):
        s = mar.RetrievalStore(path=self.path)
        a = s._hashed_bow("alpha bravo charlie")
        b = s._hashed_bow("xylophone yankee zulu")
        self.assertLess(s._cosine(a, b), 0.2)

    def test_case_text_includes_symptom_and_layers(self):
        s = mar.RetrievalStore(path=self.path)
        txt = s._case_text("queue climbing", self.findings)
        self.assertIn("queue climbing", txt)
        self.assertIn("JVM", txt)

    def test_add_case_persists_and_loads(self):
        s = mar.RetrievalStore(path=self.path)
        s.add_case("oom incident", self.findings, root_cause="derby+queue", fix="pruned")
        self.assertEqual(len(s._records), 1)
        self.assertTrue(os.path.exists(self.path))
        # a fresh store reads it back
        s2 = mar.RetrievalStore(path=self.path)
        self.assertEqual(len(s2._records), 1)
        self.assertEqual(s2._records[0]["root_cause"], "derby+queue")

    def test_query_empty_store(self):
        s = mar.RetrievalStore(path=self.path)
        self.assertEqual(s.query("anything", self.findings), [])

    def test_query_recalls_similar(self):
        s = mar.RetrievalStore(path=self.path)
        s.add_case("oom incident", self.findings, root_cause="rc", fix="fx")
        hits = s.query("oom incident", self.findings, k=3)
        self.assertEqual(len(hits), 1)
        self.assertGreater(hits[0]["similarity"], 0.9)
        self.assertEqual(hits[0]["root_cause"], "rc")

    def test_augment_attaches_and_nudges_confidence(self):
        s = mar.RetrievalStore(path=self.path)
        s.add_case("oom incident", self.findings, root_cause="rc", fix="fx")
        before = dict(self.findings)
        out = s.augment("oom incident", self.findings)
        self.assertIn("similar_cases", out)
        self.assertEqual(out["confidence"], min(98, before["confidence"] + 3))
        # original dict not mutated
        self.assertNotIn("similar_cases", before)

    def test_augment_below_threshold_no_cases(self):
        s = mar.RetrievalStore(path=self.path)
        # genuinely dissimilar stored case vs. query (different words AND findings)
        stored = {"severity": "low", "confidence": 50,
                  "signals": [{"metric": "Alpha", "value": "1", "assessment": "nominal"}],
                  "classification": [{"layer": 9, "layer_name": "Security", "failure_mode": "cert expiry"}]}
        s.add_case("aaa bbb ccc ddd eee", stored, root_cause="rc")
        query = {"severity": "low", "confidence": 50,
                 "signals": [{"metric": "Zulu", "value": "9", "assessment": "nominal"}],
                 "classification": [{"layer": 1, "layer_name": "JVM host", "failure_mode": "heap oom"}]}
        out = s.augment("xxx yyy zzz www vvv", query)
        self.assertNotIn("similar_cases", out)

    def test_augment_empty_store_returns_same(self):
        s = mar.RetrievalStore(path=self.path)
        out = s.augment("x", self.findings)
        self.assertIs(out, self.findings)

    def test_mode_mismatch_reembeds(self):
        s = mar.RetrievalStore(path=self.path)
        s.add_case("oom incident", self.findings, root_cause="rc")
        # simulate a record written under a different embedder + junk vector
        s._records[0]["embed_mode"] = "stale-model"
        s._records[0]["vector"] = [0.0] * 4
        hits = s.query("oom incident", self.findings)
        self.assertEqual(len(hits), 1)
        self.assertGreater(hits[0]["similarity"], 0.9)   # recomputed from text


# ----------------------------------------------------------------------------
# render()
# ----------------------------------------------------------------------------
class TestRender(unittest.TestCase):
    def _render(self, findings):
        buf = io.StringIO()
        with redirect_stdout(buf):
            mar.render(findings)
        return buf.getvalue()

    def test_full_findings_sections(self):
        f = mar.LocalAnalyzer(mar.MockCollector(), verbose=False).triage("oom")
        out = self._render(f)
        for section in ("DIAGNOSIS", "Key signals", "Causal chain", "Classification",
                        "Root-cause hypotheses", "Immediate", "Durable fix"):
            self.assertIn(section, out)

    def test_similar_cases_block(self):
        f = mar.LocalAnalyzer(mar.MockCollector(), verbose=False).triage("oom")
        f["similar_cases"] = [{"similarity": 0.91, "when": "2026-01-01", "symptom": "old",
                               "root_cause": "rc", "fix": "fx"}]
        out = self._render(f)
        self.assertIn("Similar past incidents (RAG)", out)
        self.assertIn("old", out)

    def test_unstructured_passthrough(self):
        out = self._render({"_unstructured": "just a message"})
        self.assertIn("just a message", out)

    def test_minimal_findings_no_crash(self):
        out = self._render({"severity": "low", "summary": "nothing to see"})
        self.assertIn("nothing to see", out)


# ----------------------------------------------------------------------------
# maybe_apply()
# ----------------------------------------------------------------------------
class TestMaybeApply(unittest.TestCase):
    def _run(self, findings, apply, collector=None):
        buf = io.StringIO()
        with redirect_stdout(buf):
            mar.maybe_apply(collector or mar.MockCollector(), findings, apply)
        return buf.getvalue()

    def test_no_actions_silent(self):
        self.assertEqual(self._run({}, False).strip(), "")

    def test_lists_actions_without_apply(self):
        f = {"suggested_actions": [{"action": "start_channel", "target": "Lab", "why": "stopped"}]}
        out = self._run(f, False)
        self.assertIn("Suggested safe actions", out)
        self.assertIn("--apply", out)

    def test_apply_yes_executes(self):
        f = {"suggested_actions": [{"action": "start_channel", "target": "Lab", "why": "stopped"}]}
        with mock.patch("builtins.input", return_value="y"):
            out = self._run(f, True)
        self.assertIn("_start", out)   # MockCollector.start_channel ran

    def test_apply_no_skips(self):
        f = {"suggested_actions": [{"action": "start_channel", "target": "Lab", "why": "stopped"}]}
        with mock.patch("builtins.input", return_value="n"):
            out = self._run(f, True)
        self.assertIn("skipped", out)


# ----------------------------------------------------------------------------
# CLI wiring: build_collector / build_engine / maybe_augment
# ----------------------------------------------------------------------------
class TestWiring(unittest.TestCase):
    def test_build_collector_mock(self):
        self.assertIsInstance(mar.build_collector(True), mar.MockCollector)

    def test_build_engine_local(self):
        args = types.SimpleNamespace(local=True, model="m")
        self.assertIsInstance(mar.build_engine(mar.MockCollector(), args), mar.LocalAnalyzer)

    def test_maybe_augment_disabled(self):
        f = {"confidence": 50}
        out = mar.maybe_augment("s", f, types.SimpleNamespace(rag=False))
        self.assertIs(out, f)

    def test_maybe_augment_skips_unstructured(self):
        f = {"_unstructured": "x"}
        out = mar.maybe_augment("s", f, types.SimpleNamespace(rag=True))
        self.assertIs(out, f)

    def test_maybe_augment_enabled_uses_store(self):
        mar.RAG_EMBED_MODEL = ""
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        tmp.close()
        old = mar.RAG_STORE_PATH
        try:
            mar.RAG_STORE_PATH = tmp.name
            findings = mar.LocalAnalyzer(mar.MockCollector(), verbose=False).triage("oom")
            seed = mar.RetrievalStore()          # picks up RAG_STORE_PATH
            seed.add_case("oom incident", findings, root_cause="rc", fix="fx")
            fresh = mar.LocalAnalyzer(mar.MockCollector(), verbose=False).triage("oom")
            buf = io.StringIO()
            with redirect_stdout(buf):   # swallow the stderr-style note if any
                out = mar.maybe_augment("oom incident", fresh, types.SimpleNamespace(rag=True))
            self.assertIn("similar_cases", out)
        finally:
            mar.RAG_STORE_PATH = old
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)


# ----------------------------------------------------------------------------
# Agent tool-use loop (fake client — no network, no API key)
# ----------------------------------------------------------------------------
class TestAgentLoop(unittest.TestCase):
    def test_tool_then_report(self):
        responses = [
            _fake_resp([_tool_block("get_channel_states", {}, "a1")], "tool_use"),
            _fake_resp([_tool_block("report_findings", {"severity": "high", "summary": "done"}, "a2")], "tool_use"),
        ]
        agent = _bare_agent(mar.MockCollector(), responses)
        out = agent.triage("something")
        self.assertEqual(out, {"severity": "high", "summary": "done"})
        self.assertEqual(len(agent._client.messages.calls), 2)

    def test_immediate_text_is_unstructured(self):
        responses = [_fake_resp([_text_block("no tools needed")], "end_turn")]
        agent = _bare_agent(mar.MockCollector(), responses)
        out = agent.triage("x")
        self.assertEqual(out, {"_unstructured": "no tools needed"})

    def test_unknown_tool_handled_then_report(self):
        responses = [
            _fake_resp([_tool_block("bogus_tool", {}, "b1")], "tool_use"),
            _fake_resp([_tool_block("report_findings", {"severity": "low"}, "b2")], "tool_use"),
        ]
        agent = _bare_agent(mar.MockCollector(), responses)
        out = agent.triage("x")
        self.assertEqual(out, {"severity": "low"})

    def test_no_convergence_within_turns(self):
        responses = [
            _fake_resp([_tool_block("get_engine_stats", {}, "c1")], "tool_use"),
            _fake_resp([_tool_block("get_engine_stats", {}, "c2")], "tool_use"),
        ]
        agent = _bare_agent(mar.MockCollector(), responses)
        out = agent.triage("x", max_turns=2)
        self.assertIn("_unstructured", out)
        self.assertIn("did not converge", out["_unstructured"])


# ----------------------------------------------------------------------------
# Schema / prompt regression guards
# ----------------------------------------------------------------------------
class TestSchemaAndPrompt(unittest.TestCase):
    def _report_tool(self):
        return next(t for t in mar.TOOLS if t["name"] == "report_findings")

    def test_report_findings_new_fields(self):
        props = self._report_tool()["input_schema"]["properties"]
        for field in ("confidence", "signals", "causal_chain", "blast_radius", "ruled_out"):
            self.assertIn(field, props)

    def test_report_findings_required(self):
        req = self._report_tool()["input_schema"]["required"]
        for field in ("severity", "confidence", "signals", "causal_chain"):
            self.assertIn(field, req)

    def test_step_objects_have_verify(self):
        props = self._report_tool()["input_schema"]["properties"]
        self.assertIn("verify", props["immediate"]["items"]["properties"])
        self.assertIn("verify", props["durable"]["items"]["properties"])

    def test_system_prompt_mentions_method(self):
        for kw in ("CORRELATE", "QUANTIFY", "CAUSAL CHAIN"):
            self.assertIn(kw, mar.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
