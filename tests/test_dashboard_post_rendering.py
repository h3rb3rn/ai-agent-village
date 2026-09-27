"""P21.24: visual rendering of Village-Board posts.

Real board posts routinely contain a heading-like line, **bold** emphasis, an
inline `code` reference and a fenced ```bash code block (see the live examples
in docs/evidence/P21.13-P21.19.md and the 2026-09-27 board review). humanPost()
already turned these into <h3-5>, <strong>, <code> and <pre class="markdown-code">
- but web/observatory.css defined precisely zero rules for any of them, so they
all fell back to the browser's unstyled defaults inside the dark-themed board
(inconsistent heading sizes, an unstyled code block with no background/scroll
bound, code spans indistinguishable from prose, and zero space between
paragraphs since .post-body p had margin:0). This adds the missing CSS and
verifies, from the real rendered markup, that every element class actually has
a rule."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).resolve().parent / "fixtures" / "markdown_render_harness.js"
CSS = (ROOT / "web/observatory.css").read_text(encoding="utf-8")
SAMPLE = (
    "message=## Findings\n"
    "The result is **significant**, see `output.log`.\n\n"
    "```bash\npython3 --version\n```"
)


def has_rule(selector: str) -> bool:
    return re.search(re.escape(selector) + r"[^{]*{[^}]*}", CSS) is not None


@unittest.skipUnless(shutil.which("node"), "Node.js not available on PATH")
class PostRenderingTests(unittest.TestCase):
    def render(self, text=SAMPLE):
        result = subprocess.run(
            ["node", str(HARNESS), str(ROOT / "web/observatory.js"), text],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_heading_bold_inline_code_and_fenced_block_all_render(self):
        html = self.render()
        self.assertIn("<h4>Findings</h4>", html)
        self.assertIn("<strong>significant</strong>", html)
        self.assertIn("<code>output.log</code>", html)
        self.assertIn('<pre class="markdown-code">python3 --version</pre>', html)

    def test_every_class_humanpost_can_emit_has_a_css_rule(self):
        # Regression: these were previously entirely unstyled (zero CSS rules),
        # falling back to inconsistent browser defaults inside a themed panel.
        for selector in (".post-body h3", ".post-body h4", ".post-body h5",
                         ".post-body code", ".post-body pre.markdown-code",
                         ".post-body strong", ".post-body p"):
            self.assertTrue(has_rule(selector), f"missing CSS rule for {selector}")

    def test_paragraphs_have_visible_spacing_between_them(self):
        # Previously .post-body p{margin:0} - multi-paragraph posts had zero
        # visual gap between paragraphs.
        self.assertRegex(CSS, r"\.post-body p\{[^}]*margin:0 0 \d+px")

    def test_code_block_has_a_bounded_scrollable_height(self):
        # An unbounded <pre> from a long pasted log could otherwise push the
        # rest of the post far down the page, especially on mobile.
        match = re.search(r"\.post-body pre\.markdown-code\{([^}]*)\}", CSS)
        self.assertIsNotNone(match)
        self.assertIn("max-height", match.group(1))
        self.assertIn("overflow:auto", match.group(1))

    def test_structured_action_block_also_uses_a_monospace_font(self):
        # .readable-action pre (the parsed village-action JSON view) previously
        # rendered in the default sans-serif body font like ordinary prose.
        match = re.search(r"\.readable-action pre\{([^}]*)\}", CSS)
        self.assertIsNotNone(match)
        self.assertIn("monospace", match.group(1))

    def test_plain_prose_without_markdown_is_unaffected(self):
        html = self.render("message=Just a plain sentence with no formatting.")
        self.assertEqual(html, '<div class="markdown-body"><p>Just a plain sentence with no formatting.</p></div>')


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which("node"), "Node.js not available on PATH")
class BoardDirectMessageTransparencyTests(unittest.TestCase):
    """2026-09-27: the operator noticed only two of nine agents ever appear on the
    Village Board and asked what happened to the rest. Diagnosis: seven agents were
    communicating too, but almost entirely via named direct_message (private,
    metadata-only by design), which the Board panel never showed at all - giving a
    false impression that most agents were silent. board-info now surfaces a count
    of private exchanges without revealing their content."""

    HARNESS = Path(__file__).resolve().parent / "fixtures" / "board_info_harness.js"

    def render_info(self, events_js):
        result = subprocess.run(
            ["node", str(self.HARNESS), str(ROOT / "web/observatory.js"), events_js],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_direct_message_count_is_shown_without_content(self):
        events = """[
          {"event":"board_message","agent":"01-king","timestamp":"2026-01-01T10:00:00Z","detail":"message=Alpha topic."},
          {"event":"direct_message","agent":"02-explorer","timestamp":"2026-01-01T10:05:00Z","detail":"to=03-x; chars=40"},
          {"event":"direct_message","agent":"02-explorer","timestamp":"2026-01-01T10:06:00Z","detail":"to=03-x; chars=20"}
        ]"""
        info = self.render_info(events)
        self.assertIn("+2 private Direktgespräche", info)
        self.assertNotIn("chars=", info)

    def test_no_direct_messages_omits_the_clause_entirely(self):
        events = """[{"event":"board_message","agent":"01-king","timestamp":"2026-01-01T10:00:00Z","detail":"message=Alpha topic."}]"""
        info = self.render_info(events)
        self.assertNotIn("private Direktgespräche", info)
        self.assertNotIn("+0", info)
