from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from crawler import db
from . import config as auth_config
from . import queries
from .deps import SESSION_COOKIE_NAME, get_current_user, require_admin, require_user
from .google_oauth import oauth
from .models import User
from .rate_limit import is_failed_login_throttled, rate_limit, record_failed_login

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/config")
def public_config():
    """Not secret, not per-user — the same URL every account connects
    an MCP client to (see /account). Just avoids hardcoding it into
    frontend JS a second place, so it can't drift from what
    mcp_server.py itself advertises as its resource_server_url."""
    return {"mcp_server_url": auth_config.MCP_SERVER_URL}


@router.get("/google/login")
async def google_login(request: Request, next: Optional[str] = None):
    # Carried in the same transient Starlette session already used for
    # the OAuth CSRF state — this round-trips through Google and back,
    # so the callback below knows where to send the browser afterward
    # (e.g. straight back to the OAuth-consent page that sent them here
    # to sign in). Only ever a relative path, checked again below, so
    # this can't be turned into an open redirect.
    if next and next.startswith("/"):
        request.session["post_login_next"] = next
    redirect_uri = str(request.url_for("google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get(
    "/google/callback", name="google_callback",
    dependencies=[Depends(rate_limit(20, 60, "google_callback"))],
)
async def google_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or await oauth.google.userinfo(token=token)

    with db.session() as conn:
        user = queries.get_or_create_google_user(
            conn, google_sub=userinfo["sub"], email=userinfo["email"],
            display_name=userinfo.get("name"),
        )
        session_token = queries.create_session(conn, user.id)

    next_url = request.session.pop("post_login_next", None)
    response = RedirectResponse(url=next_url if next_url and next_url.startswith("/") else "/")
    response.set_cookie(
        SESSION_COOKIE_NAME, session_token,
        httponly=True, samesite="lax", max_age=int(queries.SESSION_TTL.total_seconds()),
    )
    return response


@router.post("/logout")
def logout(
    response: Response,
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    if session_token:
        with db.session() as conn:
            queries.delete_session(conn, session_token)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(user: Optional[User] = Depends(get_current_user)):
    if user is None:
        return {"signed_in": False}
    return {
        "signed_in": True,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "mcp_access": user.mcp_access,
    }


class LocalLoginBody(BaseModel):
    username: str
    password: str


@router.post("/local/login", dependencies=[Depends(rate_limit(10, 60, "local_login"))])
def local_login(body: LocalLoginBody, response: Response):
    # Keyed on the attempted username itself, checked before touching
    # the database — catches a slow/distributed brute force that
    # rotates source IPs to dodge the per-IP throttle above, which the
    # per-IP check alone can't.
    if is_failed_login_throttled(body.username):
        raise HTTPException(
            status_code=429, detail="Too many failed attempts for this account — try again in a few minutes"
        )
    with db.session() as conn:
        user = queries.authenticate_local(conn, body.username, body.password)
        if user is None:
            record_failed_login(body.username)
            raise HTTPException(status_code=401, detail="Invalid username or password")
        session_token = queries.create_session(conn, user.id)

    response.set_cookie(
        SESSION_COOKIE_NAME, session_token,
        httponly=True, samesite="lax", max_age=int(queries.SESSION_TTL.total_seconds()),
    )
    return {"ok": True}


class CreateTestAccountBody(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = None


@router.post("/admin/test-accounts", dependencies=[Depends(rate_limit(20, 60, "create_test_account"))])
def create_test_account(body: CreateTestAccountBody, admin: User = Depends(require_admin)):
    with db.session() as conn:
        user = queries.create_local_test_account(conn, body.username, body.password, body.display_name)
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "status": user.status}


@router.get("/admin/users")
def list_users(admin: User = Depends(require_admin)):
    with db.session() as conn:
        users = queries.list_users(conn)
    return [
        {
            "id": u.id, "email": u.email, "display_name": u.display_name, "is_admin": u.is_admin,
            "status": u.status, "mcp_access": u.mcp_access,
        }
        for u in users
    ]


class SetStatusBody(BaseModel):
    status: str


@router.post("/admin/users/{user_id}/status")
def set_user_status(user_id: int, body: SetStatusBody, admin: User = Depends(require_admin)):
    if body.status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'disabled'")
    with db.session() as conn:
        queries.set_user_status(conn, user_id, body.status)
    return {"ok": True}


class SetMcpAccessBody(BaseModel):
    enabled: bool


@router.post("/admin/users/{user_id}/mcp-access")
def set_mcp_access(user_id: int, body: SetMcpAccessBody, admin: User = Depends(require_admin)):
    """Separate from status above — blocks MCP only (self-service and
    OAuth-issued tokens both), website login untouched. See
    queries.set_mcp_access / create_mcp_token for the actual
    enforcement points."""
    with db.session() as conn:
        queries.set_mcp_access(conn, user_id, body.enabled)
    return {"ok": True}


@router.post("/admin/users/{user_id}/revoke-sessions")
def revoke_all_sessions(user_id: int, admin: User = Depends(require_admin)):
    """The missing fourth lever from plan §15's risk notes: ends this
    account's website sessions only, right now, leaving MCP access and
    every MCP token completely untouched — the mirror image of
    set_mcp_access above. The account stays active; they can sign back
    in immediately, this just forces that re-login rather than banning
    them."""
    with db.session() as conn:
        count = queries.revoke_all_sessions(conn, user_id)
    return {"ok": True, "sessions_ended": count}


# Personal MCP tokens (plan §12 item 3) — self-service for any signed-in
# user, not admin-only. Independent of the website session on purpose:
# revoking one of these never touches the other (see
# queries.get_user_by_mcp_token / revoke_mcp_token).
class CreateMcpTokenBody(BaseModel):
    name: Optional[str] = None


@router.post("/mcp-tokens")
def create_mcp_token(body: CreateMcpTokenBody, user: User = Depends(require_user)):
    with db.session() as conn:
        try:
            token = queries.create_mcp_token(conn, user.id, body.name)
        except queries.McpAdminOnlyError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except queries.McpTokenLimitExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc))
    if token is None:
        raise HTTPException(status_code=403, detail="MCP access has been disabled for your account")
    # Shown exactly once — only its hash is ever stored, so this is the
    # only response that will ever contain the raw token.
    return {"token": token}


@router.get("/mcp-tokens")
def list_mcp_tokens(user: User = Depends(require_user)):
    with db.session() as conn:
        return queries.list_mcp_tokens(conn, user.id)


@router.post("/mcp-tokens/{token_id}/revoke")
def revoke_mcp_token(token_id: int, user: User = Depends(require_user)):
    with db.session() as conn:
        ok = queries.revoke_mcp_token(conn, token_id, user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"ok": True}
