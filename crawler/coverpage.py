"""Fallback metadata extraction from a TDoc's own cover page.

Groups without a structured TDoc list (Tdoclists/*.xlsx exists only for
some RAN groups; SA/CT/most of RAN have nothing but zips) still carry
title/source/release/work-item/etc. on the document's own cover page —
either as a plain tab-separated paragraph (the LS template) or as a Word
table (the CR template). Both survive into the plain text produced by
extract.py; this module pulls known fields back out of it.

Only fills gaps: callers should treat this as a fallback for fields the
TDoc list Excel/HTML didn't already provide, never as an override.
"""
import re

_SPLIT_RE = re.compile(r"\t|\s\|\s")

_LABEL_TO_FIELD = {
    "title": "title",
    "source": "source",
    "source to wg": "source",
    "source to tsg": "source",
    "release": "release",
    "work item": "related_wis",
    "work item code": "related_wis",
    "work item(s)": "related_wis",
    "agenda item": "agenda_item",
    "agenda item number": "agenda_item",
    "document for": "for_action",
    "category": "cr_category",
}


def _dedupe_consecutive(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if not out or out[-1] != item:
            out.append(item)
    return out


def _split_line(line: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT_RE.split(line) if p.strip()]
    return _dedupe_consecutive(parts)


def parse_cover_page(text: str) -> dict:
    """Best-effort field extraction. Returns only the keys it actually
    found — never guesses, never returns empty-string placeholders."""
    fields: dict = {}

    for line in text.splitlines():
        parts = _split_line(line)
        if len(parts) < 2:
            continue

        label = parts[0].rstrip(":").strip().lower()
        field = _LABEL_TO_FIELD.get(label)
        if field and field not in fields:
            fields[field] = parts[1]

        # The standard CR form's identity row has no "label:" at all —
        # it's a fixed-position row: [spec, "CR", number, "rev", revision,
        # "Current version:", version]. Detected by the literal "CR" in
        # position 2 rather than a label match.
        if len(parts) >= 3 and parts[1].upper() == "CR" and "specification" not in fields:
            fields["specification"] = parts[0]
            fields["cr_number"] = parts[2]
            if len(parts) >= 5 and parts[3].lower() == "rev":
                fields["cr_revision"] = parts[4]

    return fields
