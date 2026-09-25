"""Inference adapter module for AI Village supporting Ollama and OpenAI-compatible providers.

Provides request builder, credential security, redirect isolation, and response
normalization for both local Ollama lanes and remote OpenAI-compatible endpoints.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that strips Authorization headers when redirected to an untrusted domain."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None

        # Compare origin host:port
        old_parsed = urllib.parse.urlsplit(req.full_url)
        new_parsed = urllib.parse.urlsplit(newurl)

        if (old_parsed.hostname, old_parsed.port) != (new_parsed.hostname, new_parsed.port):
            # Target origin differs: strip Bearer credential to prevent leak
            if "Authorization" in new_req.headers:
                del new_req.headers["Authorization"]
            if "authorization" in new_req.headers:
                del new_req.headers["authorization"]

        return new_req


@dataclass
class InferenceResult:
    """Normalized response from an LLM inference endpoint."""

    content: str
    finish_reason: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    raw_response: Dict[str, Any] = field(default_factory=dict)
    normalized_answer: Dict[str, Any] = field(default_factory=dict)


def build_ollama_request(
    url: str,
    model: str,
    messages: List[Dict[str, str]],
    num_ctx: int = 8192,
    num_predict: int = 768,
    think_level: str = "off",
    keep_alive: str = "10m",
    temperature: float = 0.35,
    api_token: Optional[str] = None,
) -> urllib.request.Request:
    """Build an HTTP Request targeting an Ollama native /api/chat endpoint.

    Args:
        url: Base URL of Ollama service.
        model: Model identifier.
        messages: List of message dictionaries with 'role' and 'content'.
        num_ctx: Context window in tokens.
        num_predict: Maximum completion tokens.
        think_level: Thinking/reasoning level (off, low, medium, high, max).
        keep_alive: Keep-alive duration string (e.g. 10m, 24h).
        temperature: Sampling temperature.
        api_token: Optional Bearer authentication token.

    Returns:
        urllib.request.Request: Fully configured request object.
    """
    endpoint = url.rstrip("/")
    if not endpoint.endswith("/api/chat"):
        endpoint = f"{endpoint}/api/chat"

    payload: Dict[str, Any] = {
        "model": model,
        "stream": False,
        "keep_alive": keep_alive or "10m",
        "options": {
            "num_ctx": int(num_ctx),
            "num_predict": int(num_predict),
            "temperature": float(temperature),
        },
        "messages": messages,
    }
    if think_level and think_level != "off":
        payload["think"] = think_level

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
    }
    if api_token and api_token.strip():
        headers["Authorization"] = f"Bearer {api_token.strip()}"

    data = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(endpoint, data=data, headers=headers)


def build_openai_request(
    url: str,
    model: str,
    messages: List[Dict[str, str]],
    num_predict: int = 768,
    think_level: str = "off",
    temperature: float = 0.35,
    api_token: Optional[str] = None,
) -> urllib.request.Request:
    """Build an HTTP Request targeting an OpenAI-compatible /v1/chat/completions endpoint.

    Note: num_ctx and keep_alive are intentionally NOT sent as top-level parameters
    to OpenAI endpoints, as they are not standard OpenAI completion parameters.

    Args:
        url: Base URL of OpenAI-compatible service.
        model: Model identifier.
        messages: List of message dictionaries with 'role' and 'content'.
        num_predict: Maximum completion tokens (mapped to max_tokens).
        think_level: Thinking/reasoning level (mapped to reasoning_effort if supported).
        temperature: Sampling temperature.
        api_token: Optional Bearer authentication token.

    Returns:
        urllib.request.Request: Fully configured request object.
    """
    endpoint = url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        if endpoint.endswith("/v1"):
            endpoint = f"{endpoint}/chat/completions"
        else:
            endpoint = f"{endpoint}/v1/chat/completions"

    payload: Dict[str, Any] = {
        "model": model,
        "stream": False,
        "max_tokens": int(num_predict),
        "temperature": float(temperature),
        "messages": messages,
    }

    if think_level and think_level in ("low", "medium", "high"):
        payload["reasoning_effort"] = think_level

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
    }
    if api_token and api_token.strip():
        headers["Authorization"] = f"Bearer {api_token.strip()}"

    data = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(endpoint, data=data, headers=headers)


def normalize_ollama_response(raw_data: Dict[str, Any], duration_ms: int) -> InferenceResult:
    """Normalize raw Ollama response dictionary into InferenceResult.

    Args:
        raw_data: JSON response dictionary from Ollama /api/chat.
        duration_ms: Total elapsed round-trip time in milliseconds.

    Returns:
        InferenceResult: Standardized result object.
    """
    msg = raw_data.get("message", {})
    content = msg.get("content", "")
    finish_reason = raw_data.get("done_reason", "stop")

    metrics = {
        "total_duration": raw_data.get("total_duration", duration_ms * 1_000_000),
        "prompt_eval_count": raw_data.get("prompt_eval_count", 0),
        "prompt_eval_duration": raw_data.get("prompt_eval_duration", 0),
        "eval_count": raw_data.get("eval_count", 0),
        "eval_duration": raw_data.get("eval_duration", 0),
        "done_reason": finish_reason,
    }

    # normalized_answer matches the legacy Ollama dictionary expected by decision.py
    normalized_answer = {
        "message": msg,
        "done_reason": finish_reason,
        **metrics,
    }

    return InferenceResult(
        content=content,
        finish_reason=finish_reason,
        metrics=metrics,
        raw_response=raw_data,
        normalized_answer=normalized_answer,
    )


def normalize_openai_response(raw_data: Dict[str, Any], duration_ms: int) -> InferenceResult:
    """Normalize raw OpenAI-compatible response dictionary into InferenceResult.

    Args:
        raw_data: JSON response dictionary from /v1/chat/completions.
        duration_ms: Total elapsed round-trip time in milliseconds.

    Returns:
        InferenceResult: Standardized result object.
    """
    choices = raw_data.get("choices", [])
    if choices:
        first_choice = choices[0]
        msg = first_choice.get("message", {})
        content = msg.get("content") or ""
        finish_reason = first_choice.get("finish_reason") or "stop"
    else:
        msg = {"role": "assistant", "content": ""}
        content = ""
        finish_reason = "empty"

    usage = raw_data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    # Convert finish_reason 'length' to Ollama-compatible 'length'
    done_reason = "length" if finish_reason == "length" else "stop"

    metrics = {
        "total_duration": duration_ms * 1_000_000,
        "prompt_eval_count": prompt_tokens,
        "prompt_eval_duration": 0,
        "eval_count": completion_tokens,
        "eval_duration": 0,
        "done_reason": done_reason,
    }

    normalized_answer = {
        "message": {"role": "assistant", "content": content},
        "done_reason": done_reason,
        **metrics,
    }

    return InferenceResult(
        content=content,
        finish_reason=done_reason,
        metrics=metrics,
        raw_response=raw_data,
        normalized_answer=normalized_answer,
    )


def execute_inference(
    api_type: str,
    url: str,
    model: str,
    messages: List[Dict[str, str]],
    num_ctx: int = 8192,
    num_predict: int = 768,
    think_level: str = "off",
    keep_alive: str = "10m",
    temperature: float = 0.35,
    api_token: Optional[str] = None,
    timeout_seconds: int = 1800,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> InferenceResult:
    """Execute LLM inference call with safe redirect protection and response normalization.

    Args:
        api_type: 'ollama' or 'openai'.
        url: Service endpoint URL.
        model: Model name.
        messages: Chat history / prompt messages.
        num_ctx: Context budget (used in Ollama payload, local budget in OpenAI).
        num_predict: Max completion tokens.
        think_level: Reasoning level.
        keep_alive: Keep-alive duration string.
        temperature: Sampling temperature.
        api_token: Optional Bearer token.
        timeout_seconds: Network timeout.
        opener: Optional custom urllib opener (for testing/mocking).

    Returns:
        InferenceResult: Normalized response object.

    Raises:
        urllib.error.HTTPError: On HTTP error status (4xx/5xx).
        urllib.error.URLError: On connection failure or timeout.
        ValueError: On corrupted JSON response.
    """
    api_type = (api_type or "ollama").lower()
    if api_type == "openai":
        request = build_openai_request(
            url=url,
            model=model,
            messages=messages,
            num_predict=num_predict,
            think_level=think_level,
            temperature=temperature,
            api_token=api_token,
        )
    else:
        request = build_ollama_request(
            url=url,
            model=model,
            messages=messages,
            num_ctx=num_ctx,
            num_predict=num_predict,
            think_level=think_level,
            keep_alive=keep_alive,
            temperature=temperature,
            api_token=api_token,
        )

    # Use custom SafeRedirectHandler if default opener is used
    if opener is None:
        opener = urllib.request.build_opener(SafeRedirectHandler())

    start_time = time.monotonic()
    # Note: caller or test may patch urllib.request.urlopen or pass an opener
    with opener.open(request, timeout=timeout_seconds) as response:
        raw_text = response.read().decode("utf-8", errors="replace")
        raw_json = json.loads(raw_text)

    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    if api_type == "openai":
        return normalize_openai_response(raw_json, elapsed_ms)
    else:
        return normalize_ollama_response(raw_json, elapsed_ms)


def cancel_upstream_inference(
    provider: str,
    request_id: str,
    endpoint_url: str,
    api_token: Optional[str] = None,
) -> Tuple[bool, str]:
    """Attempt upstream cancellation if supported by the provider.

    Notice: As documented in P07, network disconnect or socket termination
    alone is not proof of upstream cancellation acknowledgement. Universal
    cancel endpoints must not be fabricated.

    Args:
        provider: Provider type ('ollama' or 'openai').
        request_id: Request identifier.
        endpoint_url: Upstream endpoint base URL.
        api_token: Optional authentication token.

    Returns:
        Tuple[bool, str]: (backend_acknowledged, diagnostic_reason)
    """
    if provider.lower() == "openai":
        return False, "OpenAI-compatible endpoints do not support out-of-band request cancellation"
    return False, "Ollama connection disconnect terminates local client stream, but backend does not issue cancellation receipt"
