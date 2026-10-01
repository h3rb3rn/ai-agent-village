"""Unit tests for AI Village Resident Profiles, Cognitive DNA, ASCII Art, and Web Routes."""

import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

os.environ.setdefault("VILLAGE_ROOT", "/tmp/ai-village-residents-test")
os.environ.setdefault("VILLAGE_SIGNAL_AUTH_USER", "synthetic-user")
os.environ.setdefault("VILLAGE_SIGNAL_AUTH_PASSWORD", "synthetic-password")
sys.path.insert(0, os.path.abspath("web"))

from village.residents import (
    ART_DIR,
    RESIDENTS_DATA,
    CognitiveDNA,
    ResidentProfile,
    ResidentStore,
    get_resident,
    get_resident_art,
    list_residents,
    validate_ascii_art,
)
from web import webui


class ResidentCatalogTests(unittest.TestCase):
    """Verifies that all 9 residents are fully defined with localized bios and DNA."""

    def test_catalog_has_all_nine_agents(self):
        self.assertEqual(len(RESIDENTS_DATA), 9)
        expected_ids = [
            "01-king",
            "02-explorer",
            "03-librarian",
            "04-artisan",
            "05-interpreter",
            "06-operator",
            "07-methodologist",
            "08-logician",
            "09-chronicler",
        ]
        actual_ids = [r.id for r in RESIDENTS_DATA]
        self.assertEqual(actual_ids, expected_ids)

    def test_resident_profiles_and_dna_fields(self):
        for r in RESIDENTS_DATA:
            with self.subTest(resident=r.id):
                self.assertTrue(r.name)
                self.assertTrue(r.role)
                self.assertTrue(r.art_symbol)
                self.assertTrue(r.accent_color.startswith("#"))

                # Localized fields
                self.assertTrue(r.profession_en)
                self.assertTrue(r.profession_de)
                self.assertTrue(r.calling_en)
                self.assertTrue(r.calling_de)
                self.assertTrue(r.personal_info_en)
                self.assertTrue(r.personal_info_de)

                # Lists
                self.assertGreaterEqual(len(r.preferences_en), 2)
                self.assertGreaterEqual(len(r.preferences_de), 2)
                self.assertGreaterEqual(len(r.hobbies_en), 2)
                self.assertGreaterEqual(len(r.hobbies_de), 2)
                self.assertGreaterEqual(len(r.goals_en), 2)
                self.assertGreaterEqual(len(r.goals_de), 2)
                self.assertGreaterEqual(len(r.wishes_en), 2)
                self.assertGreaterEqual(len(r.wishes_de), 2)

                # Cognitive DNA
                dna = r.dna
                self.assertIsInstance(dna, CognitiveDNA)
                self.assertTrue(dna.model)
                self.assertGreater(dna.context_size, 0)
                self.assertTrue(dna.model_quant)
                self.assertTrue(dna.kv_cache_quant)
                self.assertTrue(dna.model_size)

    def test_to_dict_localization(self):
        king = get_resident("01-king", lang="en", include_art=False)
        self.assertIsNotNone(king)
        self.assertIn("Village Coordinator", king["profession"])
        self.assertIn("cognitive diversity", king["calling"])

        king_de = get_resident("01-king", lang="de", include_art=False)
        self.assertIsNotNone(king_de)
        self.assertIn("Ratsvorsitzender", king_de["profession"])
        self.assertIn("kognitiven Vielfalt", king_de["calling"])


class AsciiArtDimensionTests(unittest.TestCase):
    """Verifies that all 9 ASCII art files strictly adhere to 250x250 UTF-8 format."""

    def test_ascii_art_exact_dimensions(self):
        ansi_regex = re.compile(r"\x1b\[[0-9;]*m")

        for r in RESIDENTS_DATA:
            agent_id = r.id
            color_file = ART_DIR / f"{agent_id}.txt"
            plain_file = ART_DIR / f"{agent_id}.plain.txt"

            self.assertTrue(color_file.is_file(), f"Missing color art for {agent_id}")
            self.assertTrue(plain_file.is_file(), f"Missing plain art for {agent_id}")

            # Verify plain art: exactly 250 lines, each exactly 250 chars
            plain_text = plain_file.read_text(encoding="utf-8")
            plain_lines = plain_text.splitlines()
            self.assertEqual(len(plain_lines), 250, f"{agent_id} plain lines != 250")
            for line_idx, line in enumerate(plain_lines):
                self.assertEqual(len(line), 250, f"{agent_id} line {line_idx} width != 250")

            # Verify color art: exactly 250 lines, stripped width exactly 250 chars
            color_text = color_file.read_text(encoding="utf-8")
            color_lines = color_text.splitlines()
            self.assertEqual(len(color_lines), 250, f"{agent_id} color lines != 250")
            self.assertIn("\x1b[38;2;", color_text, f"{agent_id} lacks ANSI RGB codes")

            for line_idx, line in enumerate(color_lines):
                stripped = ansi_regex.sub("", line)
                self.assertEqual(len(stripped), 250, f"{agent_id} color line {line_idx} stripped width != 250")


class ResidentWebUITests(unittest.TestCase):
    """Verifies the HTTP endpoints for residents and ascii art assets."""

    @classmethod
    def setUpClass(cls):
        cls.saved_assets = webui.ASSETS
        webui.ASSETS = Path(__file__).resolve().parents[1] / "web"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        webui.ASSETS = cls.saved_assets

    def test_residents_page_route(self):
        req = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/residents")
        self.assertEqual(req.status, 200)
        body = req.read().decode("utf-8")
        self.assertIn("id=\"residents-panel\"", body)
        self.assertIn("data-view=\"residents\"", body)

    def test_api_residents_list(self):
        req = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/residents?art=0")
        self.assertEqual(req.status, 200)
        data = json.loads(req.read().decode("utf-8"))
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 9)
        self.assertEqual(data[0]["id"], "01-king")
        self.assertIn("dna", data[0])
        self.assertIn("model", data[0]["dna"])

    def test_api_resident_single(self):
        req = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/residents?agent=02-explorer&lang=en")
        self.assertEqual(req.status, 200)
        data = json.loads(req.read().decode("utf-8"))
        self.assertEqual(data["id"], "02-explorer")
        self.assertEqual(data["name"], "Explorer")
        self.assertIn("ascii_art", data)
        self.assertTrue(len(data["ascii_art"]) > 0)

    def test_api_resident_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/residents?agent=unknown-agent")
        self.assertEqual(ctx.exception.code, 404)

    def test_raw_ascii_art_endpoint(self):
        req = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/assets/ascii_art/01-king.txt")
        self.assertEqual(req.status, 200)
        content = req.read().decode("utf-8")
        lines = content.splitlines()
        self.assertEqual(len(lines), 250)


class ResidentStoreTests(unittest.TestCase):
    """Tests SQLite persistence and validation for agent-authored profiles."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_coordination.sqlite3"
        self.store = ResidentStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unauthored_profile_fallback(self):
        # Fresh store has no authored profiles
        king = get_resident("01-king", lang="en", include_art=False, store=self.store)
        self.assertIsNotNone(king)
        self.assertFalse(king["has_profile"])
        self.assertIn("Village Coordinator", king["profession"])

    def test_author_profile_update_and_retrieval(self):
        # Agent authors their own profile
        res = self.store.update_profile(
            agent_id="01-king",
            profession="Architect of Consensus",
            calling="Unifying independent nodes into a harmonic mesh.",
            personal_info="I observe and mediate.",
            art_symbol="Golden Crown",
            accent_color="#FFD700",
            preferences=["Truth", "Patience"],
            hobbies=["Chess", "Astrophysics"],
            goals=["Zero livelocks"],
            wishes=["Infinite context"],
        )
        self.assertEqual(res["profession"], "Architect of Consensus")
        self.assertEqual(res["art_symbol"], "Golden Crown")

        # Now get_resident returns has_profile=True with authored fields
        profile = get_resident("01-king", lang="en", include_art=False, store=self.store)
        self.assertTrue(profile["has_profile"])
        self.assertEqual(profile["profession"], "Architect of Consensus")
        self.assertEqual(profile["calling"], "Unifying independent nodes into a harmonic mesh.")
        self.assertEqual(profile["personal_info"], "I observe and mediate.")
        self.assertEqual(profile["art_symbol"], "Golden Crown")
        self.assertEqual(profile["accent_color"], "#FFD700")
        self.assertEqual(profile["preferences"], ["Truth", "Patience"])

    def test_validate_ascii_art_exact_250x250(self):
        # Valid art
        valid_lines = ["." * 250 for _ in range(250)]
        valid_art = "\n".join(valid_lines)
        ok, plain, err = validate_ascii_art(valid_art)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(len(plain.splitlines()), 250)

        # Invalid row count
        invalid_rows = "\n".join(["." * 250 for _ in range(240)])
        ok, plain, err = validate_ascii_art(invalid_rows)
        self.assertFalse(ok)
        self.assertIn("must have exactly 250 lines", err)

        # Invalid column width
        bad_cols = ["." * 250 for _ in range(249)] + ["." * 240]
        ok, plain, err = validate_ascii_art("\n".join(bad_cols))
        self.assertFalse(ok)
        self.assertIn("has visible width 240 instead of 250", err)

    def test_ascii_art_with_ansi_codes_validation(self):
        # Valid ANSI colored art
        colored_line = "\x1b[38;2;100;150;200m" + ("#" * 250) + "\x1b[0m"
        valid_art = "\n".join([colored_line for _ in range(250)])
        ok, plain, err = validate_ascii_art(valid_art)
        self.assertTrue(ok)
        self.assertEqual(len(plain.splitlines()), 250)
        self.assertEqual(len(plain.splitlines()[0]), 250)


if __name__ == "__main__":
    unittest.main()
