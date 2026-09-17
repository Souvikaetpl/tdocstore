import re
import threading
from contextlib import contextmanager

import psycopg

from . import config

_ALTER_ADD_COLUMN_RE = re.compile(
    r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", re.IGNORECASE
)

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS meetings (
        id              SERIAL PRIMARY KEY,
        tsg             TEXT NOT NULL,
        wg_short        TEXT NOT NULL,
        wg_path         TEXT NOT NULL,
        meeting_folder  TEXT NOT NULL,
        meeting_url     TEXT NOT NULL,
        modified_at     TEXT,
        first_seen_at   TIMESTAMPTZ DEFAULT now(),
        last_synced_at  TIMESTAMPTZ,
        UNIQUE(tsg, wg_short, meeting_folder)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tdocs (
        tdoc_id                 TEXT PRIMARY KEY,
        meeting_id              INTEGER NOT NULL REFERENCES meetings(id),
        tsg                     TEXT NOT NULL,
        wg_short                TEXT NOT NULL,
        title                   TEXT,
        source                  TEXT,
        contact                 TEXT,
        contact_id              TEXT,
        doc_type                TEXT,
        for_action              TEXT,
        abstract                TEXT,
        secretary_remarks       TEXT,
        agenda_item             TEXT,
        agenda_item_description TEXT,
        status                  TEXT,
        reservation_date        TEXT,
        uploaded_at             TEXT,
        is_revision_of          TEXT,
        revised_to              TEXT,
        release                 TEXT,
        specification           TEXT,
        spec_version            TEXT,
        related_wis             TEXT,
        cr_number               TEXT,
        cr_revision             TEXT,
        cr_category             TEXT,
        tsg_cr_pack             TEXT,
        reply_to                TEXT,
        ls_to                   TEXT,
        ls_cc                   TEXT,
        original_ls             TEXT,
        reply_in                TEXT,
        file_url                TEXT,
        file_size_bytes         BIGINT,
        file_modified_at        TEXT,
        local_zip_path          TEXT,
        text_path               TEXT,
        extraction_status       TEXT,
        extraction_error        TEXT,
        extracted_source_file   TEXT,
        ingested_at             TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tdocs_meeting ON tdocs(meeting_id)",
    "CREATE INDEX IF NOT EXISTS idx_tdocs_source ON tdocs(source)",
    "CREATE INDEX IF NOT EXISTS idx_tdocs_specification ON tdocs(specification)",
    # Additive migration — tdocs already has real backfilled data by the
    # time this was added, so ALTER rather than redefining CREATE TABLE.
    "ALTER TABLE tdocs ADD COLUMN IF NOT EXISTS rendered_path TEXT",
    "ALTER TABLE tdocs ADD COLUMN IF NOT EXISTS render_status TEXT",
    "ALTER TABLE tdocs ADD COLUMN IF NOT EXISTS render_error TEXT",
    # Real date range/location, parsed from each meeting's Invitation
    # document — not available from the TDoc list Excel or any TDoc's own
    # cover page. NULL until fetch_meeting_info() succeeds for it.
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS start_date TEXT",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS end_date TEXT",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS location TEXT",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS country TEXT",
    # Set only for meeting-calendar entries with no FTP folder of their own
    # (e.g. RAN5's "TTCN Workshop#75" — a real dated event on 3GPP's own
    # meeting calendar, but never mirrored as a document directory) —
    # overrides the normally-computed "3GPP<WG>#<n>" display title, since
    # there's no meeting_folder naming convention to derive one from.
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS display_title TEXT",
]

_TDOC_FIELDS = [
    "tdoc_id", "meeting_id", "tsg", "wg_short", "title", "source", "contact", "contact_id",
    "doc_type", "for_action", "abstract", "secretary_remarks", "agenda_item",
    "agenda_item_description", "status", "reservation_date", "uploaded_at", "is_revision_of",
    "revised_to", "release", "specification", "spec_version", "related_wis", "cr_number",
    "cr_revision", "cr_category", "tsg_cr_pack", "reply_to", "ls_to", "ls_cc", "original_ls",
    "reply_in", "file_url", "file_size_bytes", "file_modified_at", "local_zip_path",
]


_schema_ready = False
_schema_lock = threading.Lock()


def _existing_columns(cur, table: str) -> set:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
    )
    return {row[0] for row in cur.fetchall()}


def _run_schema_statements(conn) -> None:
    with conn.cursor() as cur:
        existing_by_table: dict = {}
        for statement in SCHEMA_STATEMENTS:
            m = _ALTER_ADD_COLUMN_RE.search(statement)
            if m:
                table, column = m.group(1), m.group(2)
                # A no-op "ADD COLUMN IF NOT EXISTS" still takes an
                # AccessExclusiveLock on the table just to check — on an
                # already-migrated schema (the normal case, every time
                # after the very first deploy) every one of these columns
                # already exists, so skipping via a plain read from
                # information_schema avoids ever taking that lock at all.
                if table not in existing_by_table:
                    existing_by_table[table] = _existing_columns(cur, table)
                if column in existing_by_table[table]:
                    continue
            cur.execute(statement)
    conn.commit()


def connect() -> psycopg.Connection:
    """Every caller in the codebase goes through this — including one API
    request per call (service/queries.py) and a long-running crawl/render
    job holding a single connection for hours. Re-running every ALTER
    TABLE ... ADD COLUMN IF NOT EXISTS on EVERY connection was fine in
    isolation, but each one takes an AccessExclusiveLock on its table
    just to check, even when the column already exists — which collides
    with anything else concurrently holding even a row-level lock on that
    table. This genuinely deadlocked a running --render job against
    nothing more than a normal read query (a one-off DB check) started
    while it was mid-transaction.

    Two layers of defense: migrations run at most once per process
    (cached after the first connection — every connection after that
    skips straight to psycopg.connect()), AND even that first run skips
    any ALTER whose column is already present (see _run_schema_statements)
    — the common case (an already-migrated schema, which is every run
    after the very first deploy) never takes the lock at all, in any
    process, including a brand-new one started while another is mid-write."""
    global _schema_ready
    conn = psycopg.connect(config.DATABASE_URL)
    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                _run_schema_statements(conn)
                _schema_ready = True
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_meeting(conn, tsg, wg_short, wg_path, meeting_folder, meeting_url, modified_at) -> int:
    row = conn.execute(
        """
        INSERT INTO meetings (tsg, wg_short, wg_path, meeting_folder, meeting_url, modified_at, last_synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (tsg, wg_short, meeting_folder) DO UPDATE SET
            modified_at = excluded.modified_at,
            last_synced_at = now()
        RETURNING id
        """,
        (tsg, wg_short, wg_path, meeting_folder, meeting_url, modified_at.isoformat() if modified_at else None),
    ).fetchone()
    return row[0]


def meeting_needs_info(conn, meeting_id: int) -> bool:
    """True if this meeting's real date/location hasn't been fetched yet
    (or the last attempt found nothing) — used to avoid re-fetching the
    Invitation document on every re-crawl once we already have it."""
    row = conn.execute("SELECT start_date FROM meetings WHERE id = %s", (meeting_id,)).fetchone()
    return row is not None and row[0] is None


def update_meeting_info(conn, meeting_id: int, info) -> None:
    conn.execute(
        "UPDATE meetings SET start_date = %s, end_date = %s, location = %s, country = %s WHERE id = %s",
        (info.start_date, info.end_date, info.location, info.country, meeting_id),
    )


def upsert_calendar_only_meeting(conn, tsg, wg_short, wg_path, meeting_folder, meeting_url,
                                  display_title, start_date, end_date, location) -> int:
    """For a meeting-calendar entry with no FTP folder of its own (see
    display_title column comment) — dates/location come straight from
    3GPP's dynareport page, not an Invitation document, so (unlike
    upsert_meeting + update_meeting_info) this always overwrites rather
    than only filling in NULLs: dynareport is cheap to re-check every
    sync and is the only source of truth for these entries."""
    row = conn.execute(
        """
        INSERT INTO meetings (tsg, wg_short, wg_path, meeting_folder, meeting_url, last_synced_at,
                               display_title, start_date, end_date, location)
        VALUES (%s, %s, %s, %s, %s, now(), %s, %s, %s, %s)
        ON CONFLICT (tsg, wg_short, meeting_folder) DO UPDATE SET
            last_synced_at = now(),
            display_title = excluded.display_title,
            start_date = excluded.start_date,
            end_date = excluded.end_date,
            location = excluded.location
        RETURNING id
        """,
        (tsg, wg_short, wg_path, meeting_folder, meeting_url, display_title, start_date, end_date, location),
    ).fetchone()
    return row[0]


def get_meeting_dates(conn, meeting_id: int):
    """(start_date, end_date), either possibly None — used to decide which
    candidate meetings are actually past vs upcoming by real date, not
    just by folder name/modified-time (see _select_meetings_to_download)."""
    row = conn.execute("SELECT start_date, end_date FROM meetings WHERE id = %s", (meeting_id,)).fetchone()
    return row if row else (None, None)


def known_groups(conn):
    """Distinct (tsg, wg_short) pairs already tracked in meetings — the
    scope a daily refresh should re-sync, without needing a separate
    config list to keep in step with what's actually been crawled."""
    return conn.execute("SELECT DISTINCT tsg, wg_short FROM meetings ORDER BY tsg, wg_short").fetchall()


def get_tdoc_file_state(conn, tdoc_id: str):
    row = conn.execute(
        "SELECT file_modified_at, local_zip_path FROM tdocs WHERE tdoc_id = %s",
        (tdoc_id,),
    ).fetchone()
    return row


def reset_extraction(conn, tdoc_id: str):
    conn.execute(
        """
        UPDATE tdocs
        SET text_path = NULL, extraction_status = NULL, extraction_error = NULL, extracted_source_file = NULL
        WHERE tdoc_id = %s
        """,
        (tdoc_id,),
    )


def tdocs_pending_extraction(conn):
    return conn.execute(
        """
        SELECT tdoc_id, tsg, wg_short, local_zip_path
        FROM tdocs
        WHERE local_zip_path IS NOT NULL AND extraction_status IS NULL
        """
    ).fetchall()


def save_extraction_result(conn, tdoc_id: str, *, text_path, extraction_status, extraction_error, extracted_source_file):
    conn.execute(
        """
        UPDATE tdocs
        SET text_path = %s, extraction_status = %s, extraction_error = %s, extracted_source_file = %s
        WHERE tdoc_id = %s
        """,
        (text_path, extraction_status, extraction_error, extracted_source_file, tdoc_id),
    )


def get_render_status(conn, tdoc_id: str):
    """(rendered_path, render_status), used to re-check whether another
    concurrent request already rendered this document while this one was
    waiting on the render semaphore — avoids a redundant re-render."""
    row = conn.execute(
        "SELECT rendered_path, render_status FROM tdocs WHERE tdoc_id = %s", (tdoc_id,)
    ).fetchone()
    return row if row else (None, None)


def tdocs_pending_render(conn):
    return conn.execute(
        """
        SELECT tdoc_id, tsg, wg_short, local_zip_path
        FROM tdocs
        WHERE local_zip_path IS NOT NULL AND render_status IS NULL
        """
    ).fetchall()


def save_render_result(conn, tdoc_id: str, *, rendered_path, render_status, render_error):
    conn.execute(
        """
        UPDATE tdocs
        SET rendered_path = %s, render_status = %s, render_error = %s
        WHERE tdoc_id = %s
        """,
        (rendered_path, render_status, render_error, tdoc_id),
    )


_COVERPAGE_BACKFILL_FIELDS = {
    "title", "source", "release", "related_wis", "agenda_item",
    "for_action", "cr_category", "specification", "cr_number", "cr_revision",
}


def backfill_metadata_from_coverpage(conn, tdoc_id: str, fields: dict):
    """Fill only currently-NULL columns from cover-page-parsed fields —
    never overwrites metadata that already came from a TDoc list export."""
    cols = [k for k in fields if k in _COVERPAGE_BACKFILL_FIELDS]
    if not cols:
        return
    set_clause = ", ".join(f"{c} = COALESCE({c}, %s)" for c in cols)
    conn.execute(
        f"UPDATE tdocs SET {set_clause} WHERE tdoc_id = %s",
        [fields[c] for c in cols] + [tdoc_id],
    )


def upsert_tdoc(conn, record: dict):
    columns = [f for f in _TDOC_FIELDS if f in record]
    placeholders = ", ".join("%s" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "tdoc_id")
    sql = f"""
        INSERT INTO tdocs ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT (tdoc_id) DO UPDATE SET {updates}
    """
    conn.execute(sql, [record[c] for c in columns])
