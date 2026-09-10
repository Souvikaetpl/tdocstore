# tdocstore — Implementation Plan for an executing agent (Claude Sonnet / Opus)

Owner: RS. Purpose: self-hosted 3GPP TDoc data layer (TDocHamster-equivalent) exposed via MCP, REST and Python import, consumed by Claude, ChatGPT, the RAN Intelligence Tool (RIT) and future tools.

This document is the contract. Execute stages in order. Stop at every **CHECKPOINT** and report to RS before continuing. Do not skip acceptance tests. Do not "improve" the architecture without asking.

---

## 0. Decisions already made (do not reopen)

| Item | Decision |
|---|---|
| Hosting | One VPS (Linux) running everything, behind a Cloudflare Tunnel for HTTPS + access control. No Workers/D1. |
| Coverage | RAN2 first (2025–2026 meetings), other WGs backfilled later with the same code. |
| Extraction | Fast path: `python-docx` for DOCX (≈90 % of TDocs). Docling only for PDF/PPTX, optional extra. |
| Storage | SQLite, one DB per working group (`data/db/RAN2.sqlite`), FTS5 for search. Raw zips + docx kept on disk. |
| Layering | `store.py` is the ONLY read API. MCP, REST and RIT import all call it. Ingestion writes; nothing else writes. |
| LLM output | Lives only in the `analyses` table, flagged as generated. Never mixed into text/paragraph tables. Stage 4, optional. |
| Verbatim | `paragraphs` table = faithful DOCX body text, document order. Verbatim spans are sliced from it, never from markdown. |

Hard rules (from RS's other tools, apply here too):
1. Verbatim spec/TDoc evidence is extracted programmatically, never LLM-generated.
2. Every response object carries `tdoc_id`, `meeting_ref`, `source_type`, `extracted_at`, and a `file_url` back to the 3GPP server.
3. Strict 3GPP terminology in all user-facing strings (agenda item, TDoc, working group, meeting, source, CR, LS).

---

## 1. Status: Stage 1 is IMPLEMENTED and TESTED (see README.md). Start at section 3 (Stage 2).

### 1a. What the kit contains now

The zip `tdocstore-scaffold.zip` contains:

```
tdocstore/
  pyproject.toml            deps: httpx, python-docx, openpyxl, mcp ; optional: docling
  tdocstore/__init__.py
  tdocstore/config.py       Config (env TDOCSTORE_*), GROUP_LAYOUT (FTP paths per WG), meeting_ref helpers
  tdocstore/db.py           full SQLite schema (meetings, tdocs, tdoc_text, paragraphs, sections, sections_fts, previous_versions, analyses)
  tdocstore/tdoclist.py     parser for TDoc_List_Meeting_<WG>#<n>.xlsx with header aliasing
  tdocstore/fetch.py        polite async HTTPS crawler over www.3gpp.org/ftp listings (throttle, backoff, resume)
  tdocstore/extract.py      DOCX fast path (paragraphs + markdown + sections), zip handling, docling fallback stub
```

Also written and tested: `ingest.py` (online/offline/sync), `store.py`, `mcp_server.py` (stdio + streamable-http), `cli.py`, `textnorm.py`, `tests/` (13 passing). Not yet written: `rest.py` (Stage 2), Inbox tools + auth (Stage 3).

**Remaining Stage 1 item for the executing agent: the first live run against www.3gpp.org (README "First live run") and the CHECKPOINT 1 report.** Everything else in section 2 below is done; read it as the spec the code satisfies.

Read all five existing files fully before writing anything. Keep their function signatures unless a test forces a change; if you change one, say so in the checkpoint report.

---

## 2. Stage 1 — core + one meeting (R2-135) + stdio MCP

Goal: RS links a local stdio MCP server to Claude Desktop and can list, search and read RAN2#135 TDocs.

### 1.1 Environment
- Python ≥ 3.11 with SQLite ≥ 3.35 (FTS5 compiled in). Verify: `python -c "import sqlite3;sqlite3.connect(':memory:').execute('create virtual table t using fts5(a)')"`.
- `pip install -e .` from the scaffold. Do not pin versions beyond `pyproject.toml` unless something breaks.
- Confirm the 3GPP server is reachable from this machine: `curl -sI https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_135/Docs/`. If blocked, stop; everything downstream needs it.

### 1.2 Verify the two external formats before writing code against them
These are the two places assumptions can be wrong. Spend 15 minutes here; it saves hours.
- Download the Docs listing HTML for RAN2#135 and confirm `fetch.parse_listing()` returns `R2-2604501.zip … ` plus the `TDoc_List_Meeting_RAN2#135.xlsx` file name (exact name may differ: match `TDoc_List*.xlsx`, case-insensitive). Fix the parser if the listing markup differs.
- Download that xlsx and run `tdoclist.parse_tdoc_list()` on it. Print the first 3 records and the header map. Confirm `tdoc_id`, `title`, `source`, `agenda_item`, `document_type`, `is_revision_of`, `revised_to`, `tdoc_status` are populated. Add header aliases if any are None.
- Record findings in `NOTES.md` (listing format, xlsx header names, meeting folder name pattern). Future WGs will need this.

### 1.3 `ingest.py`
Functions (all synchronous wrappers over async fetch are fine):
- `register_meeting(con, group, number, *, title=None, dates=None, location=None)` → inserts/updates `meetings`; `docs_url` from `config.GROUP_LAYOUT` + `meeting_folder()`; `status` derived from dates if known, else `UNKNOWN`.
- `ingest_meeting(cfg, group, number, *, limit=None, only_agenda=None, offline_dir=None)`:
  1. list Docs folder (or read `offline_dir` when given — needed for tests and for RS's existing downloads);
  2. fetch + parse the TDoc_List xlsx → upsert `tdocs` rows (`fetch_status='pending'`);
  3. build file names: `<tdoc_id>.zip` (verify against listing; some are `.docx` directly, a few are missing → `fetch_status='missing'`);
  4. fetch files into `data/files/<GROUP>/<meeting_ref>/`, honoring `limit`/`only_agenda` filters for partial runs;
  5. for each fetched file: `extract.extract_any()` → write `tdoc_text`, `paragraphs` (with `char_start/char_end` computed over `'\n'.join(texts)`), `sections`, then `db.delete_tdoc_sections()` before re-insert on re-runs, then `db.reindex_fts()`; set `extract_status`, `source_type`, `extracted_at`, `text_chars`, `error`;
  6. update `meetings.doc_count`, `ingested_at`.
- Idempotent: re-running must not duplicate rows or FTS entries. Test this explicitly.
- Log one line per TDoc: `R2-2604936 fetched 812KB docx-fast 41 paragraphs 7 sections`.
- Text cap: if markdown > `cfg.max_text_chars`, truncate and set `tdoc_text.truncated=1`; paragraphs are never truncated.

### 1.4 `store.py` — the read API
Class `Store(cfg)` opening one DB per group lazily. Every method returns plain dicts/lists (JSON-serialisable), never sqlite rows.

| Method | Behaviour |
|---|---|
| `list_meetings(group=None, status=None, search=None, limit=30)` | from `meetings`, newest first |
| `list_tdocs(meeting_ref, agenda_item=None, source=None, limit=50, offset=0)` | `agenda_item` is a prefix match (`'8.1'` matches `8.1.2`); returns `total, returned, offset, has_more, tdocs[]` |
| `list_agenda_items(meeting_ref)` | `agenda_item, description, tdoc_count`, sorted by numeric tuple not string (`8.10` after `8.9`) |
| `search(meeting_ref, query, limit=20)` | FTS5 over `sections_fts` restricted by `meeting_ref`; returns ranked `tdoc_id, title, source, agenda_item, hits[] {heading, snippet}`; group multiple section hits per TDoc; `bm25` ranking |
| `get_text(tdoc_id)` | metadata + markdown + `truncated` |
| `get_sections(tdoc_id)` | `[ {idx, heading, level, body} ]` |
| `get_paragraphs(tdoc_id, start=None, end=None)` | faithful paragraphs with offsets |
| `get_verbatim(tdoc_id, char_start, char_end)` | exact slice from the paragraph concatenation, plus the paragraph indices it spans |
| `find_verbatim(tdoc_id, needle)` | exact substring search over paragraph text; returns all `(char_start, char_end, paragraph_idx)` — this is what RIT's `patch_verbatim.py` will call |
| `get_tdoc(tdoc_id)` | metadata row only |
| `previous_version(tdoc_id)` | Stage 2; return `status='not_computed'` in Stage 1 |
| `service_info()` | groups loaded, meetings per group, doc counts, schema version, extractor versions |

FTS query builder (`store._fts_query`): split user text into quoted phrases and bare tokens; quote every token (`"AI/ML"`), join with implicit AND; strip FTS operators from bare tokens so user input can never produce a syntax error; on `sqlite3.OperationalError` return `status='invalid_query'` with the message, never raise. Test with: `"AI/ML model" LCM`, `R2-2604936`, `model-based`, `(`, `NEAR`, empty string.

Snippets: use `snippet(sections_fts, 1, '<mark>', '</mark>', '…', 24)` on `body` and return heading separately. Return plain text with `<mark>` only — no other HTML.

### 1.5 `mcp_server.py`
Use `mcp.server.fastmcp.FastMCP`. Tool set mirrors TDocHamster names so existing prompts/skills transfer:

`service_info`, `list_meetings`, `list_tdoc_agenda_items`, `list_tdocs`, `search_tdocs`, `get_tdoc_text`, `get_tdoc_sections`, `get_tdoc_paragraphs`, `find_verbatim`, `get_verbatim`, `get_previous_version` (Stage 2), `browse_inbox` + `get_inbox_file_text` (Stage 3).

Rules:
- Every tool docstring says what it does NOT do (Hamster's docstrings are a good model: "official numbered TDocs, not Inbox drafts").
- Tool results are dicts; include `file_url` for every TDoc.
- Cap `limit` at 50; cap `get_tdoc_text` output at `max_text_chars`.
- Transport selectable: `tdocstore-mcp --transport stdio` (default) and `--transport streamable-http --host 127.0.0.1 --port 8765` (Stage 2 wires auth in front of this).
- No LLM calls anywhere in this file.

### 1.6 `cli.py`
```
tdocstore meeting add RAN2 135 [--title ... --start ... --end ... --location ...]
tdocstore ingest RAN2 135 [--limit N] [--agenda 8.1] [--offline-dir PATH] [--no-docling]
tdocstore search R2-135 '"AI/ML model" LCM'
tdocstore text R2-2604936
tdocstore stats
```

### 1.7 Tests (`tests/`, pytest)
- `test_tdoclist.py`: parse a real downloaded xlsx (commit a 20-row trimmed copy as fixture) and a synthetic one with shuffled/renamed headers.
- `test_extract.py`: build DOCX fixtures with `python-docx` covering: Heading 1/2 styles, manual-numbered headings, bold runs mid-paragraph, a table with merged cells, a list paragraph, a zip containing one docx, a zip containing two docx. Assert: paragraph order, section split, markdown headings, `find_verbatim` round-trip (slice equals needle byte-for-byte).
- `test_ingest_offline.py`: offline dir with 5 synthetic TDocs + synthetic xlsx → ingest twice → row counts unchanged, FTS count unchanged, search finds expected doc, `has_more`/`offset` paging correct.
- `test_mcp.py`: spawn the stdio server with the `mcp` client library, call `list_tdocs` and `search_tdocs`, assert schema of results.

### 1.8 Live run
- `tdocstore meeting add RAN2 135 --start 2026-08-24 --end 2026-08-28 --location Maastricht`
- `tdocstore ingest RAN2 135 --limit 50` first (sanity), then full (~1,464 TDocs, ≈1 GB, expect 1–3 h at concurrency 3).
- Report: fetched/missing/error counts, extract_status histogram, `source_type` histogram, 5 slowest files, any `warnings`.
- Spot-check 5 TDocs against the 3GPP server by opening the docx and comparing a paragraph with `get_verbatim`.

### **CHECKPOINT 1** — deliver to RS
1. Claude Desktop config snippet:
```json
{"mcpServers": {"tdocstore": {"command": "tdocstore-mcp", "args": ["--transport", "stdio"], "env": {"TDOCSTORE_DATA": "/abs/path/data"}}}}
```
2. The live-run report above.
3. NOTES.md with the verified external formats.
4. Any deviation from this plan, with reason.
Wait for RS to confirm search/text quality before Stage 2.

---

## 3. Stage 2 — verbatim hardening, previous-version, HTTP + REST, RIT import

### 2.1 Previous-version matcher (`linker.py`)
Strategy order, first hit wins, all recorded in `previous_versions.strategy`:
1. `revised_from_tdoclist`: within-meeting `is_revision_of` from the TDoc_List (authoritative).
2. `prev_meeting_source_title`: previous meeting of the same group (by `start_date`), same normalised `source` (lowercase, strip punctuation, split on `,` and take the first org), title similarity via `difflib.SequenceMatcher` ≥ 0.6, tie-break by same `agenda_item` then highest ratio. Store `score`.
3. none → `previous_tdoc_id=NULL, strategy='none'`.
Expose `Store.previous_version(tdoc_id)` returning `status`, `previous_tdoc_id`, `strategy`, `score`. Label the heuristic as heuristic in the MCP docstring.
Optional: `Store.diff_paragraphs(tdoc_id)` → `difflib.unified_diff` over paragraph text of current vs previous. Deterministic, no LLM. This alone covers most of what Hamster's "diff summary" gives.

### 2.2 REST adapter (`rest.py`, FastAPI or Starlette)
Same methods as `store.py`, one route each, plus batch endpoints RIT needs:
- `GET /meetings/{ref}/tdocs?agenda_item=8.1.2&full=true` → all TDocs for an agenda item **with** markdown + paragraphs in one response (RIT unit-analyst input). No 50 cap here; cap by bytes (e.g. 20 MB) with `next` cursor.
- `POST /verbatim/find` body `{tdoc_id, needles[]}` → batch `find_verbatim`.
Mount the MCP streamable-HTTP app under `/mcp` on the same process so there is one port.

### 2.3 RIT integration
- RIT scripts import `tdocstore.Store` directly when on the same machine; otherwise use REST. Replace `tdoc_downloader.py`'s download step with `tdocstore ingest`, keep its downstream unchanged.
- `patch_verbatim.py` calls `find_verbatim` for resolution. Add a test that a needle with NBSP/curly quotes in the TDoc still resolves (normalise both sides with the same function; store the normalisation function in `tdocstore/textnorm.py` and use it everywhere).

### **CHECKPOINT 2** — previous-version accuracy sample (30 random R2-135 TDocs, manual check), REST running locally, RIT reading one agenda item end-to-end.

---

## 4. Stage 3 — Inbox, VPS deployment, auth, ChatGPT

### 3.1 Inbox
- `browse_inbox(working_group, path='Inbox', recursive=False, max_depth=1)` over the live `.../Inbox/` folder via `fetch.list_dir()`; newest-first uses the listing's date column if present (verify in NOTES.md), else `HEAD` Last-Modified per file (slow; cap at 100 files).
- `get_inbox_file_text(working_group, file_path)`: download to `data/inbox_cache/<sha of path+mtime>`, extract with `extract_any`, cache 1 h. 25 MiB cap.

### 3.2 VPS
- Ubuntu LTS, 2 vCPU / 4 GB / 100 GB disk is enough for RAN2 2025–2026 (≈15 meetings × ~1 GB files + ~1 GB DB). All WGs: plan 300–500 GB.
- systemd unit for `tdocstore serve --host 127.0.0.1 --port 8765` (REST + MCP-HTTP).
- systemd timer: `tdocstore sync RAN2` daily; it discovers new meeting folders in the WG FTP dir (`TSGR2_*`), registers unknown ones, ingests those with `status != ENDED-and-complete`. Re-ingest the current meeting daily during meeting weeks (revisions arrive continuously).
- Cloudflare Tunnel (`cloudflared`) → `tdocs.<your-domain>`; Cloudflare Access policy or a bearer-token middleware in `rest.py` with per-consumer keys in `data/keys.json` (`name, key_hash, scopes, revoked`). Revocation = flip flag, no restart.

### 3.3 Client wiring
- Claude Desktop / Claude Code: remote MCP URL `https://tdocs.<domain>/mcp` with bearer header.
- ChatGPT: custom connector to the same URL. **Verify current OpenAI requirements first** (remote MCP over HTTPS; auth mode; whether `search`/`fetch` tool names are mandatory for its deep-research mode). If required, add thin alias tools `search` and `fetch` mapping to `search_tdocs` and `get_tdoc_text`.

### **CHECKPOINT 3** — RS connects Claude and ChatGPT to the hosted endpoint; key revocation demonstrated.

---

## 5. Stage 4 (optional) — LLM enrichment
Only after RS asks. Summary/proposals/diff narratives generated by a local model (Ollama/vLLM) or API, written to `analyses` with `model`, `provider`, `generated_at`. Extract "Proposal N:" / "Observation N:" lines programmatically first (regex over paragraphs) — that covers most of the value without any LLM and is exact.

---

## 6. Backfill other WGs
Same code. Per WG: verify `GROUP_LAYOUT` FTP path and folder pattern against the live server (RAN1 uses `TSGR1_122b` style for bis — confirm), verify TDoc_List headers, then `tdocstore sync <WG> --from 2025-01`. Run one WG at a time; the 3GPP server is shared infrastructure.

---

## 7. Known traps (read before Stage 1)
- 3GPP directory listing pages: two hosts, slightly different HTML; parse `href`s only.
- Meeting folders: `TSGR2_135`, `TSGR2_133bis`, ad-hoc/e-meetings vary (`TSGR2_AHs`, `TSGR2_129-e`). Treat folder discovery as data, not code.
- TDoc_List xlsx: header names drift; first row may be a title, not headers; `agenda_item` may be a float cell (`8.1` → keep string, never `8.10` ↔ `8.1` confusion).
- Some TDocs are `.docx`/`.doc`/`.pptx` uploaded without zip; `.doc` (binary) needs LibreOffice or Docling — mark unsupported in Stage 1, count them.
- Contentless FTS5: you cannot delete rows without supplying the original values; `db.delete_tdoc_sections()` does it correctly — use it.
- python-docx returns accepted text for tracked changes; CRs with `w:del` lose deleted text. Acceptable for now; note it in `source_type` as `docx-fast` and in `service_info`.
- Merged table cells repeat the same `_tc`; the extractor de-dups by `id(cell._tc)`.
- NBSP (`\xa0`), curly quotes, soft hyphens break verbatim matching; one shared normaliser, applied identically at index time and query time.
- Never raise from a `store.py` method on bad user input; return `{status: 'invalid_argument', message}`.

---

## 8. Paste-ready kickoff prompt for the executing agent

```
You are implementing `tdocstore`, a self-hosted 3GPP TDoc data layer. The contract is IMPLEMENTATION_PLAN.md in the repo root; read it fully first, then read every file in tdocstore/ (the starting kit). Execute Stage 1 only. Stop at CHECKPOINT 1 and report with the four items listed there. Do not reopen decisions in section 0. Do not add LLM calls anywhere. Verify the two external formats (section 1.2) against the live 3GPP server before writing ingest.py, and record findings in NOTES.md. All store.py methods return JSON-serialisable dicts and never raise on bad input. Write the tests in 1.7 and make them pass. Use strict 3GPP terminology in all strings. When something in the plan is impossible or wrong, say so explicitly with evidence rather than working around it silently.
```
