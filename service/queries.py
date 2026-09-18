"""Search / read / compare — the one place SQL for reading TDoc data
lives. FastAPI and the MCP server both call into this module rather than
writing their own queries, per the plan's shared-service-layer phase.
"""
from pathlib import Path
from typing import Optional

from . import db
from .models import MeetingSummary, Page, TDocDetail, TDocSummary

_SUMMARY_SELECT = """
    t.tdoc_id, t.title, t.source, t.doc_type, t.status, t.tsg, t.wg_short,
    m.meeting_folder, t.specification, t.uploaded_at
"""
_SUMMARY_FIELD_COUNT = 10

_DETAIL_SELECT = _SUMMARY_SELECT + """,
    t.contact, t.for_action, t.abstract, t.agenda_item, t.agenda_item_description,
    t.release, t.spec_version, t.related_wis, t.cr_number, t.cr_revision, t.cr_category,
    t.is_revision_of, t.revised_to, t.ls_to, t.ls_cc,
    t.file_url, t.local_zip_path, t.text_path, t.extraction_status,
    t.rendered_path, t.render_status
"""

_FROM = "FROM tdocs t JOIN meetings m ON t.meeting_id = m.id"


def _row_to_summary(row) -> TDocSummary:
    return TDocSummary(*row[:_SUMMARY_FIELD_COUNT])


def _row_to_detail(row) -> TDocDetail:
    summary_fields = row[:_SUMMARY_FIELD_COUNT]
    rest = row[_SUMMARY_FIELD_COUNT:]
    detail = TDocDetail(*summary_fields)
    (
        detail.contact, detail.for_action, detail.abstract, detail.agenda_item,
        detail.agenda_item_description, detail.release, detail.spec_version,
        detail.related_wis, detail.cr_number, detail.cr_revision, detail.cr_category,
        detail.is_revision_of, detail.revised_to, detail.ls_to, detail.ls_cc,
        detail.file_url, detail.local_zip_path, detail.text_path, detail.extraction_status,
        detail.rendered_path, detail.render_status,
    ) = rest
    return detail


def _load_text(detail: TDocDetail) -> TDocDetail:
    if detail.extraction_status == "success" and detail.text_path:
        path = Path(detail.text_path)
        if path.exists():
            detail.text = path.read_text(encoding="utf-8", errors="replace")
    return detail


def search_tdocs(
    query: Optional[str] = None,
    tsg: Optional[str] = None,
    wg_short: Optional[str] = None,
    meeting_folder: Optional[str] = None,
    doc_type: Optional[str] = None,
    specification: Optional[str] = None,
    source: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
) -> Page:
    conditions = []
    params: list = []

    if query:
        # The search box promises "TDoc number, title, or keyword" — plain
        # full-text search alone never matches a literal ID like
        # "R2-2604807" (it isn't part of the tsvector, and a hyphenated
        # alphanumeric token doesn't tokenize the way title words do), so
        # an ID match is OR'd in alongside the title/abstract search.
        conditions.append(
            "(t.tdoc_id ILIKE %s OR "
            "to_tsvector('english', coalesce(t.title,'') || ' ' || coalesce(t.abstract,'')) "
            "@@ plainto_tsquery('english', %s))"
        )
        params.append(f"%{query}%")
        params.append(query)
    if tsg:
        conditions.append("t.tsg = %s")
        params.append(tsg)
    if wg_short:
        conditions.append("t.wg_short = %s")
        params.append(wg_short)
    if meeting_folder:
        conditions.append("m.meeting_folder = %s")
        params.append(meeting_folder)
    if doc_type:
        conditions.append("t.doc_type = %s")
        params.append(doc_type)
    if specification:
        conditions.append("t.specification = %s")
        params.append(specification)
    if source:
        conditions.append("t.source = %s")
        params.append(source)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT {_SUMMARY_SELECT}, COUNT(*) OVER() AS total_count
        {_FROM}
        {where}
        ORDER BY t.tdoc_id
        OFFSET %s LIMIT %s
    """
    with db.connect() as conn:
        rows = conn.execute(sql, params + [offset, limit]).fetchall()

    total = rows[0][-1] if rows else 0
    items = [_row_to_summary(row[:-1]) for row in rows]
    return Page(items=items, total=total, offset=offset, limit=limit)


def get_tdoc(tdoc_id: str) -> Optional[TDocDetail]:
    sql = f"SELECT {_DETAIL_SELECT} {_FROM} WHERE t.tdoc_id = %s"
    with db.connect() as conn:
        row = conn.execute(sql, (tdoc_id,)).fetchone()
    if row is None:
        return None
    return _load_text(_row_to_detail(row))


def list_meetings(
    tsg: Optional[str] = None,
    wg_short: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
) -> Page:
    conditions = []
    params: list = []
    if tsg:
        conditions.append("m.tsg = %s")
        params.append(tsg)
    if wg_short:
        conditions.append("m.wg_short = %s")
        params.append(wg_short)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    sql = f"""
        SELECT m.id, m.tsg, m.wg_short, m.meeting_folder, m.modified_at,
               COUNT(t.tdoc_id) AS tdoc_count,
               m.start_date, m.end_date, m.location, m.country, m.display_title,
               COUNT(*) OVER() AS total_count
        FROM meetings m LEFT JOIN tdocs t ON t.meeting_id = m.id
        {where}
        GROUP BY m.id
        ORDER BY m.modified_at DESC NULLS LAST
        OFFSET %s LIMIT %s
    """
    with db.connect() as conn:
        rows = conn.execute(sql, params + [offset, limit]).fetchall()

    total = rows[0][-1] if rows else 0
    items = [MeetingSummary(*row[:-1]) for row in rows]
    return Page(items=items, total=total, offset=offset, limit=limit)


def list_tdocs_for_meeting(
    tsg: str, wg_short: str, meeting_folder: str, offset: int = 0, limit: int = 50
) -> Page:
    return search_tdocs(
        tsg=tsg, wg_short=wg_short, meeting_folder=meeting_folder, offset=offset, limit=limit
    )


def get_stats() -> dict:
    """Real, cheap counts for the homepage's stats strip — no hardcoded
    numbers, since those would drift the moment another group gets
    backfilled."""
    with db.connect() as conn:
        total_tdocs = conn.execute("SELECT COUNT(*) FROM tdocs").fetchone()[0]
        total_groups = conn.execute("SELECT COUNT(DISTINCT (tsg, wg_short)) FROM meetings").fetchone()[0]
        total_tsgs = conn.execute("SELECT COUNT(DISTINCT tsg) FROM meetings").fetchone()[0]
    return {"total_tdocs": total_tdocs, "total_groups": total_groups, "total_tsgs": total_tsgs}


def compare_tdocs(tdoc_ids: list[str], max_docs: int = 3) -> list[TDocDetail]:
    results = []
    for tdoc_id in tdoc_ids[:max_docs]:
        detail = get_tdoc(tdoc_id)
        if detail is not None:
            results.append(detail)
    return results
