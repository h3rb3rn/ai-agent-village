#!/usr/bin/env python3
"""Secure, ACID-compliant SQLite memory gateway for AI Village.

Provides authoritative journal storage with lexical retrieval, private/shared scope
isolation, atomic write quotas, fail-closed credential validation, and full CRUD operations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("memory.gateway")

try:
    from memory.projection import record_outbox_event
except ImportError:
    try:
        from projection import record_outbox_event
    except ImportError:
        def record_outbox_event(conn, memory_id, operation, payload):
            now_str = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                "INSERT INTO memory_outbox (memory_id, operation, payload, created_at) VALUES (?, ?, ?, ?)",
                (memory_id, operation, json.dumps(payload, ensure_ascii=False), now_str),
            )
            return cursor.lastrowid or 0

ROOT = Path(os.environ.get("VILLAGE_ROOT", "/var/lib/ai-village"))
DB = Path(os.environ.get("MEMORY_DB", ROOT / "memory" / "memory.sqlite3"))
HOST = os.environ.get("MEMORY_BIND", "127.0.0.1")
PORT = int(os.environ.get("MEMORY_PORT", "8090"))
TOKEN = os.environ.get("MEMORY_GATEWAY_TOKEN", "")
TOKENS_FILE = Path(os.environ.get("MEMORY_AGENT_TOKENS_FILE", "/etc/ai-village/memory-agent-tokens.json"))
MAX_CONTENT = int(os.environ.get("MEMORY_MAX_CONTENT_CHARS", "12000"))
MAX_RESULTS = int(os.environ.get("MEMORY_MAX_RESULTS", "12"))
RATE_LIMIT = int(os.environ.get("MEMORY_WRITES_PER_HOUR", "120"))

# Active ChromaDB projection adapter (lazily initialized or injected for testing)
_CHROMA_ADAPTER: Optional[Any] = None


def get_chroma_adapter() -> Optional[Any]:
    """Return active ChromaProjectionAdapter instance, or None if disabled/unavailable."""
    global _CHROMA_ADAPTER
    if os.environ.get("CHROMA_ENABLED", "1").lower() in ("0", "false", "no", "disabled"):
        return None
    if _CHROMA_ADAPTER is not None:
        return _CHROMA_ADAPTER
    try:
        from memory.chroma_adapter import create_chroma_adapter

        # Use chroma directory colocated with the active database
        chroma_dir = DB.parent / "chroma"
        _CHROMA_ADAPTER = create_chroma_adapter(data_dir=chroma_dir)
        return _CHROMA_ADAPTER
    except Exception as exc:
        logger.warning("Failed to initialize Chroma adapter: %s", exc)
        return None


def set_chroma_adapter(adapter: Optional[Any]) -> None:
    """Set or override active ChromaProjectionAdapter (for testing or reconfiguration)."""
    global _CHROMA_ADAPTER
    _CHROMA_ADAPTER = adapter


# Active Neo4j graph projection adapter (lazily initialized or injected for testing)
_NEO4J_ADAPTER: Optional[Any] = None


def get_neo4j_adapter() -> Optional[Any]:
    """Return active Neo4jProjectionAdapter instance, or None if disabled/unavailable."""
    global _NEO4J_ADAPTER
    if os.environ.get("NEO4J_ENABLED", "1").lower() in ("0", "false", "no", "disabled"):
        return None
    if _NEO4J_ADAPTER is not None:
        return _NEO4J_ADAPTER
    try:
        from memory.neo4j_adapter import create_neo4j_adapter

        _NEO4J_ADAPTER = create_neo4j_adapter()
        return _NEO4J_ADAPTER
    except Exception as exc:
        logger.warning("Failed to initialize Neo4j adapter: %s", exc)
        return None


def set_neo4j_adapter(adapter: Optional[Any]) -> None:
    """Set or override active Neo4jProjectionAdapter (for testing or reconfiguration)."""
    global _NEO4J_ADAPTER
    _NEO4J_ADAPTER = adapter


def now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db() -> sqlite3.Connection:
    """Open or initialize authoritative memory SQLite database with secure permissions."""
    db_dir = DB.parent
    db_dir.mkdir(parents=True, exist_ok=True)
    try:
        db_dir.chmod(0o700)
    except OSError:
        pass

    conn = sqlite3.connect(str(DB), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            agent TEXT NOT NULL,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            source_event TEXT,
            confidence REAL NOT NULL,
            expires_at TEXT,
            metadata TEXT NOT NULL,
            idempotency_key TEXT UNIQUE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS memories_agent_time ON memories(agent, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS memories_expires ON memories(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS memories_idempotency ON memories(idempotency_key)")

    # P15: Transactional outbox and projection state tracking
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_outbox (
            sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_seq ON memory_outbox(sequence_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projection_states (
            backend TEXT PRIMARY KEY,
            last_sequence_id INTEGER NOT NULL DEFAULT 0,
            last_projected_at TEXT,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )
    conn.commit()

    if DB.exists():
        try:
            DB.chmod(0o600)
        except OSError:
            pass

    return conn


def extract_tokens(text: str) -> Set[str]:
    """Tokenize text into lowercase alphanumeric keywords."""
    return set(re.findall(r"[a-z0-9äöüß_-]{3,}", text.lower()))


def caller(handler: BaseHTTPRequestHandler) -> Optional[str]:
    """Resolve caller identity from Authorization Bearer token.

    Returns:
        Optional[str]: '*' for admin/master token, agent ID for agent token, or None.
    """
    raw_header = handler.headers.get("Authorization", "")
    if not raw_header.startswith("Bearer "):
        return None
    supplied = raw_header[7:].strip()
    if not supplied:
        return None

    # Master token grants admin '*' access
    if TOKEN and supplied == TOKEN:
        return "*"

    # Check agent tokens file
    if TOKENS_FILE.exists():
        try:
            tokens_data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
            if isinstance(tokens_data, dict):
                for agent_id, token_val in tokens_data.items():
                    if token_val == supplied:
                        return agent_id
        except (OSError, ValueError):
            return None

    return None


def auth(handler: BaseHTTPRequestHandler) -> bool:
    """Validate that caller is properly authenticated (FAIL-CLOSED)."""
    return caller(handler) is not None


def json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    """Parse and validate JSON request body with strict size bounds and UTF-8 verification."""
    length_header = handler.headers.get("Content-Length")
    if length_header is None:
        raise ValueError("Missing Content-Length header")
    try:
        length = int(length_header)
    except ValueError:
        raise ValueError("Invalid Content-Length header")

    if length <= 0 or length > (MAX_CONTENT * 4 + 8192):
        raise ValueError(f"Invalid body size: {length} bytes")

    raw_bytes = handler.rfile.read(length)
    try:
        decoded_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Malformed UTF-8 encoding: {exc}")

    try:
        value = json.loads(decoded_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON syntax: {exc}")

    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def format_memory_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Format SQLite row into API response dictionary."""
    item = dict(row)
    try:
        item["metadata"] = json.loads(item["metadata"])
    except (ValueError, TypeError):
        item["metadata"] = {}
    return item


def is_expired(expires_at: Optional[str]) -> bool:
    """Check if an expiration timestamp is in the past."""
    if not expires_at:
        return False
    try:
        exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > exp_dt
    except (ValueError, TypeError):
        return False


class Handler(BaseHTTPRequestHandler):
    """HTTP request handler for memory gateway."""

    def log_message(self, *_):
        """Suppress default stdout logging."""
        pass

    def send_json(self, status: int, value: Dict[str, Any]):
        """Send formatted JSON HTTP response."""
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Handle GET requests."""
        route = urlsplit(self.path).path

        # 1. Unauthenticated healthcheck
        if route == "/healthz":
            return self.send_json(200, {"ok": True, "backend": "sqlite-authoritative"})

        # 2. Local stats check
        local_stats = route == "/v1/stats" and self.client_address[0] in ("127.0.0.1", "::1")
        if not local_stats and not auth(self):
            return self.send_json(401, {"error": "unauthorized"})

        owner = caller(self)

        # 3. List memories
        if route == "/v1/memories":
            try:
                conn = db()
                now_str = now()
                # Exclude expired memories
                if owner in (None, "*"):
                    rows = conn.execute(
                        """
                        SELECT * FROM memories
                        WHERE (expires_at IS NULL OR expires_at = '' OR expires_at > ?)
                        ORDER BY created_at DESC LIMIT ?
                        """,
                        (now_str, MAX_RESULTS),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM memories
                        WHERE (agent = ? OR scope = 'shared')
                          AND (expires_at IS NULL OR expires_at = '' OR expires_at > ?)
                        ORDER BY created_at DESC LIMIT ?
                        """,
                        (owner, now_str, MAX_RESULTS),
                    ).fetchall()
                conn.close()
                return self.send_json(200, {"items": [format_memory_row(r) for r in rows]})
            except sqlite3.OperationalError as exc:
                return self.send_json(500, {"error": f"Database error: {exc}"})

        # 4. Get memory provenance: /v1/memories/<id>/provenance
        if route.startswith("/v1/memories/") and route.endswith("/provenance"):
            prefix_len = len("/v1/memories/")
            suffix_len = len("/provenance")
            mem_id = route[prefix_len:-suffix_len].strip("/")
            if not mem_id:
                return self.send_json(400, {"error": "memory id required"})

            # Attempt graph provenance retrieval via Neo4j
            neo4j = get_neo4j_adapter()
            if neo4j and neo4j.is_healthy():
                try:
                    prov = neo4j.get_provenance(mem_id, caller_agent=owner or "anonymous")
                    if prov is not None:
                        return self.send_json(200, {"id": mem_id, "provenance": prov, "backend": "neo4j-graph"})
                except Exception as exc:
                    logger.warning("Neo4j provenance query failed (%s); falling back to SQLite", exc)

            # Fallback to authoritative SQLite provenance
            try:
                conn = db()
                row = conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
                conn.close()
                if not row:
                    return self.send_json(404, {"error": "memory not found"})
                if owner not in (None, "*") and row["agent"] != owner and row["scope"] != "shared":
                    return self.send_json(403, {"error": "access denied to private memory"})
                if is_expired(row["expires_at"]):
                    return self.send_json(410, {"error": "memory expired"})

                prov_fallback = {
                    "id": mem_id,
                    "agent": row["agent"],
                    "scope": row["scope"],
                    "kind": row["kind"],
                    "content": row["content"],
                    "confidence": float(row["confidence"]),
                    "created_at": row["created_at"],
                    "source_event": row["source_event"],
                    "author_relation_status": "observed",
                }
                return self.send_json(200, {"id": mem_id, "provenance": prov_fallback, "backend": "sqlite-provenance-fallback"})
            except sqlite3.OperationalError as exc:
                return self.send_json(500, {"error": f"Database error: {exc}"})

        # 5. Get single memory by ID: /v1/memories/<id>
        if route.startswith("/v1/memories/"):
            mem_id = route[len("/v1/memories/") :].strip()
            if not mem_id:
                return self.send_json(400, {"error": "memory id required"})
            try:
                conn = db()
                row = conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
                conn.close()
                if not row:
                    return self.send_json(404, {"error": "memory not found"})
                if owner not in (None, "*") and row["agent"] != owner and row["scope"] != "shared":
                    return self.send_json(403, {"error": "access denied to private memory"})
                if is_expired(row["expires_at"]):
                    return self.send_json(410, {"error": "memory expired"})
                return self.send_json(200, format_memory_row(row))
            except sqlite3.OperationalError as exc:
                return self.send_json(500, {"error": f"Database error: {exc}"})

        # 5. Stats
        if route == "/v1/stats":
            try:
                conn = db()
                rows = conn.execute(
                    """
                    SELECT agent, count(*) AS memories, coalesce(sum(length(content)), 0) AS chars, max(created_at) AS last_at
                    FROM memories GROUP BY agent ORDER BY agent
                    """
                ).fetchall()
                conn.close()
                return self.send_json(
                    200,
                    {
                        "agents": [dict(r) for r in rows],
                        "total": sum(r["memories"] for r in rows),
                        "chars": sum(r["chars"] for r in rows),
                    },
                )
            except sqlite3.OperationalError as exc:
                return self.send_json(500, {"error": f"Database error: {exc}"})

        return self.send_json(404, {"error": "not found"})

    def do_POST(self):
        """Handle POST requests."""
        if not auth(self):
            return self.send_json(401, {"error": "unauthorized"})

        route = urlsplit(self.path).path
        owner = caller(self)

        try:
            value = json_body(self)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"error": str(exc)})

        # 1. Create memory: /v1/memories
        if route == "/v1/memories":
            content = str(value.get("content", "")).strip()
            # Derive agent identity from credential when not admin
            payload_agent = str(value.get("agent", "")).strip()
            if owner not in (None, "*"):
                if payload_agent and payload_agent != owner:
                    return self.send_json(403, {"error": "token is bound to another agent"})
                agent = owner
            else:
                agent = payload_agent or "system"

            scope = str(value.get("scope", "private")).strip()
            if scope not in ("private", "shared"):
                return self.send_json(400, {"error": "scope must be private or shared"})

            if not content or len(content) > MAX_CONTENT:
                return self.send_json(400, {"error": f"content must be between 1 and {MAX_CONTENT} chars"})

            if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", agent):
                return self.send_json(400, {"error": "invalid agent identifier"})

            # Validate confidence (must be float between 0.0 and 1.0, not NaN/Inf)
            raw_conf = value.get("confidence", 0.5)
            try:
                conf = float(raw_conf)
                if math.isnan(conf) or math.isinf(conf):
                    return self.send_json(400, {"error": "confidence must be a finite number between 0.0 and 1.0"})
                conf = max(0.0, min(1.0, conf))
            except (ValueError, TypeError):
                return self.send_json(400, {"error": "invalid confidence value"})

            # Validate optional expiration timestamp
            expires_at = value.get("expires_at")
            if expires_at:
                expires_at = str(expires_at).strip()
                try:
                    datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                except ValueError:
                    return self.send_json(400, {"error": "invalid expires_at ISO timestamp"})
            else:
                expires_at = None

            idempotency_key = value.get("idempotency_key")
            if idempotency_key:
                idempotency_key = str(idempotency_key)[:128]

            conn = None
            max_retries = 10
            for attempt in range(max_retries):
                try:
                    conn = db()
                    # BEGIN IMMEDIATE transaction to prevent quota race conditions
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower() and attempt < max_retries - 1:
                        if conn:
                            try:
                                conn.close()
                            except Exception:
                                pass
                        time.sleep(0.02 * (attempt + 1))
                        continue
                    if conn:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    return self.send_json(500, {"error": f"Database error: {exc}"})

            try:
                # If idempotency key provided, check if already recorded
                if idempotency_key:
                    existing = conn.execute("SELECT * FROM memories WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
                    if existing:
                        conn.rollback()
                        conn.close()
                        return self.send_json(200, format_memory_row(existing))

                # Check rate limit quota inside immediate transaction
                cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
                count = conn.execute(
                    "SELECT count(*) FROM memories WHERE agent = ? AND created_at >= ?",
                    (agent, cutoff),
                ).fetchone()[0]

                if count >= RATE_LIMIT:
                    conn.rollback()
                    conn.close()
                    return self.send_json(429, {"error": "agent memory write quota exceeded"})

                created = now()
                ident = hashlib.sha256(f"{agent}\0{created}\0{content}".encode()).hexdigest()[:24]
                metadata_str = json.dumps(value.get("metadata", {}), ensure_ascii=False) if isinstance(value.get("metadata"), dict) else "{}"

                item = {
                    "id": ident,
                    "created_at": created,
                    "updated_at": created,
                    "agent": agent,
                    "scope": scope,
                    "kind": str(value.get("kind", "observation"))[:32],
                    "content": content,
                    "source_event": str(value.get("source_event", ""))[:200],
                    "confidence": conf,
                    "expires_at": expires_at,
                    "metadata": metadata_str,
                    "idempotency_key": idempotency_key,
                }

                conn.execute(
                    """
                    INSERT INTO memories (
                        id, created_at, updated_at, agent, scope, kind,
                        content, source_event, confidence, expires_at,
                        metadata, idempotency_key
                    ) VALUES (
                        :id, :created_at, :updated_at, :agent, :scope, :kind,
                        :content, :source_event, :confidence, :expires_at,
                        :metadata, :idempotency_key
                    )
                    """,
                    item,
                )
                record_outbox_event(conn, ident, "upsert", item)
                conn.commit()
                row = conn.execute("SELECT * FROM memories WHERE id = ?", (ident,)).fetchone()
                conn.close()
                return self.send_json(201, format_memory_row(row))
            except sqlite3.OperationalError as exc:
                return self.send_json(500, {"error": f"Database error: {exc}"})

        # 2. Search memories: /v1/search
        if route == "/v1/search":
            query = str(value.get("query", "")).strip()
            scope = value.get("scope")
            req_agent = value.get("agent")
            limit = min(MAX_RESULTS, max(1, int(value.get("limit", 8))))
            force_lexical = bool(value.get("force_lexical", False))

            if not query:
                return self.send_json(400, {"error": "query required"})

            # Attempt semantic search via Chroma unless force_lexical is requested
            chroma = None if force_lexical else get_chroma_adapter()
            semantic_candidates: List[Dict[str, Any]] = []
            if chroma and chroma.is_healthy():
                try:
                    semantic_candidates = chroma.search(
                        query=query,
                        caller_agent=owner or "anonymous",
                        scope=str(scope) if scope else None,
                        limit=limit * 2,
                    )
                except Exception as exc:
                    logging.getLogger("memory.gateway").warning("Chroma semantic search failed (%s); falling back to lexical", exc)
                    semantic_candidates = []

            # If semantic candidates found, re-verify each candidate against authoritative SQLite
            if semantic_candidates:
                try:
                    conn = db()
                    now_str = now()
                    verified_items: List[Dict[str, Any]] = []

                    for cand in semantic_candidates:
                        cand_id = cand["id"]
                        row = conn.execute("SELECT * FROM memories WHERE id = ?", (cand_id,)).fetchone()
                        if not row:
                            # Dropped from authoritative database
                            continue

                        # Check expiration
                        if row["expires_at"] and str(row["expires_at"]) <= now_str:
                            continue

                        # Check ownership / scope access control
                        row_scope = row["scope"]
                        row_agent = row["agent"]
                        if owner not in (None, "*"):
                            if row_scope == "private" and row_agent != owner:
                                continue

                        # Check caller request filters
                        if scope and row_scope != scope:
                            continue
                        if req_agent and row_agent != req_agent:
                            continue

                        item = format_memory_row(row)
                        cand_score = float(cand.get("score", 0.5))
                        item["score"] = round(cand_score * float(item.get("confidence", 0.5)), 4)
                        item["distance"] = cand.get("distance")
                        verified_items.append(item)

                    conn.close()

                    if verified_items:
                        verified_items.sort(key=lambda x: (x["score"], x["created_at"]), reverse=True)
                        return self.send_json(
                            200,
                            {
                                "query": query,
                                "items": verified_items[:limit],
                                "backend": "chroma-semantic",
                                "model_info": chroma.get_model_info(),
                            },
                        )
                except Exception as exc:
                    logging.getLogger("memory.gateway").warning("Re-verification of semantic candidates failed (%s); falling back to lexical", exc)

            # Lexical fallback (when Chroma disabled, offline, or yielded no verified hits)
            try:
                conn = db()
                now_str = now()
                clauses = ["(expires_at IS NULL OR expires_at = '' OR expires_at > ?)"]
                args = [now_str]

                # Scope access control: caller can only see own private memories or shared memories
                if owner not in (None, "*"):
                    clauses.append("(agent = ? OR scope = 'shared')")
                    args.append(owner)

                if scope:
                    clauses.append("scope = ?")
                    args.append(str(scope))
                if req_agent:
                    clauses.append("agent = ?")
                    args.append(str(req_agent))

                where_sql = " WHERE " + " AND ".join(clauses)
                rows = [
                    format_memory_row(r)
                    for r in conn.execute(
                        f"SELECT * FROM memories{where_sql} ORDER BY created_at DESC LIMIT 500",
                        args,
                    ).fetchall()
                ]
                conn.close()

                wanted = extract_tokens(query)
                for item in rows:
                    item_tokens = extract_tokens(item["content"])
                    match_count = len(wanted & item_tokens)
                    item["_score"] = (match_count / max(1, len(wanted))) * float(item.get("confidence", 0.5))

                ranked = sorted(
                    (x for x in rows if x["_score"] > 0),
                    key=lambda x: (x["_score"], x["created_at"]),
                    reverse=True,
                )[:limit]

                for x in ranked:
                    x["score"] = x.pop("_score")

                return self.send_json(
                    200,
                    {
                        "query": query,
                        "items": ranked,
                        "backend": "sqlite-lexical-fallback",
                    },
                )
            except sqlite3.OperationalError as exc:
                return self.send_json(500, {"error": f"Database error: {exc}"})

        return self.send_json(404, {"error": "not found"})

    def do_PATCH(self):
        """Handle PATCH updates to existing memories: /v1/memories/<id>."""
        if not auth(self):
            return self.send_json(401, {"error": "unauthorized"})

        route = urlsplit(self.path).path
        if not route.startswith("/v1/memories/"):
            return self.send_json(404, {"error": "not found"})

        mem_id = route[len("/v1/memories/") :].strip()
        owner = caller(self)

        try:
            value = json_body(self)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"error": str(exc)})

        try:
            conn = db()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
            if not row:
                conn.rollback()
                conn.close()
                return self.send_json(404, {"error": "memory not found"})

            # Ownership check: only author or admin can update
            if owner not in (None, "*") and row["agent"] != owner:
                conn.rollback()
                conn.close()
                return self.send_json(403, {"error": "only author can modify memory"})

            updates = []
            params = []

            if "content" in value:
                content = str(value["content"]).strip()
                if not content or len(content) > MAX_CONTENT:
                    conn.rollback()
                    conn.close()
                    return self.send_json(400, {"error": f"content must be 1 to {MAX_CONTENT} chars"})
                updates.append("content = ?")
                params.append(content)

            if "scope" in value:
                scope = str(value["scope"]).strip()
                if scope not in ("private", "shared"):
                    conn.rollback()
                    conn.close()
                    return self.send_json(400, {"error": "scope must be private or shared"})
                updates.append("scope = ?")
                params.append(scope)

            if "confidence" in value:
                conf = float(value["confidence"])
                if math.isnan(conf) or math.isinf(conf):
                    conn.rollback()
                    conn.close()
                    return self.send_json(400, {"error": "invalid confidence"})
                updates.append("confidence = ?")
                params.append(max(0.0, min(1.0, conf)))

            if "metadata" in value and isinstance(value["metadata"], dict):
                updates.append("metadata = ?")
                params.append(json.dumps(value["metadata"], ensure_ascii=False))

            updates.append("updated_at = ?")
            params.append(now())

            params.append(mem_id)
            conn.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id = ?", params)
            updated = conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
            upd_dict = format_memory_row(updated)
            op = "scope_change" if "scope" in value else "upsert"
            record_outbox_event(conn, mem_id, op, upd_dict)
            conn.commit()
            conn.close()
            return self.send_json(200, upd_dict)
        except sqlite3.OperationalError as exc:
            return self.send_json(500, {"error": f"Database error: {exc}"})

    def do_DELETE(self):
        """Handle DELETE requests: /v1/memories/<id>."""
        if not auth(self):
            return self.send_json(401, {"error": "unauthorized"})

        route = urlsplit(self.path).path
        if not route.startswith("/v1/memories/"):
            return self.send_json(404, {"error": "not found"})

        mem_id = route[len("/v1/memories/") :].strip()
        owner = caller(self)

        try:
            conn = db()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
            if not row:
                conn.rollback()
                conn.close()
                return self.send_json(404, {"error": "memory not found"})

            # Ownership check: only author or admin can delete
            if owner not in (None, "*") and row["agent"] != owner:
                conn.rollback()
                conn.close()
                return self.send_json(403, {"error": "only author can delete memory"})

            tombstone = {
                "id": mem_id,
                "agent": row["agent"],
                "scope": row["scope"],
                "deleted_at": now(),
            }
            record_outbox_event(conn, mem_id, "delete", tombstone)
            conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
            conn.commit()
            conn.close()
            return self.send_json(200, {"ok": True, "deleted_id": mem_id})
        except sqlite3.OperationalError as exc:
            return self.send_json(500, {"error": f"Database error: {exc}"})


def backup_database(dest: Path) -> Path:
    """Create a verified, private backup of the memory database.

    Args:
        dest: Destination backup directory or file.

    Returns:
        Path: Path to created backup file.
    """
    dest = Path(dest)
    if dest.is_dir():
        backup_file = dest / f"memory-backup-{int(time.time())}.sqlite3"
    else:
        backup_file = dest

    backup_file.parent.mkdir(parents=True, exist_ok=True)

    src_conn = sqlite3.connect(str(DB))
    dest_conn = sqlite3.connect(str(backup_file))
    with dest_conn:
        src_conn.backup(dest_conn)
    dest_conn.close()
    src_conn.close()

    backup_file.chmod(0o600)
    return backup_file


def verify_database(path: Path) -> Tuple[bool, str]:
    """Verify database integrity via PRAGMA quick_check.

    Args:
        path: Path to SQLite database file.

    Returns:
        Tuple[bool, str]: (is_healthy, status_message)
    """
    try:
        conn = sqlite3.connect(str(path))
        cursor = conn.execute("PRAGMA quick_check")
        row = cursor.fetchone()
        conn.close()
        if row and row[0] == "ok":
            return True, "Integrity check passed: ok"
        return False, f"Integrity check failed: {row}"
    except Exception as exc:
        return False, f"Verification failed: {exc}"


def main() -> int:
    """CLI entrypoint for memory gateway."""
    parser = argparse.ArgumentParser(description="Authoritative AI Village Memory Gateway")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="Run HTTP server")
    backup_p = subparsers.add_parser("backup", help="Create backup")
    backup_p.add_argument("destination", type=Path, help="Backup destination path")
    verify_p = subparsers.add_parser("verify", help="Check database integrity")
    verify_p.add_argument("db_path", type=Path, nargs="?", default=DB, help="Database path")

    args = parser.parse_args()

    if args.command == "backup":
        bk = backup_database(args.destination)
        print(f"Memory database backed up to {bk}")
        return 0

    if args.command == "verify":
        ok, msg = verify_database(args.db_path)
        print(msg)
        return 0 if ok else 1

    # Default: serve
    conn = db()
    conn.close()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Memory gateway listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
