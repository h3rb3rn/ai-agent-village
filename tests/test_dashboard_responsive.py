"""P44: the operator reported the Auditor dashboard "table" (the auditor-panel
stat cards) as not responsive. Root cause: #memory-panel's grid/card layout
had a dedicated mobile treatment (auto-fit grid, stacked cards, ellipsis
labels at <=560px), but #auditor-panel reuses the exact same .memory-services/
.memory-agents classes (see web/observatory.html) without that treatment ever
being extended to it - it fell back to the generic, non-stacking base rule.

Also covers two further page-wide gaps found in the same pass while auditing
every fixed-width/fixed-column layout against @media coverage, per the
operator's follow-up ask ("nachher die ganze Webseite responsive"):
.container-card (fixed 240px, no mobile stacking) and .connection-card-grid
(fixed 3 columns, no narrower-viewport override anywhere).

Uses a tiny brace-aware parser rather than a single regex, because this
stylesheet's rules are comma-separated selector lists sharing one body
(``#memory-panel .x,\\n#auditor-panel .x{...}``), which a naive
``selector{body}`` regex cannot associate correctly, and because a selector
legitimately repeats once per breakpoint."""
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

CSS = (Path(__file__).resolve().parents[1] / "web/observatory.css").read_text(encoding="utf-8")


def parse_blocks(css: str, start: int = 0, end: int = None) -> List[Tuple[str, str]]:
    """Top-level (selector_or_at_rule, body) pairs between start and end,
    scanning by brace depth so nested @media content is never mistaken for
    the end of an enclosing block."""
    if end is None:
        end = len(css)
    blocks = []
    i = start
    while i < end:
        open_brace = css.find("{", i, end)
        if open_brace == -1:
            break
        header = css[i:open_brace].strip()
        depth = 1
        j = open_brace + 1
        while depth > 0 and j < end:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        body = css[open_brace + 1:j - 1]
        blocks.append((header, body))
        i = j
    return blocks


def selector_bodies(css: str) -> Dict[str, List[str]]:
    """Map each individual selector (comma-list entries split out) to every
    body it is given, at the top level and inside every @media block."""
    result: Dict[str, List[str]] = {}

    def register(header: str, body: str):
        for selector in header.split(","):
            selector = selector.strip()
            if selector:
                result.setdefault(selector, []).append(body)

    for header, body in parse_blocks(css):
        if header.startswith("@media"):
            for inner_header, inner_body in parse_blocks(body):
                register(inner_header, inner_body)
        else:
            register(header, body)
    return result


RULES = selector_bodies(CSS)


class AuditorPanelMatchesMemoryPanelTests(unittest.TestCase):
    """For every #memory-panel-scoped responsive rule, an equivalent
    #auditor-panel-scoped rule must exist - the exact class of bug reported."""

    SUFFIXES = [
        " .memory-services", " .memory-agents", " .memory-service",
        " .memory-agent", " .memory-service small",
        " .memory-agent>span:first-child", " .memory-agent>span:first-child b",
        " .memory-agent>span:first-child small", " .memory-agent>strong",
        " .memory-agent>span:nth-child(3)", " .memory-agent>time",
    ]

    def test_every_memory_panel_rule_has_an_auditor_panel_counterpart(self):
        for suffix in self.SUFFIXES:
            memory_bodies = RULES.get("#memory-panel" + suffix, [])
            auditor_bodies = RULES.get("#auditor-panel" + suffix, [])
            self.assertTrue(memory_bodies, f"no #memory-panel{suffix} rule found - test is stale")
            self.assertEqual(
                sorted(memory_bodies), sorted(auditor_bodies),
                f"#auditor-panel{suffix} does not match #memory-panel{suffix}",
            )

    def test_the_narrow_viewport_grid_actually_collapses_to_one_column(self):
        for selector in ("#memory-panel .memory-services", "#memory-panel .memory-agents",
                         "#auditor-panel .memory-services", "#auditor-panel .memory-agents"):
            bodies = RULES.get(selector, [])
            self.assertTrue(any("grid-template-columns:1fr" in b for b in bodies),
                            f"{selector} never collapses to a single column at any breakpoint")


class RemainingFixedWidthLayoutsTests(unittest.TestCase):
    """Two further gaps found auditing every fixed-width/fixed-column rule in
    the stylesheet against @media coverage: neither had any mobile override
    before this fix."""

    def test_container_card_has_a_max_width_safety_net(self):
        bodies = RULES.get(".container-card", [])
        self.assertTrue(any("max-width:100%" in b for b in bodies))

    def test_container_card_stacks_full_width_on_narrow_viewports(self):
        bodies = RULES.get(".container-card", [])
        self.assertTrue(any("width:100%" in b and "display:block" in b for b in bodies))

    def test_connection_card_grid_narrows_in_two_steps(self):
        bodies = RULES.get(".connection-card-grid", [])
        self.assertTrue(any("repeat(2," in b for b in bodies), bodies)
        self.assertTrue(any(b.strip() == "grid-template-columns:1fr" for b in bodies), bodies)


if __name__ == "__main__":
    unittest.main()
