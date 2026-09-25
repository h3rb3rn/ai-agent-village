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
