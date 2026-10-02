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
    infer_cognitive_dna,
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
        # Verify that all agents retain their cognitive DNA while biographies remain unauthored
        for r in RESIDENTS_DATA:
            with self.subTest(resident=r.id):
                self.assertTrue(r.name)
                self.assertTrue(r.role)
                self.assertFalse(r.has_profile, "Profile should start unauthored")

                # Localized bio fields should be blank strings waiting for agent authoring
                self.assertEqual(r.profession_en, "")
                self.assertEqual(r.profession_de, "")
                self.assertEqual(r.calling_en, "")
                self.assertEqual(r.calling_de, "")
                self.assertEqual(r.personal_info_en, "")
                self.assertEqual(r.personal_info_de, "")

                # Lists should be empty waiting for agent authoring
                self.assertEqual(r.preferences_en, [])
                self.assertEqual(r.preferences_de, [])
                self.assertEqual(r.hobbies_en, [])
                self.assertEqual(r.hobbies_de, [])
                self.assertEqual(r.goals_en, [])
                self.assertEqual(r.goals_de, [])
                self.assertEqual(r.wishes_en, [])
                self.assertEqual(r.wishes_de, [])

                # Cognitive DNA must remain intact and fully specified
                dna = r.dna
                self.assertIsInstance(dna, CognitiveDNA)
                self.assertTrue(dna.model)
                self.assertGreater(dna.context_size, 0)
                self.assertTrue(dna.model_quant)
                self.assertTrue(dna.kv_cache_quant)
                self.assertTrue(dna.model_size)
                self.assertGreater(dna.batch_size, 0)
                self.assertGreater(dna.num_predict, 0)
                self.assertTrue(dna.think_level)
                self.assertTrue(dna.keep_alive)
                self.assertTrue(dna.ollama_url)

    def test_to_dict_localization(self):
        # Verify that unauthored residents serialize with empty bios and intact DNA
        king = get_resident("01-king", lang="en", include_art=False)
        self.assertIsNotNone(king)
        self.assertFalse(king["has_profile"])
        self.assertEqual(king["profession"], "")
        self.assertEqual(king["calling"], "")
        self.assertEqual(king["dna"]["model"], "qwen3.6:35b")
        self.assertEqual(king["dna"]["context_size"], 131072)
        self.assertEqual(king["dna"]["batch_size"], 256)
        self.assertEqual(king["dna"]["num_predict"], 8192)
        self.assertEqual(king["dna"]["think_level"], "medium")
        self.assertEqual(king["dna"]["keep_alive"], "24h")
        self.assertEqual(king["dna"]["ollama_url"], "http://192.168.155.222:11434")


class AsciiArtDimensionTests(unittest.TestCase):
    """Verifies that 250x250 ASCII art validation strictly enforces dimensions and UTF-8 characters."""

    def test_validate_ascii_art_exact_250x250(self):
        # A valid 250x250 plain matrix must pass validation
        valid_matrix = "\n".join(["." * 250 for _ in range(250)])
        ok, plain, err = validate_ascii_art(valid_matrix)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(len(plain.splitlines()), 250)
        self.assertEqual(len(plain.splitlines()[0]), 250)

    def test_validate_ascii_art_colored_250x250(self):
        # ANSI RGB color codes must be preserved in art and stripped for dimension checks
        colored_line = "\x1b[38;2;255;128;0m" + ("@" * 250) + "\x1b[0m"
        colored_art = "\n".join([colored_line for _ in range(250)])
        ok, plain, err = validate_ascii_art(colored_art)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(len(plain.splitlines()), 250)
        self.assertEqual(len(plain.splitlines()[0]), 250)

    def test_validate_ascii_art_rejects_wrong_dimensions(self):
        # Fewer than 250 rows must fail
        short_rows = "\n".join(["#" * 250 for _ in range(249)])
        ok, _, err = validate_ascii_art(short_rows)
        self.assertFalse(ok)
        self.assertIn("must have exactly 250 lines", err)

        # Line shorter than 250 columns must fail
        short_cols = "\n".join(["#" * 250 for _ in range(249)] + ["#" * 240])
        ok, _, err = validate_ascii_art(short_cols)
        self.assertFalse(ok)
        self.assertIn("has visible width 240 instead of 250", err)


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
        # Unauthored resident returns empty string for ascii_art
        self.assertEqual(data["ascii_art"], "")

    def test_api_resident_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/residents?agent=unknown-agent")
        self.assertEqual(ctx.exception.code, 404)

    def test_raw_ascii_art_endpoint_unauthored_returns_404(self):
        # Since pre-fabricated ASCII art was removed, requesting raw art for unauthored agent returns 404
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/assets/ascii_art/01-king.txt")
        self.assertEqual(ctx.exception.code, 404)


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
        self.assertEqual(king["profession"], "")
        self.assertEqual(king["calling"], "")

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

    def test_infer_cognitive_dna_all_settings(self):
        # Verify that infer_cognitive_dna returns full Ollama DNA settings
        dna = infer_cognitive_dna("01-king")
        self.assertEqual(dna.model, "qwen3.6:35b")
        self.assertEqual(dna.context_size, 131072)
        self.assertEqual(dna.batch_size, 256)
        self.assertEqual(dna.num_predict, 8192)
        self.assertEqual(dna.think_level, "medium")
        self.assertEqual(dna.keep_alive, "24h")
        self.assertEqual(dna.ollama_url, "http://192.168.155.222:11434")
        self.assertEqual(dna.temperature, 0.35)


if __name__ == "__main__":
    unittest.main()

