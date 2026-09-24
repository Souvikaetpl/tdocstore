"""FastAPI backend. Thin HTTP layer over service/queries.py — no query
logic lives here, matching the plan's shared-service-layer phase (the
MCP server, built later, calls the same service functions directly).
"""
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from auth import config as auth_config
from auth.consent_routes import router as oauth_consent_router
from auth.deps import get_current_user, require_user
from auth.models import User
from auth.routes import router as auth_router
from crawler import db as crawler_db
from crawler.render import render_to_pdf
from crawler import config as crawler_config
from service import bookmarks, queries, search_history
from service.models import Page

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Each render spawns its own LibreOffice process — heavy on CPU/memory, so
# concurrent on-demand renders (e.g. several people opening different
# not-yet-rendered documents at once) are capped rather than left
# unbounded. Isolated profile dirs (render.py) already stop them from
# corrupting each other; this just keeps resource use sane. A plain
# threading.Semaphore is enough here since sync routes like this one run
# in Starlette's thread pool within this single process.
_RENDER_SEMAPHORE = threading.Semaphore(2)

app = FastAPI(title="3GPP TDoc Replica API")

# Only backs the transient state/nonce during the Google OAuth handshake
# (authlib's Starlette integration expects request.session) — unrelated
# to our own login sessions (auth/queries.py), which live in Postgres as
# opaque tokens, not a signed cookie.
app.add_middleware(SessionMiddleware, secret_key=auth_config.SESSION_MIDDLEWARE_SECRET)
app.include_router(auth_router)
app.include_router(oauth_consent_router)


def _page_dict(page: Page) -> dict:
    return {
        "items": [asdict(item) for item in page.items],
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/stats")
def stats():
    return queries.get_stats()


@app.get("/api/tdocs")
def search_tdocs(
    q: Optional[str] = None,
    tsg: Optional[str] = None,
    wg: Optional[str] = None,
    meeting: Optional[str] = None,
    doc_type: Optional[str] = None,
    specification: Optional[str] = None,
    source: Optional[str] = None,
    offset: int = 0,
    limit: int = Query(default=20, le=100),
    user: Optional[User] = Depends(get_current_user),
):
    page = queries.search_tdocs(
        query=q, tsg=tsg, wg_short=wg, meeting_folder=meeting, doc_type=doc_type,
        specification=specification, source=source, offset=offset, limit=limit,
    )
    # Automatic, not an explicit save — only a fresh search (offset 0)
    # with at least one real filter counts; pagination clicks and an
    # empty/cleared search both stay out of the history entirely.
    if user is not None and offset == 0:
        params = {k: v for k, v in {
            "q": q, "tsg": tsg, "wg": wg, "meeting": meeting, "source": source, "doc_type": doc_type,
        }.items() if v}
        if params:
            with crawler_db.session() as conn:
                search_history.record_search(conn, user.id, params)
    return _page_dict(page)


@app.get("/api/tdocs/compare")
def compare_tdocs(ids: list[str] = Query(...)):
    results = queries.compare_tdocs(ids, max_docs=3)
    return {"items": [asdict(r) for r in results]}


@app.get("/api/tdocs/{tdoc_id}")
def get_tdoc(tdoc_id: str, user: Optional[User] = Depends(get_current_user)):
    detail = queries.get_tdoc(tdoc_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"TDoc {tdoc_id} not found")
    data = asdict(detail)
    if user is not None:
        with crawler_db.session() as conn:
            data["bookmarked"] = bookmarks.is_bookmarked(conn, user.id, tdoc_id)
    return data


@app.post("/api/bookmarks/{tdoc_id}")
def add_bookmark(tdoc_id: str, user: User = Depends(require_user)):
    if queries.get_tdoc(tdoc_id) is None:
        raise HTTPException(status_code=404, detail=f"TDoc {tdoc_id} not found")
    with crawler_db.session() as conn:
        try:
            bookmarks.add_bookmark(conn, user.id, tdoc_id)
        except bookmarks.BookmarkLimitExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc))
    return {"ok": True}


@app.delete("/api/bookmarks/{tdoc_id}")
def remove_bookmark(tdoc_id: str, user: User = Depends(require_user)):
    with crawler_db.session() as conn:
        bookmarks.remove_bookmark(conn, user.id, tdoc_id)
    return {"ok": True}


@app.get("/api/bookmarks")
def list_bookmarks(
    user: User = Depends(require_user),
    offset: int = 0,
    limit: int = Query(default=20, le=100),
):
    with crawler_db.session() as conn:
        page = bookmarks.list_bookmarks(conn, user.id, offset=offset, limit=limit)
    return _page_dict(page)


@app.get("/api/search-history")
def get_search_history(user: User = Depends(require_user)):
    with crawler_db.session() as conn:
        return search_history.list_recent(conn, user.id)


@app.get("/api/tdocs/{tdoc_id}/download")
def download_tdoc(tdoc_id: str):
    """Dev-mode stand-in: proxies the local file. Once storage moves to
    MinIO, this becomes a redirect to the object's public URL instead."""
    detail = queries.get_tdoc(tdoc_id)
    if detail is None or not detail.local_zip_path:
        raise HTTPException(status_code=404, detail=f"No downloaded file for {tdoc_id}")
    return FileResponse(detail.local_zip_path, filename=f"{tdoc_id}.zip")


@app.get("/api/tdocs/{tdoc_id}/view")
def view_tdoc(tdoc_id: str):
    """Rendered PDF for in-browser preview (real formatting, not the flat
    extracted text). Renders on first view rather than requiring a bulk
    `--render` pass beforehand — LibreOffice conversion is too slow to run
    ahead of time for documents nobody may ever open; the first click on
    a given TDoc pays that cost once, every view after that is instant
    since the PDF is cached to disk and the DB row updated.

    This is the one place the API writes rather than just reads — a
    deliberate, narrow exception to the service layer being read-only,
    justified because this genuinely produces and caches a new artifact
    rather than querying existing data."""
    detail = queries.get_tdoc(tdoc_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"TDoc {tdoc_id} not found")

    if not detail.rendered_path and detail.local_zip_path:
        dest = crawler_config.RENDER_DIR / detail.tsg / detail.wg_short / detail.meeting_folder / f"{tdoc_id}.pdf"
        with _RENDER_SEMAPHORE:
            # Re-check under the semaphore: another thread may have just
            # rendered this same document while we were waiting our turn.
            with crawler_db.session() as conn:
                already = crawler_db.get_render_status(conn, tdoc_id)
            if already and already[0]:
                detail.rendered_path = already[0]
            else:
                result = render_to_pdf(Path(detail.local_zip_path), tdoc_id, dest)
                rendered_path = str(dest) if result.status == "success" else None
                with crawler_db.session() as conn:
                    crawler_db.save_render_result(
                        conn, tdoc_id,
                        rendered_path=rendered_path, render_status=result.status, render_error=result.error,
                    )
                detail.rendered_path = rendered_path

    if not detail.rendered_path:
        raise HTTPException(status_code=404, detail=f"No rendered view for {tdoc_id}")
    return FileResponse(detail.rendered_path, media_type="application/pdf")


@app.get("/api/meetings")
def list_meetings(
    tsg: Optional[str] = None,
    wg: Optional[str] = None,
    offset: int = 0,
    limit: int = Query(default=20, le=100),
):
    page = queries.list_meetings(tsg=tsg, wg_short=wg, offset=offset, limit=limit)
    return _page_dict(page)


@app.get("/api/meetings/{tsg}/{wg_short}/{meeting_folder}/tdocs")
def list_tdocs_for_meeting(
    tsg: str,
    wg_short: str,
    meeting_folder: str,
    offset: int = 0,
    limit: int = Query(default=50, le=200),
):
    page = queries.list_tdocs_for_meeting(tsg, wg_short, meeting_folder, offset=offset, limit=limit)
    return _page_dict(page)


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/search")
def search_page():
    return FileResponse(FRONTEND_DIR / "search.html")


@app.get("/account")
def account_page():
    return FileResponse(FRONTEND_DIR / "account.html")


@app.get("/bookmarks")
def bookmarks_page():
    return FileResponse(FRONTEND_DIR / "bookmarks.html")


@app.get("/admin")
def admin_page():
    # The page itself just checks /auth/me client-side to decide what to
    # show — real enforcement is server-side on the /auth/admin/* routes
    # (require_admin), so serving this file to a non-admin leaks nothing
    # beyond "this page exists".
    return FileResponse(FRONTEND_DIR / "admin.html")


# Mounted last and at "/" so it never shadows the /api/* routes above —
# Starlette matches explicit routes before falling through to a mount.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
