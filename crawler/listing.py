import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

_SIZE_RE = re.compile(r"^([\d.,]+)\s*(Byte|Bytes|KB|MB|GB)$", re.IGNORECASE)
_UNIT_MULTIPLIER = {"byte": 1, "bytes": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}


@dataclass
class Entry:
    name: str
    url: str
    is_dir: bool
    modified_at: Optional[datetime]
    size_bytes: Optional[int]


def _parse_size(text: str) -> Optional[int]:
    text = text.strip()
    if not text:
        return None
    m = _SIZE_RE.match(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    return int(value * _UNIT_MULTIPLIER[unit])


def _parse_date(text: str) -> Optional[datetime]:
    text = text.strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_directory_listing(html: str) -> list[Entry]:
    """Parse a 3GPP FTP-style directory listing page into Entry objects.

    Files have <a class="file" href="...">, directories have a plain
    <a href="..."> with no class attribute. Both sit in a <tr> alongside
    a modified-date cell and (for files) a size cell.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[Entry] = []

    for row in soup.find_all("tr"):
        link = row.find("a", href=True)
        if link is None:
            continue
        href = link["href"]
        if href.startswith("?") or href in ("https://www.3gpp.org/",):
            continue
        if "/ftp/" not in href:
            continue

        is_file = link.get("class") is not None and "file" in link.get("class")
        cells = row.find_all("td")
        date_text = cells[3].get_text() if len(cells) > 3 else ""
        size_text = cells[4].get_text() if len(cells) > 4 else ""

        entries.append(
            Entry(
                name=link.get_text(strip=True),
                url=href,
                is_dir=not is_file,
                modified_at=_parse_date(date_text),
                size_bytes=_parse_size(size_text) if is_file else None,
            )
        )

    return entries
