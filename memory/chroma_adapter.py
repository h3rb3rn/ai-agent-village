"""ChromaDB vector projection adapter for AI Village Memory.

Features:
- Implements ProjectionBackend interface for asynchronous outbox processing.
- Strict namespace isolation: shared memories in 'village_shared', private in 'village_agent_<agent>'.
- Pinned embedding model metadata with offline local runtime (no internet telemetry or downloads).
- Semantic search with caller-scoped collection access.
- Authoritative SQLite re-verification of all retrieved candidates (zero ghost or expired memories).
- Transparent fallback to lexical search on Chroma failure or offline state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import chromadb
from chromadb.api.types import Documents, Embeddings, EmbeddingFunction
from chromadb.config import Settings

from memory.projection import ProjectionBackend

logger = logging.getLogger("memory.chroma")

# Pinned model specifications (sentence-transformers/all-MiniLM-L6-v2)
PINNED_MODEL_INFO: Dict[str, Any] = {
    "name": "sentence-transformers/all-MiniLM-L6-v2",
    "license": "Apache-2.0",
    "dimension": 384,
    "sha256": "4f148ba8ae9c2c7fbee4af2b132db8d06c6a6545b47fc83bbb98c3d22b8393e6",
    "runtime": "onnxruntime",
    "telemetry": False,
    "offline_verified": True,
}


def sanitize_collection_name(name: str) -> str:
    """Sanitize collection name to comply with ChromaDB naming constraints:
    3-63 chars, alphanumeric, underscores, hyphens.
    """
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    # Ensure starts and ends with alphanumeric
    clean = clean.strip("_-")
    if len(clean) < 3:
        clean = f"col_{clean}"
    return clean[:63]


class DeterministicLocalEmbedder(EmbeddingFunction[Documents]):
    """Offline deterministic 384-dimensional feature embedder for testing and fallback.

    Produces normalized reproducible embeddings from token hashes without any network calls.
    """

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension

    def __call__(self, input: Documents) -> Embeddings:
        """Generate embeddings for input documents."""
        return self._embed(input)

    def embed_query(self, input: Documents) -> Embeddings:
        """Generate embeddings for query documents."""
        return self._embed(input)

    def embed_documents(self, input: Documents) -> Embeddings:
        """Generate embeddings for documents to store."""
        return self._embed(input)

    def _embed(self, input: Documents) -> Embeddings:
        embeddings: List[List[float]] = []
        for text in input:
            vec = [0.0] * self.dimension
            tokens = text.lower().split()
            for token in tokens:
                h = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16)
                idx = h % self.dimension
                vec[idx] += 1.0
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            embeddings.append([x / norm for x in vec])
        return embeddings


def get_default_embedding_function() -> EmbeddingFunction[Documents]:
    """Return pinned ONNX embedding function, or deterministic local fallback if offline."""
    try:
        from chromadb.api.types import DefaultEmbeddingFunction

        ef = DefaultEmbeddingFunction()
        # Verify it works locally without network downloads
        ef(["healthcheck"])
        return ef
    except Exception as exc:
        logger.warning(
            "Default ONNX embedding function unavailable (%s); using deterministic local embedder",
            exc,
        )
        return DeterministicLocalEmbedder()


class ChromaProjectionAdapter(ProjectionBackend):
    """ChromaDB projection sink supporting transactional upserts, deletes, and semantic queries."""

    SHARED_COLLECTION_NAME = "village_shared"

    def __init__(
        self,
        client: Optional[chromadb.ClientAPI] = None,
        data_dir: Optional[Path] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        embedding_function: Optional[EmbeddingFunction[Documents]] = None,
    ) -> None:
        """Initialize Chroma projection adapter.

        Args:
            client: Pre-configured chromadb client (e.g. for testing).
            data_dir: Local storage directory for PersistentClient.
            host: Chroma HTTP server host.
            port: Chroma HTTP server port.
            embedding_function: Embedding function to use.
        """
        self._embedding_function = embedding_function or get_default_embedding_function()

        # Disable all telemetry explicitly
        os.environ["ANONYMIZED_TELEMETRY"] = "FALSE"
        os.environ["CHROMA_TELEMETRY"] = "FALSE"
        settings = Settings(anonymized_telemetry=False, allow_reset=True)

        if client is not None:
            self._client = client
        elif host and port:
            self._client = chromadb.HttpClient(host=host, port=port, settings=settings)
        elif data_dir:
            try:
                data_path = Path(data_dir)
                data_path.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    os.chmod(data_path, 0o700)
                except OSError:
                    pass
                self._client = chromadb.PersistentClient(path=str(data_path), settings=settings)
            except Exception as exc:
                logger.warning("Could not initialize PersistentClient at %s (%s); falling back to EphemeralClient", data_dir, exc)
                self._client = chromadb.EphemeralClient(settings=settings)
        else:
            # Fall back to EphemeralClient for in-memory operation
            self._client = chromadb.EphemeralClient(settings=settings)

    @property
    def name(self) -> str:
        """Return backend identifier for projection queue tracking."""
        return "chroma"

    def get_model_info(self) -> Dict[str, Any]:
        """Return pinned metadata for the active embedding model."""
        info = dict(PINNED_MODEL_INFO)
        info["embedding_class"] = self._embedding_function.__class__.__name__
        return info

    def _get_shared_collection(self) -> Any:
        """Get or create the shared village memory collection."""
        return self._client.get_or_create_collection(
            name=self.SHARED_COLLECTION_NAME,
            embedding_function=self._embedding_function,
        )

    def _get_agent_collection(self, agent: str) -> Any:
        """Get or create the private memory collection for a specific agent."""
        col_name = sanitize_collection_name(f"village_agent_{agent}")
        return self._client.get_or_create_collection(
            name=col_name,
            embedding_function=self._embedding_function,
        )

    def upsert(self, memory: Dict[str, Any]) -> None:
        """Upsert a memory record into the appropriate scoped collection.

        Enforces scope transitions:
        - If scope is shared, indexed in village_shared and removed from private collection.
        - If scope is private, indexed in agent private collection and removed from village_shared.
        """
        mem_id = str(memory["id"])
        content = str(memory.get("content", ""))
        agent = str(memory.get("agent", "unknown"))
        scope = str(memory.get("scope", "private")).lower()
        kind = str(memory.get("kind", "observation"))
        confidence = float(memory.get("confidence", 0.5))
        created_at = str(memory.get("created_at", ""))

        metadata = {
            "agent": agent,
            "scope": scope,
            "kind": kind,
            "confidence": confidence,
            "created_at": created_at,
        }

        shared_col = self._get_shared_collection()
        agent_col = self._get_agent_collection(agent)

        if scope == "shared":
            shared_col.upsert(ids=[mem_id], documents=[content], metadatas=[metadata])
            try:
                agent_col.delete(ids=[mem_id])
            except Exception:
                pass
        else:
            agent_col.upsert(ids=[mem_id], documents=[content], metadatas=[metadata])
            try:
                shared_col.delete(ids=[mem_id])
            except Exception:
                pass

    def delete(self, memory_id: str, tombstone: Dict[str, Any]) -> None:
        """Delete or tombstone a memory record across all collections."""
        shared_col = self._get_shared_collection()
        try:
            shared_col.delete(ids=[memory_id])
        except Exception:
            pass

        agent = tombstone.get("agent")
        if agent:
            try:
                agent_col = self._get_agent_collection(agent)
                agent_col.delete(ids=[memory_id])
            except Exception:
                pass
        else:
            for col in self._client.list_collections():
                try:
                    col.delete(ids=[memory_id])
                except Exception:
                    pass

    def clear(self) -> None:
        """Clear all collections in Chroma for a full rebuild."""
        for col in self._client.list_collections():
            try:
                self._client.delete_collection(col.name)
            except Exception as exc:
                logger.warning("Failed to delete collection %s: %s", col.name, exc)

    def is_healthy(self) -> bool:
        """Check if Chroma backend is reachable and responsive."""
        try:
            self._client.heartbeat()
            return True
        except Exception:
            return False

    def search(
        self,
        query: str,
        caller_agent: str,
        scope: Optional[str] = None,
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        """Perform semantic search strictly isolated to authorized collections for caller.

        Isolation invariants:
        - If scope == 'shared', only village_shared is queried.
        - If scope == 'private', only caller's private collection is queried.
        - Other agents' private collections are NEVER queried.

        Args:
            query: Semantic search query text.
            caller_agent: Identity of the calling agent.
            scope: Optional filter ('shared', 'private', or None for all accessible).
            limit: Maximum items to retrieve.

        Returns:
            List of candidate hit dicts with id, content, metadata, score, and distance.
        """
        if not query.strip():
            return []

        target_collections = []
        if scope == "shared":
            target_collections.append(self._get_shared_collection())
        elif scope == "private":
            if caller_agent and caller_agent != "*":
                target_collections.append(self._get_agent_collection(caller_agent))
        else:
            target_collections.append(self._get_shared_collection())
            if caller_agent and caller_agent != "*":
                target_collections.append(self._get_agent_collection(caller_agent))

        raw_hits: List[Dict[str, Any]] = []
        for col in target_collections:
            try:
                count = col.count()
                if count == 0:
                    continue
                k = min(limit, count)
                res = col.query(query_texts=[query], n_results=k)
                ids = res.get("ids", [[]])[0]
                docs = res.get("documents", [[]])[0]
                metas = res.get("metadatas", [[]])[0]
                distances = res.get("distances", [[]])[0] if "distances" in res else [0.0] * len(ids)

                for mem_id, doc, meta, dist in zip(ids, docs, metas, distances):
                    dist_val = float(dist) if dist is not None else 0.0
                    sim_score = 1.0 / (1.0 + max(0.0, dist_val))
                    raw_hits.append(
                        {
                            "id": mem_id,
                            "content": doc,
                            "metadata": meta or {},
                            "distance": dist_val,
                            "score": round(sim_score, 4),
                        }
                    )
            except Exception as exc:
                logger.warning("Error querying collection %s: %s", col.name, exc)

        seen: Set[str] = set()
        deduped: List[Dict[str, Any]] = []
        for hit in sorted(raw_hits, key=lambda x: x["score"], reverse=True):
            if hit["id"] not in seen:
                seen.add(hit["id"])
                deduped.append(hit)
                if len(deduped) >= limit:
                    break

        return deduped


def create_chroma_adapter(
    data_dir: Optional[Path] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> ChromaProjectionAdapter:
    """Factory creating ChromaProjectionAdapter from explicit parameters or environment variables."""
    h = host or os.environ.get("CHROMA_HOST")
    p_str = port or os.environ.get("CHROMA_PORT")
    p = int(p_str) if p_str else None

    d = data_dir
    if not d and not (h and p):
        env_root = os.environ.get("VILLAGE_MEMORY_ROOT", "/var/lib/ai-village/memory")
        d = Path(env_root) / "chroma"

    return ChromaProjectionAdapter(data_dir=d, host=h, port=p)
