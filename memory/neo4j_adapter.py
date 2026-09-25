"""Neo4j graph projection adapter for AI Village Memory.

Features:
- Implements ProjectionBackend interface for asynchronous outbox processing.
- Graph schema: Agent, Memory, Task, Artifact with typed, status-bearing relationships
  (observed, claimed, inferred).
- Standard library HTTP client for Neo4j Cypher transactional endpoint (no external driver dependencies).
- Parameterized Cypher statements (no string interpolation or injection).
- Author and scope access control for provenance queries.
- Error isolation and backoff compatibility.
- In-memory graph simulation backend for offline unit testing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from memory.projection import ProjectionBackend

logger = logging.getLogger("memory.neo4j")


class Neo4jError(Exception):
    """Base exception for Neo4j graph adapter operations."""

    pass


class Neo4jConnectionError(Neo4jError):
    """Raised when Neo4j service is unreachable or network connection fails."""

    pass


class InMemoryGraphStore:
    """In-memory graph store for fast offline unit testing and parameter verification.

    Stores nodes and directed edges with properties and enforces parameterization.
    """

    def __init__(self) -> None:
        self.nodes: Dict[str, Dict[str, Dict[str, Any]]] = {
            "Agent": {},
            "Memory": {},
            "Task": {},
            "Artifact": {},
        }
        # Edges stored as list of tuples: (source_label, source_key, rel_type, target_label, target_key, properties)
        self.edges: List[Dict[str, Any]] = []

    def clear(self) -> None:
        """Reset all nodes and edges in the in-memory graph."""
        for label in self.nodes:
            self.nodes[label].clear()
        self.edges.clear()

    def upsert_node(self, label: str, key_prop: str, key_val: str, props: Dict[str, Any]) -> None:
        """Upsert a node with properties."""
        if label not in self.nodes:
            self.nodes[label] = {}
        if key_val not in self.nodes[label]:
            self.nodes[label][key_val] = {key_prop: key_val}
        self.nodes[label][key_val].update(props)

    def delete_node(self, label: str, key_val: str) -> None:
        """Delete a node and all connected edges."""
        if label in self.nodes and key_val in self.nodes[label]:
            del self.nodes[label][key_val]
        # Remove attached edges
        self.edges = [
            e
            for e in self.edges
            if not (
                (e["source_label"] == label and e["source_id"] == key_val)
                or (e["target_label"] == label and e["target_id"] == key_val)
            )
        ]

    def add_edge(
        self,
        source_label: str,
        source_id: str,
        rel_type: str,
        target_label: str,
        target_id: str,
        props: Dict[str, Any],
    ) -> None:
        """Add or update a directed relationship between two nodes."""
        for e in self.edges:
            if (
                e["source_label"] == source_label
                and e["source_id"] == source_id
                and e["rel_type"] == rel_type
                and e["target_label"] == target_label
                and e["target_id"] == target_id
            ):
                e["props"].update(props)
                return
        self.edges.append(
            {
                "source_label": source_label,
                "source_id": source_id,
                "rel_type": rel_type,
                "target_label": target_label,
                "target_id": target_id,
                "props": dict(props),
            }
        )


class Neo4jProjectionAdapter(ProjectionBackend):
    """Neo4j graph projection adapter implementing the transactional projection queue backend."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth: Optional[Tuple[str, str]] = None,
        timeout: float = 10.0,
        in_memory_store: Optional[InMemoryGraphStore] = None,
    ) -> None:
        """Initialize Neo4j adapter.

        Args:
            base_url: Neo4j HTTP base URL (e.g. http://127.0.0.1:7474).
            auth: Tuple of (username, password).
            timeout: HTTP request timeout in seconds.
            in_memory_store: Optional InMemoryGraphStore for offline mock testing.
        """
        self.base_url = (base_url or os.environ.get("NEO4J_URL", "http://127.0.0.1:7474")).rstrip("/")
        self.timeout = timeout
        self._in_memory = in_memory_store

        if auth:
            self._auth_header = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        else:
            raw_env_auth = os.environ.get("NEO4J_AUTH", "neo4j/password")
            if "/" in raw_env_auth:
                u, p = raw_env_auth.split("/", 1)
            elif ":" in raw_env_auth:
                u, p = raw_env_auth.split(":", 1)
            else:
                u, p = "neo4j", raw_env_auth
            self._auth_header = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()

    @property
    def name(self) -> str:
        """Return backend identifier for projection queue tracking."""
        return "neo4j"

    def is_healthy(self) -> bool:
        """Check if Neo4j graph service is reachable and responsive."""
        if self._in_memory is not None:
            return True

        req = urllib.request.Request(
            f"{self.base_url}/db/neo4j/tx/commit",
            data=json.dumps({"statements": [{"statement": "RETURN 1"}]}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": self._auth_header,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def execute_cypher(
        self,
        statement: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Execute a parameterized Cypher statement via HTTP transactional endpoint.

        Args:
            statement: Parameterized Cypher query string.
            parameters: Dictionary of query parameters.

        Returns:
            List of row dicts mapped to returned column names.
        """
        params = parameters or {}

        # If in-memory test store is active, route through mock dispatcher
        if self._in_memory is not None:
            return self._mock_execute(statement, params)

        endpoint = f"{self.base_url}/db/neo4j/tx/commit"
        payload = {
            "statements": [
                {
                    "statement": statement,
                    "parameters": params,
                }
            ]
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": self._auth_header,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise Neo4jConnectionError(f"Failed to connect to Neo4j at {endpoint}: {exc}") from exc
        except Exception as exc:
            raise Neo4jError(f"HTTP transaction error: {exc}") from exc

        errors = resp_data.get("errors", [])
        if errors:
            raise Neo4jError(f"Neo4j Cypher error: {errors[0].get('message', 'Unknown error')}")

        results = resp_data.get("results", [])
        if not results:
            return []

        result_entry = results[0]
        columns = result_entry.get("columns", [])
        data_rows = result_entry.get("data", [])
        output = []
        for d in data_rows:
            row_vals = d.get("row", [])
            output.append(dict(zip(columns, row_vals)))
        return output

    def upsert(self, memory: Dict[str, Any]) -> None:
        """Upsert a memory and its observed/inferred relationships into the knowledge graph.

        Nodes:
        - (Agent {name})
        - (Memory {id, content, scope, kind, confidence, created_at, expires_at, source_event})
        - Optional: (Task {id})
        - Optional: (Artifact {id})

        Edges:
        - (Agent)-[:AUTHORED {status: 'observed', created_at}]->(Memory)
        - (Memory)-[:REFERENCES_TASK {status: 'inferred'}]->(Task)
        - (Memory)-[:REFERENCES_ARTIFACT {status: 'inferred'}]->(Artifact)
        """
        mem_id = str(memory["id"])
        agent = str(memory.get("agent", "unknown"))
        content = str(memory.get("content", ""))
        scope = str(memory.get("scope", "private"))
        kind = str(memory.get("kind", "observation"))
        confidence = float(memory.get("confidence", 0.5))
        created_at = str(memory.get("created_at", ""))
        expires_at = str(memory.get("expires_at", "") or "")
        source_event = str(memory.get("source_event", "") or "")

        meta = memory.get("metadata", {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}

        task_id = meta.get("task_id") or memory.get("task_id")
        artifact_id = meta.get("artifact_id") or memory.get("artifact_id")

        # 1. Parameterized Cypher to upsert Agent, Memory, and AUTHORED relationship
        statement = """
        MERGE (a:Agent {name: $agent})
        MERGE (m:Memory {id: $id})
        SET m.content = $content,
            m.scope = $scope,
            m.kind = $kind,
            m.confidence = $confidence,
            m.created_at = $created_at,
            m.expires_at = $expires_at,
            m.source_event = $source_event
        MERGE (a)-[r:AUTHORED]->(m)
        SET r.status = 'observed', r.created_at = $created_at
        """
        self.execute_cypher(
            statement,
            {
                "agent": agent,
                "id": mem_id,
                "content": content,
                "scope": scope,
                "kind": kind,
                "confidence": confidence,
                "created_at": created_at,
                "expires_at": expires_at,
                "source_event": source_event,
            },
        )

        # 2. Inferred relationship to Task if referenced
        if task_id:
            task_cypher = """
            MATCH (m:Memory {id: $id})
            MERGE (t:Task {id: $task_id})
            MERGE (m)-[r:REFERENCES_TASK]->(t)
            SET r.status = 'inferred'
            """
            self.execute_cypher(task_cypher, {"id": mem_id, "task_id": str(task_id)})

        # 3. Inferred relationship to Artifact if referenced
        if artifact_id:
            art_cypher = """
            MATCH (m:Memory {id: $id})
            MERGE (art:Artifact {id: $artifact_id})
            MERGE (m)-[r:REFERENCES_ARTIFACT]->(art)
            SET r.status = 'inferred'
            """
            self.execute_cypher(art_cypher, {"id": mem_id, "artifact_id": str(artifact_id)})

    def delete(self, memory_id: str, tombstone: Dict[str, Any]) -> None:
        """Delete a memory node and its attached relationships from the graph."""
        statement = """
        MATCH (m:Memory {id: $id})
        DETACH DELETE m
        """
        self.execute_cypher(statement, {"id": str(memory_id)})

    def clear(self) -> None:
        """Clear all nodes and edges from the graph database for full rebuild."""
        statement = "MATCH (n) DETACH DELETE n"
        self.execute_cypher(statement, {})

    def get_provenance(self, memory_id: str, caller_agent: str) -> Optional[Dict[str, Any]]:
        """Retrieve verified provenance chain for a memory item with scope access checks.

        Args:
            memory_id: Target memory identifier.
            caller_agent: Requesting agent identifier (or '*' for admin).

        Returns:
            Dictionary with author, relationships, and metadata, or None if unauthorized/missing.
        """
        statement = """
        MATCH (a:Agent)-[r:AUTHORED]->(m:Memory {id: $id})
        OPTIONAL MATCH (m)-[:REFERENCES_TASK]->(t:Task)
        OPTIONAL MATCH (m)-[:REFERENCES_ARTIFACT]->(art:Artifact)
        RETURN a.name AS agent, m.id AS id, m.scope AS scope, m.kind AS kind,
               m.content AS content, m.confidence AS confidence, m.created_at AS created_at,
               m.source_event AS source_event, t.id AS task_id, art.id AS artifact_id,
               r.status AS author_relation_status
        """
        rows = self.execute_cypher(statement, {"id": str(memory_id)})
        if not rows:
            return None

        row = rows[0]
        # Enforce scope check: private memories only visible to author or admin
        if row.get("scope") == "private" and caller_agent not in (row.get("agent"), "*"):
            return None

        return {
            "id": row.get("id"),
            "agent": row.get("agent"),
            "scope": row.get("scope"),
            "kind": row.get("kind"),
            "content": row.get("content"),
            "confidence": row.get("confidence"),
            "created_at": row.get("created_at"),
            "source_event": row.get("source_event"),
            "task_id": row.get("task_id"),
            "artifact_id": row.get("artifact_id"),
            "author_relation_status": row.get("author_relation_status", "observed"),
        }

    def _mock_execute(self, statement: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Mock dispatcher for in-memory graph operations during unit testing."""
        assert self._in_memory is not None
        stmt = statement.strip()

        if "RETURN 1" in stmt:
            return [{"1": 1}]

        if "DETACH DELETE n" in stmt:
            self._in_memory.clear()
            return []

        if "DETACH DELETE m" in stmt and "$id" in stmt:
            self._in_memory.delete_node("Memory", params["id"])
            return []

        if "MERGE (a:Agent" in stmt and "MERGE (m:Memory" in stmt:
            agent = params["agent"]
            mem_id = params["id"]
            self._in_memory.upsert_node("Agent", "name", agent, {})
            self._in_memory.upsert_node(
                "Memory",
                "id",
                mem_id,
                {
                    "content": params.get("content", ""),
                    "scope": params.get("scope", "private"),
                    "kind": params.get("kind", "observation"),
                    "confidence": params.get("confidence", 0.5),
                    "created_at": params.get("created_at", ""),
                    "expires_at": params.get("expires_at", ""),
                    "source_event": params.get("source_event", ""),
                },
            )
            self._in_memory.add_edge(
                "Agent",
                agent,
                "AUTHORED",
                "Memory",
                mem_id,
                {"status": "observed", "created_at": params.get("created_at", "")},
            )
            return []

        if "MERGE (m)-[r:REFERENCES_TASK]" in stmt:
            mem_id = params["id"]
            task_id = params["task_id"]
            self._in_memory.upsert_node("Task", "id", task_id, {})
            self._in_memory.add_edge("Memory", mem_id, "REFERENCES_TASK", "Task", task_id, {"status": "inferred"})
            return []

        if "MERGE (m)-[r:REFERENCES_ARTIFACT]" in stmt:
            mem_id = params["id"]
            art_id = params["artifact_id"]
            self._in_memory.upsert_node("Artifact", "id", art_id, {})
            self._in_memory.add_edge("Memory", mem_id, "REFERENCES_ARTIFACT", "Artifact", art_id, {"status": "inferred"})
            return []

        if "OPTIONAL MATCH (m)-[:REFERENCES_TASK]" in stmt:
            mem_id = params["id"]
            mem_node = self._in_memory.nodes["Memory"].get(mem_id)
            if not mem_node:
                return []
            # Find author
            author = None
            author_status = "observed"
            for e in self._in_memory.edges:
                if e["target_label"] == "Memory" and e["target_id"] == mem_id and e["rel_type"] == "AUTHORED":
                    author = e["source_id"]
                    author_status = e["props"].get("status", "observed")
                    break

            # Find task
            task_id = None
            for e in self._in_memory.edges:
                if e["source_label"] == "Memory" and e["source_id"] == mem_id and e["rel_type"] == "REFERENCES_TASK":
                    task_id = e["target_id"]
                    break

            # Find artifact
            art_id = None
            for e in self._in_memory.edges:
                if e["source_label"] == "Memory" and e["source_id"] == mem_id and e["rel_type"] == "REFERENCES_ARTIFACT":
                    art_id = e["target_id"]
                    break

            return [
                {
                    "agent": author,
                    "id": mem_id,
                    "scope": mem_node.get("scope"),
                    "kind": mem_node.get("kind"),
                    "content": mem_node.get("content"),
                    "confidence": mem_node.get("confidence"),
                    "created_at": mem_node.get("created_at"),
                    "source_event": mem_node.get("source_event"),
                    "task_id": task_id,
                    "artifact_id": art_id,
                    "author_relation_status": author_status,
                }
            ]

        return []


def create_neo4j_adapter(
    base_url: Optional[str] = None,
    auth: Optional[Tuple[str, str]] = None,
    in_memory: bool = False,
) -> Neo4jProjectionAdapter:
    """Factory creating Neo4jProjectionAdapter from explicit parameters or environment variables."""
    if in_memory:
        return Neo4jProjectionAdapter(in_memory_store=InMemoryGraphStore())

    url = base_url or os.environ.get("NEO4J_URL", "http://127.0.0.1:7474")
    return Neo4jProjectionAdapter(base_url=url, auth=auth)
