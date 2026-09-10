# tdocstore

Self-hosted 3GPP TDoc data layer (TDocHamster-equivalent core): crawls a working group's meeting
Docs folders on the 3GPP file server, extracts DOCX text, indexes it in SQLite/FTS5, and serves it
via MCP (stdio or HTTP), a CLI, and direct Python import. No LLM inside; verbatim evidence is
programmatic and byte-exact.

Stage 1 status: **complete and tested** (13 acceptance tests, incl. crawl against a mock 3GPP server
and the MCP server driven by the official MCP client). Live crawl of a real meeting still needs to be
run once on a machine that can reach `www.3gpp.org` — see "First live run".

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate     # Python >= 3.10, SQLite with FTS5 (standard builds have it)
pip install -e .                                  # add [docling] later for PDF/PPTX: pip install -e '.[docling]'
pytest -q                                         # 13 passed
```

## First live run (RAN2#135)

```bash
export TDOCSTORE_DATA=$HOME/tdocstore-data        # DBs in data/db/RAN2.sqlite, files in data/files/RAN2/R2-135/

tdocstore meeting add RAN2 135 --start 2026-08-24 --end 2026-08-28 --location Maastricht --country NL
tdocstore -v ingest R2-135 --limit 30             # sanity: listing format, TDoc_List headers, 30 files
tdocstore -v ingest R2-135                        # full: ~1,464 files, ~1 GB, 1–3 h at concurrency 3 (be polite)
tdocstore stats
tdocstore agenda R2-135
tdocstore search R2-135 '"AI/ML model transfer" mobility'
tdocstore text R2-2604936 | head -40
tdocstore find R2-2604936 "functionality-based LCM as the starting point"
tdocstore prev R2-2604936 --diff
```

Re-running `ingest` is idempotent: unchanged files are skipped by sha256, changed files are re-extracted.
`tdocstore sync RAN2` discovers new `TSGR2_*` meeting folders on the server and ingests every
non-upcoming meeting; restrict with `--meeting R2-135`. Dates/locations are not on the file server —
add them with `meeting add` (or later from the 3GPP meeting calendar).

If the first `--limit 30` run reports `listed` > 0 but `in_tdoclist` = 0 or many `missing`, the
listing/TDoc_List assumptions need adjusting for that server front-end: see `tdocstore/fetch.py`
(`parse_listing`) and `tdocstore/tdoclist.py` (`HEADER_ALIASES`). Both were written against the known
layout but could not be verified against the live server from the build sandbox.

Already have the zips downloaded? `tdocstore ingest R2-135 --offline-dir /path/to/Docs` (folder must
contain the `TDoc_List*.xlsx`). Nothing is written into that folder.

## Connect Claude Desktop / Claude Code (local, stdio)

`claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "tdocstore": {
      "command": "/abs/path/.venv/bin/tdocstore-mcp",
      "args": ["--transport", "stdio"],
      "env": {"TDOCSTORE_DATA": "/abs/path/tdocstore-data"}
    }
  }
}
```
Claude Code: `claude mcp add tdocstore -e TDOCSTORE_DATA=/abs/path/tdocstore-data -- /abs/path/.venv/bin/tdocstore-mcp`

## Remote (VPS + Cloudflare Tunnel) — Stage 3, but the transport already works

```bash
tdocstore-mcp --transport streamable-http --host 127.0.0.1 --port 8765     # MCP endpoint at /mcp
cloudflared tunnel ... --url http://127.0.0.1:8765                          # then https://tdocs.<domain>/mcp
```
Put Cloudflare Access (or a bearer-token proxy) in front before exposing it; the server itself has no auth yet.

## MCP tools

`service_info`, `list_meetings`, `list_tdoc_agenda_items`, `list_tdocs`, `search_tdocs`, `get_tdoc_text`,
`get_tdoc_sections`, `get_tdoc_paragraphs`, `find_verbatim`, `get_verbatim`, `get_previous_version`, `get_tdoc_diff`.
Names mirror TDocHamster where the semantics match. Every result is a JSON object with `status`; bad input
returns `invalid_argument`/`not_found`/`not_indexed`, never an exception.

## Python import (for the RAN Intelligence Tool)

```python
from tdocstore import Store
from tdocstore.config import Config
s = Store(Config(data_dir="/abs/path/tdocstore-data"))
docs = s.list_tdocs("R2-135", agenda_item="8.1.2", limit=50)
t = s.get_text("R2-2604936")                       # markdown
hit = s.find_verbatim("R2-2604936", "RAN2 to study the size and frequency")["hits"][0]
exact = s.get_verbatim("R2-2604936", hit["char_start"], hit["char_end"])["text"]   # byte-exact
```

## Data model (one SQLite per WG)

`meetings` → `tdocs` (TDoc_List metadata + fetch/extract status + `file_url`) → `tdoc_text` (markdown),
`paragraphs` (faithful body text with offsets — the verbatim source), `sections` (+ `sections_fts`, FTS5,
trigger-synced), `previous_versions` (TDoc_List `is_revision_of`, else same-source/similar-title heuristic
in the previous meeting), `analyses` (reserved for LLM output; empty in Stage 1).

## Known limitations (deliberate, documented)

- DOCX only on the fast path. `.doc`, `.pptx`, `.pdf` → `extract_status=unsupported` until the docling extra is installed.
- Tracked changes: python-docx yields accepted text (deletions dropped). CR diff marks are not preserved.
- Text boxes, footnotes, headers/footers, equations are not extracted.
- `previous_version` heuristic can mislink multi-doc sources; the response says so (`strategy`, `score`).
- Meeting dates are not discovered automatically (not on the file server).
- No auth in the HTTP transport; front it with Cloudflare Access.

## Layout

```
tdocstore/config.py     paths, GROUP_LAYOUT (FTP path per WG — verify per WG before use), meeting_ref helpers
tdocstore/db.py         schema
tdocstore/tdoclist.py   TDoc_List xlsx parser (header aliases)
tdocstore/fetch.py      throttled, resumable HTTPS crawler
tdocstore/extract.py    DOCX fast path; zip handling; docling fallback
tdocstore/textnorm.py   one normaliser for verbatim matching (NBSP, curly quotes, dashes, whitespace)
tdocstore/ingest.py     register/ingest/sync (write path)
tdocstore/store.py      the ONLY read API
tdocstore/mcp_server.py MCP adapter (stdio / streamable-http)
tdocstore/cli.py        CLI
tests/                  fixtures generator + acceptance tests
```
