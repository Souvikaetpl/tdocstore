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

Not yet run: interrupted-download recovery under real network failure (only tested via the "already exists" skip path so far).

---

## 10. Remaining phases — status as of v7 (several now done; unchanged in spirit from v4 where still open)

1. **Widen the crawl** — ✅ substantially done. All 18 tracked groups now ingest with real metadata: RAN plenary + RAN1–5 + RAN AH1, SA plenary + SA1–6, CT plenary + CT1/CT3/CT4/CT6 — not just the original RAN1–5/SA2/CT1 sample. Meeting selection was also rebuilt to pick by real date rather than by folder modified-time (§4). **Still open**: full historical backfill — each group still deliberately tracks only its current + previous meeting (keeps data fresh and correctly classified without a much larger download/extract/render workload), not the full historical archive this item originally envisioned. Whether to pursue that is a scope decision, not a bug.
2. **Shared service layer** — ✅ done. `service/queries.py` (§4): `search_tdocs`, `get_tdoc`, `compare_tdocs`, `list_meetings`, `get_stats`. Both the FastAPI backend and the MCP server call into it directly — no duplicated query logic anywhere.
3. **FastAPI backend** — ✅ done. `api/main.py` (§4): all the routes originally listed here, plus `/api/tdocs/{id}/view` for the rendered-PDF preview (§14) and the routes serving the frontend itself.
4. **MCP server** — ✅ built and verified, for local use; ⚠️ production hardening confirmed as real near-term scope, not deferred. `mcp_server.py` (§4) exposes the shared service layer as MCP tools over stdio, confirmed working end-to-end through both Claude Code and Antigravity IDE. See §13 (new in v7) for the concrete plan: transport switch to `streamable-http`, real hosting, and all four original hardening requirements (auth token, per-tool result limits, rate limiting, read-only DB role) — none of which exist yet, all of which are now confirmed-needed rather than local-use-only nice-to-haves.
5. **Frontend** — ✅ done for MVP scope. Homepage and search page (§4) — two-pane search/filter/preview/compare UI, plus a homepage with real per-WG meeting cards and a live Ongoing/Upcoming/Past status badge (§4's meeting-selection rebuild). Preview is now the PDF.js viewer from §14 (✅ done), not the old `<iframe>`. Diff and AI summaries remain explicitly post-MVP, unchanged from the original scoping.
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

So today, four independent levers exist: **disable the account** → blocks both website and MCP; **disable MCP access only** → blocks MCP (both self-service and OAuth-issued tokens, current and future), website login untouched; **revoke one MCP token** → blocks only that token; **nothing yet revokes only the website side** while leaving MCP untouched — no "kill this account's website sessions only" action is exposed anywhere yet.

**Not yet built:** personalization tables (saved searches/bookmarks) — the identity system they'd attach to now exists, but nothing is built on top of it yet.
- Rate-limiting on the OAuth callback, local-login, and test-account-creation endpoints specifically — a real gap, tracked in §15, not mitigated by anything above.

**What doesn't change regardless of the above**: this reopens the Auth question from §3, but not the Storage/Kong/Studio parts of that reasoning — self-hosted Supabase's *Auth* module (GoTrue) was evaluated against building directly on the existing Postgres, and the latter is what got built; adopting Supabase wholesale for Storage/Kong/Studio is still not justified by this alone (§3 unchanged on that point).

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
2. **Per-tool result limits, enforced server-side.** `search_tdocs(limit=...)` today defaults to 20 but doesn't stop a caller from requesting an arbitrarily large `limit` — a large, slow query and a large response payload, worse under concurrent abuse. `get_tdoc`/`compare_tdocs` return a document's full extracted text, which can be substantial per document and adds up fast across a `compare_tdocs` call with several IDs. Hardening means a hard server-side cap on `limit` regardless of what's requested, and likely a returned-text-length cap (with a note that full text is available through the direct API/download for anyone who needs it).
3. **Rate limiting**, per token/user — a cap like "N requests per minute," with a clean error beyond that. Without it, one token (compromised, or just a buggy client stuck in a loop) can degrade the service for every other real user, or directly cost money if hosted with metered bandwidth.
4. **A dedicated read-only Postgres role for the MCP server (and the FastAPI backend) to connect as** — distinct from whatever role the crawler uses to write. Today both read paths (`service/queries.py`) connect using the same credentials the crawler writes with; the code only ever issues `SELECT`, but nothing at the database level *enforces* that. A role granted only `SELECT` on the relevant tables means that even a future bug or a crafted input that tried to write would be rejected by Postgres itself, independent of whether the application code stays correct.
5. **No arbitrary SQL/filesystem tools** — a standing design constraint, already satisfied: all five tools are narrow and parameterized (search/get/compare/list/stats), none accept raw SQL or an arbitrary file path. Worth keeping explicit so it stays true as tools get added later, not because anything needs fixing today.

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
- **(New, v6) A public-facing login surface is new attack surface this project has never had.** Choosing Google sign-in (§12) deliberately minimizes this — no passwords for this project to ever store, reset, or have stolen — but session/token expiry and revocation, and rate-limiting on the OAuth callback and MCP-token-generation endpoints specifically, still need building and are real risk until they exist. Public signup (§12, decided open) also means anyone can create an account, so basic anti-abuse (e.g. capping how many MCP tokens or saved searches one account can create) is worth having even without password-related risk.
- **(New, v6) LibreOffice render pipeline hangs on specific document content** — confirmed, not hypothetical (§14): a Table-of-Contents field filtered by a custom paragraph style causes LibreOffice's layout engine to hang rather than fail fast. Mitigated by a 40-second timeout cap and per-conversion isolated profiles (§14); documents that hit this still end up with no rendered preview (the timeout makes them fail, not succeed) — a genuine content-based gap accepted for now, not a crash risk.
