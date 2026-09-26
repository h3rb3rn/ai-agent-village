"""Deterministic multi-resident simulation with a scripted fake model (no network, no GPU).

Proves the construct works end to end: schema-constrained actions, task -> orient -> consult ->
direct reply -> ack -> record, routing by the runtime, no wake loops, no Board pollution.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web.runtime as rt
from village.metrics import compute
from web.runtime import Resident, tail


class FakeResponse:
    def __init__(self, content): self.body = json.dumps({"message": {"content": content}, "done_reason": "stop",
                                                          "prompt_eval_count": 900, "eval_count": 40}).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.body


class Village:
    PEERS = [{"id": "01-king", "name": "king", "role": "king"}, {"id": "02-explorer", "name": "explorer", "role": "resident"},
             {"id": "03-librarian", "name": "librarian", "role": "steward"}]

    def __init__(self, tc):
        self.tc = tc
        self.root = Path(tempfile.mkdtemp(prefix="village-sim-"))
        (self.root / "board").mkdir(); (self.root / "telemetry").mkdir()
        (self.root / "identity.txt").write_text("identity")
        (self.root / "system-prompt.txt").write_text("FULL"); (self.root / "system-prompt-core.txt").write_text("CORE")
        policy = {"defaults": {"action_format": "schema", "prompt_profile": "compact", "auto_memory": True},
                  "agents": {"explorer": {"pair_with": ["librarian"], "allowed_actions": ["board_message", "memory_search", "memory_remember", "meeting_operation", "task_operation", "execute_bash", "idle"]},
                             "librarian": {"pair_with": ["explorer"], "allowed_actions": ["board_message", "memory_search", "memory_remember", "meeting_operation", "idle"]}}}
        (self.root / "policy.json").write_text(json.dumps(policy))
        self.scripts = {}; self.requests = []; self.memories = []
        self.agents = {n: self.make(n) for n in ("king", "explorer", "librarian")}
        real = rt.read_json
        self.p1 = patch.object(rt, "read_json", lambda path, d: self.PEERS if str(path).endswith("runtime-peers.json") else real(path, d))
        self.p2 = patch("web.runtime.urllib.request.urlopen", self.urlopen)
        self.p3 = patch.object(Resident, "memory", self.fake_memory)
        for p in (self.p1, self.p2, self.p3): p.start()

    def make(self, name):
        idx = {"king": "01-king", "explorer": "02-explorer", "librarian": "03-librarian"}[name]
        env = dict(AGENT_ID=idx, AGENT_NAME=name, AGENT_ROLE="resident", VILLAGE_ROOT=str(self.root),
                   AGENT_IDENTITY_PROMPT=str(self.root / "identity.txt"), OLLAMA_MODEL=name, OLLAMA_URL="http://fake:1",
                   VILLAGE_SHARE_DIR=str(self.root), VILLAGE_POLICY_FILE="/nonexistent",
                   VILLAGE_POLICY_LOCAL_FILE=str(self.root / "policy.json"), VILLAGE_CYCLE_SECONDS="0")
        return Resident(env)

    def fake_memory(self, endpoint, value):
        if endpoint == "/v1/search":
            return {"items": [m for m in self.memories if value.get("query") is not None]}
        item = dict(value, id=f"mem{len(self.memories)}"); self.memories.append(item); return item

    def urlopen(self, req, timeout=0):
        body = json.loads(req.data); self.requests.append(body)
        script = self.scripts[body["model"]]
        return FakeResponse(json.dumps(script.pop(0)))

    def say(self, name, *actions):
        self.scripts.setdefault(name, []).extend(actions)

    def close(self):
        for p in (self.p3, self.p2, self.p1): p.stop()
        shutil.rmtree(self.root, ignore_errors=True)


class SimulationTests(unittest.TestCase):
    def test_paired_consultation_round_trip(self):
        v = Village(self); self.addCleanup(v.close)
        ex, li = v.agents["explorer"], v.agents["librarian"]
        # 1. Explorer creates and claims a task, so checkpoints become active.
        v.say("explorer",
              {"name": "task_operation", "arguments": {"action": "create", "title": "Inventory", "success_criterion": "JSON printed"}},
              )
        ex.cycle()
        task = ex.tasks.store.list_tasks()[0] if hasattr(ex.tasks.store, "list_tasks") else json.loads((v.root / "board/work-items.json").read_text())[0]
        v.say("explorer", {"name": "task_operation", "arguments": {"action": "claim", "task_id": task["id"]}})
        ex.cycle()
        # 2. Orient: memory_search.
        v.say("explorer", {"name": "memory_search", "arguments": {"query": "inventory"}})
        ex.cycle()
        self.assertTrue(any("action=memory_search" in e.get("detail", "") for e in tail(v.root / "board/events.jsonl") + tail(v.root / "telemetry/agent-events.jsonl")))
        # 3. Consult: the schema must pin the recipient to the runtime-selected partner.
        v.say("explorer", {"name": "board_message", "arguments": {"recipient": "03-librarian", "message": "Can you check my inventory idea?"}})
        ex.cycle()
        fmt = json.dumps(v.requests[-1]["format"])
        self.assertIn('"const": "03-librarian"', fmt)
        self.assertTrue(ex.state["last_result"]["ok"], ex.state["last_result"])
        # 4. Librarian is woken by the direct message exactly once, sees it, answers the explorer.
        self.assertEqual(len(li.tasks.store.fetch_undelivered_wakeups("03-librarian")), 1)
        v.say("librarian", {"name": "board_message", "arguments": {"recipient": "02-explorer", "message": "Looks reproducible; add a test."}})
        li.cycle()
        self.assertTrue(li.state["last_result"]["ok"], li.state["last_result"])
        self.assertEqual(li.tasks.store.fetch_undelivered_wakeups("03-librarian"), [])
        self.assertEqual(li.tasks.store.fetch_unacknowledged_messages("03-librarian", source="direct"), [])
        prompt = json.dumps(v.requests[-1]["messages"])
        self.assertIn("Can you check my inventory idea?", prompt)
        self.assertNotIn("execute_bash", json.dumps(v.requests[-1]["format"]))  # librarian has no shell
        # 5. Explorer receives the reply, wakes once, and does real work followed by a recorded lesson.
        self.assertEqual(len(ex.tasks.store.fetch_undelivered_wakeups("02-explorer")), 1)
        v.say("explorer", {"name": "execute_bash", "arguments": {"command": "echo '{\"ram\": 1}'"}})
        ex.cycle()
        self.assertTrue(ex.state["last_result"]["ok"])
        self.assertTrue(any(m["kind"] == "runtime_observation" and "[runtime-observed" in m["content"] for m in v.memories))
        # 6. Metrics see direct messages and no Board pollution.
        activity = tail(v.root / "board/events.jsonl", 500)
        self.assertEqual(sum(e["event"] == "direct_message" for e in activity), 2)
        self.assertFalse(any("unexecuted proposal" in json.dumps(e) for e in activity))
        tele = tail(v.root / "telemetry/agent-events.jsonl", 500)
        inference = [e for e in tele if e["event"] in ("inference_started", "inference_finished", "event_wakeup", "runtime_exception")]
        m = compute(activity, inference)
        self.assertEqual(m["direct_messages"], 2); self.assertEqual(m["runtime_exceptions"], 0)
        self.assertEqual(m["invalid_decision_per_inference"], 0.0)
        self.assertEqual(m["median_prompt_tokens"], 900)

    def test_schema_is_role_scoped_per_agent(self):
        v = Village(self); self.addCleanup(v.close)
        v.say("librarian", {"name": "idle", "arguments": {}}); v.agents["librarian"].cycle()
        names = [var["properties"]["name"]["const"] for var in v.requests[-1]["format"]["oneOf"]]
        self.assertEqual(set(names), {"board_message", "memory_search", "memory_remember", "meeting_operation", "idle"})

    def test_auto_memory_does_not_satisfy_record_checkpoint(self):
        v = Village(self); self.addCleanup(v.close)
        ex = v.agents["explorer"]
        ex.execute({"tool_call": {"name": "execute_bash", "arguments": {"command": "echo hi"}}})
        events = tail(v.root / "telemetry/agent-events.jsonl", 100)
        self.assertTrue(any(e["event"] == "memory_auto" for e in events))
        board = tail(v.root / "board/events.jsonl", 100)
        self.assertFalse(any(e["event"] == "memory_result" for e in board + events))

    def test_auto_memory_is_rate_limited(self):
        v = Village(self); self.addCleanup(v.close)
        ex = v.agents["explorer"]
        for i in range(3):
            ex.execute({"tool_call": {"name": "execute_bash", "arguments": {"command": f"echo run{i}"}}})
        self.assertEqual(len(v.memories), 1)


if __name__ == "__main__":
    unittest.main()
