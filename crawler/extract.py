import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import docx
import openpyxl
from pptx import Presentation
from pypdf import PdfReader

log = logging.getLogger("crawler.extract")

_PREFERRED_EXTENSIONS = [".docx", ".pdf", ".pptx", ".xlsx", ".doc", ".ppt", ".xls", ".txt"]


@dataclass
class ExtractionResult:
    status: str  # 'success' | 'unsupported' | 'no_document' | 'error'
    text: Optional[str] = None
    source_filename: Optional[str] = None
    error: Optional[str] = None


def _pick_main_entry(names: list[str], tdoc_id: str) -> Optional[str]:
    candidates = [n for n in names if Path(n).suffix.lower() in _PREFERRED_EXTENSIONS]
    if not candidates:
        return None

    prefix_matches = [n for n in candidates if Path(n).stem.lower().startswith(tdoc_id.lower())]
    pool = prefix_matches or candidates

    def rank(name: str) -> int:
        ext = Path(name).suffix.lower()
        return _PREFERRED_EXTENSIONS.index(ext) if ext in _PREFERRED_EXTENSIONS else len(_PREFERRED_EXTENSIONS)

    return sorted(pool, key=rank)[0]


def _extract_docx(fileobj) -> str:
    document = docx.Document(fileobj)
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_pdf(fileobj) -> str:
    reader = PdfReader(fileobj)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_pptx(fileobj) -> str:
    presentation = Presentation(fileobj)
    parts = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _extract_xlsx(fileobj) -> str:
    wb = openpyxl.load_workbook(fileobj, data_only=True, read_only=True)
    parts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = [str(v) for v in row if v is not None]
            if cells:
                parts.append(" | ".join(cells))
    wb.close()
    return "\n".join(parts)


def _extract_txt(fileobj) -> str:
    return fileobj.read().decode("utf-8", errors="replace")


_EXTRACTORS = {
    ".docx": _extract_docx,
    ".pdf": _extract_pdf,
    ".pptx": _extract_pptx,
    ".xlsx": _extract_xlsx,
    ".txt": _extract_txt,
}


def extract_text_from_zip(zip_path: Path, tdoc_id: str) -> ExtractionResult:
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            main_entry = _pick_main_entry(names, tdoc_id)
            if main_entry is None:
                return ExtractionResult(status="no_document")

            ext = Path(main_entry).suffix.lower()
            extractor = _EXTRACTORS.get(ext)
            if extractor is None:
                return ExtractionResult(status="unsupported", source_filename=main_entry)

            with z.open(main_entry) as fileobj:
                text = extractor(fileobj)
            return ExtractionResult(status="success", text=text, source_filename=main_entry)
    except zipfile.BadZipFile as exc:
        return ExtractionResult(status="error", error=f"bad zip: {exc}")
    except Exception as exc:
        log.exception("Extraction failed for %s (%s)", zip_path, tdoc_id)
        return ExtractionResult(status="error", error=str(exc))
