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
    # Auth (plan §12). A user's stable identity is separate from how they
    # signed in — "google" today, "local" for admin-issued test accounts —
    # specifically so a second sign-in method never requires restructuring
    # this. status is the admin's revoke switch; sessions.token_hash is
    # looked up (and status re-checked) on every authenticated request,
    # not just at login, so a revoke actually takes effect immediately.
    """
    CREATE TABLE IF NOT EXISTS users (
        id            SERIAL PRIMARY KEY,
        email         TEXT NOT NULL UNIQUE,
        display_name  TEXT,
        is_admin      BOOLEAN NOT NULL DEFAULT false,
        status        TEXT NOT NULL DEFAULT 'active',
        created_at    TIMESTAMPTZ DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS identities (
        id                SERIAL PRIMARY KEY,
        user_id           INTEGER NOT NULL REFERENCES users(id),
        provider          TEXT NOT NULL,
        provider_user_id  TEXT NOT NULL,
        password_hash     TEXT,
        created_at        TIMESTAMPTZ DEFAULT now(),
        UNIQUE(provider, provider_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id          SERIAL PRIMARY KEY,
        token_hash  TEXT NOT NULL UNIQUE,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        created_at  TIMESTAMPTZ DEFAULT now(),
        expires_at  TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash)",
    # Personal MCP tokens (plan §12 item 3), independent of website
    # sessions on purpose — revoking one must not require touching the
    # other. revoked_at is a second, narrower lever than users.status:
    # disabling the whole account blocks this token too (the lookup
    # joins to users and checks status), but revoking just this token
    # leaves the account's website sessions untouched.
    """
    CREATE TABLE IF NOT EXISTS mcp_tokens (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        token_hash  TEXT NOT NULL UNIQUE,
        name        TEXT,
        created_at  TIMESTAMPTZ DEFAULT now(),
        revoked_at  TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mcp_tokens_token_hash ON mcp_tokens(token_hash)",
    # A minimal OAuth Authorization Server, layered on top of the same
    # login/mcp_tokens system above — for clients that only accept a
    # server URL (Claude.ai/ChatGPT-style connectors) and have no field
    # to paste a personal token into. Those clients expect to redirect
    # the user through a real login+consent step and receive a token
    # automatically; the token they end up with is still just an
    # mcp_tokens row, so the exact same revocation/status checks apply
    # to it as to a self-service-generated one.
    """
    CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id      TEXT PRIMARY KEY,
        client_secret  TEXT,
        redirect_uris  TEXT,
        client_name    TEXT,
        created_at     TIMESTAMPTZ DEFAULT now()
    )
    """,
    # Added after the table above — RFC 7591's registration handler
    # defaults this to "client_secret_post" for any client that doesn't
    # specify one, and the token endpoint re-checks it against whatever
    # get_client() returns; leaving it unstored meant every client's
    # auth method silently reverted to None the moment it was reloaded.
    "ALTER TABLE oauth_clients ADD COLUMN IF NOT EXISTS token_endpoint_auth_method TEXT",
    # A separate lever from users.status: an admin can block a specific
    # user's MCP access — self-service tokens AND the OAuth flow both
    # refuse to mint a new one while this is false — without touching
    # their website login at all. Revoking one token alone can't do
    # this, since the account holder can just re-approve a fresh one
    # through the same consent flow as long as their account is active.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS mcp_access BOOLEAN NOT NULL DEFAULT true",
    # Short-lived, single-use: carries one /authorize request's params
    # from the SDK's own /authorize endpoint to our own login+consent
    # page (see auth/oauth_provider.py's authorize()), since a
    # provider's authorize() only returns a redirect URL — it doesn't
    # get to run any UI itself.
    """
    CREATE TABLE IF NOT EXISTS oauth_authorize_requests (
        id              TEXT PRIMARY KEY,
        client_id       TEXT NOT NULL,
        redirect_uri    TEXT NOT NULL,
        code_challenge  TEXT NOT NULL,
        state           TEXT,
        scopes          TEXT,
        created_at      TIMESTAMPTZ DEFAULT now(),
        expires_at      TIMESTAMPTZ NOT NULL
    )
    """,
    # Minted once the user approves the consent screen; exchanged for a
    # real access token by the SDK's /token endpoint. used=true after
    # exchange so a leaked/replayed code can't be spent twice (RFC 6749
    # §4.1.2 treats a code as single-use).
    """
    CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
        code            TEXT PRIMARY KEY,
        client_id       TEXT NOT NULL,
        user_id         INTEGER NOT NULL REFERENCES users(id),
        code_challenge  TEXT NOT NULL,
        redirect_uri    TEXT NOT NULL,
        scopes          TEXT,
        expires_at      TIMESTAMPTZ NOT NULL,
        used            BOOLEAN NOT NULL DEFAULT false
    )
    """,
    # First personalization table (plan §12). User-generated data, not
    # crawled content — reads and writes both go through the
    # full-privilege connection (crawler.db, same as users/sessions/
    # mcp_tokens above), never through service/db.py's read-only role;
    # that role's SELECT-only grant is specifically about protecting
    # the crawled tdocs/meetings data, not about this.
    """
    CREATE TABLE IF NOT EXISTS bookmarks (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        tdoc_id     TEXT NOT NULL REFERENCES tdocs(tdoc_id),
        created_at  TIMESTAMPTZ DEFAULT now(),
        UNIQUE(user_id, tdoc_id)
    )
    """,
    # Second personalization table (plan §12) — same rationale as
    # bookmarks above: user-generated, full-privilege connection, never
    # the read-only role. Automatic, not an explicit save action —
    # every fresh search a signed-in user runs (not every pagination
    # click, not an empty/cleared search) gets recorded here, so a
    # small "Recent searches" list can just show what actually
    # happened rather than what someone remembered to save.
    """
    CREATE TABLE IF NOT EXISTS search_history (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        params      TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT now()
    )
    """,
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
