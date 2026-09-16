"""Render a TDoc's main document to PDF for in-browser preview.

Browsers can't display .docx with real formatting, only PDF (via their
built-in viewer) or HTML. This produces the PDF, using LibreOffice
headless as the conversion engine — chosen over Pandoc specifically
because it renders through an actual Word-compatible layout engine
(preserves colored table cells, exact CR-form layout), where Pandoc's
structural conversion tends to lose that. When the original document is
already a PDF, it's used as-is — no conversion needed.
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

log = logging.getLogger("crawler.render")

_CONVERTIBLE_EXTENSIONS = {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"}


@dataclass
class RenderResult:
    status: str  # 'success' | 'no_document' | 'unsupported' | 'error'
    error: Optional[str] = None


def render_to_pdf(zip_path: Path, tdoc_id: str, dest_path: Path) -> RenderResult:
    if config.SOFFICE_PATH is None:
        return RenderResult(status="error", error="LibreOffice (soffice) not found")

    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            main_entry = pick_main_entry(names, tdoc_id)
            if main_entry is None:
                return RenderResult(status="no_document")

            ext = Path(main_entry).suffix.lower()
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            if ext == ".pdf":
                with z.open(main_entry) as src, open(dest_path, "wb") as dst:
                    dst.write(src.read())
                return RenderResult(status="success")

            if ext not in _CONVERTIBLE_EXTENSIONS:
                return RenderResult(status="unsupported")

            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_dir_path = Path(tmp_dir)
                extracted = tmp_dir_path / Path(main_entry).name
                with z.open(main_entry) as src, open(extracted, "wb") as dst:
                    dst.write(src.read())

                # LibreOffice headless locks its user profile — two
                # `soffice --headless` conversions running at the same
                # moment on the DEFAULT profile collide, and the second one
                # fails outright rather than queuing (hit exactly this way:
                # several users viewing different not-yet-rendered
                # documents at once). A fresh, isolated profile directory
                # per call removes the shared lock entirely, so concurrent
                # renders (from concurrent web requests, or overlapping
                # with a background --render batch) no longer step on
                # each other.
                with tempfile.TemporaryDirectory() as profile_dir:
                    result = subprocess.run(
                        [config.SOFFICE_PATH, "--headless",
                         f"-env:UserInstallation=file:///{Path(profile_dir).as_posix()}",
                         "--convert-to", "pdf",
                         "--outdir", str(tmp_dir_path), str(extracted)],
                        capture_output=True, text=True, timeout=config.RENDER_TIMEOUT_SECONDS,
                    )
                converted = extracted.with_suffix(".pdf")
                if not converted.exists():
                    return RenderResult(
                        status="error",
                        error=(result.stderr or result.stdout or "conversion failed").strip()[:500],
                    )
                shutil.move(str(converted), str(dest_path))
                return RenderResult(status="success")
    except zipfile.BadZipFile as exc:
        return RenderResult(status="error", error=f"bad zip: {exc}")
    except subprocess.TimeoutExpired:
        return RenderResult(status="error", error="conversion timed out")
    except Exception as exc:
        log.exception("Render failed for %s (%s)", zip_path, tdoc_id)
        return RenderResult(status="error", error=str(exc))
