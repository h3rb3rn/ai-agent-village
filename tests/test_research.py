import json
import unittest
from unittest.mock import patch

from village.research import ResearchBroker, ResearchError, ResearchPolicy


class FakeResponse:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return self.body


class ResearchTests(unittest.TestCase):
    def test_sources_are_allowlisted_and_normalized(self):
        def opener(request, timeout):
            self.assertIn("api.github.com", request.full_url)
            return FakeResponse(json.dumps({"items": [{"full_name": "org/tool", "html_url": "https://github.com/org/tool"}]}).encode())
        result = ResearchBroker(opener=opener).search("github", "memory graph", 2)
        self.assertEqual(result["results"][0]["full_name"], "org/tool")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["deployment_performed"])

    def test_unknown_source_and_oversized_response_are_rejected(self):
        with self.assertRaises(ResearchError): ResearchBroker().search("gitlab", "tool")
        with self.assertRaises(ResearchError):
            ResearchBroker(ResearchPolicy(max_bytes=10), opener=lambda request, timeout: FakeResponse(b"x" * 20)).search("github", "tool")

    def test_wikipedia_shape(self):
        payload = ["q", ["Article"], ["summary"], ["https://en.wikipedia.org/wiki/Article"]]
        result = ResearchBroker(opener=lambda request, timeout: FakeResponse(json.dumps(payload).encode())).search("wikipedia", "Article")
        self.assertEqual(result["results"][0]["title"], "Article")


class HuggingFaceMetadataTests(unittest.TestCase):
    """P33: dataset metadata lookup only. The network path is inert until an operator
    adds huggingface.co to the external NAT/Squid allowlist; these tests never touch
    the network (FakeResponse), they verify the allowlist/normalization/query-safety
    logic that will run once that firewall decision is made."""

    def test_dataset_metadata_is_normalized_with_license_and_files(self):
        payload = {"id": "NetoAISolutions/NetBench", "cardData": {"license": "apache-2.0"},
                   "downloads": 120, "likes": 4, "tags": ["network"],
                   "siblings": [{"rfilename": "data/train.jsonl"}, {"rfilename": "README.md"}]}
        def opener(request, timeout):
            self.assertIn("huggingface.co/api/datasets/", request.full_url)
            self.assertIn("NetoAISolutions", request.full_url)
            return FakeResponse(json.dumps(payload).encode())
        result = ResearchBroker(opener=opener).search("huggingface", "NetoAISolutions/NetBench")
        self.assertTrue(result["read_only"]); self.assertFalse(result["deployment_performed"])
        self.assertIn("no dataset content", result["note"])
        row = result["results"][0]
        self.assertEqual(row["license"], "apache-2.0")
        self.assertEqual(row["files"], ["data/train.jsonl", "README.md"])

    def test_host_is_restricted_to_huggingface_co(self):
        from village.research import ALLOWED_HOSTS
        self.assertEqual(ALLOWED_HOSTS["huggingface"], ("huggingface.co",))

    def test_invalid_dataset_id_shape_is_rejected_before_any_request(self):
        def opener(request, timeout):
            raise AssertionError("must not make a network request for an invalid id")
        with self.assertRaises(ResearchError):
            ResearchBroker(opener=opener).search("huggingface", "../../etc/passwd")

    def test_gated_or_private_dataset_is_flagged_not_hidden(self):
        payload = {"id": "some/gated-set", "gated": True, "private": False}
        result = ResearchBroker(opener=lambda request, timeout: FakeResponse(json.dumps(payload).encode())).search("huggingface", "some/gated-set")
        self.assertTrue(result["results"][0]["gated"])

    def test_non_dict_payload_normalizes_to_empty_results_not_a_crash(self):
        result = ResearchBroker(opener=lambda request, timeout: FakeResponse(json.dumps([1, 2, 3]).encode())).search("huggingface", "org/name")
        self.assertEqual(result["results"], [])
