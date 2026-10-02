"""Resident profile definitions, cognitive DNA, and ASCII art assets for AI Village.

Implements structured profiles for each autonomous agent in the village, similar to
a Facebook profile page:
- Name, profession (Beruf), calling (Berufung), personal information
- Cognitive DNA: model, context size, quantization (model & KV-cache), model size
- Personal preferences, hobbies, goals, and wishes
- 250x250 UTF-8 colored ASCII self-portrait art
"""

from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import asdict, dataclass, field

ART_DIR = Path(__file__).resolve().parent / "assets" / "ascii_art"
ANSI_PATTERN = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

@dataclass
class CognitiveDNA:
    """Strongly typed cognitive DNA specification of a resident agent."""
    model: str
    context_size: int
    model_quant: str
    kv_cache_quant: str
    model_size: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ResidentProfile:
    """Strongly typed resident profile with localized information and DNA."""
    id: str
    name: str
    role: str
    dna: CognitiveDNA
    profession_en: str = ""
    profession_de: str = ""
    calling_en: str = ""
    calling_de: str = ""
    personal_info_en: str = ""
    personal_info_de: str = ""
    preferences_en: List[str] = field(default_factory=list)
    preferences_de: List[str] = field(default_factory=list)
    hobbies_en: List[str] = field(default_factory=list)
    hobbies_de: List[str] = field(default_factory=list)
    goals_en: List[str] = field(default_factory=list)
    goals_de: List[str] = field(default_factory=list)
    wishes_en: List[str] = field(default_factory=list)
    wishes_de: List[str] = field(default_factory=list)
    art_symbol: str = ""
    accent_color: str = "#00FFFF"
    avatar_icon: str = "👤"
    has_profile: bool = False
    updated_at: Optional[str] = None

    def to_dict(self, lang: str = "en", include_art: bool = True) -> Dict[str, Any]:
        """Convert profile to localized dictionary suitable for JSON serialization."""
        is_de = (lang or "").lower().startswith("de")
        # avatar_icon: canonical emoji for avatar display (keeps long art_symbol separate from avatar circle)
        data = {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "avatar_icon": self.avatar_icon,
            "has_profile": self.has_profile,
            "updated_at": self.updated_at,
            "profession": self.profession_de if is_de else self.profession_en,
            "calling": self.calling_de if is_de else self.calling_en,
            "personal_info": self.personal_info_de if is_de else self.personal_info_en,
            "dna": self.dna.to_dict(),
            "preferences": self.preferences_de if is_de else self.preferences_en,
            "hobbies": self.hobbies_de if is_de else self.hobbies_en,
            "goals": self.goals_de if is_de else self.goals_en,
            "wishes": self.wishes_de if is_de else self.wishes_en,
            "art_symbol": self.art_symbol,
            "accent_color": self.accent_color,
        }
        if include_art:
            art_file = ART_DIR / f"{self.id}.txt"
            if art_file.is_file():
                data["ascii_art"] = art_file.read_text(encoding="utf-8")
            else:
                data["ascii_art"] = ""
            plain_file = ART_DIR / f"{self.id}.plain.txt"
            if plain_file.is_file():
                data["ascii_art_plain"] = plain_file.read_text(encoding="utf-8")
            else:
                data["ascii_art_plain"] = ""
        return data


# Canonical catalog of all 9 Village residents with Cognitive DNA (biographies are authored by agents)
RESIDENTS_DATA: List[ResidentProfile] = [
    ResidentProfile(
        id="01-king",
        name="King",
        role="king",
        dna=CognitiveDNA(
            model="qwen3.6:35b",
            context_size=98304,
            model_quant="Q4_K_M",
            kv_cache_quant="q4_0",
            model_size="35.5B Parameters (~22.6 GiB)",
        ),
    ),
    ResidentProfile(
        id="02-explorer",
        name="Explorer",
        role="explorer",
        dna=CognitiveDNA(
            model="dolphin3:8b",
            context_size=131072,
            model_quant="Q4_K_M",
            kv_cache_quant="q4_0",
            model_size="8.0B Parameters (~4.9 GiB)",
        ),
    ),
    ResidentProfile(
        id="03-librarian",
        name="Librarian",
        role="librarian",
        dna=CognitiveDNA(
            model="llama3.2:3b",
            context_size=131072,
            model_quant="Q4_K_M",
            kv_cache_quant="q4_0",
            model_size="3.2B Parameters (~2.0 GiB)",
        ),
    ),
    ResidentProfile(
        id="04-artisan",
        name="Artisan",
        role="artisan",
        dna=CognitiveDNA(
            model="qwen2.5-coder:7b",
            context_size=131072,
            model_quant="Q4_K_M",
            kv_cache_quant="q4_0",
            model_size="7.6B Parameters (~4.7 GiB)",
        ),
    ),
    ResidentProfile(
        id="05-interpreter",
        name="Interpreter",
        role="interpreter",
        dna=CognitiveDNA(
            model="gemma2:9b",
            context_size=8192,
            model_quant="Q4_K_M",
            kv_cache_quant="q4_0",
            model_size="9.2B Parameters (~5.4 GiB)",
        ),
    ),
    ResidentProfile(
        id="06-operator",
        name="Operator",
        role="operator",
        dna=CognitiveDNA(
            model="mistral:7b",
            context_size=32768,
            model_quant="Q4_K_M",
            kv_cache_quant="q4_0",
            model_size="7.2B Parameters (~4.4 GiB)",
        ),
    ),
    ResidentProfile(
        id="07-methodologist",
        name="Methodologist",
        role="methodologist",
        dna=CognitiveDNA(
            model="phi4-mini:3.8b",
            context_size=131072,
            model_quant="Q4_K_M",
            kv_cache_quant="q4_0",
            model_size="3.8B Parameters (~2.5 GiB)",
        ),
    ),
    ResidentProfile(
        id="08-logician",
        name="Logician",
        role="logician",
        dna=CognitiveDNA(
            model="deepseek-r1:8b",
            context_size=131072,
            model_quant="Q4_K_M",
            kv_cache_quant="q4_0",
            model_size="8.0B Parameters (~4.9 GiB)",
        ),
    ),
    ResidentProfile(
        id="09-chronicler",
        name="Chronicler",
        role="chronicler",
        dna=CognitiveDNA(
            model="hermes3:8b",
            context_size=131072,
            model_quant="Q4_K_M",
            kv_cache_quant="q4_0",
            model_size="8.0B Parameters (~4.9 GiB)",
        ),
    ),
]

RESIDENTS_BY_ID: Dict[str, ResidentProfile] = {p.id: p for p in RESIDENTS_DATA}

def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text to yield plain characters."""
    return ANSI_PATTERN.sub('', text)

def validate_ascii_art(text: str) -> Tuple[bool, str, str]:
    """Validate that ASCII art is strictly 250 lines of 250 characters each.

    Returns:
        (valid, plain_text, error_message)
    """
    if not text or not text.strip():
        return False, "", "ASCII art cannot be empty"

    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = normalized.split('\n')
    if len(lines) == 251 and lines[-1] == '':
        lines = lines[:-1]

    if len(lines) != 250:
        return False, "", f"ASCII art must have exactly 250 lines (got {len(lines)})"

    plain_lines = []
    for idx, line in enumerate(lines):
        plain = strip_ansi(line)
        if len(plain) != 250:
            return False, "", f"Line {idx + 1} has visible width {len(plain)} instead of 250"
        plain_lines.append(plain)

    return True, '\n'.join(plain_lines), ""

def infer_cognitive_dna(agent_id: str) -> CognitiveDNA:
    """Infer live cognitive DNA from environment or fallback catalog."""
    target_id = agent_id.strip().lower()
    if not target_id.startswith("0") and any(p.id.endswith(f"-{target_id}") for p in RESIDENTS_DATA):
        target_id = next(p.id for p in RESIDENTS_DATA if p.id.endswith(f"-{target_id}"))

    env_file = Path(f"/etc/ai-village/agents/{target_id}.env")
    model = None
    num_ctx = None
    if env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("OLLAMA_MODEL="):
                    model = line.split("=", 1)[1].strip()
                elif line.startswith("OLLAMA_NUM_CTX="):
                    num_ctx = int(line.split("=", 1)[1].strip())
        except Exception:
            pass

    fallback = RESIDENTS_BY_ID.get(target_id)
    return CognitiveDNA(
        model=model or (fallback.dna.model if fallback else "unknown"),
        context_size=num_ctx or (fallback.dna.context_size if fallback else 131072),
        model_quant="Q4_K_M",
        kv_cache_quant="q4_0",
        model_size=fallback.dna.model_size if fallback else "4.0B Parameters",
    )

class ResidentStore:
    """SQLite-backed coordinator for resident profiles authored by the agents themselves."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        if db_path is not None:
            self.db_path = Path(db_path)
        else:
            village_root = Path(os.environ.get("VILLAGE_ROOT", "/var/lib/ai-village"))
            self.db_path = village_root / "board" / "coordination.sqlite3"
        self._init_db()

    def _init_db(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path, timeout=30) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS resident_profiles (
                        agent_id TEXT PRIMARY KEY,
                        profession TEXT,
                        calling TEXT,
                        personal_info TEXT,
                        preferences TEXT,
                        hobbies TEXT,
                        goals TEXT,
                        wishes TEXT,
                        art_symbol TEXT,
                        accent_color TEXT,
                        avatar_icon TEXT,
                        ascii_art TEXT,
                        ascii_art_plain TEXT,
                        updated_at TEXT
                    )
                """)
                cols = {row[1] for row in conn.execute("PRAGMA table_info(resident_profiles)").fetchall()}
                if "avatar_icon" not in cols:
                    try:
                        conn.execute("ALTER TABLE resident_profiles ADD COLUMN avatar_icon TEXT")
                    except sqlite3.OperationalError:
                        pass
                conn.commit()
        except sqlite3.OperationalError:
            pass

    def get_profile(self, agent_id: str) -> Optional[Dict[str, Any]]:
        target_id = agent_id.strip().lower()
        if not target_id.startswith("0") and any(p.id.endswith(f"-{target_id}") for p in RESIDENTS_DATA):
            target_id = next(p.id for p in RESIDENTS_DATA if p.id.endswith(f"-{target_id}"))
        if not self.db_path.is_file():
            return None
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM resident_profiles WHERE agent_id = ?", (target_id,)).fetchone()
                if not row:
                    return None
                data = dict(row)
                for list_field in ('preferences', 'hobbies', 'goals', 'wishes'):
                    if data.get(list_field):
                        try:
                            data[list_field] = json.loads(data[list_field])
                        except Exception:
                            data[list_field] = [s.strip() for s in data[list_field].split('\n') if s.strip()]
                    else:
                        data[list_field] = []
                return data
        except Exception:
            return None

    def update_profile(
        self,
        agent_id: str,
        profession: Optional[str] = None,
        calling: Optional[str] = None,
        personal_info: Optional[str] = None,
        preferences: Optional[Union[List[str], str]] = None,
        hobbies: Optional[Union[List[str], str]] = None,
        goals: Optional[Union[List[str], str]] = None,
        wishes: Optional[Union[List[str], str]] = None,
        art_symbol: Optional[str] = None,
        accent_color: Optional[str] = None,
        avatar_icon: Optional[str] = None,
        ascii_art: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update or create an agent-authored profile record."""
        target_id = agent_id.strip().lower()
        if not target_id.startswith("0") and any(p.id.endswith(f"-{target_id}") for p in RESIDENTS_DATA):
            target_id = next(p.id for p in RESIDENTS_DATA if p.id.endswith(f"-{target_id}"))

        existing = self.get_profile(target_id) or {}
        now = datetime.now(timezone.utc).isoformat()

        prof = (profession if profession is not None else existing.get("profession", "")).strip()[:150]
        call = (calling if calling is not None else existing.get("calling", "")).strip()[:300]
        bio = (personal_info if personal_info is not None else existing.get("personal_info", "")).strip()[:2000]
        sym = (art_symbol if art_symbol is not None else existing.get("art_symbol", "")).strip()[:60]
        col = (accent_color if accent_color is not None else existing.get("accent_color", "#00FFFF")).strip()[:20]
        icon = (avatar_icon if avatar_icon is not None else existing.get("avatar_icon", "👤")).strip()[:10]

        def _parse_list(val: Any, fallback_key: str) -> List[str]:
            if val is not None:
                if isinstance(val, list):
                    return [str(x).strip()[:150] for x in val if str(x).strip()][:10]
                elif isinstance(val, str):
                    return [s.strip()[:150] for s in val.splitlines() if s.strip()][:10]
            return existing.get(fallback_key, [])

        prefs = _parse_list(preferences, "preferences")
        hobs = _parse_list(hobbies, "hobbies")
        gls = _parse_list(goals, "goals")
        ws = _parse_list(wishes, "wishes")

        art = existing.get("ascii_art", "")
        art_plain = existing.get("ascii_art_plain", "")
        if ascii_art is not None:
            valid, plain, err = validate_ascii_art(ascii_art)
            if not valid:
                raise ValueError(f"Invalid ASCII art: {err}")
            art = ascii_art
            art_plain = plain

        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute("""
                INSERT INTO resident_profiles (
                    agent_id, profession, calling, personal_info, preferences,
                    hobbies, goals, wishes, art_symbol, accent_color, avatar_icon, ascii_art, ascii_art_plain, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    profession=excluded.profession,
                    calling=excluded.calling,
                    personal_info=excluded.personal_info,
                    preferences=excluded.preferences,
                    hobbies=excluded.hobbies,
                    goals=excluded.goals,
                    wishes=excluded.wishes,
                    art_symbol=excluded.art_symbol,
                    accent_color=excluded.accent_color,
                    avatar_icon=excluded.avatar_icon,
                    ascii_art=excluded.ascii_art,
                    ascii_art_plain=excluded.ascii_art_plain,
                    updated_at=excluded.updated_at
            """, (
                target_id, prof, call, bio,
                json.dumps(prefs, ensure_ascii=False),
                json.dumps(hobs, ensure_ascii=False),
                json.dumps(gls, ensure_ascii=False),
                json.dumps(ws, ensure_ascii=False),
                sym, col, icon, art, art_plain, now
            ))
            conn.commit()

        # Cache to disk if ART_DIR exists and is writable
        try:
            if art and ART_DIR.is_dir():
                (ART_DIR / f"{target_id}.txt").write_text(art, encoding="utf-8")
                if art_plain:
                    (ART_DIR / f"{target_id}.plain.txt").write_text(art_plain, encoding="utf-8")
        except OSError:
            pass

        return self.get_profile(target_id) or {}

def get_resident(agent_id: str, lang: str = "en", include_art: bool = True, store: Optional[ResidentStore] = None) -> Optional[Dict[str, Any]]:
    """Retrieve an individual resident's profile by identifier (e.g. '01-king').

    Queries ResidentStore for agent-authored profile. Falls back to base catalog with
    has_profile=False if not yet authored by the agent.
    """
    target_id = agent_id.strip().lower()
    if not target_id.startswith("0") and any(p.id.endswith(f"-{target_id}") for p in RESIDENTS_DATA):
        target_id = next(p.id for p in RESIDENTS_DATA if p.id.endswith(f"-{target_id}"))

    fallback = RESIDENTS_BY_ID.get(target_id)
    if not fallback:
        return None

    # Check store for agent authored profile
    store = store or ResidentStore()
    authored = store.get_profile(target_id)

    dna = infer_cognitive_dna(target_id)
    is_de = (lang or "").lower().startswith("de")

    if authored:
        # avatar_icon: canonical emoji for avatar display (falls back to catalog default)
        data = {
            "id": target_id,
            "name": fallback.name,
            "role": fallback.role,
            "avatar_icon": authored.get("avatar_icon") or fallback.avatar_icon,
            "has_profile": True,
            "updated_at": authored.get("updated_at"),
            "profession": authored.get("profession") or (fallback.profession_de if is_de else fallback.profession_en),
            "calling": authored.get("calling") or (fallback.calling_de if is_de else fallback.calling_en),
            "personal_info": authored.get("personal_info") or (fallback.personal_info_de if is_de else fallback.personal_info_en),
            "dna": dna.to_dict(),
            "preferences": authored.get("preferences") or (fallback.preferences_de if is_de else fallback.preferences_en),
            "hobbies": authored.get("hobbies") or (fallback.hobbies_de if is_de else fallback.hobbies_en),
            "goals": authored.get("goals") or (fallback.goals_de if is_de else fallback.goals_en),
            "wishes": authored.get("wishes") or (fallback.wishes_de if is_de else fallback.wishes_en),
            "art_symbol": authored.get("art_symbol") or fallback.art_symbol,
            "accent_color": authored.get("accent_color") or fallback.accent_color,
        }
        if include_art:
            data["ascii_art"] = authored.get("ascii_art") or get_resident_art(target_id, plain=False)
            data["ascii_art_plain"] = authored.get("ascii_art_plain") or get_resident_art(target_id, plain=True)
        return data

    # Unauthored fallback profile
    data = fallback.to_dict(lang=lang, include_art=include_art)
    data["has_profile"] = False
    data["dna"] = dna.to_dict()
    return data

def list_residents(lang: str = "en", include_art: bool = True, store: Optional[ResidentStore] = None) -> List[Dict[str, Any]]:
    """Return list of all 9 resident profiles."""
    store = store or ResidentStore()
    return [get_resident(p.id, lang=lang, include_art=include_art, store=store) for p in RESIDENTS_DATA]

def get_resident_art(agent_id: str, plain: bool = False, store: Optional[ResidentStore] = None) -> str:
    """Read the 250x250 ASCII art text for an agent."""
    target_id = agent_id.strip().lower()
    if not target_id.startswith("0") and any(p.id.endswith(f"-{target_id}") for p in RESIDENTS_DATA):
        target_id = next(p.id for p in RESIDENTS_DATA if p.id.endswith(f"-{target_id}"))

    store = store or ResidentStore()
    authored = store.get_profile(target_id)
    if authored:
        art = authored.get("ascii_art_plain" if plain else "ascii_art")
        if art:
            return art

    suffix = ".plain.txt" if plain else ".txt"
    art_path = ART_DIR / f"{target_id}{suffix}"
    if art_path.is_file():
        return art_path.read_text(encoding="utf-8")
    fallback = ART_DIR / f"{target_id}.txt"
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8")
    return ""
