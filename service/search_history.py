"""Automatic search history — the second personalization feature
(plan §12), replacing an earlier explicit "save this search" design
that the project owner didn't want. Same full-privilege-connection
rationale as service/bookmarks.py: user-generated data, never through
service/db.py's read-only role.
"""
import json


def record_search(conn, user_id: int, params: dict) -> None:
    """Called on every fresh search a signed-in user runs (api/main.py
    only calls this for offset == 0 and a non-empty params dict — see
    the route). Skips inserting if it's an exact repeat of the most
    recent entry, so re-clicking Search without changing anything
    doesn't spam the list with duplicates; otherwise always inserts,
    then trims down to the most recent 50 for this user so storage
    doesn't grow without bound."""
    params_json = json.dumps(params, sort_keys=True)
    last = conn.execute(
        "SELECT params FROM search_history WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if last and last[0] == params_json:
        return

    conn.execute("INSERT INTO search_history (user_id, params) VALUES (%s, %s)", (user_id, params_json))
    conn.execute(
        """
        DELETE FROM search_history WHERE user_id = %s AND id NOT IN (
            SELECT id FROM search_history WHERE user_id = %s ORDER BY created_at DESC LIMIT 50
        )
        """,
        (user_id, user_id),
    )


def list_recent(conn, user_id: int, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT id, params, created_at FROM search_history WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
        (user_id, limit),
    ).fetchall()
    return [{"id": r[0], "params": json.loads(r[1]), "created_at": r[2]} for r in rows]
