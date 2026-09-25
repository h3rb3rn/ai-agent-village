"""Allowlisted, read-only research broker for Wikipedia, GitHub and Docker Hub."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class ResearchError(ValueError):
    pass


@dataclass(frozen=True)
class ResearchPolicy:
    max_bytes: int = 65536
    timeout_seconds: int = 300


ALLOWED_HOSTS = {
    "wikipedia": ("en.wikipedia.org", "de.wikipedia.org"),
    "github": ("api.github.com", "github.com"),
    "dockerhub": ("hub.docker.com", "registry-1.docker.io"),
}


def _allowed(url: str, source: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS[source]:
        raise ResearchError(f"host is not allowlisted for {source}")
    return parsed


class ResearchBroker:
    """Read-only source adapter. It never clones, pulls, builds or deploys."""

    def __init__(self, policy: ResearchPolicy = ResearchPolicy(), opener: Callable = urllib.request.urlopen):
        self.policy = policy
        self.opener = opener

    def _get_json(self, url: str, source: str) -> tuple[Any, str]:
        _allowed(url, source)
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ai-village-research/1"})
        try:
            with self.opener(request, timeout=self.policy.timeout_seconds) as response:
                body = response.read(self.policy.max_bytes + 1)
        except Exception as exc:
            raise ResearchError(f"{source} request failed: {exc}") from exc
        if len(body) > self.policy.max_bytes:
            raise ResearchError("response exceeds research size limit")
        try:
            return json.loads(body.decode("utf-8")), hashlib.sha256(body).hexdigest()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchError(f"{source} returned invalid JSON") from exc

    def search(self, source: str, query: str, limit: int = 5) -> dict[str, Any]:
        source, query = str(source).strip().lower(), str(query).strip()
        if source not in ALLOWED_HOSTS:
            raise ResearchError("source must be wikipedia, github or dockerhub")
        if not query or len(query) > 240:
            raise ResearchError("query must contain 1-240 characters")
        limit = max(1, min(int(limit), 10))
        if source == "wikipedia":
            params = urllib.parse.urlencode({"action": "opensearch", "search": query, "limit": limit, "namespace": 0, "format": "json"})
            url = f"https://en.wikipedia.org/w/api.php?{params}"
        elif source == "github":
            url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({"q": query, "per_page": limit})
        else:
            url = "https://hub.docker.com/v2/search/repositories?" + urllib.parse.urlencode({"query": query, "page_size": limit})
        payload, digest = self._get_json(url, source)
        return {"source": source, "query": query, "url": url, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "sha256": digest, "read_only": True, "deployment_performed": False, "results": self._normalize(source, payload)}

    @staticmethod
    def _normalize(source: str, payload: Any) -> list[dict[str, Any]]:
        if source == "wikipedia":
            titles = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
            urls = payload[3] if isinstance(payload, list) and len(payload) > 3 else []
            return [{"title": title, "url": urls[i] if i < len(urls) else None} for i, title in enumerate(titles)]
        rows = payload.get("items", []) if isinstance(payload, dict) else []
        if source == "github":
            keys = ("full_name", "html_url", "description", "stargazers_count", "language", "license", "updated_at")
        else:
            keys = ("repo_name", "short_description", "star_count", "pull_count", "last_updated")
        return [{key: row.get(key) for key in keys} for row in rows]
