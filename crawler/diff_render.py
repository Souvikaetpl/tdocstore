"""Render a "what changed since the previous revision" PDF for a TDoc,
using LibreOffice's own document-comparison feature (Tools > Compare
Document) rather than a plain-text diff — produces the same inline
strikethrough/underline markup as opening both documents in Word and
comparing them, preserving the original CR-form layout rather than a
wall of diffed text.

Scoped to Writer documents (.docx and legacy .doc — the formats
`.uno:CompareDocuments` applies to) — anything else (PDF, PPTX, XLSX)
cleanly reports `no_document`, mirroring render.py's own
`status="unsupported"` pattern rather than trying to make every format
participate.

The comparison itself only runs under LibreOffice's own bundled Python
(see crawler/_diff_worker.py's docstring for why) — this module stays
in the project's regular venv and shells out to that worker script,
the same arm's-length relationship render.py already has with the
`soffice` binary itself, just one layer further for this feature.
"""
import logging
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config
from .extract import pick_main_entry

log = logging.getLogger("crawler.diff_render")

_WORKER_SCRIPT = Path(__file__).resolve().parent / "_diff_worker.py"


@dataclass
class DiffRenderResult:
    status: str  # 'success' | 'no_document' | 'unsupported' | 'error'
    error: Optional[str] = None


_COMPARABLE_EXTENSIONS = {".docx", ".doc"}


def _extract_docx(zip_path: Path, tdoc_id: str, out_dir: Path, label: str) -> Optional[Path]:
    """Returns None if there's no main document, or it's not a format
    Writer's document comparison applies to -- both are legitimate
    "can't diff this one" outcomes, not errors. Legacy .doc included
    alongside .docx: Writer loads either into the same internal document
    model before comparing (the same reason render.py's plain conversion
    already treats them interchangeably), confirmed live rather than
    assumed — 3GPP's older/administrative documents (e.g. "agenda") are
    commonly .doc, not .docx, and were silently excluded before this."""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        main_entry = pick_main_entry(names, tdoc_id)
        if main_entry is None:
            return None
        ext = Path(main_entry).suffix.lower()
        if ext not in _COMPARABLE_EXTENSIONS:
            return None
        dest = out_dir / f"{label}{ext}"
        with z.open(main_entry) as src, open(dest, "wb") as dst:
            dst.write(src.read())
        return dest


def render_diff_to_pdf(new_zip_path: Path, old_zip_path: Path, new_tdoc_id: str,
                        old_tdoc_id: str, dest_path: Path) -> DiffRenderResult:
    if config.SOFFICE_PYTHON_PATH is None:
        return DiffRenderResult(status="error", error="LibreOffice's bundled Python not found")

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_dir_path = Path(tmp_dir)
            try:
                new_docx = _extract_docx(new_zip_path, new_tdoc_id, tmp_dir_path, "new")
                old_docx = _extract_docx(old_zip_path, old_tdoc_id, tmp_dir_path, "old")
            except zipfile.BadZipFile as exc:
                return DiffRenderResult(status="error", error=f"bad zip: {exc}")

            if new_docx is None or old_docx is None:
                return DiffRenderResult(status="no_document")

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            worker_dest = tmp_dir_path / "diff.pdf"

            result = subprocess.run(
                [config.SOFFICE_PYTHON_PATH, str(_WORKER_SCRIPT),
                 str(new_docx), str(old_docx), str(worker_dest)],
                capture_output=True, text=True, timeout=config.DIFF_RENDER_TIMEOUT_SECONDS,
            )
            if result.returncode != 0 or not worker_dest.exists():
                return DiffRenderResult(
                    status="error",
                    error=(result.stderr or result.stdout or "diff render failed").strip()[:500],
                )

            shutil.move(str(worker_dest), str(dest_path))
            return DiffRenderResult(status="success")

    except subprocess.TimeoutExpired:
        return DiffRenderResult(status="error", error="diff render timed out")
    except Exception as exc:
        log.exception("Diff render failed for %s vs %s", new_tdoc_id, old_tdoc_id)
        return DiffRenderResult(status="error", error=str(exc))
