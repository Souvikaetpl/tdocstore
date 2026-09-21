"""MCP server exposing the same read-only queries as the HTTP API
(service/queries.py) as tools an MCP client (Claude Code, Claude Desktop,
etc.) can call directly. A third thin layer over the shared service
functions, parallel to api/main.py's HTTP layer — no query logic
duplicated here, same as the plan's shared-service-layer design intended.

Run directly for local stdio use (the normal case — a client launches
this as a subprocess and talks to it over stdin/stdout, no network port,
no bearer token, no auth at all — trust comes from "you can launch this
subprocess", same as before this file grew a second mode):
    python mcp_server.py

Or run it as a real network service (plan §12 item 3 / §13), gated
either by a personal token pasted into a client's header config, or by
a real OAuth login+consent redirect for clients (Claude.ai, ChatGPT)
that only accept a server URL — both paths issue the same kind of
token and both are revocable the same way (see auth/oauth_provider.py):
    python mcp_server.py --http

Or explore interactively during development with the SDK's inspector:
    mcp dev mcp_server.py
"""
import sys
from dataclasses import asdict
from typing import Optional
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer

from auth import config as auth_config
from auth.oauth_provider import LocalOAuthProvider
from service import queries

_INSTRUCTIONS = (
    "Search and browse 3GPP TDocs (meeting documents) and meetings, "
    "crawled from 3GPP's own public FTP archive. Read-only — no writes."
)


def _build_server(http_mode: bool) -> MCPServer:
    if not http_mode:
        return MCPServer(name="3gpp-tdoc-replica", instructions=_INSTRUCTIONS)
    # issuer_url/resource_server_url must be THIS server's own origin —
    # it's where the SDK mounts /authorize, /token, /register, and
    # /.well-known/oauth-authorization-server (auth_server_provider
    # below implements what those call into). Derived from the single
    # shared MCP_SERVER_URL (auth/config.py) so this can never drift
    # from what /account shows the user as "the MCP server URL".
    resource_server_url = auth_config.MCP_SERVER_URL
    issuer_url = urlparse(resource_server_url)._replace(path="").geturl()
    return MCPServer(
        name="3gpp-tdoc-replica",
        instructions=_INSTRUCTIONS,
        # validate_token_resource=False because these tokens carry no
        # RFC 8707 resource/audience claim to check.
        auth=AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=resource_server_url,
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        ),
        auth_server_provider=LocalOAuthProvider(),
    )


mcp = _build_server(http_mode="--http" in sys.argv)

# Hard caps, enforced regardless of what a caller asks for — an AI
# client with an unbounded `limit` or several compare_tdocs calls in a
# row can otherwise pull a large fraction of the corpus's full text
# into its own context for no real reason. Full, untruncated text
# remains available through the direct API/download for anyone who
# actually needs it — this only bounds what MCP tool calls return.
MAX_PAGE_LIMIT = 50
# 20,000 was truncating 22% of the whole corpus — a measured pass over
# every extracted document's actual length showed 99,999 instead covers
# 96.35% of it untruncated (only the true long tail, up to 3.2M chars
# for the largest single document, still gets cut), while still being
# a small fraction of a modern model's context window (~25k tokens).
MAX_TEXT_CHARS = 99_999


def _page_dict(page) -> dict:
    return {
        "items": [asdict(item) for item in page.items],
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
    }


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE_LIMIT))


def _truncate_text(detail_dict: dict) -> dict:
    text = detail_dict.get("text")
    if isinstance(text, str) and len(text) > MAX_TEXT_CHARS:
        detail_dict["text"] = (
            text[:MAX_TEXT_CHARS]
            + f"\n\n[truncated at {MAX_TEXT_CHARS} characters — full text available via "
              f"the website or GET /api/tdocs/{{tdoc_id}} on the direct API]"
        )
    return detail_dict


@mcp.tool()
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
) -> dict:
    """Search 3GPP TDocs (meeting documents) by keyword and/or filters.

    `query` runs a full-text search over each document's title and
    abstract. Any of the filters can be combined: tsg ("ran"/"sa"/"ct"),
    wg_short (e.g. "SA2", "plenary"), meeting_folder (e.g.
    "TSGS2_177_Prague_2026-10"), doc_type, specification (e.g. "23.501"),
    source (submitting company/group). Returns a page of TDoc summaries
    with `total` for how many matched overall.
    """
    page = queries.search_tdocs(
        query=query, tsg=tsg, wg_short=wg_short, meeting_folder=meeting_folder,
        doc_type=doc_type, specification=specification, source=source,
        offset=offset, limit=_clamp_limit(limit),
    )
    return _page_dict(page)


@mcp.tool()
def get_tdoc(tdoc_id: str) -> Optional[dict]:
    """Get full detail for one TDoc by its exact ID (e.g. "S2-2312345"),
    including its extracted text content when available (truncated for
    a very long document — see the returned text itself for how to get
    the rest). Returns null if no TDoc with that ID is tracked.
    """
    detail = queries.get_tdoc(tdoc_id)
    return _truncate_text(asdict(detail)) if detail is not None else None


@mcp.tool()
def compare_tdocs(tdoc_ids: list[str]) -> list[dict]:
    """Get full detail for up to 3 TDocs at once, for side-by-side
    comparison. Any ID that doesn't exist is silently skipped rather than
    erroring the whole call.
    """
    return [_truncate_text(asdict(detail)) for detail in queries.compare_tdocs(tdoc_ids)]


@mcp.tool()
def list_meetings(
    tsg: Optional[str] = None,
    wg_short: Optional[str] = None,
    offset: int = 0,
    limit: int = 20,
) -> dict:
    """List tracked 3GPP meetings, optionally filtered by tsg
    ("ran"/"sa"/"ct") and/or wg_short (e.g. "SA2", "plenary"). Each
    meeting includes its real date range and location when known, and its
    TDoc count.
    """
    page = queries.list_meetings(tsg=tsg, wg_short=wg_short, offset=offset, limit=_clamp_limit(limit))
    return _page_dict(page)


@mcp.tool()
def get_stats() -> dict:
    """Overall corpus stats: total TDocs, total working groups tracked,
    and total TSGs (RAN/SA/CT) tracked.
    """
    return queries.get_stats()


_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_REQUESTS = 60
_rate_limit_state: dict = {}


def _rate_limit_check(key: str) -> bool:
    """A plain fixed-window counter, not a sliding log — good enough
    for 'stop one runaway/compromised token or IP from degrading the
    service for everyone else', which is the actual goal here, not
    perfectly smooth throttling. Safe without locks: this only ever
    runs on the single asyncio event loop thread, and does no I/O
    between the read and the write below."""
    import time

    now = time.time()
    window = int(now // _RATE_LIMIT_WINDOW_SECONDS)
    window_start, count = _rate_limit_state.get(key, (window, 0))
    if window_start != window:
        window_start, count = window, 0
    count += 1
    _rate_limit_state[key] = (window_start, count)
    return count <= _RATE_LIMIT_MAX_REQUESTS


class _RateLimitASGIMiddleware:
    """Wraps the app streamable_http_app() returns, rather than using
    the SDK's own middleware= hook — this only needs to run outside
    everything else, keyed on the raw bearer token (or source IP for
    an unauthenticated request, so even token-guessing attempts are
    throttled) before any of the OAuth/tool-dispatch machinery runs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode()
        token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
        client = scope.get("client")
        key = token or (client[0] if client else "unknown")

        if not _rate_limit_check(key):
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {"error": f"rate limit exceeded — max {_RATE_LIMIT_MAX_REQUESTS} requests per "
                          f"{_RATE_LIMIT_WINDOW_SECONDS}s, try again shortly"},
                status_code=429,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


if __name__ == "__main__":
    if "--http" in sys.argv:
        import uvicorn

        http_app = _RateLimitASGIMiddleware(mcp.streamable_http_app(host="127.0.0.1"))
        uvicorn.run(http_app, host="127.0.0.1", port=8001)
    else:
        mcp.run(transport="stdio")
