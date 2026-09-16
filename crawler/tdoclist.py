import re

import openpyxl

# Keys are normalized (stripped, whitespace-collapsed, lowercased) before
# lookup — different groups' exports use slightly different header text
# for the same field (RAN2's Tdoclists/ snapshot: "Tdoc"/"Specification"/
# "LS_To"; CT1's Docs/TDoc_List_Meeting_*.xlsx: "TDoc"/"Spec"/"To").
_HEADER_TO_FIELD = {
    "tdoc": "tdoc_id",
    "title": "title",
    "source": "source",
    "contact": "contact",
    "contact id": "contact_id",
    "type": "doc_type",
    "for": "for_action",
    "abstract": "abstract",
    "secretary remarks": "secretary_remarks",
    "agenda item sort order": "agenda_item_sort_order",
    "agenda item": "agenda_item",
    "agenda item description": "agenda_item_description",
    "tdoc sort order within agenda item": "tdoc_sort_order",
    "tdoc status": "status",
    "reservation date": "reservation_date",
    "uploaded": "uploaded_at",
    "is revision of": "is_revision_of",
    "revised to": "revised_to",
    "rel": "release",
    "release": "release",
    "specification": "specification",
    "spec": "specification",
    "version": "spec_version",
    "related wis": "related_wis",
    "cr": "cr_number",
    "cr revision": "cr_revision",
    "cr category": "cr_category",
    "tsg cr pack": "tsg_cr_pack",
    "reply to": "reply_to",
    "ls_to": "ls_to",
    "to": "ls_to",
    "ls_cc": "ls_cc",
    "cc": "ls_cc",
    "original ls": "original_ls",
    "reply in": "reply_in",
}


def _normalize_header(h) -> str:
    return re.sub(r"\s+", " ", str(h).strip()).lower()


def _find_tdoc_sheet(wb):
    for ws in wb.worksheets:
        if ws.title.startswith("TDoc_List"):
            return ws
    return wb.worksheets[0]


def parse_tdoc_list(xlsx_path) -> list[dict]:
    """Parse a 3GPP meeting TDoc list Excel export into normalized dicts.

    Column names come directly from the observed export (see
    _HEADER_TO_FIELD, matched case/whitespace-insensitively since groups
    vary slightly); unrecognized columns are kept under their raw header
    text so nothing is silently dropped.
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    ws = _find_tdoc_sheet(wb)

    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    fields = [_HEADER_TO_FIELD.get(_normalize_header(h), h) for h in header]

    records = []
    for row in rows:
        if row[0] is None:
            continue
        record = dict(zip(fields, row))
        records.append(record)

    wb.close()
    return records


_EOM_RE = re.compile(r"_eom\.xlsx$", re.IGNORECASE)


def pick_latest_tdoc_list(entries) -> "object | None":
    """Given directory Entry objects for a set of candidate TDoc-list
    Excel files, pick the authoritative one: the end-of-meeting file if
    present, else the most recently modified xlsx."""
    xlsx_entries = [e for e in entries if not e.is_dir and e.name.lower().endswith(".xlsx")]
    if not xlsx_entries:
        return None
    eom = [e for e in xlsx_entries if _EOM_RE.search(e.name)]
    if eom:
        return eom[0]
    return max(xlsx_entries, key=lambda e: e.modified_at or 0)
