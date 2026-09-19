import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt

from .models import User

OAUTH_CODE_TTL = timedelta(minutes=5)
OAUTH_AUTHORIZE_REQUEST_TTL = timedelta(minutes=10)

SESSION_TTL = timedelta(days=30)

_USER_FIELDS = "id, email, display_name, is_admin, status, mcp_access"


def _row_to_user(row) -> User:
    return User(
        id=row[0], email=row[1], display_name=row[2], is_admin=row[3], status=row[4], mcp_access=row[5]
    )


def get_user_by_id(conn, user_id: int) -> Optional[User]:
    row = conn.execute(f"SELECT {_USER_FIELDS} FROM users WHERE id = %s", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_email(conn, email: str) -> Optional[User]:
    row = conn.execute(f"SELECT {_USER_FIELDS} FROM users WHERE email = %s", (email,)).fetchone()
    return _row_to_user(row) if row else None


def get_identity(conn, provider: str, provider_user_id: str):
    return conn.execute(
        "SELECT id, user_id, password_hash FROM identities WHERE provider = %s AND provider_user_id = %s",
        (provider, provider_user_id),
    ).fetchone()


def create_user(conn, email: str, display_name: Optional[str]) -> User:
    row = conn.execute(
        f"INSERT INTO users (email, display_name) VALUES (%s, %s) RETURNING {_USER_FIELDS}",
        (email, display_name),
    ).fetchone()
    return _row_to_user(row)


def link_identity(
    conn, user_id: int, provider: str, provider_user_id: str, password_hash: Optional[str] = None
) -> None:
    conn.execute(
        "INSERT INTO identities (user_id, provider, provider_user_id, password_hash) VALUES (%s, %s, %s, %s)",
        (user_id, provider, provider_user_id, password_hash),
    )


def get_or_create_google_user(conn, google_sub: str, email: str, display_name: Optional[str]) -> User:
    """Called on every successful Google callback. provider_user_id is
    Google's own stable 'sub' claim, not the email — an email address can
    change hands or be reused, sub cannot."""
    identity = get_identity(conn, "google", google_sub)
    if identity:
        return get_user_by_id(conn, identity[1])
    user = get_user_by_email(conn, email) or create_user(conn, email, display_name)
    link_identity(conn, user.id, "google", google_sub)
    return user


def create_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
        (token_hash, user_id, expires_at),
    )
    return token


def get_user_by_session_token(conn, token: str) -> Optional[User]:
    """The one place account status is enforced on every authenticated
    request, not just at login — a disabled account or an
    expired/deleted session row fails this lookup immediately."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = conn.execute(
        """
        SELECT u.id, u.email, u.display_name, u.is_admin, u.status, u.mcp_access
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.expires_at > now() AND u.status = 'active'
        """,
        (token_hash,),
    ).fetchone()
    return _row_to_user(row) if row else None


def delete_session(conn, token: str) -> None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))


def set_user_status(conn, user_id: int, status: str) -> None:
    conn.execute("UPDATE users SET status = %s WHERE id = %s", (status, user_id))
    if status == "disabled":
        # Deletes any of their live sessions too, rather than relying on
        # the per-request status check to catch them on their next
        # request — makes a revoke immediate rather than "immediate for
        # new logins, eventually immediate for an existing session".
        conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


def set_mcp_access(conn, user_id: int, enabled: bool) -> None:
    """Separate from set_user_status on purpose — this blocks MCP only,
    website login untouched. Turning it off also revokes every live
    token this user already has (self-service or OAuth-issued), same
    'instant, not just for the next attempt' principle as status —
    otherwise a token minted five minutes ago would just keep working
    until someone thought to revoke it individually too."""
    conn.execute("UPDATE users SET mcp_access = %s WHERE id = %s", (enabled, user_id))
    if not enabled:
        conn.execute("UPDATE mcp_tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))


def list_users(conn):
    rows = conn.execute(f"SELECT {_USER_FIELDS} FROM users ORDER BY created_at DESC").fetchall()
    return [_row_to_user(row) for row in rows]


# Admin-issued test accounts (plan §12 item 5) — sign in with a username
# and password the admin sets, rather than Google. Reuses the same
# users/identities/sessions tables and the same status/revocation path;
# the only thing specific to this provider is where the password lives
# and how it's checked.
def create_local_test_account(conn, username: str, password: str, display_name: Optional[str]) -> User:
    # users.email is NOT NULL UNIQUE — real accounts always have a real
    # one via Google. A synthetic placeholder here keeps that constraint
    # without implying this account has (or needs) a real mailbox.
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user = create_user(conn, email=f"{username}@local.test", display_name=display_name or username)
    link_identity(conn, user.id, "local", username, password_hash=password_hash)
    return user


def create_mcp_token(conn, user_id: int, name: Optional[str] = None) -> Optional[str]:
    """Returns the raw token — the only moment it's ever visible. Only
    its SHA-256 hash is stored, same as sessions, so a database leak
    doesn't hand over usable tokens directly.

    Returns None if this user's MCP access has been switched off by an
    admin — checked here, in the one place both the self-service
    /account button and the OAuth code-exchange path both go through,
    so blocking it can't be bypassed by either route re-approving their
    way to a fresh token."""
    user = get_user_by_id(conn, user_id)
    if user is None or not user.mcp_access:
        return None
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn.execute(
        "INSERT INTO mcp_tokens (user_id, token_hash, name) VALUES (%s, %s, %s)",
        (user_id, token_hash, name),
    )
    return token


def list_mcp_tokens(conn, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, created_at, revoked_at FROM mcp_tokens WHERE user_id = %s ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return [{"id": r[0], "name": r[1], "created_at": r[2], "revoked_at": r[3]} for r in rows]


def revoke_mcp_token(conn, token_id: int, user_id: int) -> bool:
    """Scoped to the owning user — this is self-service (any signed-in
    user manages their own tokens), not an admin action, so it must
    never let one user revoke another's token by guessing an id."""
    result = conn.execute(
        "UPDATE mcp_tokens SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
        (token_id, user_id),
    )
    return result.rowcount > 0


def get_user_by_mcp_token(conn, token: str) -> Optional[User]:
    """The MCP-side equivalent of get_user_by_session_token — same
    per-request enforcement, different table. Checking u.status here
    (not just t.revoked_at) is what makes disabling the whole account
    also cut off MCP, without that being a separate step; checking
    u.mcp_access is the narrower admin lever that blocks MCP alone;
    checking t.revoked_at independently of both is what makes revoking
    just this one token possible without touching anything else."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = conn.execute(
        """
        SELECT u.id, u.email, u.display_name, u.is_admin, u.status, u.mcp_access
        FROM mcp_tokens t JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = %s AND t.revoked_at IS NULL AND u.status = 'active' AND u.mcp_access = true
        """,
        (token_hash,),
    ).fetchone()
    return _row_to_user(row) if row else None


# --- OAuth Authorization Server state (auth/oauth_provider.py) ---

def get_oauth_client(conn, client_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT client_id, client_secret, redirect_uris, client_name, token_endpoint_auth_method "
        "FROM oauth_clients WHERE client_id = %s",
        (client_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "client_id": row[0], "client_secret": row[1],
        "redirect_uris": json.loads(row[2]) if row[2] else [], "client_name": row[3],
        "token_endpoint_auth_method": row[4],
    }


def register_oauth_client(
    conn, client_id: str, client_secret: Optional[str], redirect_uris: list[str], client_name: Optional[str],
    token_endpoint_auth_method: Optional[str],
) -> None:
    conn.execute(
        "INSERT INTO oauth_clients (client_id, client_secret, redirect_uris, client_name, token_endpoint_auth_method) "
        "VALUES (%s, %s, %s, %s, %s)",
        (client_id, client_secret, json.dumps(redirect_uris), client_name, token_endpoint_auth_method),
    )


def create_oauth_authorize_request(
    conn, client_id: str, redirect_uri: str, code_challenge: str, state: Optional[str], scopes: list[str]
) -> str:
    """Stashes one /authorize call's params server-side under a short
    opaque id, so our own consent page (GET /oauth/consent?request_id=…)
    only has to carry that id through the login redirect, rather than
    re-threading every OAuth param through a page it doesn't otherwise
    need to know about."""
    request_id = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + OAUTH_AUTHORIZE_REQUEST_TTL
    conn.execute(
        """
        INSERT INTO oauth_authorize_requests (id, client_id, redirect_uri, code_challenge, state, scopes, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (request_id, client_id, redirect_uri, code_challenge, state, json.dumps(scopes or []), expires_at),
    )
    return request_id


def get_oauth_authorize_request(conn, request_id: str) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT client_id, redirect_uri, code_challenge, state, scopes
        FROM oauth_authorize_requests WHERE id = %s AND expires_at > now()
        """,
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "client_id": row[0], "redirect_uri": row[1], "code_challenge": row[2],
        "state": row[3], "scopes": json.loads(row[4]) if row[4] else [],
    }


def create_authorization_code(
    conn, client_id: str, user_id: int, code_challenge: str, redirect_uri: str, scopes: list[str]
) -> str:
    code = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + OAUTH_CODE_TTL
    conn.execute(
        """
        INSERT INTO oauth_authorization_codes
            (code, client_id, user_id, code_challenge, redirect_uri, scopes, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (code, client_id, user_id, code_challenge, redirect_uri, json.dumps(scopes or []), expires_at),
    )
    return code


def get_authorization_code(conn, code: str) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT client_id, user_id, code_challenge, redirect_uri, scopes, expires_at
        FROM oauth_authorization_codes WHERE code = %s AND expires_at > now() AND used = false
        """,
        (code,),
    ).fetchone()
    if row is None:
        return None
    return {
        "client_id": row[0], "user_id": row[1], "code_challenge": row[2],
        "redirect_uri": row[3], "scopes": json.loads(row[4]) if row[4] else [],
        "expires_at": row[5].timestamp(),
    }


def consume_authorization_code(conn, code: str) -> bool:
    """Marks the code used — a second exchange attempt with the same
    code (replay) then finds nothing via get_authorization_code, per
    RFC 6749 §4.1.2's single-use requirement."""
    result = conn.execute(
        "UPDATE oauth_authorization_codes SET used = true WHERE code = %s AND used = false", (code,)
    )
    return result.rowcount > 0


def revoke_mcp_token_by_raw(conn, token: str) -> None:
    """Like revoke_mcp_token, but keyed by the raw token string rather
    than (id, user_id) — what OAuthAuthorizationServerProvider.revoke_token
    is handed, since the OAuth client only ever has the raw token, never
    its row id."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn.execute(
        "UPDATE mcp_tokens SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL", (token_hash,)
    )


def authenticate_local(conn, username: str, password: str) -> Optional[User]:
    identity = get_identity(conn, "local", username)
    if identity is None:
        return None
    _, user_id, password_hash = identity
    if not password_hash or not bcrypt.checkpw(password.encode(), password_hash.encode()):
        return None
    user = get_user_by_id(conn, user_id)
    if user is None or user.status != "active":
        return None
    return user
