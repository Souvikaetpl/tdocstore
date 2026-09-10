"""Fast DOCX extraction (python-docx), with optional Docling fallback for PDF/PPTX.

Outputs two parallel representations:
  paragraphs : faithful body text in document order, one entry per paragraph / table cell.
               No markdown decoration. This is the verbatim-evidence source.
  markdown   : headings (#), bold (**), tables (pipe rows) — for display and LLM consumption.
  sections   : (heading, level, body) split on headings; body is the plain paragraph text.

Known limitations (state them, do not hide them):
  - Tracked changes: python-docx returns the *accepted* text (insertions kept, deletions dropped).
    For CRs with change marks this is what you want for "current text", not for diffing.
  - Text boxes, footnotes, headers/footers are not walked. TDoc cover-page boxes are usually plain paragraphs.
  - Equations (OMML) come out empty or as fragments.
  - Numbering: auto-numbered headings ("1", "2.1") are not in the run text; we reconstruct a
    level from the style and keep the visible text as-is.
"""
from __future__ import annotations
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

HEADING_STYLE_RE = re.compile(r"^(heading|überschrift|titre)\s*(\d)", re.I)
# Manual numbering fallback: "2.3 Discussion", "2 Discussion", "Annex A", up to depth 4
MANUAL_HEADING_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+([A-Z][^\n]{2,120})$")
ANNEX_RE = re.compile(r"^Annex\s+[A-Z]\b", re.I)


@dataclass
class Para:
    idx: int
    style: str
    text: str
    level: int = 0            # heading level (0 = body)
    is_table_cell: bool = False


@dataclass
class Extracted:
    source_type: str
    paragraphs: list[Para] = field(default_factory=list)
    markdown: str = ""
    sections: list[tuple[str, int, str]] = field(default_factory=list)   # (heading, level, body)
    warnings: list[str] = field(default_factory=list)


# ---------- DOCX ----------

def _iter_block_items(doc):
    """Yield Paragraph and Table objects in document order (python-docx lacks this)."""
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _para_text(p: Paragraph) -> str:
    # Faithful: keep NBSP and tabs (byte-exact evidence); drop stray CR only.
    return p.text.replace("\r", "").strip()


def _para_md(p: Paragraph) -> str:
    """Markdown with bold runs merged. Falls back to plain text when runs are odd."""
    out = []
    for r in p.runs:
        t = r.text.replace(" ", " ")
        if not t:
            continue
        if r.bold:
            out.append(("b", t))
        else:
            out.append(("n", t))
    # merge adjacent same-kind runs
    merged: list[list] = []
    for k, t in out:
        if merged and merged[-1][0] == k:
            merged[-1][1] += t
        else:
            merged.append([k, t])
    s = ""
    for k, t in merged:
        if k == "b" and t.strip():
            lead = t[: len(t) - len(t.lstrip())]
            trail = t[len(t.rstrip()):]
            s += f"{lead}**{t.strip()}**{trail}"
        else:
            s += t
    return s.strip()


def _heading_level(p: Paragraph, text: str) -> int:
    name = (p.style.name if p.style is not None else "") or ""
    m = HEADING_STYLE_RE.match(name)
    if m:
        return int(m.group(2))
    if name.lower() == "title":
        return 1
    # manual-numbering fallback, only for short lines
    if len(text) <= 120 and not text.endswith((".", ":", ";")):
        m2 = MANUAL_HEADING_RE.match(text)
        if m2:
            return min(m2.group(1).count(".") + 1, 4)
        if ANNEX_RE.match(text):
            return 1
    return 0


def extract_docx(path: Path) -> Extracted:
    doc = Document(str(path))
    ex = Extracted(source_type="docx-fast")
    md_lines: list[str] = []
    idx = 0
    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            text = _para_text(block)
            if not text:
                continue
            style = block.style.name if block.style is not None else ""
            lvl = _heading_level(block, text)
            ex.paragraphs.append(Para(idx, style, text, lvl))
            idx += 1
            if lvl:
                md_lines.append(f"\n{'#' * lvl} {text}\n")
            else:
                md = _para_md(block) or text
                if style.lower().startswith("list"):
                    md = "- " + md
                md_lines.append(md + "\n")
        else:  # Table
            rows_md = []
            try:
                for ri, row in enumerate(block.rows):
                    cells = []
                    seen_tc = set()
                    for cell in row.cells:
                        # merged cells repeat the same _tc; skip repeats
                        if id(cell._tc) in seen_tc:
                            continue
                        seen_tc.add(id(cell._tc))
                        ctext = " ".join(_para_text(p) for p in cell.paragraphs if _para_text(p))
                        cells.append(ctext)
                        if ctext:
                            ex.paragraphs.append(Para(idx, "TableCell", ctext, 0, True))
                            idx += 1
                    rows_md.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
                    if ri == 0:
                        rows_md.append("|" + "---|" * len(cells))
            except Exception as e:  # malformed tables happen
                ex.warnings.append(f"table skipped: {e}")
            if rows_md:
                md_lines.append("\n" + "\n".join(rows_md) + "\n")
    ex.markdown = "\n".join(md_lines).strip() + "\n"
    ex.sections = _split_sections(ex.paragraphs)
    return ex


def _split_sections(paras: list[Para]) -> list[tuple[str, int, str]]:
    sections: list[tuple[str, int, list[str]]] = [("Document", 0, [])]
    for p in paras:
        if p.level and not p.is_table_cell:
            sections.append((p.text, p.level, []))
        else:
            sections[-1][2].append(p.text)
    out = []
    for h, lvl, body in sections:
        b = "\n".join(body).strip()
        if b or h != "Document":
            out.append((h, lvl, b))
    return out


# ---------- containers / other formats ----------

def _pick_from_zip(zpath: Path, workdir: Path) -> list[Path]:
    """Extract retained documents from a TDoc zip. Prefer .docx; keep .pptx/.pdf/.xlsx; skip junk."""
    keep = []
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = info.filename
            low = name.lower()
            if info.is_dir() or "__macosx" in low or low.rsplit("/", 1)[-1].startswith("~$"):
                continue
            if low.endswith((".docx", ".doc", ".pptx", ".pdf", ".xlsx", ".zip")):
                target = workdir / Path(name).name
                with z.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                keep.append(target)
    return keep


def extract_any(path: Path, workdir: Path, *, allow_docling: bool = True) -> Extracted:
    """Entry point: zip/docx/pptx/pdf -> Extracted. Multiple docs in a zip are concatenated."""
    workdir.mkdir(parents=True, exist_ok=True)
    low = path.suffix.lower()
    if low == ".zip":
        members = _pick_from_zip(path, workdir)
        if not members:
            ex = Extracted(source_type="zip-empty")
            ex.warnings.append("no supported document inside zip")
            return ex
        parts = [extract_any(m, workdir, allow_docling=allow_docling) for m in members]
        if len(parts) == 1:
            return parts[0]
        ex = Extracted(source_type="zip-multi:" + ",".join(p.source_type for p in parts))
        off = 0
        for m, p in zip(members, parts):
            ex.markdown += f"\n\n<!-- file: {m.name} -->\n\n" + p.markdown
            for q in p.paragraphs:
                ex.paragraphs.append(Para(off + q.idx, q.style, q.text, q.level, q.is_table_cell))
            off += len(p.paragraphs)
            ex.sections.extend([(f"[{m.name}] {h}", lvl, b) for h, lvl, b in p.sections])
            ex.warnings.extend(p.warnings)
        return ex
    if low == ".docx":
        return extract_docx(path)
    if low in (".pptx", ".pdf", ".doc", ".xlsx"):
        if allow_docling:
            try:
                return extract_docling(path)
            except ImportError:
                pass
        ex = Extracted(source_type=f"{low[1:]}-unsupported")
        ex.warnings.append(f"{low} needs the optional docling extra (pip install tdocstore[docling])")
        return ex
    ex = Extracted(source_type="unsupported")
    ex.warnings.append(f"unsupported file type {low}")
    return ex


def extract_docling(path: Path) -> Extracted:
    """Slow path. Import is deferred so the fast path has no heavy deps."""
    from docling.document_converter import DocumentConverter  # type: ignore
    conv = DocumentConverter()
    res = conv.convert(str(path))
    md = res.document.export_to_markdown()
    ex = Extracted(source_type=f"{path.suffix[1:]}-docling", markdown=md)
    # Derive paragraphs/sections from markdown (lossy but consistent)
    idx = 0
    for line in md.splitlines():
        t = line.strip()
        if not t:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", t)
        if m:
            ex.paragraphs.append(Para(idx, f"Heading {len(m.group(1))}", m.group(2), len(m.group(1))))
        else:
            ex.paragraphs.append(Para(idx, "Normal", re.sub(r"\*\*(.+?)\*\*", r"\1", t)))
        idx += 1
    ex.sections = _split_sections(ex.paragraphs)
    return ex
