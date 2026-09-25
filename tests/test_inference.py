"""Unit tests for village.inference module and multi-provider adapter."""

import io
import json
import urllib.error
import urllib.request
from urllib.error import HTTPError
import unittest
from unittest.mock import MagicMock, patch

from village.inference import (
    InferenceResult,
    SafeRedirectHandler,
    build_ollama_request,
    build_openai_request,
    execute_inference,
    normalize_ollama_response,
    normalize_openai_response,
)


class TestVillageInference(unittest.TestCase):
    """Test suite for Ollama and OpenAI-compatible inference request construction and normalization."""

    def test_build_ollama_request(self):
        """Verify Ollama request payload, options, and Bearer token."""
        req = build_ollama_request(
            url="http://192.168.1.50:11434",
            model="qwen2.5:7b",
            messages=[{"role": "user", "content": "hello"}],
            num_ctx=16384,
            num_predict=1024,
            think_level="medium",
            keep_alive="24h",
            temperature=0.2,
            api_token="test-ollama-secret",
        )
        self.assertEqual(req.full_url, "http://192.168.1.50:11434/api/chat")
        self.assertEqual(req.headers.get("Authorization"), "Bearer test-ollama-secret")
        self.assertEqual(req.headers.get("Content-type"), "application/json")

        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["model"], "qwen2.5:7b")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["keep_alive"], "24h")
        self.assertEqual(payload["think"], "medium")
        self.assertEqual(payload["options"]["num_ctx"], 16384)
        self.assertEqual(payload["options"]["num_predict"], 1024)
        self.assertEqual(payload["options"]["temperature"], 0.2)

    def test_build_openai_request(self):
        """Verify OpenAI request payload, headers, and exclusion of server-side KV cache settings."""
        # 1. URL without /v1
        req = build_openai_request(
            url="https://api.example.com",
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "analyze code"}],
            num_predict=512,
            think_level="low",
            temperature=0.7,
            api_token="sk-synthetic-openai-key-test",
        )
        self.assertEqual(req.full_url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(req.headers.get("Authorization"), "Bearer sk-synthetic-openai-key-test")

        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["model"], "gpt-4o-mini")
        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["reasoning_effort"], "low")
        # Ensure Ollama-specific options are NOT sent to OpenAI endpoints
        self.assertNotIn("num_ctx", payload)
        self.assertNotIn("options", payload)
        self.assertNotIn("keep_alive", payload)

        # 2. URL with /v1 already present
        req_v1 = build_openai_request(
            url="https://api.example.com/v1",
            model="gpt-4o-mini",
            messages=[],
        )
        self.assertEqual(req_v1.full_url, "https://api.example.com/v1/chat/completions")

    def test_safe_redirect_handler_strips_auth_on_cross_domain(self):
        """Ensure Authorization header is stripped when redirecting to a different origin."""
        handler = SafeRedirectHandler()

        # Same origin: auth header preserved
        req_same = urllib.request.Request("http://api.lan/v1/chat", headers={"Authorization": "Bearer secret123"})
        new_req_same = handler.redirect_request(req_same, None, 302, "Found", {}, "http://api.lan/v1/chat_new")
        self.assertIn("Authorization", new_req_same.headers)

        # Cross origin: auth header stripped
        req_cross = urllib.request.Request("http://api.lan/v1/chat", headers={"Authorization": "Bearer secret123"})
        new_req_cross = handler.redirect_request(req_cross, None, 302, "Found", {}, "http://evil.com/leak")
        self.assertNotIn("Authorization", new_req_cross.headers)

    def test_normalize_openai_response(self):
        """Verify normalization of OpenAI choices and usage tokens."""
        raw_openai = {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": 'Ich teste das System.\n```village-action\n{"name":"idle"}\n```',
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 128,
                "completion_tokens": 32,
                "total_tokens": 160,
            },
        }

        res = normalize_openai_response(raw_openai, duration_ms=450)
        self.assertEqual(res.finish_reason, "stop")
        self.assertEqual(res.metrics["prompt_eval_count"], 128)
        self.assertEqual(res.metrics["eval_count"], 32)
        self.assertIn("village-action", res.content)

        # Check normalized_answer shape expected by decision.py
        norm_ans = res.normalized_answer
        self.assertEqual(norm_ans["done_reason"], "stop")
        self.assertEqual(norm_ans["message"]["role"], "assistant")
        self.assertEqual(norm_ans["message"]["content"], res.content)

    def test_normalize_openai_response_length_truncated(self):
        """Verify OpenAI finish_reason length correctly translates to done_reason length."""
        raw_openai = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "incomplete..."},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 100},
        }
        res = normalize_openai_response(raw_openai, duration_ms=300)
        self.assertEqual(res.finish_reason, "length")
        self.assertEqual(res.normalized_answer["done_reason"], "length")

    def test_execute_inference_mocked_success_and_errors(self):
        """Test execute_inference dispatching, response parsing, and error propagation."""
        # 1. Ollama successful call
        mock_ollama_resp = {
            "message": {"role": "assistant", "content": "ollama reply"},
            "done_reason": "stop",
            "prompt_eval_count": 80,
            "eval_count": 25,
        }
        mock_opener = MagicMock()
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(mock_ollama_resp).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_opener.open.return_value = mock_response

        res = execute_inference(
            api_type="ollama",
            url="http://127.0.0.1:11434",
            model="qwen2.5:7b",
            messages=[],
            opener=mock_opener,
        )
        self.assertEqual(res.content, "ollama reply")
        self.assertEqual(res.metrics["prompt_eval_count"], 80)

        # 2. HTTP 401 Authentication failure
        mock_opener.open.side_effect = HTTPError(
            url="http://127.0.0.1:11434/api/chat",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error": "invalid token"}'),
        )
        with self.assertRaises(HTTPError) as ctx:
            execute_inference(
                api_type="ollama",
                url="http://127.0.0.1:11434",
                model="qwen2.5:7b",
                messages=[],
                opener=mock_opener,
            )
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
