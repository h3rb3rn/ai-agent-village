"""Pure-Python PDF writer for compiled Gazette editions (Stufe 4, Teil 2 of
docs/analysis/GAZETTE-PLAN-2026-09-28.md).

No external dependency: N06-M10 has no PDF tool installed (wkhtmltopdf,
weasyprint, reportlab, fpdf - all checked, none present) and AGENTS.md
prefers the standard library. Same zero-dependency principle already used
for the MCP server (P30) - a self-written writer emitting the PDF 1.4
object/xref structure directly.

Uses only the PDF core-14 fonts (Courier / Courier-Bold), which every PDF
reader must support without any embedded font data. Courier specifically -
not Helvetica/Times - because it is truly fixed-width (exactly 600/1000 em
per its AFM metrics), so line-wrapping math is exact from the point size
alone; a proportional font would need a real per-glyph width table this
module deliberately does not carry.

Text is written as literal (uncompressed) PDF strings encoded with
cp1252/WinAnsiEncoding - covers German umlauts and ß correctly; any
character outside that encoding (e.g. stray emoji a model might produce)
is replaced with '?' rather than raising, matching this project's existing
preference for a always-succeeding deterministic renderer over a perfect
one (see compile_edition()'s docstring).
"""
from __future__ import annotations
from typing import Any, Dict, List, Tuple

PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN = 50
BODY_SIZE = 10
HEADING_SIZE = 14
TITLE_SIZE = 20
CHAR_WIDTH_FACTOR = 0.6  # Courier: exactly 600/1000 em, fixed-width
LINE_GAP = 1.35

# (level, text) - level selects font+size; 'gap' inserts a blank half-line
# and carries no text.
Section = Tuple[str, str]

_SIZES = {"title": TITLE_SIZE, "heading": HEADING_SIZE, "body": BODY_SIZE}


def _wrap(text: str, size: int) -> List[str]:
    max_chars = max(1, int((PAGE_WIDTH - 2 * MARGIN) / (size * CHAR_WIDTH_FACTOR)))
    words = str(text).split()
    if not words:
        return [""]
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _escape(text: str) -> str:
    return str(text).replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _pdf_string(text: str) -> bytes:
    return _escape(text).encode("cp1252", errors="replace")


def build_pdf(sections: List[Section]) -> bytes:
    """Render ``sections`` (level, text) pairs to a paginated PDF 1.4
    document. ``level`` is one of 'title', 'heading', 'body', 'gap'."""
    lines: List[Tuple[str, int, str]] = []  # (font, size, text) or ('gap', 0, '')
    for level, text in sections:
        if level == "gap":
            lines.append(("gap", 0, ""))
            continue
        size = _SIZES[level]
        font = "F2" if level in ("title", "heading") else "F1"
        for wrapped in _wrap(text, size):
            lines.append((font, size, wrapped))

    pages: List[List[Tuple[str, int, str, float]]] = []
    current_page: List[Tuple[str, int, str, float]] = []
    y = float(PAGE_HEIGHT - MARGIN)
    for font, size, text in lines:
        if font == "gap":
            y -= BODY_SIZE * LINE_GAP
            continue
        line_height = size * LINE_GAP
        if y - line_height < MARGIN:
            pages.append(current_page)
            current_page = []
            y = float(PAGE_HEIGHT - MARGIN)
        current_page.append((font, size, text, y))
        y -= line_height
    pages.append(current_page)  # last (or only, possibly empty) page

    objects: List[bytes] = []

    def add_object(content: bytes) -> int:
        objects.append(content)
        return len(objects)

    font1_num = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    font2_num = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>")

    pages_num = len(objects) + 1
    objects.append(b"")  # placeholder, filled in once page object numbers are known

    page_obj_nums: List[int] = []
    for page_lines in pages:
        parts = [b"BT"]
        cursor_y = None
        last_font = last_size = None
        for font, size, text, y in page_lines:
            if cursor_y is None:
                parts.append(f"/{font} {size} Tf".encode())
                parts.append(f"{MARGIN} {y:.2f} Td".encode())
            else:
                if font != last_font or size != last_size:
                    parts.append(f"/{font} {size} Tf".encode())
                parts.append(f"0 {y - cursor_y:.2f} Td".encode())
            parts.append(b"(" + _pdf_string(text) + b") Tj")
            cursor_y, last_font, last_size = y, font, size
        parts.append(b"ET")
        stream = b"\n".join(parts)
        content_num = add_object(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        page_num = len(objects) + 1
        objects.append(b"")  # placeholder, filled below
        page_obj_nums.append(page_num)
        objects[page_num - 1] = (
            f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 {font1_num} 0 R /F2 {font2_num} 0 R >> >> "
            f"/Contents {content_num} 0 R >>"
        ).encode()

    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects[pages_num - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_obj_nums)} >>".encode()
    catalog_num = add_object(f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode())

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n"
    out += f"<< /Size {len(objects) + 1} /Root {catalog_num} 0 R >>\n".encode()
    out += b"startxref\n"
    out += f"{xref_offset}\n".encode()
    out += b"%%EOF"
    return bytes(out)


def edition_sections(edition: Dict[str, Any], issue_number: int, previous_id: str | None) -> List[Section]:
    """Build the same structural sections as village/gazette.py's
    compile_edition() - deliberately duplicated rather than refactored to
    share code, since compile_edition() is already tested and shipped;
    see docs/evidence/P66.md for the tradeoff. Only ever includes
    review_status == 'approved' contributions, same guarantee as the HTML
    archive."""
    from village.gazette import CONTRIBUTION_KINDS, KIND_LABELS

    approved = [c for c in edition["contributions"] if c.get("review_status") == "approved"]
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for contrib in approved:
        by_kind.setdefault(contrib["kind"], []).append(contrib)

    sections: List[Section] = [("title", "AI Village Gazette")]
    meta = f"Ausgabe Nr. {issue_number} · {edition['id']} · eröffnet von {edition['opened_by']}"
    sections.append(("body", meta))
    if previous_id:
        sections.append(("body", f"Vorherige Ausgabe: {previous_id}"))
    sections.append(("gap", ""))

    if by_kind.get("village_news"):
        sections.append(("heading", "Dorfmeldungen"))
        for c in by_kind["village_news"]:
            sections.append(("body", f"{c['content']} — {c['agent']}"))
        sections.append(("gap", ""))

    sections.append(("heading", "Spiel des Tages"))
    sections.append(("body", edition["game_name"]))
    if edition["game_pair"]:
        sections.append(("body", f"Ausgelost: {', '.join(edition['game_pair'])}"))
    for c in by_kind.get("game_result", []):
        sections.append(("body", f"{c['content']} — {c['agent']}"))
    sections.append(("gap", ""))

    interview_kinds = [k for k in CONTRIBUTION_KINDS if k not in ("village_news", "game_result")]
    agents_with_content = sorted({c["agent"] for k in interview_kinds for c in by_kind.get(k, [])})
    if agents_with_content:
        sections.append(("heading", "Interviews"))
        for agent in agents_with_content:
            sections.append(("body", agent))
            for kind in interview_kinds:
                match = next((c for c in by_kind.get(kind, []) if c["agent"] == agent), None)
                if match:
                    sections.append(("body", f"{KIND_LABELS.get(kind, kind)}: {match['content']}"))
            sections.append(("gap", ""))

    return sections


def render_edition_pdf(edition: Dict[str, Any], issue_number: int, previous_id: str | None) -> bytes:
    return build_pdf(edition_sections(edition, issue_number, previous_id))
