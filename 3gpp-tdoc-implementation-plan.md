# 3GPP TDoc Replica — Implementation Plan (v7)
### Plain PostgreSQL + local disk → MinIO, verified against the live 3GPP site, MCP-ready

---

## 0. What changed in this version

v4 proposed self-hosted Supabase (Postgres + Storage + Kong + `pg_cron`/`pgmq`) and listed a Phase 0 verification list as mostly *unverified*, built from a sandbox with no access to the live 3GPP site. Since v4 was written, the crawler has actually been built and run against the real site — real HTTP requests, real downloads, real text extraction, real Postgres storage. Several of v4's "confirmed" facts turned out to be **wrong**, not just unverified (see §2). v5 replaces those assumptions with what was actually observed, and replaces the backend architecture with the simpler stack that's already running and already has real data in it.

**What v5 did differently:**
1. Drops self-hosted Supabase as the backend platform. Plain Postgres (already installed and in use) + local filesystem now, MinIO later. See §3 for why.
2. Corrects the live-site facts in §2 with what was actually verified — folder structure, `robots.txt`, TDoc list Excel schema, zip contents — replacing several incorrect assumptions v4 carried from an unverified build.
3. Documents the crawler that already exists and has been run end-to-end (§4), instead of describing one still to be built.
4. Keeps the parts of v4 that were genuinely good ideas independent of the backend choice: the two-pane UI target, MCP security hardening (auth token, per-tool limits, read-only DB role), and the general phase discipline (verify → backfill → shared service layer → MCP → API → frontend).
5. Notes what's reusable from the separately-built `tdocstore` package and what isn't (§11) — its text-normalization logic is sound and worth adopting; its crawler/parser logic was never run against the live site and encodes at least one confirmed-wrong assumption.
6. Adds a universal cover-page metadata fallback (§8) after discovering the Excel TDoc list is RAN2-specific, not RAN-wide or 3GPP-wide as originally assumed.

**What v6 added** (two new, deliberately-scoped requirements from the project owner — not a re-verification pass):
1. **Document viewer: PDF.js over the existing LibreOffice render pipeline** (§14, new). The render pipeline itself already exists and runs (LibreOffice headless, `crawler/render.py`) — this section documents it for the first time (a gap in v5) and specifies the one thing still missing: swapping the frontend's plain `<iframe>` preview for an explicit Mozilla PDF.js viewer.
2. **Authentication & user accounts** (§12, new). v4/v5 explicitly scoped this project as having **no login** (§3, "Auth — not needed... this project has no login"). That decision is now reversed at the project owner's request. §12 captures the intent and the concrete decisions still needed before this is buildable rather than aspirational — this is the one part of v6 that is a genuine open question, not a settled plan.

**What v7 does** (a catch-up pass, not new scope): v6 flagged that §4/§10 still described the crawler as of v5 and lagged what was actually running. v7 closes that gap — it's a status update, marking phases §10 called "remaining" as done where they now are, and documenting several things that were built in the meantime but never written up here:
1. §4 now documents `meeting_info.py` (real meeting dates/locations, parsed from four distinct invitation-letter formats), `dynareport.py` (a second, independent data source used for calendar-only entries and as a date fallback), and the meeting-selection rebuild (real-date-based, replacing the old "most-recently-modified folder" heuristic that was confirmed wrong for several groups) — plus a Postgres schema-migration deadlock this rebuild's testing surfaced and fixed in `db.py`.
2. §10 items 2, 3, and 5 (shared service layer, FastAPI backend, frontend) are marked done, with the actual files/routes/pages that satisfy them. Item 1 (widen the crawl) is marked substantially done — all 18 RAN/SA/CT groups now tracked, not just the original RAN1–5/SA2/CT1 sample — with full historical backfill still explicitly open. Item 4 (MCP server) is marked built-and-verified for local use, with the original hardening requirements (auth token, result limits, rate limiting, read-only DB role) confirmed still genuinely not done, not just unmentioned.
3. **§13, new**: MCP production hardening & deployment — the project owner has confirmed this is going to production, not staying local-only, which makes both the `stdio`→`streamable-http` transport switch and all four hardening items from §10 item 4 real, near-term work rather than a someday-maybe.
4. **§12's four open questions are now answered**, not just posed: personalization (not just access control), Google sign-in first (deliberately designed so a second method can be added later without rework), MCP tokens tied to the same login, and open public signup. §12 is rewritten accordingly — from "intent, not yet a spec" to an actual concrete requirements list.

**Since v7**: §12 gains a 5th item — admin-issued test accounts alongside Google sign-in (`is_admin` flag, `status` flag, and a still-open choice between a password or a one-time login link for how a test account signs in without Google), plus the explicit requirement that account status is re-checked on every authenticated request, not only at login — otherwise disabling an account wouldn't actually cut off an already-logged-in session.

---

## 1. Architecture overview

```
3GPP public FTP (www.3gpp.org/ftp) — verified structure, see §2
        │
        ▼
  Python crawler (crawler/)             already built, already run live
  - discover meetings                   RAN2#135: 1,452 TDocs ingested,
  - fetch TDoc list Excel               19 zips downloaded + text-extracted,
  - download zips, extract text         all verified against real Postgres rows
        │
        ▼
  PostgreSQL (native install, already running: "tdoc_replica")
    - meetings, tdocs (see §5 for real schema)
    - Postgres full-text search (tsvector/GIN) — no extra service needed
        │
        ▼
  Local filesystem (dev) → MinIO (prod, when actually needed)
    - raw zips, extracted text files — referenced by path/URL in Postgres,
      never stored as blobs in the database
        │           │
        ▼           ▼
   MCP server   FastAPI web backend        ← both phases still ahead, §10
   (protected)  (reads Postgres + files)
                     │
                     ▼
               Web frontend (two-pane UI, unchanged target from v4)
```

No Kong, no GoTrue, no Supabase Studio, no containerized second Postgres. One database, one file store, one crawler. Job scheduling (`pg_cron`/`pgmq`) is deferred to whenever real scheduled/multi-group ingestion volume justifies it — and when it's needed, it attaches to the Postgres instance that already exists, not to a separate platform.

---

## 2. Live-verified facts (corrects v4's Phase 0 section)

v4 listed these as "still to verify" or "confirmed" from a sandbox with no site access. Here's what's actually true, checked directly against `https://www.3gpp.org/ftp/`:

| v4 claimed | Actually verified |
|---|---|
| `robots.txt` "fetch attempts have failed so far" | Fetched successfully. `User-agent: *` has **no** disallow on `/ftp/`. Only named AI-crawler bots (`GPTBot`, `Google-Extended`, `ClaudeBot`, `CCBot`, `PerplexityBot`, `Amazonbot`) are blocked site-wide. A generic, honestly-identifying custom User-Agent gets `200 OK`; the block is bot-name-based, not behavior-based. |
| TDoc list filename: `TDoc_List_Meeting_<WG>#<n>.xlsx` in the meeting's `Docs/` folder | **Wrong.** The real file lives in a separate `Tdoclists/` folder, as multiple timestamped snapshots per meeting (`tdocList_2022-11-21_20h42_eom.xlsx`), updated throughout the week. The authoritative one is the `_eom` (end-of-meeting) suffixed file when present, else the most recently modified snapshot. `Docs/` only contains the TDoc zips. |
| "Real spreadsheet column headers (never actually opened yet)" | Opened and parsed. 31 real columns on sheet `TDoc_List_r1`, including `CR`, `CR revision`, `CR category`, `Rel`, `Specification`, `Related WIs`, `Is revision of`/`Revised to` (a full revision chain), and LS routing fields (`LS_To`/`LS_Cc`/`Reply in`). This is much richer than expected — CR/spec metadata comes structured from Excel, not from parsing document bodies. |
| "Whether a TDoc zip contains one file or several" | Usually one primary document (`.docx` in every case observed so far), named as the TDoc ID with an optional suffix (`R2-2604504_C1-262609.docx`). Some zips bundle supplementary files (`PartList_...xlsx`, a re-exported TDoc list) that must be filtered out by matching the TDoc ID as a filename prefix, not by picking the first or largest file. |
| Working-group root paths / meeting folder naming "vary" (general statement) | Specific and confirmed: `tsg_ran/WG2_RL2`, `tsg_sa/WG2_Arch`, `tsg_ct/WG4_protocollars_ex-CN4`, etc. (full map in `crawler/config.py`). Meeting folders have no embedded year (`TSGR2_120`) and irregular suffixes (`TSGR2_109_e` for an electronic meeting, `TSGR2_119bis`) — date must come from the directory listing's modified-time column, not the folder name. |
| Directory listing format | Plain ASP.NET listing. Files are `<a class="file" href="...">`; directories are `<a href="...">` with no `class` attribute — a reliable, tested discriminator (`crawler/listing.py`). |

**Volume, for scoping**: one RAN2 meeting (#135) = 1,452 TDocs, ~267MB of zips. Across years/groups this reaches the tens of thousands of rows / tens of GB — trivial for Postgres and for local/MinIO storage, no special scaling design needed yet.

**One more correction, found while widening the crawl beyond RAN2**: `Tdoclists/*.xlsx` is not a RAN-wide convention, let alone a 3GPP-wide one — it's specific to RAN2. Checked directly: RAN1, RAN3, RAN4, and RAN5 all have the same bare structure as CT groups (`Docs/`, `Agenda/`, `Report/`, `Invitation/`, `LS`/`LSin`/`LSout`, no Excel at all). See §8 for how this is actually handled.

---

## 3. Why not Supabase (and why plain Postgres + MinIO instead)

Self-hosted Supabase bundles four things on top of Postgres: **Auth (GoTrue)**, **Storage API**, **Kong gateway**, **Studio**. Going through each against this project's actual, current scope:

- **Auth** — not needed *at the time this was written*. This project had no login (confirmed as an explicit target in v4 itself, §"no login"), and Auth/GoTrue is Supabase's single biggest differentiator over plain Postgres — it solved a problem this project didn't have. **(v6 update: this project now does have a login requirement — see §12.)** That still doesn't retroactively make Supabase/GoTrue the right call: §12 evaluates auth approaches on their own merits rather than reopening the Supabase-vs-Postgres question, since nothing else about the Storage/Kong/Studio reasoning above has changed.
- **Storage API** — a wrapper around S3-compatible storage. Already decided independently: MinIO directly for object storage (raw zips, extracted text), local filesystem in dev. Same functional outcome as Supabase Storage, without an extra Kong hop in front of it.
- **Kong gateway** — routes requests to Storage/PostgREST. Not needed: FastAPI is the planned API layer, not PostgREST, so there's nothing for Kong to front.
- **Studio** — admin UI for Postgres. Already covered by pgAdmin, already in use, already showing correct data (verified meeting/TDoc counts, doc-type breakdowns, extraction status all cross-checked against the crawler's own output).
- **`pg_cron` / `pgmq`** — the one genuinely good idea in v4. Both are **plain Postgres extensions**, not exclusive to Supabase. They can be added directly to the Postgres instance this project already runs, whenever real scheduled/queued ingestion volume justifies it. Adopting all of Supabase to get these two extensions is adopting three unused services to get one used feature.

**The concrete cost of adopting Supabase now**: it runs its own containerized Postgres. Since a native Postgres install (`tdoc_replica`) is already running with real crawled data in it (1,452+ TDocs, verified in pgAdmin), moving to Supabase means migrating working infrastructure sideways for capabilities (Auth, PostgREST, Kong, Realtime) that aren't part of this project's scope — added operational surface (more containers to patch, monitor, and back up) with no functional gain today.

**When Supabase (or an equivalent bundled platform) would become the right call**: only if the project later adds real user accounts/Auth, or wants an auto-generated REST layer (PostgREST) instead of hand-written FastAPI, or wants built-in Realtime subscriptions. None of those are in scope. That's a future deployment decision, not a build decision now.

**Conclusion**: Postgres (native, already running) + local filesystem now → MinIO later, with `pg_cron`/`pgmq` added directly to that Postgres only once scheduled ingestion is actually needed. Fewer containers, no migration of already-working infrastructure, no unused services to operate.

---

## 4. What's already built and verified (not a plan anymore — this exists and runs)

`crawler/` (Python), tested end-to-end against the live site:

- **`config.py`** — verified FTP path map for all RAN/SA/CT plenary + working groups; honest self-identifying User-Agent (not a spoofed browser, not an impersonated bot name); conservative request rate limiting.
- **`http_client.py`** — rate-limited, retrying HTTP client.
- **`listing.py`** — parses 3GPP's directory listing HTML into file/directory entries with real modified-time and size.
- **`tdoclist.py`** — parses the real 31-column TDoc list Excel schema (RAN2 only — see §8); picks the authoritative `_eom`/latest snapshot from `Tdoclists/`.
- **`discover.py`** — lists meetings for a group, lists a meeting's `Docs/`/`Tdoclists/` contents.
- **`extract.py`** — extracts text from `.docx` (including tables, with merged-cell duplication deduped), `.pdf`, `.pptx`, `.xlsx`, `.txt`; picks the correct main document inside a zip by TDoc-ID prefix matching, filtering out bundled supplementary files.
- **`coverpage.py`** — fallback metadata parser for groups with no TDoc list export: recovers title/source/release/work-item/CR fields directly from a document's own cover page. See §8.
- **`meeting_info.py`** *(new since v5)* — parses a meeting's real start/end date and location from its Invitation document. Four distinct invitation-letter formats confirmed in practice, tried most-specific-first: a structured per-meeting table (ETSI-hosted joint weeks), a per-TSG "`TITLE DATE`" line with no table at all (ATIS-hosted TSG-plenary weeks), a host-organized three-line header block ("Invitation to the 3GPP `<TITLE>` Meeting" / date / location — seen from a non-ETSI/non-ATIS host), and a shared prose paragraph naming every WG meeting that week with one common date/venue and no per-group breakdown (ATIS-hosted WG weeks) — the last resort, since it can't pin a date to one specific group any more precisely than "this group is listed as meeting that week." Also handles a zip that bundles an unrelated document alongside the real invitation (observed: a visa/immigration info letter) by trying every candidate document inside, not just the first.
- **`dynareport.py`** *(new since v5)* — a second, independent data source: 3GPP's own meeting-calendar pages (`dynareport?code=Meetings-<code>.htm`), separate from the FTP document archive everything else in this project is built on. Used for exactly two things the FTP-based approach can never cover on its own: (1) calendar-only entries with no FTP folder at all (e.g. a recurring cross-vendor "TTCN Workshop" — real, dated, but no Invitation/Docs directory to crawl), narrowly filtered to genuine workshop-style entries rather than the far more common ad-hoc SWG calls / conference calls / social events / hotel bookings the same calendar pages also list (confirmed these exist and would otherwise be wrongly treated as real meetings); (2) a fallback date for a real numbered meeting that has actual downloaded documents but never got its own Invitation letter published. Explicitly skips any calendar row whose title says "CANCELLED" — confirmed necessary: a competing tool was found showing a cancelled meeting as a real upcoming one.
- **Meeting selection, rebuilt** (`ingest.py`) — the original approach (take the N most-recently-modified meeting folders per group) was confirmed wrong in practice, not just theoretically risky: a numerically-later placeholder folder is sometimes touched more recently than the meeting that's actually next, which silently tracked the wrong "current" meeting for several groups (RAN1, SA3, SA4, SA5, SA6 all confirmed affected). Replaced with a two-phase approach — build a wider candidate pool by meeting number, cheaply date-check each candidate (`meeting_info.py`, falling back to `dynareport.py`), then select the actual next/previous meeting by real date. The homepage's "Upcoming"/"Previous" meeting cards also carry a live Ongoing/Upcoming/Past status badge computed from these same real dates against the current date.
- **`render.py`** *(new since v5)* — renders a TDoc's main document to PDF via LibreOffice headless, for the web preview (see §14). Two concurrency/stability fixes made after concrete failures: each conversion gets its own isolated LibreOffice profile directory (the shared default profile locks and collides under concurrent conversions), and the timeout is capped at 40 seconds rather than left unbounded (a small number of documents — root-caused to a Table-of-Contents field filtered by a custom paragraph style — make LibreOffice's layout engine hang rather than run slow; an unbounded timeout on those was enough to peg a CPU core long enough to destabilize the host machine).
- **`db.py`** — Postgres schema (`meetings`, `tdocs`) and upsert logic, migrated from an initial SQLite prototype once the crawl/extract logic was proven. Also: change-detection (`get_tdoc_file_state`) and cover-page backfill (`backfill_metadata_from_coverpage`, gap-filling only, never overwrites real data). **Also fixed** *(new since v5)*: schema migrations (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) used to re-run on every single connection — harmless in isolation, but each one takes a table-level exclusive lock even when it's a no-op, which genuinely deadlocked a long-running crawl/render job against nothing more than a concurrent read query. Migrations now run at most once per process, and skip any column that already exists (a plain `information_schema` read, no lock) rather than ever taking that lock in the already-migrated common case.
- **`ingest.py`** — orchestrates discovery → metadata merge → download → extraction → cover-page backfill, idempotent (skips already-downloaded files unless the server-side file changed, tracks per-TDoc extraction status).
- **`main.py`** — CLI: `python main.py --wg ran:RAN2 --meetings-per-wg N [--download --max-files N] [--extract] [--refresh]`.

**Beyond `crawler/`, also built and verified since v5** (previously undocumented here — see §10 for which "remaining phase" each satisfies):
- **`service/queries.py` + `service/models.py`** — the shared service layer: `search_tdocs`, `get_tdoc`, `compare_tdocs`, `list_meetings`, `get_stats`, over plain SQL against the schema in §5. The one place TDoc/meeting query logic lives.
- **`api/main.py`** — FastAPI backend calling directly into the service layer: `/api/tdocs`, `/api/tdocs/{id}`, `/api/tdocs/compare`, `/api/meetings`, `/api/meetings/{tsg}/{wg}/{folder}/tdocs`, `/api/stats`, `/api/tdocs/{id}/download`, `/api/tdocs/{id}/view` (rendered-PDF preview), and serves the frontend itself (`/`, `/search`).
- **`mcp_server.py`** — MCP server (official `mcp` SDK) exposing the same five service-layer functions as tools, over stdio. Verified working through the real MCP protocol (a scripted client session: `initialize` → `list_tools` → `call_tool`, including the not-found case), then confirmed live through both Claude Code and Antigravity IDE via a project-level `.mcp.json`. Hardening from §10 item 4 (auth token, per-tool result limits, rate limiting, read-only DB role) is correctly not yet built — this is local single-user use only so far.
- **`frontend/index.html` + `home.js`** — the homepage: TSG tabs, per-WG rows showing the real current/previous meeting (from the rebuilt selection logic above) with the live status badge, real corpus stats. **`frontend/search.html` + `app.js`** — the two-pane search/filter/preview/compare UI.

**Proven results**: RAN2#135 fully ingested — 1,452 TDocs with real metadata (196 CRs, LS in/out with routing, discussion papers, agenda items), 19 zips downloaded and text-extracted (19/19 success), all cross-verified directly in pgAdmin against the crawler's own reported counts. Since then, widened to all 18 RAN/SA/CT groups (§10 item 1) with tens of thousands of TDocs tracked.

---

## 5. Data model (as actually implemented in `crawler/db.py`)

```sql
CREATE TABLE meetings (
    id              SERIAL PRIMARY KEY,
    tsg             TEXT NOT NULL,
    wg_short        TEXT NOT NULL,
    wg_path         TEXT NOT NULL,
    meeting_folder  TEXT NOT NULL,
    meeting_url     TEXT NOT NULL,
    modified_at     TEXT,
    first_seen_at   TIMESTAMPTZ DEFAULT now(),
    last_synced_at  TIMESTAMPTZ,
    UNIQUE(tsg, wg_short, meeting_folder)
);

CREATE TABLE tdocs (
    tdoc_id                 TEXT PRIMARY KEY,
    meeting_id              INTEGER NOT NULL REFERENCES meetings(id),
    tsg TEXT NOT NULL, wg_short TEXT NOT NULL,
    title TEXT, source TEXT, contact TEXT, contact_id TEXT,
    doc_type TEXT, for_action TEXT, abstract TEXT, secretary_remarks TEXT,
    agenda_item TEXT, agenda_item_description TEXT,
    status TEXT, reservation_date TEXT, uploaded_at TEXT,
    is_revision_of TEXT, revised_to TEXT,               -- revision chain, for the future Diff feature
    release TEXT, specification TEXT, spec_version TEXT, related_wis TEXT,
    cr_number TEXT, cr_revision TEXT, cr_category TEXT, tsg_cr_pack TEXT,
    reply_to TEXT, ls_to TEXT, ls_cc TEXT, original_ls TEXT, reply_in TEXT,
    file_url TEXT, file_size_bytes BIGINT, file_modified_at TEXT,
    local_zip_path TEXT,                                 -- path reference only; file lives on disk/MinIO
    text_path TEXT, extraction_status TEXT, extraction_error TEXT, extracted_source_file TEXT,
    ingested_at TIMESTAMPTZ DEFAULT now()
);
```

Files (zips, extracted text) are never stored as database blobs — only paths/URLs are, per §3's storage split. Full-text search uses Postgres's built-in `to_tsvector`/`tsquery` (no separate index to keep in sync, no extra service):

```sql
SELECT tdoc_id, title, source
FROM tdocs
WHERE to_tsvector('english', title) @@ to_tsquery('english', 'handover & interruption');
```

---

## 6. Daily refresh and update handling (new in v5 — this exists and runs)

A gap in earlier versions: nothing specified how new or changed documents actually get picked up on an ongoing basis. `pg_cron`/`pgmq` (§3, deferred) solves *scheduling reliability at scale*, not this — a plain periodic re-run of the existing crawler solves it now, at zero extra infrastructure cost, because the crawler is already idempotent.

**New documents / new meetings**: `python main.py --refresh` re-syncs every group already tracked in the database (queried via `db.known_groups()` — no separate scope list to maintain), picks up newly created meeting folders and newly uploaded TDocs, and runs extraction on anything new. Capped at 200 downloads per meeting by default so a refresh can't silently turn into a full backfill on a group that isn't fully backfilled yet — pass `--refresh-max-files` to override when an intentional backfill is wanted.

**Updated documents — the harder case, now handled**: a TDoc's zip can be replaced in place on 3GPP's server under the same filename (a corrected LS, a CR revised before the submission deadline, etc.). The original crawler only checked `local_path.exists()` before skipping a download — so a same-filename replacement would never be noticed once the file existed locally. Fixed: before skipping, the crawler now compares the file's live modified-time (from the directory listing) against what's stored for that TDoc (`db.get_tdoc_file_state`). A mismatch means the file changed since we last saw it — it gets re-downloaded, and `db.reset_extraction` clears the stale `text_path`/`extraction_status` so the next extraction pass reprocesses it from the new content, rather than silently keeping stale extracted text next to a replaced source file.

Verified directly: forced a stale `file_modified_at` on an already-extracted TDoc, ran `--refresh`, confirmed the log showed *"changed on the server ... re-downloading"*, confirmed `extraction_status`/`text_path` were nulled, then confirmed a normal `--extract` pass re-populated them from the new file.

**Scheduling**: for now, `main.py --refresh` run via Windows Task Scheduler (dev machine) on whatever cadence makes sense (daily is reasonable given 3GPP meeting cycles). On a future Linux deployment, plain cron calling the same command. `pg_cron`/`pgmq` only become relevant later, if this needs to run across many groups with per-job retry isolation rather than one script's try/except-and-continue per meeting (already present in `ingest_scope`).

**Known minor edge case**: group discovery treats every directory under a working group's FTP path as a candidate meeting (e.g. a `Specifications/` folder under `WG2_RL2/` got swept in during testing). Harmless today — it just produces a "no TDoc list found, 0 docs" log line and moves on — but worth a naming-pattern filter (meeting folders start with a TSG-specific prefix) if it gets noisy as more groups are added.

---

## 7. Text normalization (for MCP/AI consumption)

Raw extracted text has real problems for an AI reader: non-breaking spaces, soft hyphens, curly quotes, em-dashes, and zero-width characters that look fine to a human but confuse search and waste an AI's attention. Plan: keep the raw extracted text as the byte-exact source of truth (these are official document quotes, never altered), and additionally produce a normalized form (NFKC unicode normalization, whitespace/punctuation cleanup) used for search indexing and for what an MCP tool actually returns. This is independent of the MCP server's build timing — it's a data-quality improvement to the extraction pipeline that can happen now.

---

## 8. Cover-page metadata fallback (new — closes a bigger gap than expected)

Widening the crawl surfaced that the `Tdoclists/*.xlsx` mechanism (§2, §4) isn't RAN-wide, let alone 3GPP-wide — it's RAN2-specific. Checked directly:

- **RAN1, RAN3, RAN4, RAN5**: same bare structure as CT groups (`Docs/`, `Agenda/`, `Report/`, `Invitation/`, `LS`/`LSin`/`LSout`), no Excel at all.
- **SA2**: a different, non-Excel export — `TdocsByAgenda.htm` (a real, substantial HTML file, ~9,200 lines, thousands of TDoc references) — not parsed yet, but genuinely there.
- **CT1**: no per-TDoc metadata file of any kind. Confirmed by checking `Report/` (post-meeting only, not useful mid-cycle) and `Agenda/agenda.csv` (322 lines of agenda topic numbers, no TDoc references at all).

Rather than build a separate parser per TSG/format, `crawler/coverpage.py` takes a different approach: every 3GPP TDoc `.docx`, regardless of which TSG produced it, carries its own cover page — a `Title:`/`Source:`/`Release:`/`Work Item:` block for LS and discussion papers, or a standardized fixed-layout table for CRs (`[spec, "CR", number, "rev", revision, "Current version:", version]`). This parses that directly out of the plain text `extract.py` already produces, via label matching plus positional detection for the CR row. `db.py::backfill_metadata_from_coverpage` fills only currently-`NULL` columns — it never overwrites metadata that came from a real TDoc list export, so RAN2 is completely unaffected.

**Verified against real CT1 documents that had zero metadata before this**: correctly recovered title/source/release/work-item for LS and chair-guidance documents — e.g. `C1-263013` → title "Reply LS on support of PWS for eMTC NTN from Rel-17", source `SA1`, release `Rel-19`, work item `5GSAT_ARCH`, parsed with no TDoc list export involved at all.

**Known limitation, accepted deliberately**: this is a heuristic, not an authoritative source. One case observed: a schedule/timetable document with its own "Title" table column produced an incorrect title (`C1-263009` → "Start date"). Acceptable specifically because the backfill only ever fills what would otherwise be `NULL` — a wrong guess on an administrative document is strictly better than no metadata at all for groups with nothing else, and it can never corrupt a group (like RAN2) that has a real structured source.

**Also fixed while building this**: `extract.py`'s `_extract_docx` was repeating merged table-cell text once per grid column it spans (a python-docx quirk) — e.g. a CR's title cell merged across 7 columns showed up 7 times, joined with `" | "`. Deduped (`_dedupe_consecutive`) — this both fixed noisy extracted text and made the cover-page parser's job simpler. Applies to extractions going forward; the handful of files extracted before the fix keep their noisier (but not incorrect) text until re-extracted.

`TdocsByAgenda.htm` (SA2 and likely other SA groups) remains unparsed — real data sitting there, genuine follow-up work, not urgent now that cover-page parsing covers SA groups' documents too (just without whatever SA-specific fields that HTML export might carry beyond what's on the cover page).

**A second fix, found while actually widening the crawl**: `discover.list_meetings` treated every directory under a working group as a candidate meeting, sorted by modified-time. This went from the "harmless log noise" noted in §6 to actually costing data — crawling RAN5 with `--meetings-per-wg 2` picked `Working_documents` and `PRD` (non-meeting utility folders with recent modified-times) and got **zero real meetings**, not one. Fixed by filtering to folders starting with `TSG` — true of every real meeting folder observed across RAN/SA/CT (`TSGR2_135`, `TSGS2_176_Prague_2026-08`, `TSGC1_162_Prague`, ...), true of none of the utility folders (`PRD`, `Working_documents`, `Workshop`, `Specifications`, `Templates`). Re-ran RAN5 after the fix: correctly picked `TSGR5__112_Maastricht` (1,594 real docs) instead.

**Widening verified across all of RAN, plus SA2 and CT1**, cover-page fallback and meeting-filter fix both in effect:

| Group | TDocs seen | Have title (Excel or cover-page) | Downloaded+extracted (sampled) |
|---|---|---|---|
| RAN1 | 1,734 | 26 | 29/30 |
| RAN2 | 1,452 | 1,429 (Excel-native) | 134/144 |
| RAN3 | 591 | 30 | 30/30 |
| RAN4 | 2,480 | 24 | 30/30 |
| RAN5 | 1,594 | 8 | 13/20 |
| SA2 | 2,470 | 11 | 14/15 |
| CT1 | 897 | 8 | 15/15 |

Metadata counts outside RAN2 are low relative to TDoc count because only the small sample actually downloaded+extracted so far has cover-page metadata — the fallback runs at extraction time, not at discovery time, so title coverage will rise as more of each meeting is downloaded. A few stale `meetings` rows from before the folder filter (`RAN2/Specifications`, `RAN4/PRD`, `RAN5/Working_documents`, `RAN5/PRD`) remain in the table from earlier test runs — harmless, won't be re-selected going forward, not urgent to clean up.

---

## 9. Phase 0 test matrix (still worth running, now against real code instead of a plan)

| Test | Expected result |
|---|---|
| One meeting's TDoc list | All rows parse correctly — already true for RAN2#135 |
| One missing/corrupt document | Marked failed, ingestion continues for the rest |
| Re-run the same meeting twice | No duplicate rows (idempotent upsert — already true) |
| A second working group (different naming/casing) | Ingests correctly with the same code, no group-specific hacks — verified across RAN1/RAN3/RAN4/RAN5, SA2, and CT1 (§8) |
| Non-ASCII text | Stays readable through extraction and normalization |
| Interrupted download, re-run | Resumes without re-downloading completed files (already true — verified) |

**✅ Interrupted-download recovery under real network failure — now tested, not just the "already exists" skip path.** Verified with a raw TCP server under our control, forcing genuine connection drops/resets (`WinError 10054`/`10061`, not mocked exceptions) at three points: mid-stream during download, on every one of the 3 retry attempts (sustained outage), and before any HTTP response is received at all (connection refused). In every case `dest_path` is correctly never created — only `http_client.py`'s atomic `tmp_path.replace(dest_path)` on full success creates it — so `ingest.py`'s `local_path.exists()` check correctly retries on the next run, and a subsequent run against a healthy server recovers and produces byte-correct output.

**Found and fixed while testing**: a fully-failed download (all retries exhausted) left its `.part` temp file orphaned on disk — never mistaken for a real download, but genuine disk-hygiene debt that would accumulate during any sustained 3GPP-side outage. Fixed in `crawler/http_client.py`: `tmp_path` is now computed once upfront (rather than only after a response is received) and removed on every failed attempt, not just successful ones.

---

## 10. Remaining phases — status as of v7 (several now done; unchanged in spirit from v4 where still open)

1. **Widen the crawl** — ✅ substantially done. All 18 tracked groups now ingest with real metadata: RAN plenary + RAN1–5 + RAN AH1, SA plenary + SA1–6, CT plenary + CT1/CT3/CT4/CT6 — not just the original RAN1–5/SA2/CT1 sample. Meeting selection was also rebuilt to pick by real date rather than by folder modified-time (§4). **Still open**: full historical backfill — each group still deliberately tracks only its current + previous meeting (keeps data fresh and correctly classified without a much larger download/extract/render workload), not the full historical archive this item originally envisioned. Whether to pursue that is a scope decision, not a bug. Confirmed with the project owner: stays at current + previous (`meetings_per_wg=2`) going forward — a one-off wider crawl (`meetings_per_wg=3`) was used temporarily to investigate the missing-zip item below and is not the steady-state setting (`main.py`'s `--refresh` reverted back to 2).

**✅ Found and fixed while checking that every group actually has both an Upcoming and Previous meeting (per the project owner's request, prompted by the above): CT4 was silently stuck on 2024-era meetings, missing 14+ real, currently-happening meetings.** `discover.list_meetings`'s folder-name filter already had documented cases for groups that dropped the "TSG" prefix over time (CT6: `"CT6-127_..."`, hyphen; CT plenary: `"CT_113_..."`, tsg-code + underscore) — CT4 turned out to be a third, unhandled variant: `wg_short` + underscore, e.g. `CT4_125_Hefei` onward (2025-02 through at least 2027-02), matching neither existing pattern. Not assumed from a live-server check alone: cross-checked against 3GPP's own dynareport meeting calendar (showing CT4 numbered to #150, scheduled through 2028) and independently against a second public TDoc archive (TDocHamster, showing CT4 content through #137) before concluding this was a real crawler gap rather than a genuine gap in 3GPP's own publishing. Fixed with one added filter condition in `discover.py`'s `is_meeting()` (`{wg_short}_\d`, mirroring the existing `{wg_short}-\d` hyphen case). Verified live: CT4 now correctly finds meetings through #139 (Feb 2027, previously stuck at #124/Aug 2024), with zero false positives (every one of the 146 folders found is a genuine CT4/TSGCT4-numbered meeting) — and re-checked RAN5, CT1, CT6, SA2, and RAN2 afterward to confirm the added condition caused no regression (RAN5's known utility-folder exclusion, e.g., still holds).

Separately checked all 19 tracked groups for having both a real Upcoming and Previous meeting: 17/19 correct. The 18th, **RANAH1**, has no dynareport calendar page at all (already documented in `dynareport.py`) and its newest folder on record is from 2010 — a genuinely dormant group on 3GPP's side, not a bug. CT4 was the 19th, now fixed above.
2. **Shared service layer** — ✅ done. `service/queries.py` (§4): `search_tdocs`, `get_tdoc`, `compare_tdocs`, `list_meetings`, `get_stats`. Both the FastAPI backend and the MCP server call into it directly — no duplicated query logic anywhere.
3. **FastAPI backend** — ✅ done. `api/main.py` (§4): all the routes originally listed here, plus `/api/tdocs/{id}/view` for the rendered-PDF preview (§14) and the routes serving the frontend itself.
4. **MCP server** — ✅ built and verified, for local use; ⚠️ production hardening confirmed as real near-term scope, not deferred. `mcp_server.py` (§4) exposes the shared service layer as MCP tools over stdio, confirmed working end-to-end through both Claude Code and Antigravity IDE. See §13 (new in v7) for the concrete plan: transport switch to `streamable-http`, real hosting, and all four original hardening requirements (auth token, per-tool result limits, rate limiting, read-only DB role) — none of which exist yet, all of which are now confirmed-needed rather than local-use-only nice-to-haves.
5. **Frontend** — ✅ done for MVP scope. Homepage and search page (§4) — two-pane search/filter/preview/compare UI, plus a homepage with real per-WG meeting cards and a live Ongoing/Upcoming/Past status badge (§4's meeting-selection rebuild). Preview is now the PDF.js viewer from §14 (✅ done), not the old `<iframe>`. AI summaries remain explicitly post-MVP, unchanged from the original scoping. **Diff — ✅ built, no longer post-MVP.**

**✅ Diff, built as a real document-level comparison (not a plain-text diff).** Investigated a second public TDoc archive (TDocHamster) first to see what "diff" actually needs to look like: their feature renders inline strikethrough (removed) / underline (added) directly on the formatted page — the same visual result as opening two Word documents and using Track Changes — not a plain-text side-by-side. That ruled out a simple `difflib`-on-extracted-text approach before any code was written.

Built on LibreOffice's own document-comparison feature (Tools > Compare Document), which is only exposed via its UNO scripting API, not the simple `--convert-to` CLI flag `crawler/render.py` already uses — confirmed this was actually possible in this environment with a throwaway prototype *before* building the real pipeline, per this project's standing rule to verify rather than assume. That prototype worked on the first real test pair and reproduced 3GPP's own two-tone CR-diff convention (clean version, then the marked-up original) natively, exceeding the TDocHamster reference in fidelity.

Shape of what got built:
- **`crawler/_diff_worker.py`** — runs under LibreOffice's *own* bundled Python (has the compiled `uno`/`pyuno` bindings; the project's regular venv Python cannot import these), invoked as a subprocess. Self-contained: starts its own isolated soffice listener (fresh profile dir + free port per call, same one-call-per-conversion isolation principle as `render.py`'s existing profile isolation, for the same reason — concurrent LibreOffice instances collide on a shared profile), connects via UNO, dispatches `.uno:CompareDocuments`, exports to PDF, tears the listener down.
- **`crawler/diff_render.py`** — the regular-venv-side wrapper: extracts both documents' .docx from their zips (scoped to `.docx` only — the format `.uno:CompareDocuments` targets, and the common case for CRs/LS documents — anything else cleanly reports `no_document`/`unsupported`, mirroring `render.py`'s own pattern rather than trying to force every format through), shells out to the worker script with a timeout, caches the result.
- **`crawler/db.py`** — three new columns (`diff_rendered_path`, `diff_render_status`, `diff_render_error`), deliberately separate from the plain-render columns rather than reusing them, since a TDoc can have both cached independently.
- **`api/main.py`** — `GET /api/tdocs/{id}/diff`, same render-on-first-view-then-cache shape as the existing `/view` route, sharing its concurrency semaphore (LibreOffice instances are heavy either way). Distinct 404 messages for each real "nothing to diff" case (no revision link at all; predecessor never downloaded; this document itself not downloaded) rather than one generic failure.
- **`frontend/app.js`** — a "Diff" button next to Download/Bookmark, shown only when `is_revision_of` is present, toggling the same PDF.js iframe between `/view` and `/diff` via the existing delegated click-handler pattern.

**Two real bugs found and fixed while building this, both from actually running it rather than trusting the design on paper:**
- A `PermissionError` from Windows file-locking: the worker script's temp profile directory was being deleted (via `TemporaryDirectory`'s `__exit__`) *before* the soffice listener process holding files open inside it had actually terminated — fixed by moving listener teardown into a `finally` nested inside the `with` block, so termination always completes before the directory cleanup runs.
- An `OSError` from `Path.replace()` failing across drives (the OS temp dir is on `C:`, the project's `data/rendered` is on `D:`) — the exact same class of bug `render.py` had already solved with `shutil.move()` instead; `diff_render.py` was missing that and got the same fix.

**Verified live, twice — once at the API layer, once in an actual browser.** Direct HTTP testing confirmed: a real `200`/`application/pdf` response with correct byte-identical output to the standalone prototype; a cached second request in ~0.15s instead of re-rendering; and the three distinct 404 cases each firing correctly. Then driven through a real headless Chromium session (Playwright) against a live dev server on a separate port from the one already running for manual use: navigated to a real document with a genuine revision predecessor, confirmed the Diff button appears, clicked it, confirmed the PDF viewer's page count changes (4→5 pages) and the tracked-changes markup renders exactly as designed, clicked again, confirmed it reverts cleanly to the plain view — no console errors traceable to this feature.

**Extended the same day: a second, different kind of predecessor, for documents `is_revision_of` was never going to cover.** Live comparison against TDocHamster (checked with the project owner's own real-browser screenshots) showed their Diff working on `RP-261575`, a RAN plenary meeting agenda with an empty `is_revision_of` — a document type with no formal 3GPP revision chain at all. Investigated rather than assumed: `RP-261575` ("Agenda for meeting RAN #113...") and what TDocHamster diffed it against, `RP-261475` ("Revised agenda for meeting RAN #112..."), are two independent documents from two different meetings, not a revision pair — agendas are conventionally copy-edited from the previous meeting's as a starting template, but 3GPP's own metadata never formally links them as revisions of each other.

Checked how safe a "same doc_type, previous meeting" match actually is before building it: `doc_type` averages 3.56 "agenda" documents per meeting project-wide, not 1 — a naive match would frequently be ambiguous. The real previous meeting (`TSGR_112`) had three: an original agenda plus two later revisions. TDocHamster's pick, confirmed by checking upload timestamps, was the most recently uploaded of the three — not the alphabetically/numerically first. That's the rule `service/queries.py`'s new `find_cross_meeting_predecessor` implements: for a small, explicit allowlist of recurring administrative doc types (`_CROSS_MEETING_DOC_TYPES = {"agenda"}` — deliberately not CR/pCR/discussion/LS/etc., which are individual contributions with no "the meeting's one copy of this" concept, so this comparison isn't meaningful for them), find the nearest earlier meeting of the same group, then the most-recently-uploaded document of that type within it. Verified against the real case before wiring it in: returns exactly `RP-261475`, matching TDocHamster's own choice. Returns `None` — never a guess — whenever any step is ambiguous or missing (no meeting date, no previous meeting, no same-type candidate, or the best candidate was never downloaded).

`GET /api/tdocs/{id}/diff` (`api/main.py`) now tries `is_revision_of` first (a real, 3GPP-declared revision — highest confidence), and this cross-meeting lookup second; the frontend button (`frontend/app.js`) shows whenever either *could* apply (`is_revision_of` set, or `doc_type === "agenda"`), with the API's existing specific-404 handling covering the case where the lookup still comes up empty.

**A second real bug found running this against real data, not assumed:** the render came back `no_document` on the very first live test. `RP-261575.zip`/`RP-261475.zip` both contain legacy `.doc` files, not `.docx` — `diff_render.py` had only ever been tested against a CR (always `.docx` in this corpus), so its `.docx`-only scoping silently excluded exactly the doc type this extension was built for. Fixed by widening `_extract_docx` to accept `.doc` too, on the same reasoning `render.py`'s plain conversion already relies on — Writer loads either into the same internal document model before acting on it, confirmed by the fix immediately working, not assumed from that reasoning alone. Also fixed a real latent bug surfaced by the same change: the render call was passing `detail.is_revision_of` (empty in the cross-meeting case) as the label used to pick the right file out of the predecessor's zip, instead of the actual predecessor id — corrected to use the resolved `predecessor_id` regardless of which path found it.

Verified live end-to-end again after both fixes: API returns a real `200`/PDF for `RP-261575`, content confirmed to show the exact same changes as TDocHamster's own screenshot (meeting number, TDoc number, city, and dates all correctly marked); browser-driven Playwright pass confirmed the button appears, toggles, and the page count changes (15→16) exactly as with the first (CR-based) case.

**Third bug, found by the project owner directly clicking Diff on their own long-running dev server: a blank/black viewer on failure, indistinguishable from a real crash.** Root cause of that specific report was the dev server itself predating this whole feature (no `--reload`, so none of the day's code — not just Diff — was actually loaded; confirmed by the same `/diff` route returning a bare framework `404 {"detail":"Not Found"}` rather than any of this feature's own messages) — resolved by restarting it. But retesting after the restart surfaced a second, real, and more general problem underneath: a *legitimate* "nothing to diff" 404 (this time for `R1-2603480`, doc_type `agenda`, whose previous meeting `TSGR1_124b` has zero tracked documents at all — a meeting that rolled out of the current+previous crawl window before ever being fully crawled, same class of gap as CT4's old meetings, not a new bug) looked exactly the same to the user as a crash: the PDF.js iframe was pointed straight at the `/diff` URL, and a JSON error body can't render as a PDF, so it just showed blank.

Fixed properly rather than special-cased: `frontend/app.js`'s diff-toggle handler now fetches `/diff` itself before ever pointing the iframe at it, and branches on the result — success swaps the iframe source as before; any failure hides the iframe and shows the API's own specific error message (e.g. "No predecessor found to diff R1-2603480 against") in its place. Fixes the reported symptom and every other way `/diff` can fail, not just this one case. Verified live in the browser for both outcomes side by side: the failure case now shows a clear, readable message with the iframe hidden; the success case (re-run) shows no regression — same toggle behavior as before, confirmed by an explicit check that the error element is *not* shown when the diff genuinely succeeds.

**A fourth, unrelated finding while chasing the third: search/browse results were never actually sorted by recency at all.** The project owner's own follow-up test — comparing RAN1's document list against TDocHamster — showed RAN1#125's documents ahead of RAN1#126's newer ones, which looked like RAN1#126 was missing from the corpus entirely. It wasn't (1,738 documents, agenda included, all downloaded and diff-verified) — `service/queries.py`'s `search_tdocs` had always ordered results with a plain `ORDER BY t.tdoc_id`, a **string sort on the TDoc ID**, not a date sort. It happened to look roughly chronological most of the time purely because TDoc numbers are assigned in order, which is exactly what made this easy to miss until a specific case (`"...3..." < "...5..."` lexically, regardless of which meeting is actually newer) broke that illusion. No sort control existed anywhere on the search page to work around it either.

**✅ Fixed**: `ORDER BY m.start_date DESC NULLS LAST, t.tdoc_id` — most recent meeting first by real date, TDoc ID kept only as the tiebreaker within a meeting (so documents still read in natural submission order once you're looking at one). Applies site-wide to every search/browse view, not just RAN1. Verified directly against the exact case that surfaced it: querying RAN1 with no filters now returns `TSGR1_126b` (the newest tracked meeting) first, not the oldest numerically-labeled documents.
6. **Only if/when real scale demands it**: `pg_cron`/`pgmq` on the existing Postgres for per-job retry isolation and parallel workers across many groups (§6 already covers the basic "what's new/changed" refresh without needing either); MinIO for object storage in place of local disk; connection pooling (PgBouncer) once concurrent public traffic is real. Still correctly deferred — no sign yet that real scale demands any of this.
7. **Document viewer** — see §14. ✅ Done — backend (LibreOffice render pipeline) and the frontend PDF.js swap both verified working, including the multi-instance compare view.
8. **Authentication & user accounts** — see §12. Reverses v4/v5's explicit "no login" scoping decision (§3). Decided in v7: personalization (not just access control), Google sign-in first (designed for a second method later), MCP tokens tied to the same login, open public signup. Nothing built yet — this is now a concrete spec to build against, not an open question.

---

## 11. What to do with the separately-built `tdocstore` package

`tdocstore/` (a fuller package with its own CLI, MCP server, and revision-diff logic) exists in git history but is deleted from the working tree. Its own README states it was *"written against the known layout but could not be verified against the live server from the build sandbox."* That shows in practice: `tdocstore/tdoclist.py` assumes the TDoc list lives in `Docs/` as `TDoc_List_Meeting_<WG>#<n>.xlsx` — confirmed wrong in §2 (real location: `Tdoclists/`, timestamped snapshots). Its crawler/parser layer should not be restored over the verified `crawler/` implementation.

**What is worth reusing from it, later, deliberately** (pulled from git history, not restored wholesale):
- `textnorm.py` — pure text-cleanup logic, doesn't touch site structure, directly relevant to §7.
- Its CLI shape (`search`, `agenda`, `prev --diff`) and MCP tool structure — reasonable design references for the MCP server and Frontend phases in §10 above.
- Its revision-diff approach — relevant once `is_revision_of`/`revised_to` (already captured in `tdocs`) gets a Compare/Diff feature built on top.

---

## 12. Authentication & user accounts (new in v6, decided in v7, core built after v7)

**Reverses a prior decision.** §3 explicitly scoped this project as having no login, and that was the deciding factor against self-hosted Supabase's bundled Auth (GoTrue). The project owner has now asked for authentication and login to be added, and confirmed the shape of it — the four open questions this section originally posed are now settled:

1. **What auth gates: personalization, not just access control.** Real per-user data — saved searches, bookmarks, per-group watch/notify preferences — not just a login wall. This needs an actual `users` table with per-user foreign keys elsewhere, not a bare login check.
2. **Method: Google sign-in (OAuth) first**, deliberately designed so email+password (or another provider) can be added later without restructuring anything. The way to keep that true: a user's identity is a stable internal ID, and "signed in with Google" is one *linked identity* attached to that ID, not the identity itself. Adding a second sign-in method later is then just attaching a second linked identity to the same user row. (The one thing to decide *later*, only once a second method is actually being added: whether a matching email address on a new sign-in method auto-links to an existing account or creates a separate one — not a decision needed now.)
3. **MCP tie-in: same login, personal token.** Once a user is signed in, they generate their own personal MCP token from their account settings, and the MCP server (§13) checks that token against the same user/identity system — one identity system for both the website and MCP access, not two.
4. **Signup: open to anyone**, via Google. Since sign-in is Google-only for now, this needs none of the heavier public-signup work a password-based flow would (no email verification, no password-reset flow) — Google already handles proving the person owns that identity.

**✅ Built (core, after v7):**
- `users`, `identities`, and `sessions` tables (`crawler/db.py` — same migration mechanism as everything else). `identities` separates *who* (`users.id`) from *how they signed in* (`provider` = `"google"` or `"local"`, `provider_user_id`), exactly as decided above.
- `auth/` package: `queries.py` (all DB access), `deps.py` (`get_current_user`/`require_user`/`require_admin` — this is where session lookup + `status = 'active'` is re-checked on **every** authenticated request, not just at login), `google_oauth.py` (authlib client), `routes.py` (all endpoints below), `config.py`.
- Google sign-in: `GET /auth/google/login` → Google → `GET /auth/google/callback`, which upserts the `users`/`identities` rows (keyed on Google's stable `sub`, not email) and issues a session. Sessions are opaque random tokens (`secrets.token_urlsafe`), stored server-side hashed (SHA-256) in `sessions`, set as an `httponly` cookie — not a JWT, specifically so a session can be killed outright by deleting its row rather than having to wait out an expiry.
- `GET /auth/me`, `POST /auth/logout` — wired into a minimal header widget (`frontend/auth-widget.js`) on the home and search pages.
- **Item 5, admin-issued test accounts — the open question is now resolved: username + password** (not a one-time link), because the project owner confirmed revocation works identically either way, and password is more convenient for a tester logging back in over several days. Built as `provider="local"` identities (bcrypt-hashed password), a `POST /auth/local/login` endpoint, and `is_admin`/`status` columns on `users`. A minimal admin page (`/admin`, gated server-side by `require_admin` on every endpoint it calls — the page itself just hides the UI client-side, it isn't the actual security boundary) lets the admin create test accounts and flip any account's status.
- **Verified live** (curl, end to end, with a temporary account deleted afterward): admin creates a test account → tester logs in → admin disables the account → the tester's *already-issued* session stops working on its very next request (not just their next login) → a fresh login attempt with the correct password also fails. Also confirmed the 401 (not signed in) vs. 403 (signed in, not admin) distinction on admin-only routes.

**Real Google OAuth credentials are now in place and verified live** (not just curl) — the project owner created the OAuth client in Google Cloud Console, completed a real Google sign-in on `localhost:8000`, and was promoted to `is_admin` directly via SQL (a one-time bootstrap step — there's no self-serve way to become the first admin, deliberately, since that would defeat the point of `is_admin` gating anything). The `/admin` page then worked exactly as designed: create-test-account form and the live users table, gated correctly.

**✅ Item 3, personal MCP tokens, built and verified** (adds a `mcp_tokens` table, `POST/GET /auth/mcp-tokens` and `POST /auth/mcp-tokens/{id}/revoke` — self-service for any signed-in user, not admin-only; a raw token is shown exactly once at creation, only its SHA-256 hash is ever stored). `mcp_server.py` gained a `--http` mode (`streamable-http` transport, port 8001) that verifies each request's bearer token against this table via the SDK's `TokenVerifier` extension point; the default no-argument invocation (what `.mcp.json` actually launches today) is completely unaffected — still plain unauthenticated `stdio`, confirmed by re-importing the module with no `--http` arg and checking `mcp.settings.auth is None` and all 5 tools still registered.

This directly answers the project owner's question about independent revocation, and all three cases were verified live end-to-end (temporary account, cleaned up after): a garbage/unissued token is rejected; revoking one specific token blocks only that token while a second, un-revoked token for the same account kept working; disabling the whole account then blocked that second (never-revoked) token too, *and* blocked website login the same way.

**A gap surfaced by live testing, and closed the same day**: revoking one token alone doesn't stop the account holder from simply getting a *new* one — the MCP Inspector, on a 401 from a revoked token, automatically re-ran the full OAuth flow, and since the account's website session was still active, one click on the consent screen produced a fresh, working token. That's correct behavior when the account holder revokes their own token (reconnecting is their own choice) — but it meant an **admin** trying to cut off one specific user's MCP access had no way to make it stick short of disabling their whole account. Fixed with a fourth lever, `users.mcp_access` (`auth/routes.py`, `POST /auth/admin/users/{id}/mcp-access`, a toggle in `/admin`'s user table) — separate from `status`, checked in the one place a token can ever be minted (`queries.create_mcp_token`, called by both the self-service `/account` button and the OAuth exchange), so neither path can produce a working token while it's off. Turning it off also revokes every token the user currently holds, immediately. The consent page checks it too, so the user sees a clear "MCP access has been disabled" message rather than approving something that silently fails. Verified live with a scripted test: a live token was still working, got instantly killed the moment the admin flipped the flag, self-service token creation was refused with a clear `403`, and the target's website login remained completely unaffected throughout.

**✅ The fifth and final lever — force website sign-out only, MCP untouched — now built.** `queries.revoke_all_sessions` deletes every live `sessions` row for the account (returns how many, so the UI can show "ended 2" vs "none active" rather than a blind "done"), exposed as `POST /auth/admin/users/{id}/revoke-sessions` (admin-only) and a "Force sign-out" button in `/admin`'s user table. Deliberately does not touch `status` or `mcp_access` — the account stays active, so this is "sign them out right now" (they can log back in on their very next attempt), not a ban; that distinction is what makes it the true mirror image of `set_mcp_access` (which blocks MCP only, website untouched) rather than a weaker version of disabling the account. Verified live end-to-end with two temporary accounts (a target and a separate admin actor, cleaned up after): the target's real session cookie, captured before the call, stops authenticating immediately after the admin revokes it; a live MCP token for the same account keeps validating throughout, completely unaffected; the account signs back in successfully right away since `status` was never touched; and calling the endpoint with zero active sessions returns `sessions_ended: 0` cleanly rather than erroring.

So today, five independent levers exist: **disable the account** → blocks both website and MCP; **disable MCP access only** → blocks MCP (both self-service and OAuth-issued tokens, current and future), website login untouched; **revoke one MCP token** → blocks only that token; **force website sign-out only** → ends current sessions, MCP and future logins untouched. All five levers documented, built, and verified live.

**✅ MCP tokens restricted to admin accounts only (project owner decision, made after public signup shipped).** Previously any signed-in user could self-service a personal MCP token; now only `is_admin` accounts can, at every point a token could ever come into existence or be used: `queries.create_mcp_token` raises a new `McpAdminOnlyError` (distinct from the existing None-return for "admin but mcp_access is off," so the two cases keep giving accurate, different messages) — checked by both the self-service `/account` route and the OAuth code-exchange path in `oauth_provider.py`; the `/oauth/consent` page checks `is_admin` before its existing `mcp_access` check, so a non-admin sees a clear "MCP access is admin-only" message before ever reaching the approve button; and `get_user_by_mcp_token`'s per-request validation query now also requires `is_admin = true`, matching the project's existing pattern of re-checking every gate on every request (not just at mint time) — an admin demoted to a regular user has any existing tokens stop working on their very next request, no separate revoke step needed. `frontend/account.js` hides the generate-token UI entirely for non-admins, showing a short explanatory note instead (the public MCP server URL itself stays visible to everyone — it's not secret). Confirmed empirically (temporary admin + non-admin test accounts, cleaned up after): a non-admin's `/auth/mcp-tokens` POST gets a `403` with the admin-only message; an admin succeeds normally; an admin with `mcp_access` off still gets the pre-existing, distinctly-worded "disabled" `403`; and a live token minted while admin, confirmed valid, goes invalid immediately on the same request path the instant that account is demoted.

**✅ Rate-limiting on the OAuth callback, local-login, and test-account-creation endpoints — built and verified** (`auth/rate_limit.py`). A different surface from the MCP rate limiting in §13 — this guards the login mechanism itself, not tool calls. Two layers, not one:
- A generic per-IP throttle on all three routes (namespaced per route, so they don't share one budget).
- A sharper, username-keyed throttle specifically on `/auth/local/login` (5 failed attempts per 5 minutes, checked *before* touching the database) — this is the one that actually matters for password-guessing, since it catches a slow/distributed attack rotating source IPs to dodge the per-IP check, which the per-IP check alone can't. Only failed attempts count toward it; verified directly that repeated *correct* logins never eat into the same budget.
- Verified live: 5 failed logins against one username get real `401`s, the 6th gets `429` before the password is even checked; 3 correct logins in a row against a fresh account all succeed normally afterward.

**✅ Personalization, first table: bookmarks.** A `bookmarks` table (`user_id`, `tdoc_id`, unique together) — user-generated data, not crawled content, so deliberately kept on the same full-privilege connection as the rest of §12's own tables (`crawler.db`), never through `service/db.py`'s read-only role (§13 item 4's read-only grant is specifically about protecting the crawled `tdocs`/`meetings` data, not about this). `service/bookmarks.py` holds the query functions, reusing `service/queries.py`'s own summary row-shape so a bookmarks list renders with the exact same frontend code already built for search results. `GET /api/tdocs/{id}` now includes a `bookmarked` field when the caller is signed in (and omits it entirely when signed out, so the frontend never renders a button that would just 401) — one request gives both the document and its bookmark state, rather than needing a second round-trip. A `★ Bookmark` toggle button on the detail card (both the single view and each column of compare view, via one delegated click handler rather than one wired per render), and a dedicated `/bookmarks` page reusing the same summary-row rendering.

Verified end-to-end with a temporary account: `bookmarked` correctly starts `false`, flips to `true` after `POST /api/bookmarks/{id}`, the bookmark shows up in the list with full title/source/etc., disappears after `DELETE`, and the `bookmarked` field is entirely absent from the response for a signed-out request.

**✅ Personalization, second feature: automatic search history** (revised — an explicit "save this search" design was built first, then explicitly rejected by the project owner in favor of automatic history; the button/table/page from that first pass were removed rather than left alongside this). A `search_history` table (`user_id`, `params` as JSON, `created_at`) records itself — no user action needed. `api/main.py`'s `/api/tdocs` search route records a new entry whenever a signed-in user runs a search, with three deliberate exclusions: pagination clicks (only `offset == 0` counts — clicking Next/Prev must never look like a new search), an empty/cleared search (no filters at all isn't a meaningful history entry), and an exact repeat of the *immediately preceding* entry (re-clicking Search without changing anything doesn't spam the list — though the same filters showing up again later, after something else, correctly is *not* suppressed, since that's a genuinely new search event, not a duplicate click). Storage is self-trimming — kept to the most recent 50 per user on every insert. A small "Recent" chip row appears under the search bar (`service/search_history.py`, `GET /api/search-history`) — hidden entirely for a signed-out visitor, each chip a plain link back into `/search?<those params>`.

Found and fixed a real, adjacent bug while wiring this up: `initFromUrl()` — the function that restores filters from the URL on page load — never actually read `source` or `doc_type` from the query string, only `q`/`tsg`/`wg`/`meeting`. A history chip using either of those two filters would have silently dropped them on click, even though it was recorded correctly. Not a history-specific bug (any deep link using those two params had the same gap), but this feature is what surfaced it, since it's the first thing that actually needs a lossless round-trip through every filter.

Verified end-to-end with a temporary account: two different fresh searches both recorded; a pagination request on the same filters added nothing; an empty search added nothing; the exact same search repeated three times back-to-back produced exactly one entry, not three; and the same filters appearing again later (with something else run in between) correctly was *not* treated as a duplicate.

**What doesn't change regardless of the above**: this reopens the Auth question from §3, but not the Storage/Kong/Studio parts of that reasoning — self-hosted Supabase's *Auth* module (GoTrue) was evaluated against building directly on the existing Postgres, and the latter is what got built; adopting Supabase wholesale for Storage/Kong/Studio is still not justified by this alone (§3 unchanged on that point).

**✅ SSL/TLS network-interception investigation — closed, no persistent issue found.** Some months earlier, crawling `www.3gpp.org` had intermittently failed with `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate`, worked around at the time only in throwaway diagnostic scripts (`verify=False`), never in the real crawler (`crawler/http_client.py` always used, and still uses, `httpx.Client()`'s default — full verification on). Re-investigated directly: pulled the actual certificate the server presents (real cert, issued by Google Trust Services for `3gpp.org`/`*.3gpp.org`, valid dates, nothing forged or proxy-like), confirmed a plain fully-verified `httpx.get()` succeeds, and confirmed the crawler's own `HttpClient` succeeds against the exact two URLs that had failed before, unmodified. `certifi` is current. Conclusion: the earlier failure was environment/network-dependent at that moment (a different network in the path that day, or 3GPP mid-rotating the cert), not a lasting defect in this codebase — nothing to fix, no `verify=False` anywhere in real code, closing the item.

---

## 13. MCP server: production hardening & deployment (new in v7 — confirmed target, not a fallback option)

The project owner has confirmed this project is going to production, not staying local-only — so both the transport change and the four hardening items §10 item 4 already called for are now real, near-term work, not a someday-maybe.

**Two separate things have to change from what exists today, and both are required — neither alone is enough:**

1. **✅ Transport: stdio → `streamable-http`, done for `--http` invocations.** `mcp_server.py --http` runs `streamable-http` on port 8001; plain `python mcp_server.py` (what `.mcp.json` actually launches) is untouched — still stdio, still auth-free, confirmed by re-importing the module with no `--http` arg and checking all 5 tools register with `auth=None`. Not yet done: making `--http` the thing actually deployed anywhere (that's hosting, item 2 below).
2. **Hosting — still not done.** A network transport is pointless without somewhere persistent and reachable to run it — a cloud VM or container with a real domain and HTTPS (not the developer's own Windows machine, which is what everything runs on today). This also means the **Postgres database and FastAPI backend need equivalent real hosting**, not just the MCP server — the MCP server is only as useful as the data it can reach, and today that data only exists on one machine.

**The four hardening items, now concrete (expanding §10 item 4):**

1. **✅ Auth token, tied to user accounts (§12) — built and verified, two separate paths for two separate kinds of client:**
   - **Static personal token** (`mcp_tokens` table, `/account` page) — for clients that let you configure a request header (Claude Code's `.mcp.json`, Antigravity, the MCP Inspector). Generated once, pasted into the client's config, checked on every request.
   - **Full OAuth Authorization Server** (`auth/oauth_provider.py`, implementing the SDK's `OAuthAuthorizationServerProvider`) — for clients that only accept a server URL and have nowhere to paste a token (Claude.ai/ChatGPT-style connectors, and the MCP Inspector's own default auto-discovery behavior). Dynamic client registration (`/register`), a login+consent redirect (`/oauth/consent`, reusing the exact same Google/local login §12 already built), and a code-for-token exchange (`/token`, PKCE-verified by the SDK itself) — all backed by three new tables (`oauth_clients`, `oauth_authorize_requests`, `oauth_authorization_codes`). Critically, **the token this flow hands back is just another `mcp_tokens` row** (named `"OAuth: <client name>"`) — not a separate credential type — so it shows up in the same `/account` list as a self-service token and is revoked exactly the same way, with the exact same immediate, per-request enforcement.
   - Verified live end-to-end, twice: once by hand (MCP Inspector — register → redirect → Google-account-adjacent login via a test account → consent → token → tool call → revoke via `/account` → next call rejected), and once by a full scripted walkthrough exercising the identical steps against a temporary account (cleaned up after). A real bug was caught and fixed in the process: the registration handler defaults an unspecified `token_endpoint_auth_method` to `"client_secret_post"`, but the client record wasn't persisting that field — every registered client's auth method silently reverted to `None` on the very next lookup, breaking `/token` for every client. Fixed by adding the column and threading it through `get_client`/`register_client`.
   - One earlier false start worth remembering: giving the SDK `AuthSettings` *without* a real `auth_server_provider` behind it (just a bare `TokenVerifier`) made the server advertise itself as a full OAuth-protected resource with nothing real to redirect to — which broke the Inspector's default connection attempt outright (it tried to redirect through a real `/authorize` that didn't exist). The fix wasn't to hide the OAuth advertisement; it was to build the real authorization server the advertisement promised.
2. **✅ Per-tool result limits, enforced server-side.** `mcp_server.py` clamps every `limit` parameter to 50 regardless of what's requested (`_clamp_limit`), and truncates any returned document text with an explicit note pointing to the website/direct API for the rest (`_truncate_text`) — applied to `search_tdocs`, `list_meetings`, `get_tdoc`, and `compare_tdocs`. Deliberately scoped to the MCP tool layer only, not `service/queries.py` — the website's own detail view still shows full text, since a human reading one document at a time isn't the abuse case this guards against.

   **The text-length cap was picked, then re-measured against the real corpus, then corrected.** The first pass used 20,000 characters — a reasonable-sounding round number, not one checked against actual data. A pass over every extracted document's real length showed that cap was truncating **22% of the entire corpus (4,807 of 21,825 documents)** — not a rare edge case, and disproportionately the substantial ones (meeting reports, TRs) where the truncated part is likely to be the conclusion or decision. Raised to **99,999 characters**, which covers **96.35%** of the corpus with zero truncation (only the true long tail — 797 documents, up to 3.2M characters for the largest single one — still gets cut). In token terms this is still modest: ≈25,000 tokens at the new cap, a small fraction of a modern model's context window. Verified directly: requesting `limit=99999` on `search_tdocs` comes back as `50`; a 186-page document's text now comes back at exactly 100,116 characters (99,999 plus the truncation note) instead of its full length.
3. **✅ Rate limiting**, per token/user, with a per-IP fallback for unauthenticated requests (so even token-guessing at volume is throttled, not just legitimate-but-excessive use). A plain fixed-window counter (60 requests/60s per key) implemented as an ASGI middleware wrapped around `mcp.streamable_http_app()` — outside the SDK's own OAuth/tool-dispatch machinery, not inside it, so it applies uniformly regardless of which auth path a request used. Verified live: 65 rapid requests from one client got `429`s starting at request #58; a request with a different token got its own full budget, unaffected by the first client's exhausted one.
4. **✅ A dedicated read-only Postgres role.** Created `tdoc_readonly`, granted `SELECT` only on `tdocs`/`meetings` — confirmed at the database level, not just by inspection, that a `SELECT` succeeds and an `UPDATE` is rejected with `permission denied for table tdocs` under this role. `service/db.py` (the shared read path both the FastAPI backend and the MCP server already went through — it was already deliberately kept separate from `crawler/db.py`'s writes, just using the same credentials until now) connects as this role via a new `READONLY_DATABASE_URL`; `crawler/db.py`'s writes (ingestion, and the on-demand render write path in `api/main.py`) are untouched, still using the full-privilege role. Verified both sides still work after the switch: search/detail/stats through the read-only role, and on-demand rendering's write still succeeding through the original role.
5. **No arbitrary SQL/filesystem tools** — a standing design constraint, already satisfied: all five tools are narrow and parameterized (search/get/compare/list/stats), none accept raw SQL or an arbitrary file path. Worth keeping explicit so it stays true as tools get added later, not because anything needs fixing today.

**All four hardening items are now done.** What's left in this section is entirely the hosting/deployment side (item 2, still open) — nothing about the MCP server itself is unhardened anymore at the scale this project runs at today.

**Not yet decided, and worth flagging now rather than discovering later**: which cloud platform/host, what the actual rate-limit numbers should be, and whether the read-only role is a single shared one or provisioned per-deployment. These don't block writing the code for items 1-5 above, but do need answers before an actual deployment.

---

## 14. Document viewer: PDF.js over the existing LibreOffice render pipeline (✅ done, v7)

**What already exists and runs** (a gap in v5 — this was built but never documented here): every TDoc that isn't already a PDF (`.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`) is converted to PDF by LibreOffice headless, via `crawler/render.py`, run as a standing background job (`python main.py --render`) that works through the backlog and is re-run incrementally as new documents arrive. Rendered PDFs are served today via `/api/tdocs/{id}/view` in the FastAPI backend (§10 item 3). Two hardening fixes already made to this pipeline, worth recording since they're exactly the kind of thing that would otherwise be silently rediscovered:
- Each LibreOffice invocation gets its **own isolated user-profile directory** (`-env:UserInstallation=...`) — LibreOffice headless locks its profile, so concurrent conversions on the shared default profile collide and fail.
- The conversion **timeout is capped at 40 seconds**, not left unbounded — a small number of documents (root-caused: a Table-of-Contents field filtered by a custom paragraph style, e.g. `TOC \t "Observation"`, a 3GPP-template pattern) make LibreOffice's layout engine hang rather than run slow, and an unbounded/long timeout on those was enough to starve the host machine's CPU and cause unrelated hangs elsewhere on the same machine.

**What's actually new in v6**: the frontend currently shows a rendered PDF via a plain `<iframe src=".../view">` — it relies entirely on the visiting browser's own built-in PDF viewer. The project owner has asked for this to be replaced with an explicit Mozilla PDF.js integration instead. Rationale, and why this is a good match given the pipeline above:

- PDF.js only ever renders PDFs — it has no way to understand `.docx`/`.xlsx` natively. The LibreOffice conversion step is therefore a hard prerequisite for a PDF.js-based viewer to work across every TDoc format, not an unrelated parallel decision — and that prerequisite is already satisfied.
- Consistent viewing experience across browsers, instead of depending on each visitor's browser having inline PDF viewing enabled (some locked-down/enterprise browsers disable it and force a download instead).
- Full control over the viewing UI (custom toolbar, in-document text search, zoom, page navigation) rather than whatever chrome the browser's native viewer happens to provide — matches the two-pane UI target in §10 item 5 more coherently than an opaque `<iframe>`.
- Mature, standard, low-risk choice: it's what Firefox itself uses internally, actively maintained by Mozilla, used broadly in production web apps.

**Scope of the actual work**: frontend-only, confirmed in practice — implementing it needed zero backend or rendering-pipeline changes. `frontend/vendor/pdfjs/` holds the self-hosted pdf.js core + worker (pinned `pdfjs-dist@4.10.38`, not a CDN dependency). `frontend/pdf-viewer.js` is a thin custom layer over pdf.js's core rendering API (not its prebuilt `viewer.mjs` UI) — chosen specifically so more than one instance can be mounted on the same page at once, which the compare view (§10 item 5, up to 3 documents side by side) requires. `app.js`'s preview rendering was restructured so `renderDetailCard` returns `{ html, mounts }` instead of a plain string — the container element for a PDF viewer doesn't exist until after that HTML is inserted into the page, so mounting happens as a separate step right after insertion, for both the single-document and compare-view call sites.

**One real bug found and fixed during verification, worth recording**: the worker script path (`pdfjsLib.GlobalWorkerOptions.workerSrc`) can't be a bare relative string — pdf.js resolved it against the *page's* URL in one internal code path and produced a doubled, still-wrong path in its fallback, so every single document silently failed to render ("Couldn't load this preview.") despite the backend serving the PDF correctly (confirmed via direct `curl`, 200 OK). Fixed by anchoring it explicitly: `new URL("./vendor/pdfjs/pdf.worker.min.mjs", import.meta.url).href`. Caught by testing the actual rendered output in a real browser rather than trusting the code looked right — worth remembering given how confidently-wrong the original one-liner looked.

**Verified working end-to-end**: single-document preview, zoom in/out (canvas dimensions confirmed changing measurably across clicks), and the compare view with two independent documents rendering simultaneously (121 total canvases across both panes, every one confirmed non-blank) — all with zero browser console errors.

---

## 15. Key risks & mitigations (updated)

- **Folder/file naming inconsistency across groups** — confirmed, not just anticipated (§2). `crawler/config.py` hardcodes verified paths per group rather than assuming a pattern; date-from-modified-time rather than date-from-folder-name is already the implemented approach.
- **Running unnecessary infrastructure** — avoided by not adopting Supabase (§3); the stack stays at one Postgres + one file store. (§12 revisits *Auth* specifically, not the rest of this reasoning.)
- **Legal/copyright** — unchanged from earlier versions: attribute 3GPP clearly, don't claim ownership, treat 3GPP's explicit anti-AI-crawler `robots.txt` stance as a real signal (identify the crawler honestly, rate-limit it, don't spoof a browser or impersonate a blocked bot name) rather than a footnote.
- **`tdocstore`'s unverified assumptions re-entering the project** — mitigated by this document (§11): reuse specific modules deliberately, never restore the package wholesale as if it were validated.
- **Stale extracted text after a document is replaced server-side** — mitigated by §6's modified-time change detection; verified working end-to-end.
- **Groups without a structured TDoc list producing no usable metadata** — mitigated by §8's cover-page fallback; accepted as heuristic (occasional wrong guess on administrative documents) rather than blocking on a per-TSG parser for every export format.
- **(v7: resolved, was open in v6) Building auth before the §12 decisions were made** — no longer a risk: §12's four questions are now answered (personalization, Google sign-in first, MCP tokens tied to login, open signup), so there's a concrete shape to build against rather than a guess. The follow-on risk below still applies once building starts.
- **(New, v6; rate-limiting resolved after v7) A public-facing login surface is new attack surface this project has never had.** Choosing Google sign-in (§12) deliberately minimizes this for Google-linked accounts — no password for this project to ever store, reset, or have stolen — but admin-issued test accounts (§12 item 5) *are* real passwords in our own database, which is exactly what the OAuth-callback/local-login/test-account-creation rate limiting (§12) now guards, including a username-specific throttle on `/auth/local/login` distinct from a plain per-IP one. Session/token expiry and revocation are also done (§12: sessions and MCP tokens are both revocable, checked per-request). **✅ Anti-abuse caps on public signup — now built and verified.** Public signup (§12, decided open) means anyone can create a Google-linked account, so two per-account ceilings now exist: `auth/queries.py` caps active (non-revoked) MCP tokens at `MAX_ACTIVE_MCP_TOKENS_PER_USER = 10` (a distinct `McpTokenLimitExceeded` exception, kept separate from the existing None-return used for MCP access being admin-disabled, so both the self-service `/auth/mcp-tokens` route and the OAuth code-exchange path in `auth/oauth_provider.py` give the correct, specific error rather than conflating the two); `service/bookmarks.py` caps total bookmarks at `MAX_BOOKMARKS_PER_USER = 500`, with re-bookmarking an already-bookmarked document explicitly exempted from the count (it's an idempotent no-op, same as the existing `ON CONFLICT DO NOTHING`, and must never look like hitting the cap). Both surface as a `429` with a clear message through the real API routes. Verified two ways: directly against the query functions (cap enforced exactly, revoking one token frees room for a new one, re-bookmarking at the cap is a no-op not an error), and end-to-end through the actual HTTP routes via `TestClient` with a real logged-in test account (10× `200` then `200`→`429` at request 11 for tokens; `200`, `200`, then `429` for bookmarks past a patched cap) — both against real data, cleaned up after.
- **(New, v6) LibreOffice render pipeline hangs on specific document content** — confirmed, not hypothetical (§14): a Table-of-Contents field filtered by a custom paragraph style causes LibreOffice's layout engine to hang rather than fail fast. Mitigated by a 40-second timeout cap and per-conversion isolated profiles (§14); documents that hit this still end up with no rendered preview (the timeout makes them fail, not succeed) — a genuine content-based gap accepted for now, not a crash risk.

---

## 16. Production deployment plan (decided, not yet executed)

Full plan for putting the working local system (Postgres, the crawled corpus, the FastAPI app/API, the MCP server) on the public internet. §16.2 records every shape that was evaluated and why each was rejected — the short version is that the chosen one is the only option requiring **zero code changes**, and it also happens to be the cheapest.

### 16.1 What's actually being deployed, measured (not guessed)

Measured directly rather than assumed, since the earlier R2 recommendation below was itself based on an unmeasured guess and had to be corrected once real numbers were pulled:

| Component | Size |
|---|---|
| Postgres database (`tdocs`, `meetings`, `users`, `sessions`, `bookmarks`, `search_history`, etc.) | 31 MB |
| `data/raw` (original downloaded zips) | 7.33 GB |
| `data/text` (extracted text) | 432 MB |
| `data/rendered` (rendered documents) | 13.04 GB |
| **Total corpus** | **~20.8 GB** |

### 16.2 Options considered, and why each was accepted or rejected

**Rejected: the project owner's own PC as the server, reached via Cloudflare Tunnel.**
Technically works (`cloudflared` exposes a `localhost` service with no open ports needed), and costs nothing — but uptime becomes tied to a personal machine staying powered on, awake, and connected 24/7, which the project owner didn't want. Ruled out by direct choice, not a technical blocker.

**Rejected: fully serverless on Cloudflare (Workers + Hyperdrive + R2), no VPS at all.**
Investigated seriously, not dismissed on assumption:
- Cloudflare Workers now runs real Python and supports FastAPI (via a Pyodide/WASM runtime), which sounded promising at first.
- But the crawler is a long-running batch job that walks 3GPP's FTP archive and writes many files to local disk — Workers execute per-request with bounded CPU time and no persistent background process or local disk. Fitting the crawler into that model would mean rewriting it around Cron Triggers/Queues in small chunked steps — a real rearchitecture of code that already works, not a deployment change.
- The project's actual dependencies (`psycopg[binary]`, `bcrypt`, `lxml` — all C-extension packages) have unverified compatibility with that Pyodide sandbox; nothing in Cloudflare's own docs confirmed they're supported, and this project's standing rule is to never assert correctness without testing it live.
- Cloudflare has no managed Postgres of its own — Hyperdrive is only a connection-pooling proxy in front of a Postgres that still has to run somewhere else, so this path doesn't even remove the need for an external database.
- Conclusion: rejected. Bigger risk, for no real benefit at this project's scale, and the project owner confirmed the actual goal was just "don't want my own PC to be the server," not "specifically Cloudflare for everything" — which a VPS solves directly without any of the above risk.

**Rejected (after measuring): moving the file corpus to Cloudflare R2.**
Initially recommended as good practice before the corpus was actually measured. Once measured (§16.1: ~20.8 GB total), it fits comfortably inside a basic VPS's *included* disk allowance (25–40 GB on any standard small VPS plan), with headroom for continued growth. Introducing R2 would mean rewriting the file-serving code path to talk to an S3-compatible API and running a migration, for a corpus that already fits where it would run anyway. Not worth the added moving part at this scale — revisit only if the corpus grows far beyond current size (§16.6).

**Rejected: Cloudflare Pages for the frontend.**
Not applicable — the frontend is served directly by the FastAPI app's own page routes (`api/main.py`), not as a standalone static site. Splitting it out would add a second deploy target for no benefit at this traffic scale.

**Rejected: Cloudflare Containers** (evaluated separately from Workers, because it's the one Cloudflare compute product that *could* run this code unchanged).
Containers went GA in April 2026 and run standard Docker images with a real Linux toolchain — so unlike the Workers WASM sandbox, `psycopg`/`bcrypt`/`lxml` and even LibreOffice would all work. It disqualifies itself on storage instead, per Cloudflare's own platform docs: *"All disk is ephemeral. When a Container instance goes to sleep, the next time it is started, it will have a fresh disk as defined by its container image."* Concretely against this project: the largest instance type tops out at 20 GB disk (our corpus is already ~20.8 GB) **and that disk is wiped on every sleep**, with a default 10-minute inactivity timeout. Postgres can't live there either (data loss on sleep), and Hyperdrive — Cloudflare's Postgres accelerator — explicitly doesn't work with Containers, only with Workers. Net: it would still require an external Postgres, an R2 migration with a file-serving rewrite, and crawler restructuring, while costing $5/month minimum (Workers Paid) — i.e. more money *and* six changes' worth of work versus a VPS that runs the code as-is.

**Rejected: hybrid shapes (Cloudflare compute + Supabase Postgres + R2 or NAS storage).**
Considered because separating storage from compute is a reasonable instinct. Two findings killed it:
- **Supabase's free tier pauses a project after 7 days of inactivity** — the Postgres instance spins down, the first request after takes 10–30s to cold-start, and it stays unreachable until manually restored from the dashboard. Disqualifying for a public site; avoiding it means Supabase Pro at ~$25/month.
- **Storage-on-NAS in this shape is the worst variant of all**: every PDF/zip request would travel home-NAS → Cloudflare → visitor (the heaviest payloads over the slowest link), the crawler would write files over the internet to a home connection instead of local disk, and it reintroduces the exact home-internet dependency that dropping the NAS was meant to remove — while now also paying for cloud compute.
- Beyond cost, the structural problem is latency: splitting compute from Postgres means **every search query becomes a network round trip**, where it's currently local and sub-millisecond. That's a direct regression to this project's core feature, paid for with six code changes.
- **The version of this idea that does have merit** — VPS (app + Postgres co-located) + R2 for files only + Cloudflare edge — is one change, not six, and is already captured in §16.6 as the thing to do *if and when* the corpus outgrows the VPS disk. Not before.

**Shelved, not rejected: the project owner's own UGREEN NASync DXP4800 Plus (8TB, Docker-capable, UPS-backed), reached via Cloudflare Tunnel.**
Was briefly the chosen path — genuinely a strong fit on paper (24/7-purpose-built hardware, already UPS-backed, 8TB makes the corpus a non-issue, $0/month recurring). Set aside by direct owner decision ("not an option for now"), not for any technical flaw found in it. Left documented here in case it's revisited later, rather than deleted.

**Chosen: a cloud VPS, with Cloudflare in front as network/edge layer — specifically Oracle Cloud's "Always Free" ARM tier first, Hetzner CX23 as the paid fallback.**
This is the §16.2 VPS option from above, now the active plan rather than a superseded one. Compared live against the alternatives (Hetzner, DigitalOcean, Vultr, Contabo):
- **Oracle Cloud "Always Free"** — 2 ARM OCPUs / 12 GB RAM / 200 GB disk / 10 TB egress, **genuinely $0/month forever**, not a trial. Vastly exceeds this project's measured needs (§16.1: ~21 GB corpus, 31 MB DB).
- **Two Oracle-specific risks, and one action that resolves both.** (a) *Capacity*: free-tier ARM instances are notoriously hard to actually provision — "out of capacity" errors are common. (b) *Idle reclamation*: Oracle may reclaim an Always Free instance if, over a rolling 7-day window, CPU **and** network **and** memory (memory applies to A1/ARM shapes specifically) all sit below 20% at the 95th percentile — plausible for a low-traffic site whose crawler only runs briefly each night, since a short burst barely moves a 7-day 95th-percentile figure. **Both are resolved by converting the account to Pay As You Go**: PAYG accounts are exempt from idle reclamation entirely, rarely hit the capacity errors, get Oracle Support access, and — critically — **still pay $0 as long as usage stays within Always Free limits**. The one caveat that comes with it: a card on file means there's no hard stop against charges if something outside the free limits is ever provisioned, where a pure Free Tier account simply cannot bill you. Mitigate with a $1 budget alert set immediately after upgrading, and by confirming "Always Free eligible" on anything created.
- **Note on the crawler's cron job**: there is no cron-specific problem on either Oracle or Hetzner — cron is standard Linux and works normally on both. The Oracle concern above is about the *instance looking idle*, not about the job failing. Once on PAYG that concern disappears, leaving only ordinary operational monitoring (did the run complete, did it error, how many documents did it process) — and even that is low-stakes here, because the crawler is **idempotent** (§6): a missed or failed run is fully recovered by the next one, with no manual repair and no risk of corruption.
- **Verified, not assumed, that ARM doesn't break this project**: every native/compiled Python dependency (`psycopg[binary]`, `bcrypt`, `lxml`, `uvloop`, `httptools`, `watchfiles`, `cryptography`) has published `manylinux_aarch64` wheels on PyPI (checked directly), so `pip install` gets prebuilt binaries, no compiling from source. LibreOffice (the one external binary this project shells out to, `crawler/render.py`/`crawler/extract.py`) is available via standard `apt install libreoffice` on ARM64, and `crawler/config.py`'s `_find_soffice()` already checks Linux paths (`/usr/bin/soffice`, `/usr/lib/libreoffice/program/soffice`) alongside Windows ones — no code change needed. The one thing worth actually measuring after deploy, not assuming: whether LibreOffice render speed on Oracle's ARM cores stays comfortably under the existing 40-second timeout cap (§14) — Ampere A1 gives dedicated (not shared/throttled) cores, so this is more likely to be fine than not, but it's a "verify live," not a guess.
- **Fallback: Hetzner CX23** — 2 vCPU / 4 GB / 40 GB disk, ~€5.99/month (~$7), x86, no ARM-compatibility question at all. Used only if Oracle's free-tier capacity genuinely can't be obtained after a real attempt.
- DigitalOcean/Vultr (~$24/month for equivalent specs) and Contabo (cheap but a known reputation for oversold hardware) were compared and ruled out as worse value than either of the above for this project's scale.

### 16.3 What runs where

**On the VPS** (Oracle Cloud Always Free ARM instance, or Hetzner CX23 if that's unavailable):
- Postgres (own container or direct install — 31 MB needs nothing special)
- The FastAPI app (`uvicorn`), serving the API, frontend pages, and file downloads straight from the VPS's disk, exactly as it runs locally today
- The MCP server (`--http` mode), reachable on its own port/path
- The crawler, run periodically via cron/a scheduled container for incremental refreshes
- The full `data/` corpus (~21 GB) on the VPS's disk — trivial against Oracle's 200 GB, comfortable against Hetzner's 40 GB too

**On Cloudflare** (free plan, no code):
- Either a Tunnel (`cloudflared` on the VPS, zero inbound ports opened — the safer default, same pattern as the NAS plan would have used) or a plain DNS proxy record pointed at the VPS's public IP with the firewall restricted to Cloudflare's published IP ranges — Tunnel is the better default even though the VPS has its own public IP, since it means the VPS is never directly reachable/scannable at all.
- Automatic TLS between visitors and Cloudflare's edge
- CDN caching of static assets, DDoS protection, WAF managed rules, and an edge-level rate-limiting rule on `/auth/*` as a second layer in front of the app's own rate limiting (§12) — belt-and-suspenders, not a replacement for it

### 16.4 Cost

| Item | Cost |
|---|---|
| VPS — Oracle Cloud Always Free, on a Pay-As-You-Go account (§16.2) | $0/month while within Always Free limits |
| VPS — Hetzner CX23 (fallback, only if Oracle can't be obtained at all) | ~$6–7/month (~€5.99) |
| Cloudflare (Tunnel or DNS proxy, TLS, WAF, DDoS, CDN — free plan) | $0 |
| Domain | already owned, renewal not new spend |
| **Total new recurring cost** | **$0/month (Oracle) or ~$6–7/month (Hetzner fallback)** |

### 16.5 Step-by-step execution order (not yet started)

1. Create the Oracle Cloud account and **convert it to Pay As You Go**, then set a **$1 budget alert** immediately (§16.2 — this is what removes both the capacity and idle-reclamation risks while keeping Always Free resources at $0). Provision an Always Free ARM instance (2 OCPU / 12 GB / 200 GB, Ubuntu), confirming it's marked "Always Free eligible." If capacity still can't be obtained after a genuine attempt, provision Hetzner CX23 instead.
2. Basic hardening: non-root sudo user, SSH key auth only, firewall open on 22/80/443 only (or fully closed if using Tunnel exclusively).
3. Install Docker (or Postgres/Python directly); set up the same container/service shape either way: Postgres, the app, the MCP server, the crawler, `cloudflared`. Point the crawler's cron entry at a log file so every run leaves a record, and add a simple failure notification — ordinary ops monitoring, made low-stakes by the crawler's idempotency (§6).
4. Configure environment variables (`DATABASE_URL`, `READONLY_DATABASE_URL`, Google OAuth credentials, MCP config, etc.) on the server — never committed to the repo.
5. Transfer the existing `data/` corpus (~21 GB) onto the VPS.
6. Run the schema migration mechanism (`crawler/db.py`) against the VPS's Postgres.
7. Create a Cloudflare Tunnel (preferred) or DNS-proxy record, and route the project owner's existing domain to the app.
8. Turn on Cloudflare's free-tier protections (WAF managed rules, Bot Fight Mode, the `/auth/*` rate-limit rule).
9. Set restart policies (`restart: unless-stopped` for Docker, or systemd auto-restart) on every service so everything survives a reboot automatically.
10. Verify everything live, end to end, on the real public domain — search, detail view, PDF/text rendering (specifically confirming LibreOffice render times on ARM if Oracle was used), Google + local-test-account login, bookmarks, search history, and an MCP tool call over HTTP with a real token — before calling it done.
11. Set up backups: a `pg_dump` cron job with a copy kept off the VPS (e.g. synced to the project owner's own machine), since a single VPS has no redundancy of its own.

### 16.6 Future scope — when to revisit this shape, not before

- **Oracle instance is lost anyway** (despite PAYG, or if PAYG is declined): fall back to Hetzner CX23 (§16.4) — the app/service shape doesn't change, only where it runs.
- **Corpus grows far beyond current size** (e.g. if a full historical backfill is ever done, per §11/§15's "not needed for now" call): Oracle's 200 GB absorbs this easily; Hetzner's 40 GB would not, and that's the point at which moving `data/raw`/`data/rendered` to Cloudflare R2 (egress-free, ~$0.015/GB-month) actually pays for the code change it requires. Not justified today either way. If it ever is, the shape to adopt is **VPS (app + Postgres co-located) + R2 (files only) + Cloudflare edge** — one change (a storage abstraction layer with local/S3 backends selected by env var), keeping Postgres local so search stays sub-millisecond. Explicitly *not* the fuller hybrid rejected in §16.2, which splits Postgres off too and turns every query into a network round trip.
- **Platform portability wanted for its own sake** (not needed today): the full change set is scoped — a storage abstraction over the ~10 call sites that touch the corpus (`crawler/http_client.py`, `crawler/ingest.py`, `crawler/render.py`, `service/queries.py`, `api/main.py`), moving rate-limit state out of process memory (`auth/rate_limit.py:17`'s in-process dict, and the equivalent in `mcp_server.py`), a `--max-items`/`--time-budget` flag so the crawler can run in bounded chunks, and a Dockerfile. That would make the project deployable on Cloudflare Containers, Fly.io, Railway, Render, Cloud Run, or a VPS interchangeably. Worth doing for independence, never worth doing to make Cloudflare Containers the host (§16.2).
- **Redundancy/uptime needs increase** beyond a single machine: a second VPS and/or a managed Postgres provider (e.g. Neon/Supabase, given how small the DB already is) — a bigger step, not needed at current single-user/small-team scale.
- **The UGREEN NAS becomes an option again**: re-read this section's "shelved" note above — the technical plan for it was already fully worked out and can be revived without redoing the analysis.
- **AI features wanted on top of the site** (semantic search, summarization): Cloudflare Workers AI (or any hosted-model API) becomes relevant *then*, as an addition — it is unrelated to, and doesn't change, how the app itself is hosted.
