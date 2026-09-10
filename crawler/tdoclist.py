import re

import openpyxl

_HEADER_TO_FIELD = {
    "Tdoc": "tdoc_id",
    "Title": "title",
    "Source": "source",
    "Contact": "contact",
    "Contact ID": "contact_id",
    "Type": "doc_type",
    "For": "for_action",
    "Abstract": "abstract",
    "Secretary Remarks": "secretary_remarks",
    "Agenda item sort order": "agenda_item_sort_order",
    "Agenda item": "agenda_item",
    "Agenda item description": "agenda_item_description",
    "TDoc sort order within agenda item": "tdoc_sort_order",
    "TDoc Status": "status",
    "Reservation date": "reservation_date",
    "Uploaded": "uploaded_at",
    "Is revision of": "is_revision_of",
    "Revised to": "revised_to",
    "Rel": "release",
    "Specification": "specification",
    "Version": "spec_version",
    "Related WIs": "related_wis",
    "CR": "cr_number",
    "CR revision": "cr_revision",
    "CR category": "cr_category",
    "TSG CR Pack": "tsg_cr_pack",
    "Reply to": "reply_to",
    "LS_To": "ls_to",
    "LS_Cc": "ls_cc",
    "Original LS": "original_ls",
    "Reply in": "reply_in",
}


def _find_tdoc_sheet(wb):
    for ws in wb.worksheets:
        if ws.title.startswith("TDoc_List"):
            return ws
    return wb.worksheets[0]


def parse_tdoc_list(xlsx_path) -> list[dict]:
    """Parse a 3GPP meeting TDoc list Excel export into normalized dicts.

    Column names come directly from the observed export (see
    _HEADER_TO_FIELD); unrecognized columns are kept under their raw
    header text so nothing is silently dropped.
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    ws = _find_tdoc_sheet(wb)

    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    fields = [_HEADER_TO_FIELD.get(h, h) for h in header]

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
    """Given directory Entry objects from a Tdoclists/ folder, pick the
    authoritative snapshot: the end-of-meeting file if present, else the
    most recently modified xlsx."""
    xlsx_entries = [e for e in entries if not e.is_dir and e.name.lower().endswith(".xlsx")]
    if not xlsx_entries:
        return None
    eom = [e for e in xlsx_entries if _EOM_RE.search(e.name)]
    if eom:
        return eom[0]
    return max(xlsx_entries, key=lambda e: e.modified_at or 0)
