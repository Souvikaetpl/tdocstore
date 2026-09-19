"""GET/POST /oauth/consent — the page a paste-a-URL-only MCP client
(Claude.ai, ChatGPT) redirects the user's browser to, via
LocalOAuthProvider.authorize() (see oauth_provider.py). Not part of
auth/routes.py because it's a browser-facing page with its own HTML,
not a JSON API endpoint like everything there.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.server.auth.provider import construct_redirect_uri

from crawler import db
from . import queries
from .deps import get_current_user, require_user
from .models import User

router = APIRouter(tags=["oauth-consent"])

_EXPIRED_HTML = (
    "<p>This authorization request has expired or was already used. "
    "Please try connecting again from the application.</p>"
)


def _page(body: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Authorize — 3GPP TDoc Replica</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body><main class="admin-main"><section class="admin-section">{body}</section></main></body></html>"""


@router.get("/oauth/consent", response_class=HTMLResponse)
def oauth_consent_page(request_id: str = Query(...), user: Optional[User] = Depends(get_current_user)):
    with db.session() as conn:
        req = queries.get_oauth_authorize_request(conn, request_id)
    if req is None:
        return HTMLResponse(_page(_EXPIRED_HTML), status_code=400)

    if user is None:
        next_url = f"/oauth/consent?request_id={request_id}"
        return HTMLResponse(_page(f"""
            <h2>Sign in to continue</h2>
            <p class="account-hint">An application wants to connect to your 3GPP TDoc Replica account.
            Sign in first, then you'll come straight back here.</p>
            <a class="auth-signin" href="/auth/google/login?next={next_url}">Sign in with Google</a>
        """))

    if not user.mcp_access:
        return HTMLResponse(_page("""
            <h2>MCP access disabled</h2>
            <p class="account-hint">An administrator has disabled MCP access for your account.
            Your website login is unaffected, but no application can be authorized to access
            TDocs/meetings through MCP on your behalf right now.</p>
        """))

    with db.session() as conn:
        client = queries.get_oauth_client(conn, req["client_id"])
    client_name = (client or {}).get("client_name") or req["client_id"]
    return HTMLResponse(_page(f"""
        <h2>Authorize access</h2>
        <p class="account-hint"><b>{client_name}</b> wants to access your 3GPP TDoc Replica account
        as <b>{user.email}</b>. It will be able to search and read TDocs and meetings on your behalf.
        You can revoke this at any time from <a href="/account">your account page</a>.</p>
        <form method="post" action="/oauth/consent">
          <input type="hidden" name="request_id" value="{request_id}">
          <button type="submit" name="decision" value="approve">Approve</button>
          <button type="submit" name="decision" value="deny" class="secondary">Deny</button>
        </form>
    """))


@router.post("/oauth/consent")
def oauth_consent_submit(request_id: str = Form(...), decision: str = Form(...), user: User = Depends(require_user)):
    with db.session() as conn:
        req = queries.get_oauth_authorize_request(conn, request_id)
    if req is None:
        return HTMLResponse(_page(_EXPIRED_HTML), status_code=400)

    if decision != "approve":
        redirect_url = construct_redirect_uri(req["redirect_uri"], error="access_denied", state=req["state"])
        return RedirectResponse(redirect_url, status_code=303)

    with db.session() as conn:
        code = queries.create_authorization_code(
            conn, client_id=req["client_id"], user_id=user.id,
            code_challenge=req["code_challenge"], redirect_uri=req["redirect_uri"], scopes=req["scopes"],
        )
    redirect_url = construct_redirect_uri(req["redirect_uri"], code=code, state=req["state"])
    return RedirectResponse(redirect_url, status_code=303)
