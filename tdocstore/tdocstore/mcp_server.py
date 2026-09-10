"""MCP adapter over Store. Tool names mirror TDocHamster so prompts/skills transfer.

Run:
  tdocstore-mcp                                  # stdio (Claude Desktop / Claude Code local)
  tdocstore-mcp --transport streamable-http --host 127.0.0.1 --port 8765   # remote (behind Cloudflare Tunnel)

No LLM calls live here. Everything is deterministic retrieval from the index.
"""
from __future__ import annotations
import argparse
import os

from mcp.server.fastmcp import FastMCP

from .config import Config
from .store import Store

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(Config())
    return _store


mcp = FastMCP(
    "tdocstore",
    instructions=(
        "Self-hosted index of officially submitted, numbered 3GPP TDocs (e.g. R2-2604936) per working-group meeting "
        "(meeting_ref like R2-135). Flow: list_meetings -> list_tdoc_agenda_items -> list_tdocs or search_tdocs -> "
        "get_tdoc_text. For evidence quoting use find_verbatim/get_verbatim: they return byte-exact text from the "
        "DOCX body, never generated. previous_version links are heuristic unless strategy=revised_from_tdoclist. "
        "There is no LLM in this service; summarise client-side."
    ),
)


@mcp.tool()
def service_info() -> dict:
    """What this tdocstore instance holds: working groups, meetings, TDoc counts, extractor limitations. Does not search."""
    return store().service_info()


@mcp.tool()
def list_meetings(group: str = "", status: str = "", search: str = "", limit: int = 30) -> dict:
    """Find ingested 3GPP meetings and their canonical meeting_ref (R2-135, R2-133-bis). Filter by working group
    (RAN2), status (UPCOMING/ONGOING/ENDED) or free text on title/location. Only meetings present in this index."""
    rows = store().list_meetings(group or None, status or None, search or None, limit)
    return {"status": "ready", "returned": len(rows), "meetings": rows}


@mcp.tool()
def list_tdoc_agenda_items(meeting_id: str) -> dict:
    """Agenda structure of one meeting with per-agenda-item TDoc counts (from the MCC TDoc_List), in agenda order.
    Use before list_tdocs to pick an agenda_item prefix. Official numbered TDocs only, not Inbox drafts."""
    return store().list_agenda_items(meeting_id)


@mcp.tool()
def list_tdocs(meeting_id: str, agenda_item: str = "", source: str = "", limit: int = 50, offset: int = 0) -> dict:
    """List officially submitted TDocs of one meeting. agenda_item is a dotted-prefix match ('8.1' matches 8.1 and 8.1.x,
    not 8.10); source is a substring match on the source organisation(s). Paginated (limit<=50, offset). Returns
    tdoc_id, title, source, agenda_item, document_type, revision links, extract_status and the 3GPP file_url."""
    return store().list_tdocs(meeting_id, agenda_item or None, source or None, limit, offset)


@mcp.tool()
def search_tdocs(meeting: str, query: str, limit: int = 20) -> dict:
    """Full-text search inside the indexed text of one meeting's TDocs. Quoted phrases are exact ("AI/ML model transfer");
    bare words are ANDed. Returns ranked TDocs with section headings and <mark>-highlighted snippets. Then call
    get_tdoc_text or find_verbatim. Official numbered TDocs only; a meeting reference is required."""
    return store().search(meeting, query, limit)


@mcp.tool()
def get_tdoc_text(tdoc_id: str) -> dict:
    """Extracted markdown-style text of one official TDoc (headings, bold, tables) plus metadata, source_type,
    extracted_at and file_url. For byte-exact quoting use find_verbatim/get_verbatim instead. Returns not_indexed
    when the meeting was not ingested or the file could not be extracted."""
    return store().get_text(tdoc_id)


@mcp.tool()
def get_tdoc_sections(tdoc_id: str) -> dict:
    """Section list (heading, level, plain body) of one TDoc, split on heading styles or manual numbering."""
    return store().get_sections(tdoc_id)


@mcp.tool()
def get_tdoc_paragraphs(tdoc_id: str, start: int = 0, end: int = 0) -> dict:
    """Faithful paragraph-level text from the DOCX body in document order with character offsets. Table cells appear
    as paragraphs with style TableCell. Optional idx range [start, end). This is the verbatim-evidence source."""
    return store().get_paragraphs(tdoc_id, start or None, end or None)


@mcp.tool()
def find_verbatim(tdoc_id: str, needle: str, max_hits: int = 20) -> dict:
    """Locate a quoted passage in one TDoc. Matching ignores NBSP/curly-quote/dash/whitespace differences but the
    returned text and offsets are from the ORIGINAL document, so get_verbatim reproduces it exactly. 'exact' tells
    whether the needle matched without normalisation."""
    return store().find_verbatim(tdoc_id, needle, max_hits)


@mcp.tool()
def get_verbatim(tdoc_id: str, char_start: int, char_end: int) -> dict:
    """Return the exact original text for a character range (from find_verbatim or get_tdoc_paragraphs offsets)
    together with the paragraph indices it spans. Never generated."""
    return store().get_verbatim(tdoc_id, char_start, char_end)


@mcp.tool()
def get_previous_version(tdoc_id: str) -> dict:
    """Link a TDoc to its earlier version: authoritative within-meeting revision from the TDoc_List
    (strategy=revised_from_tdoclist) or a heuristic same-source/similar-title match in the previous meeting
    (strategy=prev_meeting_source_title, with score). Treat heuristic links as candidates, not facts."""
    return store().previous_version(tdoc_id)


@mcp.tool()
def get_tdoc_diff(tdoc_id: str, context: int = 2) -> dict:
    """Deterministic paragraph-level unified diff between a TDoc and its linked previous version. No narrative,
    no LLM; counts of added/removed paragraphs plus the diff text."""
    return store().diff_previous(tdoc_id, context)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tdocstore-mcp")
    ap.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    ap.add_argument("--host", default=os.environ.get("TDOCSTORE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("TDOCSTORE_PORT", "8765")))
    ap.add_argument("--data", default=None, help="data dir (overrides TDOCSTORE_DATA)")
    args = ap.parse_args(argv)
    if args.data:
        os.environ["TDOCSTORE_DATA"] = args.data
    if args.transport != "stdio":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
