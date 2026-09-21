"""Bookmarks — the first personalization table (plan §12). User-
generated data, not crawled content, so unlike everything else in
service/ this deliberately does NOT go through service/db.py's
read-only connection — callers pass in a connection from
crawler.db.session() instead, same as auth/queries.py does for its
own per-user tables.
"""
from . import queries
from .models import Page


def add_bookmark(conn, user_id: int, tdoc_id: str) -> None:
    conn.execute(
        "INSERT INTO bookmarks (user_id, tdoc_id) VALUES (%s, %s) ON CONFLICT (user_id, tdoc_id) DO NOTHING",
        (user_id, tdoc_id),
    )


def remove_bookmark(conn, user_id: int, tdoc_id: str) -> bool:
    result = conn.execute(
        "DELETE FROM bookmarks WHERE user_id = %s AND tdoc_id = %s", (user_id, tdoc_id)
    )
    return result.rowcount > 0


def is_bookmarked(conn, user_id: int, tdoc_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM bookmarks WHERE user_id = %s AND tdoc_id = %s", (user_id, tdoc_id)
    ).fetchone()
    return row is not None


def list_bookmarks(conn, user_id: int, offset: int = 0, limit: int = 20) -> Page:
    # Reuses queries.py's own summary shape/row-mapper, so the frontend
    # can render a bookmarks list with the exact same row code already
    # built for search results — this is the one place outside
    # queries.py that reaches into its "private" helpers, justified by
    # being the same package.
    sql = f"""
        SELECT {queries._SUMMARY_SELECT}, COUNT(*) OVER() AS total_count
        FROM bookmarks b
        JOIN tdocs t ON t.tdoc_id = b.tdoc_id
        JOIN meetings m ON t.meeting_id = m.id
        WHERE b.user_id = %s
        ORDER BY b.created_at DESC
        OFFSET %s LIMIT %s
    """
    rows = conn.execute(sql, (user_id, offset, limit)).fetchall()
    total = rows[0][-1] if rows else 0
    items = [queries._row_to_summary(row[:-1]) for row in rows]
    return Page(items=items, total=total, offset=offset, limit=limit)
