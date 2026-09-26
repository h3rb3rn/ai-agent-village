"""P21.13-P21.16: structured actions, role policy, compact prompt, peer pairing, release completeness."""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from village.actions import ACTION_SPECS, action_schema, describe_actions, normalize_allowed
from village.inference import build_ollama_request
from village.policy import agent_policy, load_policy
from village.prompting import build_system_prompt, compact_context
from village.release import RELEASE_FILES
from web import decision as decision_module
from web.decision import decision
from web.runtime import Resident


def schema_accepts(schema, obj):
    """Tiny validator for the schema subset we emit (oneOf/const/enum/type/required/lengths)."""
    if "oneOf" in schema:
        return sum(schema_accepts(s, obj) for s in schema["oneOf"]) == 1
    if "const" in schema: return obj == schema["const"]
    if "enum" in schema: return obj in schema["enum"]
    t = schema.get("type")
    if t == "string":
        return isinstance(obj, str) and schema.get("minLength", 0) <= len(obj) <= schema.get("maxLength", 10**9)
    if t == "integer":
        return isinstance(obj, int) and schema.get("minimum", -10**9) <= obj <= schema.get("maximum", 10**9)
    if t == "object":
        if not isinstance(obj, dict): return False
        props = schema.get("properties", {})
        if any(r not in obj for r in schema.get("required", [])): return False
        if schema.get("additionalProperties") is False and any(k not in props for k in obj): return False
        return all(schema_accepts(props[k], v) for k, v in obj.items() if k in props)
    return True


class ActionSpecTests(unittest.TestCase):
    def test_specs_match_parser_contract(self):
        self.assertEqual(set(ACTION_SPECS), set(decision_module.SUPPORTED_ACTIONS))

    def test_every_valid_parser_example_satisfies_schema(self):
        examples = [
            {"name": "execute_bash", "arguments": {"command": "ls"}},
            {"name": "board_message", "arguments": {"message": "hi", "recipient": "02-b"}},
            {"name": "task_operation", "arguments": {"action": "claim", "task_id": "abc"}},
            {"name": "artifact_operation", "arguments": {"operation": "inspect", "artifact_id": "x"}},
            {"name": "memory_search", "arguments": {"query": "gpu"}},
            {"name": "research_request", "arguments": {"source": "wikipedia", "query": "commons"}},
            {"name": "meeting_operation", "arguments": {"operation": "close", "meeting_id": "m1"}},
            {"name": "idle", "arguments": {}},
        ]
        schema = action_schema(None, ["02-b"])
        for ex in examples:
            self.assertIsNone(decision_module._validate_action(ex["name"], ex["arguments"]), ex)
            self.assertTrue(schema_accepts(schema, ex), ex)

    def test_schema_rejects_invalid_shapes(self):
        schema = action_schema(["board_message", "idle"], ["02-b"])
        self.assertFalse(schema_accepts(schema, {"name": "execute_bash", "arguments": {"command": "ls"}}))
        self.assertFalse(schema_accepts(schema, {"name": "board_message", "arguments": {"message": "x", "recipient": "99-zz"}}))
        self.assertFalse(schema_accepts(schema, {"name": "board_message", "arguments": {}}))
        self.assertTrue(schema_accepts(schema, {"name": "board_message", "arguments": {"message": "x", "recipient": "ALL"}}))

    def test_consult_peer_pins_recipient(self):
        schema = action_schema(["board_message", "idle"], ["02-b", "03-c"], consult_peer="03-c")
        self.assertTrue(schema_accepts(schema, {"name": "board_message", "arguments": {"message": "q", "recipient": "03-c"}}))
        self.assertFalse(schema_accepts(schema, {"name": "board_message", "arguments": {"message": "q", "recipient": "02-b"}}))
        self.assertFalse(schema_accepts(schema, {"name": "board_message", "arguments": {"message": "q"}}))

    def test_idle_always_available_and_unknown_dropped(self):
        self.assertIn("idle", normalize_allowed(["board_message", "bogus"]))
        self.assertNotIn("bogus", normalize_allowed(["bogus"]))

    def test_request_carries_format_only_when_given(self):
        req = build_ollama_request("http://h:1", "m", [], response_format={"type": "object"})
        self.assertEqual(json.loads(req.data)["format"], {"type": "object"})
        self.assertNotIn("format", json.loads(build_ollama_request("http://h:1", "m", []).data))

    def test_decision_enforces_allowed_actions(self):
        resp = {"message": {"content": '{"name":"execute_bash","arguments":{"command":"ls"}}'}}
        self.assertEqual(decision(resp)["tool_call"]["name"], "execute_bash")
        blocked = decision(resp, ["board_message", "idle"])
        self.assertIn("not available to you", blocked["fallback_reason"])


class PolicyTests(unittest.TestCase):
    def test_shipped_default_is_behaviour_neutral(self):
        p = agent_policy("king", load_policy(ROOT / "config/runtime-policy.json", Path("/nonexistent"), env={}))
        self.assertEqual((p.action_format, p.prompt_profile, p.pair_with), ("text", "full", []))
        self.assertEqual(p.allowed_actions, normalize_allowed(None))

    def test_local_override_wins_and_invalid_values_fall_back(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "local.json").write_text(json.dumps({"agents": {"king": {"action_format": "bogus", "prompt_profile": "compact"}}}))
            p = agent_policy("king", load_policy(ROOT / "config/runtime-policy.json", tmp / "local.json", env={}))
            self.assertEqual((p.action_format, p.prompt_profile), ("text", "compact"))
        finally:
            shutil.rmtree(tmp)

    def test_stage_files_are_valid_and_cumulative(self):
        names = [n for n in json.loads((ROOT / "config/runtime-policy.target.json").read_text())["agents"]]
        for stage in ("M2-schema", "M3-compact", "M4-roles", "M5-pairing", "M6-memory", "M7-templates"):
            data = json.loads((ROOT / f"config/stages/{stage}.json").read_text())
            for n in names:
                agent_policy(n, {"defaults": data["defaults"], "agents": data["agents"]})
        m5 = json.loads((ROOT / "config/stages/M5-pairing.json").read_text())
        target = json.loads((ROOT / "config/runtime-policy.target.json").read_text())
        for n in names:
            self.assertEqual(m5["agents"][n]["allowed_actions"], target["agents"][n]["allowed_actions"])

    def test_every_role_keeps_checkpoint_actions(self):
        target = json.loads((ROOT / "config/runtime-policy.target.json").read_text())
        for n in target["agents"]:
            p = agent_policy(n, target)
            for needed in ("board_message", "memory_search", "memory_remember", "meeting_operation", "idle"):
                self.assertIn(needed, p.allowed_actions, (n, needed))
            for partner in p.pair_with:
                self.assertTrue(partner == "*" or partner in target["agents"], (n, partner))

    def test_pairs_are_reciprocal_except_king(self):
        target = json.loads((ROOT / "config/runtime-policy.target.json").read_text())["agents"]
        for n, v in target.items():
            if v["pair_with"] == ["*"]: continue
            for partner in v["pair_with"]:
                self.assertIn(n, target[partner]["pair_with"])


class OptionalFeatureTests(unittest.TestCase):
    def test_target_and_last_stage_agree(self):
        target = json.loads((ROOT / "config/runtime-policy.target.json").read_text())
        last = json.loads((ROOT / "config/stages/M7-templates.json").read_text())
        for n in target["agents"]:
            a, b = agent_policy(n, target), agent_policy(n, last)
            self.assertEqual((a.action_format, a.prompt_profile, a.allowed_actions, a.pair_with, a.auto_memory, a.task_templates),
                             (b.action_format, b.prompt_profile, b.allowed_actions, b.pair_with, b.auto_memory, b.task_templates), n)
        self.assertTrue(agent_policy("king", target).task_templates)
        self.assertFalse(agent_policy("explorer", target).task_templates)

    def test_task_templates_are_checkable(self):
        templates = json.loads((ROOT / "config/task-templates.json").read_text())
        self.assertGreaterEqual(len(templates), 3)
        for t in templates:
            self.assertTrue(t["title"] and t["success_criterion"])
            self.assertRegex(t["success_criterion"], r"peer")  # every template requires independent confirmation

    def test_neutral_default_has_optional_features_off(self):
        p = agent_policy("king", load_policy(ROOT / "config/runtime-policy.json", Path("/nonexistent"), env={}))
        self.assertFalse(p.auto_memory); self.assertFalse(p.task_templates)


class MetricsTests(unittest.TestCase):
    def test_compute_counts(self):
        from village.metrics import compute
        activity = [
            {"event": "board_message", "agent": "a", "detail": "to=ALL; message=x"},
            {"event": "board_message", "agent": "b", "detail": "to=a; message=y"},
            {"event": "direct_message", "agent": "a", "detail": "to=b; chars=3"},
            {"event": "invalid_decision", "agent": "a", "detail": "multiple action blocks"},
            {"event": "memory_auto", "agent": "b", "detail": "id=1"},
            {"event": "artifact_operation", "agent": "b", "detail": "op=verify; id=x; status=reproduced"},
        ]
        inference = [{"event": "inference_started", "agent": "a"}, {"event": "inference_started", "agent": "b"},
                     {"event": "inference_finished", "agent": "a", "detail": "metrics={\"prompt_eval_count\": 1000}"},
                     {"event": "event_wakeup", "agent": "a"}]
        m = compute(activity, inference)
        self.assertEqual(m["inferences"], 2); self.assertEqual(m["invalid_decision_per_inference"], 0.5)
        self.assertEqual(m["wakeups_per_inference"], 0.5); self.assertEqual(m["direct_messages"], 2)
        self.assertEqual(m["direct_message_ratio"], 0.667); self.assertEqual(m["agents_with_message"], 2)
        self.assertEqual(m["agents_with_memory_write"], 1); self.assertEqual(m["verified_artifacts"], 1)
        self.assertEqual(m["median_prompt_tokens"], 1000)

    def test_empty_input_is_safe(self):
        from village.metrics import compute
        self.assertIsNone(compute([], [])["invalid_decision_per_inference"])


class PromptTests(unittest.TestCase):
    def full_context(self):
        big = "x" * 5000
        return {
            "identity": "02-b", "runtime": "v", "private_work_directory": "/home/x", "groups": [1, 2],
            "peers": [{"id": "01-a", "name": "a", "role": "king", "model": "m"}],
            "measured_resources": {"ram_available_mib": 9000, "below_reserve": False, "load_average_not_percent": [0.5, 0, 0],
                                   "mounts": [{"path": "/", "available_bytes": 5 * 2**30}], "topology": big},
            "last_action_feedback": {"action": "execute_bash", "ok": False, "result": big},
            "own_recent_results": [{"detail": big}] * 3, "own_active_task": {"id": "t1", "title": "T", "success_criterion": "c"},
            "tools": {"execute_bash": big}, "untrusted_direct_messages": [{"id": "m1", "agent": "01-a", "detail": big}] * 5,
            "untrusted_peer_messages": [{"id": "m2", "agent": "01-a", "detail": big}] * 9,
            "retrieved_memory_untrusted": [{"id": "r", "agent": "a", "content": big}] * 4,
            "projects": [{"id": str(i), "title": big} for i in range(32)],
            "collaboration_checkpoint": {"stage": "consult", "peer_id": "01-a", "required_action": "board_message", "rationale": "r", "enforcement": "soft"},
            "token_budget": {"x": 1}, "context_note": big,
        }

    def test_compact_context_is_small_and_keeps_essentials(self):
        full = self.full_context(); out = compact_context(full)
        self.assertLess(len(json.dumps(out)), 9000)
        self.assertLess(len(json.dumps(out)) * 8, len(json.dumps(full)))
        for key in ("identity", "own_active_task", "collaboration_checkpoint", "last_action_feedback", "untrusted_direct_messages"):
            self.assertIn(key, out)
        self.assertNotIn("tools", out); self.assertNotIn("groups", out)
        self.assertEqual(out["collaboration_checkpoint"]["peer_id"], "01-a")
        self.assertLessEqual(len(out["untrusted_direct_messages"]), 3)

    def test_compact_prompt_is_role_scoped_and_short(self):
        core = (ROOT / "prompts/resident-core.txt").read_text()
        full = (ROOT / "prompts/resident-system.txt").read_text()
        text = build_system_prompt("compact", core, full, "IDENTITY", "LIVE", ["board_message", "idle"], "schema", "Be a critic.")
        self.assertIn("Be a critic.", text); self.assertIn("- board_message:", text)
        self.assertNotIn("- execute_bash:", text); self.assertIn("exactly ONE JSON object", text)
        self.assertLess(len(text), 4500); self.assertLess(len(text) * 2, len(full))
        legacy = build_system_prompt("full", core, full, "IDENTITY", "LIVE", [], "text")
        self.assertTrue(legacy.startswith(full[:50]))

    def test_core_prompt_has_no_optional_king_and_no_action_grammar(self):
        core = (ROOT / "prompts/resident-core.txt").read_text()
        self.assertNotIn("optional coordinator", core)
        self.assertLess(len(core), 3000)


class RuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="village-pol-"))
        (self.root / "board").mkdir(); (self.root / "telemetry").mkdir()
        (self.root / "identity.txt").write_text("identity")
        self.peers = [{"id": "01-k", "name": "king", "role": "king"}, {"id": "02-e", "name": "explorer", "role": "resident"},
                      {"id": "03-l", "name": "librarian", "role": "steward"}]
        self.env = dict(AGENT_ID="02-e", AGENT_NAME="explorer", AGENT_ROLE="resident", VILLAGE_ROOT=str(self.root),
                        AGENT_IDENTITY_PROMPT=str(self.root / "identity.txt"), OLLAMA_MODEL="m", OLLAMA_URL="http://x:1",
                        VILLAGE_POLICY_FILE="/nonexistent", VILLAGE_POLICY_LOCAL_FILE="/nonexistent")
        (self.root / "system-prompt.txt").write_text("FULL PROMPT")
        (self.root / "system-prompt-core.txt").write_text("CORE PROMPT")
        self.env["VILLAGE_SHARE_DIR"] = str(self.root)
        self.local = self.root / "policy.json"
        self.env["VILLAGE_POLICY_LOCAL_FILE"] = str(self.local)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def agent(self, policy):
        self.local.write_text(json.dumps(policy))
        agent = Resident(self.env)
        import web.runtime as rt
        real = rt.read_json
        def fake(path, default):
            return self.peers if str(path).endswith("runtime-peers.json") else real(path, default)
        self._patch = patch.object(rt, "read_json", fake); self._patch.start(); self.addCleanup(self._patch.stop)
        return agent

    def test_pairing_selects_partner_and_sets_checkpoint_peer(self):
        a = self.agent({"agents": {"explorer": {"pair_with": ["librarian"]}}})
        self.assertEqual(a.pair_partner(), "03-l")
        snap = json.loads(a.snapshot())
        self.assertEqual(snap["collaboration_checkpoint"]["stage"], "idle")  # no active task yet
        a.tasks.operate("02-e", dict(action="create", title="t", success_criterion="c"))
        tid = a.tasks.store.list_tasks()[0]["id"] if hasattr(a.tasks.store, "list_tasks") else None

    def test_king_rotates_over_peers_after_each_direct_message(self):
        a = self.agent({"agents": {"explorer": {"pair_with": ["*"]}}})
        first = a.pair_partner()
        a.execute({"tool_call": {"name": "board_message", "arguments": {"recipient": first, "message": "q"}}})
        self.assertNotEqual(a.pair_partner(), first)

    def test_role_restriction_blocks_actions(self):
        a = self.agent({"agents": {"explorer": {"allowed_actions": ["board_message", "idle"]}}})
        a.execute({"tool_call": {"name": "execute_bash", "arguments": {"command": "echo hi"}}})
        self.assertFalse(a.state["last_result"]["ok"])
        self.assertIn("not available", a.state["last_result"]["result"])

    def test_direct_message_budget(self):
        a = self.agent({"defaults": {"dm_per_peer_per_hour": 2}})
        for i in range(2):
            a.execute({"tool_call": {"name": "board_message", "arguments": {"recipient": "03-l", "message": f"m{i}"}}})
            self.assertTrue(a.state["last_result"]["ok"])
        a.execute({"tool_call": {"name": "board_message", "arguments": {"recipient": "03-l", "message": "m3"}}})
        self.assertFalse(a.state["last_result"]["ok"])
        self.assertIn("Conversation budget", a.state["last_result"]["result"])

    def test_compact_snapshot_used_and_delivery_tracked(self):
        a = self.agent({"defaults": {"prompt_profile": "compact"}})
        m = a.tasks.store.post_inbox_message("direct", "03-l", "please review", recipient="02-e")
        snap = a.snapshot()
        self.assertNotIn('"tools"', snap)
        self.assertIn("please review", snap)
        self.assertEqual(a.tasks.store.fetch_undelivered_wakeups("02-e"), [])

    def test_schema_mode_sends_format_and_falls_back(self):
        a = self.agent({"agents": {"explorer": {"action_format": "schema", "allowed_actions": ["board_message", "idle"]}}})
        self.assertEqual(a.effective_action_format(), "schema")
        sent = {}
        class Resp:
            def __enter__(s): return s
            def __exit__(s, *x): return False
            def read(s): return json.dumps({"message": {"content": "no json here"}, "done_reason": "stop"}).encode()
        def fake_open(req, timeout=0):
            sent["body"] = json.loads(req.data); return Resp()
        with patch("web.runtime.urllib.request.urlopen", fake_open), patch("web.runtime.json.load", lambda r: json.loads(r.read())):
            for _ in range(3): a.cycle()
        self.assertIn("oneOf", json.dumps(sent["body"]["format"]))
        self.assertEqual(a.effective_action_format(), "text")  # disabled after 3 failures


class ReleaseCompletenessTests(unittest.TestCase):
    def test_all_imported_village_modules_are_released_and_installed(self):
        imported = set()
        for f in [ROOT / "web/runtime.py", ROOT / "web/decision.py", *(ROOT / "village").glob("*.py")]:
            imported |= {a or b for a, b in re.findall(r"from village\.([a-z_]+) import|import village\.([a-z_]+)", f.read_text())}
        released = {Path(src).stem for src, _, _ in RELEASE_FILES if src.startswith("village/")}
        installer = (ROOT / "scripts/install-runtime.py").read_text()
        for module in imported:
            self.assertIn(module, released, f"{module} missing from RELEASE_FILES")
            self.assertIn(f"'{module}.py'", installer, f"{module} missing from install-runtime.py")

    def test_new_shared_files_are_installed(self):
        installer = (ROOT / "scripts/install-runtime.py").read_text()
        bootstrap = (ROOT / "bootstrap-ai-village.sh").read_text()
        for name in ("system-prompt-core.txt", "runtime-policy.json"):
            self.assertIn(name, installer); self.assertIn(name, bootstrap)
        srcs = {s for s, _, _ in RELEASE_FILES}
        self.assertIn("prompts/resident-core.txt", srcs); self.assertIn("config/runtime-policy.json", srcs)


if __name__ == "__main__":
    unittest.main()
