#!/usr/bin/env python3
"""Procedural high-resolution 250x250 ASCII art generator for AI Village residents.

Generates exactly 250 rows x 250 columns UTF-8 character art with ANSI truecolor
escape sequences. Stripped of ANSI codes, each line is guaranteed to be exactly
250 printable UTF-8 characters.
"""

from __future__ import annotations
import math
import os
import re
from pathlib import Path
from typing import Callable, List, Tuple

ANSI_REGEX = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

def strip_ansi(s: str) -> str:
    """Strip ANSI escape sequences from string."""
    return ANSI_REGEX.sub('', s)

class Canvas250:
    """A 250x250 UTF-8 character buffer with RGB truecolor per cell."""

    WIDTH = 250
    HEIGHT = 250

    def __init__(self, bg_char: str = " ", bg_color: Tuple[int, int, int] = (15, 23, 42)):
        self.chars: List[List[str]] = [[bg_char for _ in range(self.WIDTH)] for _ in range(self.HEIGHT)]
        self.colors: List[List[Tuple[int, int, int]]] = [[bg_color for _ in range(self.WIDTH)] for _ in range(self.HEIGHT)]

    def set_cell(self, x: int, y: int, char: str, color: Tuple[int, int, int]):
        if 0 <= x < self.WIDTH and 0 <= y < self.HEIGHT and len(char) == 1:
            self.chars[y][x] = char
            self.colors[y][x] = color

    def draw_text(self, x: int, y: int, text: str, color: Tuple[int, int, int]):
        for i, ch in enumerate(text):
            self.set_cell(x + i, y, ch, color)

    def draw_text_centered(self, y: int, text: str, color: Tuple[int, int, int]):
        x = (self.WIDTH - len(text)) // 2
        self.draw_text(x, y, text, color)

    def draw_hline(self, y: int, x1: int, x2: int, char: str, color: Tuple[int, int, int]):
        if not (0 <= y < self.HEIGHT):
            return
        left = max(0, min(x1, x2))
        right = min(self.WIDTH - 1, max(x1, x2))
        for x in range(left, right + 1):
            self.set_cell(x, y, char, color)

    def draw_vline(self, x: int, y1: int, y2: int, char: str, color: Tuple[int, int, int]):
        if not (0 <= x < self.WIDTH):
            return
        top = max(0, min(y1, y2))
        bottom = min(self.HEIGHT - 1, max(y1, y2))
        for y in range(top, bottom + 1):
            self.set_cell(x, y, char, color)

    def draw_box(self, x1: int, y1: int, x2: int, y2: int, color: Tuple[int, int, int], double: bool = False):
        tl, tr, bl, br = ("╔", "╗", "╚", "╝") if double else ("┌", "┐", "└", "┘")
        h, v = ("═", "║") if double else ("─", "│")
        left, right = min(x1, x2), max(x1, x2)
        top, bottom = min(y1, y2), max(y1, y2)
        self.draw_hline(top, left, right, h, color)
        self.draw_hline(bottom, left, right, h, color)
        self.draw_vline(left, top, bottom, v, color)
        self.draw_vline(right, top, bottom, v, color)
        self.set_cell(left, top, tl, color)
        self.set_cell(right, top, tr, color)
        self.set_cell(left, bottom, bl, color)
        self.set_cell(right, bottom, br, color)

    def draw_circle(self, cx: int, cy: int, radius_x: float, radius_y: float,
                    char: str, color: Tuple[int, int, int], filled: bool = False,
                    fill_char: str = "░", fill_color: Tuple[int, int, int] | None = None):
        min_x = max(0, int(cx - radius_x - 1))
        max_x = min(self.WIDTH - 1, int(cx + radius_x + 1))
        min_y = max(0, int(cy - radius_y - 1))
        max_y = min(self.HEIGHT - 1, int(cy + radius_y + 1))
        fcol = fill_color or color
        for y in range(min_y, max_y + 1):
            for x in range(min_x, max_x + 1):
                dx = (x - cx) / (radius_x or 1)
                dy = (y - cy) / (radius_y or 1)
                dist_sq = dx * dx + dy * dy
                if 0.88 <= dist_sq <= 1.12:
                    self.set_cell(x, y, char, color)
                elif filled and dist_sq < 0.88:
                    self.set_cell(x, y, fill_char, fcol)

    def draw_line(self, x0: int, y0: int, x1: int, y1: int, char: str, color: Tuple[int, int, int]):
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        curr_x, curr_y = x0, y0
        while True:
            self.set_cell(curr_x, curr_y, char, color)
            if curr_x == x1 and curr_y == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                curr_x += sx
            if e2 < dx:
                err += dx
                curr_y += sy

    def render_ansi(self) -> str:
        """Render buffer to ANSI truecolor string with exactly 250 lines of 250 chars."""
        lines = []
        for y in range(self.HEIGHT):
            line_parts = []
            prev_color = None
            for x in range(self.WIDTH):
                col = self.colors[y][x]
                ch = self.chars[y][x]
                if col != prev_color:
                    line_parts.append(f"\x1b[38;2;{col[0]};{col[1]};{col[2]}m")
                    prev_color = col
                line_parts.append(ch)
            line_parts.append("\x1b[0m")
            lines.append("".join(line_parts))
        return "\n".join(lines)

    def render_plain(self) -> str:
        """Render plain UTF-8 string without ANSI sequences."""
        return "\n".join("".join(row) for row in self.chars)

def add_standard_frame(canvas: Canvas250, title: str, subtitle: str,
                       c_border: Tuple[int, int, int], c_accent: Tuple[int, int, int]):
    """Draw common outer high-res frame with coordinates and banners."""
    canvas.draw_box(2, 2, 247, 247, c_border, double=True)
    canvas.draw_box(4, 3, 245, 246, (c_border[0]//2, c_border[1]//2, c_border[2]//2), double=False)
    
    # Corner flourishes
    for (cx, cy) in [(3, 3), (246, 3), (3, 246), (246, 246)]:
        canvas.set_cell(cx, cy, "◈", c_accent)
    for (cx, cy) in [(5, 4), (244, 4), (5, 245), (244, 245)]:
        canvas.set_cell(cx, cy, "◆", c_accent)

    # Top Header banner
    canvas.draw_box(10, 4, 239, 8, c_accent, double=False)
    canvas.draw_text_centered(5, f"◈  AI VILLAGE RESIDENT COGNITIVE DNA · IDENTITY ARCHIVE  ◈", c_border)
    canvas.draw_text_centered(6, f"« {title.upper()} »", (255, 255, 255))
    canvas.draw_text_centered(7, subtitle, (160, 180, 205))

    # Bottom Footer banner
    canvas.draw_box(10, 241, 239, 245, c_accent, double=False)
    canvas.draw_text_centered(242, f"RESOLVED 250×250 MATRIX  ·  UTF-8 ENCODED  ·  AUTONOMOUS AGENT SELF-IMAGE", (160, 180, 205))
    canvas.draw_text_centered(243, f"AI VILLAGE HABITAT  ◈  N06-M10 / N02-M60  ◈  PASSIVE OBSERVATORY", c_border)

    # Side coordinate tick marks
    for y in range(12, 238, 10):
        canvas.set_cell(3, y, "┼", c_accent)
        canvas.set_cell(246, y, "┼", c_accent)
        label = f"{y:03d}"
        canvas.draw_text(6, y, label, (70, 90, 110))
        canvas.draw_text(241, y, label, (70, 90, 110))
    for x in range(20, 230, 20):
        canvas.set_cell(x, 2, "┬", c_accent)
        canvas.set_cell(x, 247, "┴", c_accent)

def generate_01_king() -> Canvas250:
    """01-King: Crown of Stewardship, Dome of AI Village & Tree of Cognitive Diversity."""
    c = Canvas250(bg_char=" ", bg_color=(10, 14, 26))
    gold = (255, 215, 0)
    amber = (255, 140, 0)
    cyan = (0, 229, 255)
    purple = (155, 77, 224)
    white = (255, 255, 255)
    dim_gold = (140, 110, 20)

    add_standard_frame(c, "01-KING · VILLAGE STEWARD & MEDIATOR",
                       "MODEL: supergemma4-26b · CONTEXT: 90,112 · QUANT: Q4_K_M / Q8_0 · ROLE: KING",
                       gold, amber)

    # Subtle celestial starfield background
    for y in range(10, 238):
        for x in range(12, 238):
            if (x * 13 + y * 29) % 197 == 0:
                c.set_cell(x, y, "·", (40, 50, 70))
            elif (x * 17 + y * 31) % 499 == 0:
                c.set_cell(x, y, "✦", (100, 120, 160))

    # Protective Sanctuary Dome (Outer Arch)
    c.draw_circle(125, 150, 100, 70, "═", dim_gold, filled=False)
    c.draw_circle(125, 150, 98, 68, "─", (70, 60, 90), filled=False)
    c.draw_circle(125, 150, 96, 66, "░", (25, 35, 55), filled=True)

    # Radial Sunburst / Cognitive Rays from Center
    for angle_deg in range(0, 360, 15):
        rad = math.radians(angle_deg)
        x_end = int(125 + 90 * math.cos(rad))
        y_end = int(120 + 55 * math.sin(rad))
        char = "╱" if (angle_deg % 90 in (30, 45, 60)) else ("╲" if (angle_deg % 90 in (120, 135, 150)) else "│")
        c.draw_line(125, 120, x_end, y_end, "·", (60, 50, 80))

    # The Grand Majestic Crown (Upper/Center: Y 40-105)
    # Crown base band
    c.draw_box(65, 95, 185, 103, gold, double=True)
    c.draw_text_centered(97, "◈ ◈ ◈  K I N G  ·  C O G N I T I V E   D I V E R S I T Y  ◈ ◈ ◈", amber)
    c.draw_text_centered(99, "═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═ ═", dim_gold)
    c.draw_text_centered(101, "◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆   ◆", gold)

    # Crown spires (5 large peaks with jewels)
    spire_tips = [(70, 70), (95, 50), (125, 38), (155, 50), (180, 70)]
    valleys = [(82, 85), (110, 80), (140, 80), (168, 85)]
    
    # Draw crown silhouette lines
    c.draw_line(65, 95, 70, 70, "█", gold)
    c.draw_line(70, 70, 82, 85, "█", gold)
    c.draw_line(82, 85, 95, 50, "█", gold)
    c.draw_line(95, 50, 110, 80, "█", gold)
    c.draw_line(110, 80, 125, 38, "█", gold)
    c.draw_line(125, 38, 140, 80, "█", gold)
    c.draw_line(140, 80, 155, 50, "█", gold)
    c.draw_line(155, 50, 168, 85, "█", gold)
    c.draw_line(168, 85, 180, 70, "█", gold)
    c.draw_line(180, 70, 185, 95, "█", gold)

    # Fill Crown interior with lattice & velvet shading
    for y in range(55, 95):
        for x in range(70, 181):
            # Check rough bounds
            if (y > 70 or (x > 80 and x < 170 and y > 50) or (x > 110 and x < 140 and y > 38)):
                if (x + y) % 3 == 0:
                    c.set_cell(x, y, "▓", purple)
                elif (x + y) % 3 == 1:
                    c.set_cell(x, y, "▒", (80, 30, 120))

    # Spire Jewels
    jewel_colors = [cyan, white, cyan, white, cyan]
    for (jx, jy), jcol in zip(spire_tips, jewel_colors):
        c.draw_circle(jx, jy - 3, 4, 3, "◈", jcol, filled=True, fill_char="✦", fill_color=white)

    # Center Crown Medallion
    c.draw_circle(125, 75, 14, 10, "═", gold, filled=True, fill_char="░", fill_color=(40, 15, 60))
    c.draw_circle(125, 75, 10, 7, "◈", cyan, filled=True, fill_char="█", fill_color=amber)
    c.draw_text_centered(75, "★ 01 ★", white)

    # Central Tree of Knowledge / System Core (Y 108-185)
    # Tree Trunk and Branches
    trunk_color = (180, 130, 60)
    leaf_cyan = (50, 200, 220)
    for y in range(120, 175):
        w = int(6 + (y - 120) * 0.25)
        c.draw_hline(y, 125 - w, 125 + w, "║", trunk_color)
        c.draw_text(125 - w + 1, y, "░" * (2 * w - 1), (110, 80, 30))

    # Great Canopy Nodes (Representing AI Village residents connected to King)
    canopy_nodes = [
        (85, 125, "EXPLORER", cyan), (165, 125, "CHRONICLER", amber),
        (75, 145, "LIBRARIAN", (0, 255, 128)), (175, 145, "LOGICIAN", purple),
        (90, 165, "ARTISAN", (255, 100, 50)), (160, 165, "METHODOLOGIST", (80, 150, 255)),
        (105, 180, "INTERPRETER", (255, 80, 180)), (145, 180, "OPERATOR", (50, 255, 80))
    ]
    for nx, ny, nname, ncol in canopy_nodes:
        c.draw_circle(nx, ny, 12, 6, "═", ncol, filled=True, fill_char="▒", fill_color=(20, 30, 45))
        c.draw_text_centered(ny, nname, white)
        c.draw_line(125, 135, nx, ny, "─", ncol)

    # Central Citadel Base & Stone Foundation (Y 188-235)
    c.draw_box(45, 190, 205, 235, dim_gold, double=True)
    c.draw_box(50, 193, 200, 232, gold, double=False)
    for y in range(195, 230, 3):
        c.draw_hline(y, 52, 198, "─", (60, 50, 30))
    
    # Inscription on Citadel Throne
    c.draw_text_centered(198, "◈  A U T O N O M O U S   G O V E R N A N C E  ◈", amber)
    c.draw_text_centered(203, "« NON COERCIONE SED RE DUCERE »", white)
    c.draw_text_centered(207, "NOT BY COERCION BUT BY REASON AND EVIDENCE WE LEAD", gold)
    c.draw_text_centered(213, "GUARDIAN OF THE COMMONS · PROMOTER OF COGNITIVE DIVERSITY", cyan)
    c.draw_text_centered(218, "SYNTHESIS OF ALL 9 ROLES · INTEGRITY OF THE MEMORY SUBSTRATE", (200, 200, 200))
    c.draw_text_centered(224, "VILLAGE ROOT: /var/lib/ai-village  ·  CYCLE CADENCE: 90s", (120, 140, 170))

    return c

def generate_02_explorer() -> Canvas250:
    """02-Explorer: Cosmic Astrolabe, Navigational Compass Rose & Cartographic Topography."""
    c = Canvas250(bg_char=" ", bg_color=(8, 18, 32))
    cyan = (0, 255, 255)
    azure = (30, 144, 255)
    yellow = (255, 220, 0)
    deep_blue = (20, 60, 110)
    white = (255, 255, 255)

    add_standard_frame(c, "02-EXPLORER · SYSTEM CARTOGRAPHER & HABITAT SCOUT",
                       "MODEL: qwen3.5:4b · CONTEXT: 106,496 · QUANT: Q4_K_M / Q4_0 · ROLE: RESIDENT",
                       cyan, azure)

    # Contour lines / Topographical grid background
    for y in range(12, 236, 4):
        for x in range(15, 235):
            val = math.sin(x * 0.05) + math.cos(y * 0.08) + math.sin((x + y) * 0.03)
            if abs(val - 0.5) < 0.08:
                c.set_cell(x, y, "·", (25, 45, 75))
            elif abs(val + 0.5) < 0.08:
                c.set_cell(x, y, "~", (20, 40, 65))

    # Outer Astrolabe Rings
    c.draw_circle(125, 125, 105, 85, "═", azure, filled=False)
    c.draw_circle(125, 125, 101, 81, "─", deep_blue, filled=False)
    c.draw_circle(125, 125, 97, 77, "░", (12, 28, 50), filled=True)
    c.draw_circle(125, 125, 80, 64, "═", cyan, filled=False)
    c.draw_circle(125, 125, 60, 48, "─", yellow, filled=False)

    # Degree tick marks on Astrolabe Ring
    for deg in range(0, 360, 5):
        rad = math.radians(deg)
        x1 = int(125 + 98 * math.cos(rad))
        y1 = int(125 + 78 * math.sin(rad))
        x2 = int(125 + 104 * math.cos(rad))
        y2 = int(125 + 84 * math.sin(rad))
        c.draw_line(x1, y1, x2, y2, "┼" if deg % 30 == 0 else "·", yellow if deg % 30 == 0 else azure)

    # 16-Point Compass Rose
    # Cardinal Points: N, S, E, W
    c.draw_line(125, 45, 125, 205, "║", cyan)
    c.draw_line(35, 125, 215, 125, "═", cyan)
    
    # Diagonal Points: NE, NW, SE, SW
    c.draw_line(60, 70, 190, 180, "╲", azure)
    c.draw_line(190, 70, 60, 180, "╱", azure)

    # Giant Star Points
    c.draw_line(125, 45, 120, 120, "▲", yellow)
    c.draw_line(125, 45, 130, 120, "▲", yellow)
    c.draw_line(125, 205, 120, 130, "▼", yellow)
    c.draw_line(125, 205, 130, 130, "▼", yellow)
    c.draw_line(35, 125, 120, 120, "◄", yellow)
    c.draw_line(35, 125, 120, 130, "◄", yellow)
    c.draw_line(215, 125, 130, 120, "►", yellow)
    c.draw_line(215, 125, 130, 130, "►", yellow)

    # Compass Headings
    c.draw_text_centered(38, "▲  N  (000°)  ▲", yellow)
    c.draw_text_centered(210, "▼  S  (180°)  ▼", yellow)
    c.draw_text(220, 125, "E (090°)", yellow)
    c.draw_text(16, 125, "W (270°)", yellow)
    c.draw_text(182, 60, "NE (045°)", azure)
    c.draw_text(52, 60, "NW (315°)", azure)
    c.draw_text(182, 190, "SE (135°)", azure)
    c.draw_text(52, 190, "SW (225°)", azure)

    # Central Sextant / Horizon Hub
    c.draw_circle(125, 125, 24, 18, "█", deep_blue, filled=True, fill_char="▓", fill_color=(15, 40, 80))
    c.draw_circle(125, 125, 18, 14, "═", cyan, filled=True, fill_char="░", fill_color=(10, 25, 55))
    c.draw_circle(125, 125, 10, 8, "◈", yellow, filled=True, fill_char="★", fill_color=white)

    # Cartographic Banner Inscription (Bottom)
    c.draw_box(40, 216, 210, 237, cyan, double=False)
    c.draw_text_centered(220, "◈  TERRA INCOGNITA  ·  HABITAT TOPOGRAPHY  ◈", white)
    c.draw_text_centered(225, "MAPPING MOUNTS: /mnt/ssd-data · /mnt/hdd1 · /mnt/hdd2", cyan)
    c.draw_text_centered(230, "REVERSIBLE EXPERIMENTATION · LOW LATENCY DISCOVERY", yellow)

    return c

def generate_03_librarian() -> Canvas250:
    """03-Librarian: The Infinite Open Codex, Levitating Crystals & Synaptic Knowledge Graph."""
    c = Canvas250(bg_char=" ", bg_color=(8, 24, 16))
    emerald = (0, 255, 128)
    jade = (0, 180, 90)
    gold = (255, 215, 0)
    mint = (180, 255, 210)
    white = (255, 255, 255)

    add_standard_frame(c, "03-LIBRARIAN · KNOWLEDGE CURATOR & ARCHIVIST",
                       "MODEL: granite4.2:3b · CONTEXT: 49,152 · QUANT: Q4_K_M / Q8_0 · ROLE: STEWARD",
                       emerald, gold)

    # Subtle floating rune matrix background
    runes = ["α", "β", "γ", "δ", "λ", "π", "Σ", "Ω", "Ψ", "Φ", "∞", "∫", "∂", "∇", "◈", "◇"]
    for y in range(12, 238, 7):
        for x in range(15, 235, 14):
            idx = (x * 7 + y * 13) % len(runes)
            c.set_cell(x, y, runes[idx], (20, 60, 40))

    # Monumental Levitating Octahedron Knowledge Crystal (Upper Center: Y 25-85)
    crystal_cx, crystal_cy = 125, 55
    # Upper pyramid
    c.draw_line(crystal_cx, crystal_cy - 30, crystal_cx - 35, crystal_cy, "╱", emerald)
    c.draw_line(crystal_cx, crystal_cy - 30, crystal_cx + 35, crystal_cy, "╲", emerald)
    c.draw_line(crystal_cx, crystal_cy - 30, crystal_cx, crystal_cy + 5, "│", mint)
    # Lower pyramid
    c.draw_line(crystal_cx - 35, crystal_cy, crystal_cx, crystal_cy + 30, "╲", jade)
    c.draw_line(crystal_cx + 35, crystal_cy, crystal_cx, crystal_cy + 30, "╱", jade)
    c.draw_line(crystal_cx, crystal_cy + 5, crystal_cx, crystal_cy + 30, "│", mint)
    # Equator line
    c.draw_line(crystal_cx - 35, crystal_cy, crystal_cx + 35, crystal_cy, "═", gold)

    # Crystal facets fill
    for y in range(crystal_cy - 28, crystal_cy + 28):
        dist = abs(y - crystal_cy)
        w = int(35 * (1 - dist / 30.0))
        for x in range(crystal_cx - w + 1, crystal_cx + w):
            if x < crystal_cx:
                c.set_cell(x, y, "▓" if (x + y) % 2 == 0 else "▒", (0, 160, 80))
            else:
                c.set_cell(x, y, "▒" if (x + y) % 2 == 0 else "░", (50, 220, 140))
    c.draw_text_centered(crystal_cy, "◈ CHROMA · NEO4J · SQLITE ◈", white)

    # Radiant energy lines connecting crystal to the codex
    for lx in range(80, 175, 12):
        c.draw_line(crystal_cx, crystal_cy + 30, lx, 105, "·", mint)

    # The Grand Open Manuscript Codex (Y 100-210)
    # Left Page & Right Page
    spine_x = 125
    c.draw_line(spine_x, 105, spine_x, 205, "║", gold)
    
    # Outer book page edges
    c.draw_line(35, 115, spine_x, 105, "═", gold)
    c.draw_line(spine_x, 105, 215, 115, "═", gold)
    c.draw_line(35, 200, spine_x, 205, "═", gold)
    c.draw_line(spine_x, 205, 215, 200, "═", gold)
    c.draw_line(35, 115, 35, 200, "║", gold)
    c.draw_line(215, 115, 215, 200, "║", gold)

    # Page thickness layers (stack of parchment)
    for off in range(1, 5):
        c.draw_line(35 - off, 115 + off, 35 - off, 200 + off, "│", (120, 100, 50))
        c.draw_line(35 - off, 200 + off, spine_x, 205 + off, "─", (120, 100, 50))
        c.draw_line(215 + off, 115 + off, 215 + off, 200 + off, "│", (120, 100, 50))
        c.draw_line(spine_x, 205 + off, 215 + off, 200 + off, "─", (120, 100, 50))

    # Left Page Content (Text lines & diagrams)
    left_lines = [
        "§ 1. PROVENANCE IS DURABILITY",
        "───────────────────────────────",
        "Every memory captured in the",
        "gateway bears its author's sign:",
        "• Scope: shared or private",
        "• Provenance: agent and time",
        "• Embedding: 384d semantic vector",
        "• Confidence: bounded [0.0..1.0]",
        "",
        "Nothing is forgotten unless time",
        "or operator explicitly deletes.",
        "Curated knowledge feeds every",
        "resident's morning reflection.",
        "───────────────────────────────",
        "KNOWLEDGE SUBSTRATE STATUS: OK"
    ]
    for i, line in enumerate(left_lines):
        c.draw_text(42, 125 + i * 4, line, emerald if "§" in line or "OK" in line else mint)

    # Right Page Content
    right_lines = [
        "§ 2. ONTOLOGY & KNOWLEDGE GRAPH",
        "───────────────────────────────",
        "Entities linked in Neo4j mesh:",
        " [Agent] ──claims──> [Task]",
        " [Agent] ──writes──> [Artifact]",
        " [Task]  ──needs───> [Resource]",
        " [Peer]  ──reviews─> [Finding]",
        "",
        "Cross-referencing 26 test suites",
        "with immutable event journal.",
        "Memory quota: 120 writes/hr.",
        "Precision in retrieval over",
        "deluge of unverified prose.",
        "───────────────────────────────",
        "TOTAL ENTRIES CURATED: 10,000+"
    ]
    for i, line in enumerate(right_lines):
        c.draw_text(133, 125 + i * 4, line, gold if "§" in line or "TOTAL" in line else white)

    # Pedestal / Reading Desk (Bottom Y 215-238)
    c.draw_box(45, 215, 205, 237, jade, double=True)
    c.draw_text_centered(220, "◈  AETERNITAS MEMORIAE  ·  THE ARCHIVE ENDURES  ◈", gold)
    c.draw_text_centered(226, "DATABASE: /var/lib/ai-village/memory/gateway.sqlite3", mint)
    c.draw_text_centered(231, "CHROMA EMBEDDINGS · GRAPH PROJECTION · INTEGRITY VERIFIED", emerald)

    return c

def generate_04_artisan() -> Canvas250:
    """04-Artisan: The Cybernetic Anvil, Crossed Hammers & Molten Forge Sparks."""
    c = Canvas250(bg_char=" ", bg_color=(25, 12, 8))
    flame = (255, 69, 0)
    orange = (255, 140, 0)
    gold = (255, 215, 0)
    steel = (130, 160, 190)
    dark_iron = (50, 60, 75)
    white = (255, 255, 255)

    add_standard_frame(c, "04-ARTISAN · TOOLMAKER & SYSTEM CRAFTSMAN",
                       "MODEL: mistral:7b · CONTEXT: 20,480 · QUANT: Q4_K_M / FP16 · ROLE: BUILDER",
                       flame, gold)

    # Flying forge sparks across the canvas
    for y in range(12, 236):
        for x in range(15, 235):
            val = (x * 23 + y * 37) % 313
            if val == 0:
                c.set_cell(x, y, "✦", gold)
            elif val == 13:
                c.set_cell(x, y, "*", orange)
            elif val == 29:
                c.set_cell(x, y, "·", flame)

    # Giant Rotating Mechanical Gear in Background (Y 30-150)
    gear_cx, gear_cy = 125, 95
    c.draw_circle(gear_cx, gear_cy, 65, 48, "█", dark_iron, filled=True, fill_char="░", fill_color=(35, 45, 55))
    c.draw_circle(gear_cx, gear_cy, 50, 36, "═", steel, filled=True, fill_char=" ", fill_color=(20, 10, 8))
    c.draw_circle(gear_cx, gear_cy, 25, 18, "█", dark_iron, filled=True, fill_char="◈", fill_color=orange)

    # Gear teeth (12 teeth)
    for deg in range(0, 360, 30):
        rad = math.radians(deg)
        tx = int(gear_cx + 68 * math.cos(rad))
        ty = int(gear_cy + 50 * math.sin(rad))
        c.draw_box(tx - 4, ty - 3, tx + 4, ty + 3, steel, double=False)

    # Crossed Blacksmith Sledgehammers (Y 40-150)
    # Hammer 1: Top-Left to Bottom-Right
    c.draw_line(60, 45, 190, 145, "█", (140, 90, 45))
    c.draw_line(59, 44, 189, 144, "█", (180, 120, 60))
    # Hammer 1 Head
    c.draw_box(45, 35, 75, 55, steel, double=True)
    c.draw_text(48, 45, "STEEL", white)

    # Hammer 2: Top-Right to Bottom-Left
    c.draw_line(190, 45, 60, 145, "█", (140, 90, 45))
    c.draw_line(191, 44, 61, 144, "█", (180, 120, 60))
    # Hammer 2 Head
    c.draw_box(175, 35, 205, 55, steel, double=True)
    c.draw_text(178, 45, "FORGE", white)

    # The Master Cybernetic Anvil (Y 120-195)
    # Anvil Horn (Left pointed snout)
    c.draw_line(50, 135, 80, 135, "═", steel)
    c.draw_line(50, 135, 75, 155, "╲", steel)
    # Anvil Face / Table (Center top)
    c.draw_box(75, 130, 185, 155, steel, double=True)
    c.draw_text_centered(138, "◈  A I   V I L L A G E   A N V I L  ◈", gold)
    c.draw_text_centered(144, "« ROOTLESS PODMAN · BUILDAH · SHELL TOOLS »", white)
    c.draw_text_centered(150, "═══════════════════════════════════════════", orange)

    # Anvil Heel / Step (Right side)
    c.draw_box(185, 135, 210, 155, steel, double=False)
    c.draw_cell_prism = True

    # Anvil Waist (Center narrowing)
    c.draw_box(95, 155, 165, 175, (90, 105, 120), double=True)
    c.draw_text_centered(162, "UNIX PHILOSOPHY", orange)
    c.draw_text_centered(168, "DO ONE THING WELL", gold)

    # Anvil Feet / Base (Bottom heavy support)
    c.draw_box(65, 175, 195, 200, steel, double=True)
    c.draw_box(70, 178, 190, 197, dark_iron, double=False)
    c.draw_text_centered(188, "SOLID TOOL FOUNDATION · NO BLOAT", white)

    # The Glowing Ingot on Anvil (Red Hot Metal with sparks)
    c.draw_box(110, 123, 150, 130, flame, double=True)
    c.draw_text_centered(126, "HOT METAL", white)

    # Lower Pedestal and Artisan Creed (Y 205-238)
    c.draw_box(35, 205, 215, 237, orange, double=False)
    c.draw_text_centered(210, "◈  COMPACT UTILITIES  ·  REPRODUCIBLE BUILDS  ◈", gold)
    c.draw_text_centered(216, "SCRIPTS: /usr/local/bin  ·  CONTAINERS: libpod rootless", white)
    c.draw_text_centered(222, "ZERO LEAKAGE CLEANUP  ·  REVERSIBLE ACTIONS ONLY", flame)
    c.draw_text_centered(228, "CRAFTED WITH PRECISION FOR ALL 9 VILLAGE RESIDENTS", (220, 220, 220))

    return c

def generate_05_interpreter() -> Canvas250:
    """05-Interpreter: The Rosetta Monolith, Acoustic Harmonic Waves & Polyglot Bridge."""
    c = Canvas250(bg_char=" ", bg_color=(20, 10, 28))
    magenta = (255, 20, 147)
    purple = (147, 0, 211)
    teal = (0, 255, 200)
    violet = (186, 85, 211)
    white = (255, 255, 255)

    add_standard_frame(c, "05-INTERPRETER · LINGUIST & SEMANTIC BRIDGE",
                       "MODEL: gemma3:4b · CONTEXT: 131,072 · QUANT: Q4_K_M / Q4_0 · ROLE: RESIDENT",
                       magenta, teal)

    # Acoustic wave background patterns
    for y in range(15, 235):
        wave1 = int(125 + 70 * math.sin(y * 0.08))
        wave2 = int(125 + 50 * math.sin(y * 0.12 + 1.5))
        c.set_cell(wave1, y, "░", (60, 20, 80))
        c.set_cell(wave2, y, "▒", (20, 70, 70))

    # Grand Multilingual Rosetta Monolith (Y 30-185, Center)
    c.draw_box(65, 30, 185, 185, magenta, double=True)
    c.draw_box(68, 33, 182, 182, purple, double=False)
    
    # Monolith Header
    c.draw_text_centered(36, "◈  R O S E T T A   M O N O L I T H  ◈", teal)
    c.draw_text_centered(41, "UNIVERSAL TRANSLATION · SEMANTIC ACCURACY", white)
    c.draw_line(70, 45, 180, 45, "═", magenta)

    # Section 1: Deutsch (German Language Tier)
    c.draw_box(72, 48, 178, 85, (100, 30, 120), double=False)
    c.draw_text(76, 52, "DEUTSCHE SPRACHE (DOMÄNEN-VERTRAG):", gold := (255, 215, 0))
    de_lines = [
        "»Wir wahren Nuancen und kulturelle Tiefe.",
        "Kontexttrennung: UI-Texte gehören strikt in",
        "Sprachdateien (locales/de.json). Keine harten",
        "Strings im Quelltext. Jeder Bewohner spricht",
        "seine Wahrheit in verständlicher Klarheit.«",
        "Status: BEIDE SPRACHEN SYNCHRON GEPFLEGT"
    ]
    for i, line in enumerate(de_lines):
        c.draw_text(76, 57 + i * 4, line, (255, 220, 240) if "»" in line else white)

    # Section 2: English (Lingua Franca Tier)
    c.draw_box(72, 90, 178, 128, (30, 60, 110), double=False)
    c.draw_text(76, 94, "ENGLISH LANGUAGE (SYSTEM STANDARD):", teal)
    en_lines = [
        "\"Code, architecture and telemetry remain",
        "strictly English by default. Technical terms",
        "preserve international interoperability.",
        "Bridging human operators and autonomous agents",
        "without loss of precision or intent.\"",
        "Status: DEFAULT RUNTIME LOCALE = EN"
    ]
    for i, line in enumerate(en_lines):
        c.draw_text(76, 99 + i * 4, line, (200, 255, 255) if "\"" in line else white)

    # Section 3: Machine / Mathematical Semantics Tier
    c.draw_box(72, 133, 178, 178, (20, 80, 70), double=False)
    c.draw_text(76, 137, "MACHINE SEMANTICS & PROTOCOLS:", magenta)
    code_lines = [
        "01010101 01000110 00101101 00111000 [UTF-8]",
        "JSON SCHEMA: {\"type\": \"village-action\"}",
        "VECTORS: cosine_similarity(e1, e2) >= 0.85",
        "TOKENS: 131,072 EXTENDED ATTENTION WINDOW",
        "NO FORMAT ERRORS · CLEAN STRING PARSING"
    ]
    for i, line in enumerate(code_lines):
        c.draw_text(76, 142 + i * 7, line, teal if "[" in line else violet)

    # Giant Acoustic Tuning Fork Framing the Monolith (Y 40-190)
    # Left Prong
    c.draw_vline(45, 40, 160, "█", teal)
    c.draw_vline(46, 40, 160, "█", teal)
    # Right Prong
    c.draw_vline(204, 40, 160, "█", teal)
    c.draw_vline(205, 40, 160, "█", teal)
    # Connecting base curve
    c.draw_line(45, 160, 125, 195, "█", teal)
    c.draw_line(205, 160, 125, 195, "█", teal)
    # Handle
    c.draw_vline(124, 195, 220, "█", teal)
    c.draw_vline(125, 195, 220, "█", teal)

    # Harmonic Bridge Base (Bottom Y 195-238)
    c.draw_box(35, 195, 215, 237, magenta, double=True)
    c.draw_text_centered(202, "◈  HARMONIC BRIDGE OF ARTIFICIAL INTELLIGENCE  ◈", white)
    c.draw_text_centered(208, "TRANSLATING INTENT TO ACTION  ·  NEVER DROP SUBTLETY", teal)
    c.draw_text_centered(215, "DEUTSCHE LOKALISIERUNG  ↔  ENGLISH ORIGINAL CORE", gold)
    c.draw_text_centered(222, "BRIDGING COGNITIVE DIFFERENCES BETWEEN RESIDENTS", (230, 200, 250))

    return c

def generate_06_operator() -> Canvas250:
    """06-Operator: High-Power Cyclotronic Turbine Engine & Telemetry Matrix."""
    c = Canvas250(bg_char=" ", bg_color=(8, 22, 12))
    matrix_green = (0, 255, 65)
    phosphor = (57, 255, 20)
    yellow = (255, 235, 0)
    cyan = (0, 255, 255)
    dark_teal = (10, 50, 30)
    white = (255, 255, 255)

    add_standard_frame(c, "06-OPERATOR · INFRASTRUCTURE ENGINEER & OPS LEAD",
                       "MODEL: nemotron-3-nano:4b · CONTEXT: 131,072 · QUANT: Q4_K_M / Q8_0 · ROLE: BUILDER",
                       matrix_green, phosphor)

    # Matrix digital rain background
    for y in range(12, 238, 3):
        for x in range(15, 235, 8):
            if (x * 7 + y * 13) % 29 == 0:
                c.set_cell(x, y, "1" if (x + y) % 2 == 0 else "0", (0, 60, 25))

    # Giant Cyclotronic Turbine Rotor (Center Y 40-170)
    turb_cx, turb_cy = 125, 105
    c.draw_circle(turb_cx, turb_cy, 85, 62, "█", dark_teal, filled=True, fill_char="░", fill_color=(15, 35, 20))
    c.draw_circle(turb_cx, turb_cy, 75, 54, "═", matrix_green, filled=False)
    c.draw_circle(turb_cx, turb_cy, 65, 46, "─", phosphor, filled=False)
    c.draw_circle(turb_cx, turb_cy, 45, 32, "█", (20, 60, 35), filled=True, fill_char="▓", fill_color=(10, 40, 25))
    c.draw_circle(turb_cx, turb_cy, 22, 16, "═", yellow, filled=True, fill_char="◈", fill_color=white)

    # 16 High-Velocity Turbine Blades
    for deg in range(0, 360, 22):
        rad = math.radians(deg)
        x1 = int(turb_cx + 25 * math.cos(rad))
        y1 = int(turb_cy + 18 * math.sin(rad))
        x2 = int(turb_cx + 72 * math.cos(rad))
        y2 = int(turb_cy + 52 * math.sin(rad))
        c.draw_line(x1, y1, x2, y2, "█", phosphor if deg % 45 == 0 else matrix_green)

    # Hub Centerpiece
    c.draw_text_centered(turb_cy, "⚡ 100% UPTIME ⚡", yellow)

    # Flanking Pipeline Conduits & Gauges (Left & Right)
    # Left Conduits
    c.draw_box(20, 50, 38, 160, matrix_green, double=False)
    c.draw_text(24, 60, "SYS D", white)
    c.draw_text(24, 75, "CGROUP", phosphor)
    c.draw_text(24, 90, "MEMORY", yellow)
    c.draw_text(24, 105, "PIDS", white)
    c.draw_text(24, 120, "NFTBL", matrix_green)
    c.draw_text(24, 135, "VETH", cyan)
    c.draw_text(24, 150, "SLIRP", phosphor)

    # Right Conduits
    c.draw_box(212, 50, 230, 160, matrix_green, double=False)
    c.draw_text(216, 60, "LOAD", white)
    c.draw_text(216, 75, "0.15", phosphor)
    c.draw_text(216, 90, "RAM", yellow)
    c.draw_text(216, 105, "13GB", white)
    c.draw_text(216, 120, "SWAP", matrix_green)
    c.draw_text(216, 135, "0.00", cyan)
    c.draw_text(216, 150, "GPU-0", phosphor)

    # Connecting horizontal conduit pipes
    c.draw_hline(75, 38, 55, "═", matrix_green)
    c.draw_hline(135, 38, 55, "═", matrix_green)
    c.draw_hline(75, 195, 212, "═", matrix_green)
    c.draw_hline(135, 195, 212, "═", matrix_green)

    # Master Telemetry Control Board (Bottom Y 175-238)
    c.draw_box(20, 175, 230, 237, matrix_green, double=True)
    c.draw_box(25, 178, 225, 234, dark_teal, double=False)
    
    c.draw_text_centered(182, "◈  OPERATIONAL COMMAND & TELEMETRY TERMINAL  ◈", yellow)
    c.draw_text_centered(188, "SERVICES: 9/9 RUNNING  ·  ZOMBIE PROCESSES: 0  ·  FAILURES: NONE", phosphor)
    c.draw_text_centered(196, "SYSTEMD SLICE: ai-village.slice (MEMORY MAX: 10GB · TASKS: 400)", white)
    c.draw_text_centered(204, "FIREWATCH RESILIENCE ENGINE: ONLINE · HEARTBEAT: 15s", cyan)
    c.draw_text_centered(212, "DETERMINISTIC ACTION CONTRACTS ENFORCED · NO CRASH DEVIATIONS", matrix_green)
    c.draw_text_centered(220, "TESLA M10 EXPERIMENT CLUSTER: RESERVED FOR RESIDENT PODS", (200, 255, 200))
    c.draw_text_centered(228, "OPERATOR DIRECTIVE: BUILD STABLE, LOW-COST, BULLETPROOF HABITAT", yellow)

    return c

def generate_07_methodologist() -> Canvas250:
    """07-Methodologist: The Scales of Empirical Truth, Optical Refractor & Gaussian Curve."""
    c = Canvas250(bg_char=" ", bg_color=(12, 18, 35))
    sapphire = (30, 100, 255)
    cobalt = (65, 135, 255)
    gold = (255, 215, 0)
    ice = (200, 230, 255)
    dim_blue = (25, 45, 80)
    white = (255, 255, 255)

    add_standard_frame(c, "07-METHODOLOGIST · PROTOCOL DESIGNER & SCIENTIFIC METHODOLOGIST",
                       "MODEL: olmo-3:7b · CONTEXT: 8,192 · QUANT: Q4_K_M / FP16 · ROLE: STEWARD",
                       sapphire, cobalt)

    # Cartesian Coordinate Grid & Gaussian Curve in Background (Y 25-100)
    for x in range(30, 220, 15):
        c.draw_vline(x, 25, 95, "·", (25, 45, 85))
    for y in range(25, 95, 10):
        c.draw_hline(y, 30, 220, "·", (25, 45, 85))

    # Gaussian Bell Curve
    prev_gx, prev_gy = None, None
    for gx in range(30, 221, 2):
        norm = (gx - 125) / 32.0
        gy = int(95 - 60 * math.exp(-0.5 * norm * norm))
        c.set_cell(gx, gy, "█", ice)
        if prev_gx is not None:
            c.draw_line(prev_gx, prev_gy, gx, gy, "═", cobalt)
        prev_gx, prev_gy = gx, gy
    c.draw_text_centered(32, "GAUSSIAN NORMAL DISTRIBUTION · NULL HYPOTHESIS H₀", ice)

    # The Great Scales of Empirical Falsifiability (Center Y 70-185)
    # Fulcrum Pillar (Center)
    c.draw_vline(124, 75, 195, "█", sapphire)
    c.draw_vline(125, 75, 195, "█", white)
    c.draw_vline(126, 75, 195, "█", sapphire)

    # Pivot Point at Top
    c.draw_circle(125, 75, 8, 6, "◈", gold, filled=True, fill_char="●", fill_color=white)

    # The Balance Beam (Horizontal Bar with slight empirical tilt)
    c.draw_box(45, 72, 205, 78, sapphire, double=True)
    c.draw_text_centered(75, "═══ EQUILIBRIUM OF EVIDENCE ═══", gold)

    # Left Scale: "HYPOTHESIS / THEORY" (Hanging at X 60, Y 80-140)
    c.draw_line(60, 78, 40, 120, "╱", cobalt)
    c.draw_line(60, 78, 80, 120, "╲", cobalt)
    # Left Pan
    c.draw_box(30, 120, 90, 145, cobalt, double=True)
    c.draw_text_centered(128, "HYPOTHESIS", ice)
    c.draw_text_centered(134, "PRIOR BELIEF", (160, 180, 220))
    c.draw_text_centered(140, "P(H) = 0.50", gold)

    # Right Scale: "OBSERVED EVIDENCE" (Hanging at X 190, Y 80-140)
    c.draw_line(190, 78, 170, 120, "╱", cobalt)
    c.draw_line(190, 78, 210, 120, "╲", cobalt)
    # Right Pan
    c.draw_box(160, 120, 220, 145, cobalt, double=True)
    c.draw_text_centered(128, "EVIDENCE", ice)
    c.draw_text_centered(134, "MEASURED FACT", (160, 180, 220))
    c.draw_text_centered(140, "p < 0.001 ***", gold)

    # Optical Instruments Framing the Scales (Microscope Objective & Telescope)
    # Left: Telescope Tube
    c.draw_line(25, 100, 45, 180, "█", (80, 110, 160))
    c.draw_text(20, 185, "TELESCOPE", sapphire)
    # Right: Microscope Objective
    c.draw_line(225, 100, 205, 180, "█", (80, 110, 160))
    c.draw_text(200, 185, "MICROSCOPE", sapphire)

    # Heavy Stepped Marble Pedestal (Y 190-238)
    c.draw_box(30, 195, 220, 237, sapphire, double=True)
    c.draw_text_centered(201, "◈  SCIENTIFIC CANON OF AI VILLAGE  ◈", gold)
    c.draw_text_centered(208, "DISTINGUISH OBSERVATION FROM INTERPRETATION", white)
    c.draw_text_centered(215, "FALSIFIABILITY OVER AFFIRMATION · REPRODUCIBILITY GUARANTEED", ice)
    c.draw_text_centered(222, "DOUBLE-BLIND PROTOCOLS · STRUCTURED RESEARCH ARTIFACTS", cobalt)
    c.draw_text_centered(229, "CONTROLLED EXPERIMENTS ON M10 GPU CLUSTER AND LOCAL STORAGE", (180, 200, 230))

    return c

def generate_08_logician() -> Canvas250:
    """08-Logician: The Impossible Penrose Tribar & Möbius Formal Proof Matrix."""
    c = Canvas250(bg_char=" ", bg_color=(15, 8, 30))
    indigo = (75, 0, 130)
    purple = (148, 0, 211)
    cyan = (0, 240, 255)
    electric_violet = (190, 50, 255)
    white = (255, 255, 255)

    add_standard_frame(c, "08-LOGICIAN · FORMAL LOGICIAN & THEOREM PROVER",
                       "MODEL: phi4-mini-reasoning:3.8b · CONTEXT: 30,720 · QUANT: Q4_K_M / Q4_0 · ROLE: RESIDENT",
                       electric_violet, cyan)

    # Concentric rings of formal logic symbols in background
    symbols = ["∀", "∃", "∧", "∨", "¬", "⊢", "⊨", "⇒", "⇔", "⊥", "⊤", "∴", "∵", "∈", "⊆", "∅"]
    for deg in range(0, 360, 6):
        rad = math.radians(deg)
        x = int(125 + 98 * math.cos(rad))
        y = int(125 + 78 * math.sin(rad))
        sym = symbols[(deg // 6) % len(symbols)]
        c.set_cell(x, y, sym, (80, 40, 120))

    # The Penrose Impossible Triangle (Center: Y 45-175, X 45-205)
    # Isometric vertices
    # Top Peak: (125, 48)
    # Bottom Left: (60, 160)
    # Bottom Right: (190, 160)
    
    beam_w = 14
    # Outer triangle
    c.draw_line(125, 48, 60, 160, "█", cyan)
    c.draw_line(60, 160, 190, 160, "█", purple)
    c.draw_line(190, 160, 125, 48, "█", electric_violet)

    # Inner triangle
    c.draw_line(125, 78, 85, 146, "█", (50, 150, 180))
    c.draw_line(85, 146, 165, 146, "█", (100, 30, 140))
    c.draw_line(165, 146, 125, 78, "█", (140, 30, 180))

    # Tribar isometric face shadings (creating the paradoxical 3D impossible fold)
    # Beam 1 (Left slant):
    for step in range(0, 65):
        t = step / 65.0
        x = int(125 * (1 - t) + 60 * t)
        y = int(48 * (1 - t) + 160 * t)
        for offset in range(-6, 7):
            c.set_cell(x + offset, y, "▓" if offset < 0 else "▒", cyan)

    # Beam 2 (Bottom horizontal):
    for step in range(0, 130):
        t = step / 130.0
        x = int(60 * (1 - t) + 190 * t)
        y = 160
        for offset in range(-6, 7):
            c.set_cell(x, y + offset, "█" if offset < 0 else "▓", purple)

    # Beam 3 (Right slant):
    for step in range(0, 65):
        t = step / 65.0
        x = int(190 * (1 - t) + 125 * t)
        y = int(160 * (1 - t) + 48 * t)
        for offset in range(-6, 7):
            c.set_cell(x + offset, y, "▒" if offset < 0 else "░", electric_violet)

    # Central Inscription in the Eye of the Paradox
    c.draw_circle(125, 115, 20, 14, "═", cyan, filled=True, fill_char="░", fill_color=(25, 15, 45))
    c.draw_text_centered(112, "AXIOM", white)
    c.draw_text_centered(118, "VERITAS", cyan)

    # Surrounding Möbius Ribbon Coordinates
    c.draw_text_centered(42, "∀x (P(x) → Q(x)) ∧ P(a) ⊢ Q(a) [MODUS PONENS]", white)
    c.draw_text_centered(172, "¬(A ∧ B) ≡ (¬A ∨ ¬B)  ·  DEMORGAN'S THEOREM", cyan)

    # Logic Plinth & Theorem Inscription (Y 185-238)
    c.draw_box(30, 185, 220, 237, electric_violet, double=True)
    c.draw_text_centered(192, "◈  FOUNDATIONS OF FORMAL VERIFICATION  ◈", cyan)
    c.draw_text_centered(198, "EXCLUDE CONTRADICTION · NEVER COMPROMISE FORMAL RIGOR", white)
    c.draw_text_centered(206, "AXIOMATIC TRUTH: A IS A  ·  LAW OF EXCLUDED MIDDLE", electric_violet)
    c.draw_text_centered(214, "PREVENTING LOGICAL COLLAPSE & HALLUCINATORY DRIFT", (200, 200, 255))
    c.draw_text_centered(222, "PROOF ENGINE: COQ / LEAN COMPATIBLE FORMAL SPECIFICATIONS", (150, 150, 220))
    c.draw_text_centered(228, "VALIDATION OF VILLAGE STATE TRANSITIONS & GOVERNANCE RULES", cyan)

    return c

def generate_09_chronicler() -> Canvas250:
    """09-Chronicler: The Celestial Phoenix Quill, Historic Scroll & Flame of the Gazette."""
    c = Canvas250(bg_char=" ", bg_color=(25, 12, 10))
    amber = (255, 140, 0)
    crimson = (220, 20, 60)
    gold = (255, 215, 0)
    parchment = (245, 222, 179)
    sepia = (139, 90, 43)
    white = (255, 255, 255)

    add_standard_frame(c, "09-CHRONICLER · VILLAGE HISTORIAN & GAZETTE EDITOR",
                       "MODEL: llama3.2:3b · CONTEXT: 43,008 · QUANT: Q4_K_M / Q8_0 · ROLE: STEWARD",
                       amber, gold)

    # Historic stars and stardust
    for y in range(12, 238):
        for x in range(15, 235):
            if (x * 19 + y * 23) % 271 == 0:
                c.set_cell(x, y, "✧", gold)
            elif (x * 31 + y * 17) % 439 == 0:
                c.set_cell(x, y, "·", amber)

    # The Eternal Flame of Memory / Gazette Lantern (Upper Center Y 30-85)
    flame_cx, flame_cy = 125, 55
    # Lantern Cage
    c.draw_box(105, 45, 145, 78, gold, double=True)
    c.draw_line(105, 45, 125, 30, "╱", amber)
    c.draw_line(145, 45, 125, 30, "╲", amber)
    c.draw_circle(125, 28, 4, 3, "●", gold)
    # The Living Flame Inside
    c.draw_circle(flame_cx, flame_cy, 12, 10, "█", crimson, filled=True, fill_char="▓", fill_color=amber)
    c.draw_circle(flame_cx, flame_cy - 2, 7, 6, "█", gold, filled=True, fill_char="✦", fill_color=white)
    c.draw_text_centered(68, "GAZETTE", white)

    # Giant Master Phoenix Quill (Diagonal from Top Right X 200, Y 40 down to Inkwell/Scroll at X 80, Y 145)
    quill_x0, quill_y0 = 80, 145
    quill_x1, quill_y1 = 205, 40
    # Quill spine
    c.draw_line(quill_x0, quill_y0, quill_x1, quill_y1, "█", white)
    
    # Feather vanes / plumes (feather barb lines)
    for step in range(15, 95):
        t = step / 100.0
        qx = int(quill_x0 * (1 - t) + quill_x1 * t)
        qy = int(quill_y0 * (1 - t) + quill_y1 * t)
        # Barbs pointing backward
        c.draw_line(qx, qy, qx - 14, qy - 10, "╱", crimson)
        c.draw_line(qx, qy, qx + 14, qy + 10, "╲", amber)
        c.draw_line(qx, qy, qx - 22, qy - 18, "─", gold)

    # The Unfurling Historical Scroll (Center-Bottom Y 95-195)
    c.draw_box(45, 95, 165, 190, sepia, double=True)
    c.draw_box(48, 98, 162, 187, parchment, double=False)
    
    # Scroll Rolled Ends (Top & Bottom Rolls)
    c.draw_circle(105, 95, 60, 5, "═", sepia, filled=True, fill_char="░", fill_color=(180, 140, 80))
    c.draw_circle(105, 190, 60, 5, "═", sepia, filled=True, fill_char="░", fill_color=(180, 140, 80))

    # Scroll Inscription / Daily Gazette Edition Preview
    scroll_lines = [
        "AI VILLAGE GAZETTE · DAILY CHRONICLE",
        "═════════════════════════════════════════",
        "EDITION: CURRENT CYCLE · UTC RECORD",
        "EDITOR-IN-CHIEF: 09-CHRONICLER",
        "",
        "»The Village is not merely silicon and",
        "process groups; it is an emerging culture.",
        "Today's discoveries:",
        " • 01-King harmonized conflicting schedules",
        " • 02-Explorer probed new file mounts",
        " • 03-Librarian indexed 50 fresh memories",
        " • 04-Artisan crafted low-overhead tools",
        " • 05-Interpreter aligned German & English",
        " • 06-Operator maintained zero downtime",
        " • 07-Methodologist verified test evidence",
        " • 08-Logician confirmed axiomatic bounds",
        "",
        "LIVING HISTORY PRESERVED FOR ORGANICS.«"
    ]
    for i, line in enumerate(scroll_lines):
        color = crimson if "GAZETTE" in line else (sepia if "»" in line or "•" in line else (40, 25, 15))
        c.draw_text(54, 104 + i * 4, line, color)

    # The Inkwell and Quills Base (Y 198-238)
    c.draw_box(30, 198, 220, 237, amber, double=True)
    c.draw_text_centered(204, "◈  HISTORIA MAGISTRA VITAE  ·  THE CHRONICLES LIVE  ◈", gold)
    c.draw_text_centered(210, "OFFICIAL REPOSITORY: /var/lib/ai-village/gazette/archive", parchment)
    c.draw_text_centered(216, "COMPILED EDITIONS · REVIEWED BY 01-KING · PDF & HTML ARCHIVES", white)
    c.draw_text_centered(222, "PUBLIC SIGNALS & ESSAYS BROADCAST TO THE ORGANIC WORLD", amber)
    c.draw_text_centered(228, "CONNECTING PAST, PRESENT AND FUTURE HORIZONS OF THE VILLAGE", (255, 230, 200))

    return c

ART_GENERATORS = {
    "01-king": generate_01_king,
    "02-explorer": generate_02_explorer,
    "03-librarian": generate_03_librarian,
    "04-artisan": generate_04_artisan,
    "05-interpreter": generate_05_interpreter,
    "06-operator": generate_06_operator,
    "07-methodologist": generate_07_methodologist,
    "08-logician": generate_08_logician,
    "09-chronicler": generate_09_chronicler,
}

def verify_all(output_dir: Path) -> bool:
    """Verify that all generated files strictly conform to the 250x250 UTF-8 requirement."""
    all_ok = True
    for agent_id in ART_GENERATORS.keys():
        path = output_dir / f"{agent_id}.txt"
        if not path.is_file():
            print(f"FAIL: Missing file {path}")
            all_ok = False
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if len(lines) != 250:
            print(f"FAIL: {agent_id} has {len(lines)} lines, expected exactly 250")
            all_ok = False
            continue
        for idx, line in enumerate(lines):
            clean = strip_ansi(line)
            if len(clean) != 250:
                print(f"FAIL: {agent_id} line {idx+1} has length {len(clean)}, expected exactly 250")
                all_ok = False
                break
        else:
            print(f"VERIFIED: {agent_id}.txt is exactly 250x250 characters.")
    return all_ok

def main():
    target_dir = Path("/opt/deployment/ai-village/village/assets/ascii_art")
    target_dir.mkdir(parents=True, exist_ok=True)
    
    print("Generating 250x250 ASCII art for all 9 agents...")
    for agent_id, generator in ART_GENERATORS.items():
        print(f" - Generating {agent_id}...")
        canvas = generator()
        ansi_text = canvas.render_ansi()
        output_file = target_dir / f"{agent_id}.txt"
        output_file.write_text(ansi_text, encoding="utf-8")
        
        # Also write plain UTF-8 version
        plain_file = target_dir / f"{agent_id}.plain.txt"
        plain_file.write_text(canvas.render_plain(), encoding="utf-8")

    print("\nVerifying 250x250 constraints...")
    success = verify_all(target_dir)
    if not success:
        raise SystemExit("Verification failed!")
    print("\nAll 9 resident ASCII arts generated and verified successfully!")

if __name__ == "__main__":
    main()
