from contextlib import contextmanager

import psycopg

from . import config

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
]

_TDOC_FIELDS = [
    "tdoc_id", "meeting_id", "tsg", "wg_short", "title", "source", "contact", "contact_id",
    "doc_type", "for_action", "abstract", "secretary_remarks", "agenda_item",
    "agenda_item_description", "status", "reservation_date", "uploaded_at", "is_revision_of",
    "revised_to", "release", "specification", "spec_version", "related_wis", "cr_number",
    "cr_revision", "cr_category", "tsg_cr_pack", "reply_to", "ls_to", "ls_cc", "original_ls",
    "reply_in", "file_url", "file_size_bytes", "file_modified_at", "local_zip_path",
]


def connect() -> psycopg.Connection:
    conn = psycopg.connect(config.DATABASE_URL)
    with conn.cursor() as cur:
        for statement in SCHEMA_STATEMENTS:
            cur.execute(statement)
    conn.commit()
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
