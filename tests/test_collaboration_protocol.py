import unittest

from village.collaboration import assess, is_checkpoint_action


def event(agent, kind, detail, timestamp):
    return {"agent": agent, "event": kind, "detail": detail, "timestamp": timestamp}


class CollaborationProtocolTests(unittest.TestCase):
    def test_requires_memory_orientation_first(self):
        checkpoint = assess([], "a", has_active_task=True, peer_id="b", now=1000)
        self.assertEqual(checkpoint.stage, "orient")
        self.assertTrue(is_checkpoint_action(checkpoint, "memory_search"))
        self.assertFalse(is_checkpoint_action(checkpoint, "execute_bash"))

    def test_requires_named_peer_after_search(self):
        rows = [event("a", "memory_result", "result=success; action=memory_search; matches=2", "1970-01-01T00:15:00+00:00")]
        checkpoint = assess(rows, "a", has_active_task=True, peer_id="b", now=1000)
        self.assertEqual(checkpoint.stage, "consult")
        self.assertEqual(checkpoint.peer_id, "b")

    def test_requires_record_after_work(self):
        rows = [
            event("a", "memory_result", "result=success; action=memory_search; matches=2", "1970-01-01T00:15:00+00:00"),
            event("a", "board_message", "to=b; message=question", "1970-01-01T00:15:30+00:00"),
            event("a", "command_result", "result=success", "1970-01-01T00:16:00+00:00"),
        ]
        checkpoint = assess(rows, "a", has_active_task=True, peer_id="b", now=1000)
        self.assertEqual(checkpoint.stage, "record")

    def test_idle_agent_is_not_forced(self):
        checkpoint = assess([], "a", has_active_task=False, peer_id="b", now=1000)
        self.assertTrue(checkpoint.satisfied)


if __name__ == "__main__":
    unittest.main()
