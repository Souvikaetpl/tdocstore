# 3GPP TDoc Replica — Implementation Plan (v5)
### Plain PostgreSQL + local disk → MinIO, verified against the live 3GPP site, MCP-ready

---

## 0. What changed in this version

v4 proposed self-hosted Supabase (Postgres + Storage + Kong + `pg_cron`/`pgmq`) and listed a Phase 0 verification list as mostly *unverified*, built from a sandbox with no access to the live 3GPP site. Since v4 was written, the crawler has actually been built and run against the real site — real HTTP requests, real downloads, real text extraction, real Postgres storage. Several of v4's "confirmed" facts turned out to be **wrong**, not just unverified (see §2). v5 replaces those assumptions with what was actually observed, and replaces the backend architecture with the simpler stack that's already running and already has real data in it.

**What this version does differently:**
1. Drops self-hosted Supabase as the backend platform. Plain Postgres (already installed and in use) + local filesystem now, MinIO later. See §3 for why.
2. Corrects the live-site facts in §2 with what was actually verified — folder structure, `robots.txt`, TDoc list Excel schema, zip contents — replacing several incorrect assumptions v4 carried from an unverified build.
3. Documents the crawler that already exists and has been run end-to-end (§4), instead of describing one still to be built.
4. Keeps the parts of v4 that were genuinely good ideas independent of the backend choice: the two-pane UI target, MCP security hardening (auth token, per-tool limits, read-only DB role), and the general phase discipline (verify → backfill → shared service layer → MCP → API → frontend).
5. Notes what's reusable from the separately-built `tdocstore` package and what isn't (§11) — its text-normalization logic is sound and worth adopting; its crawler/parser logic was never run against the live site and encodes at least one confirmed-wrong assumption.
6. Adds a universal cover-page metadata fallback (§8) after discovering the Excel TDoc list is RAN2-specific, not RAN-wide or 3GPP-wide as originally assumed.

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

- **Auth** — not needed. This project has no login (confirmed as an explicit target in v4 itself, §"no login"). Auth/GoTrue is Supabase's single biggest differentiator over plain Postgres, and it solves a problem this project doesn't have.
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
- **`db.py`** — Postgres schema (`meetings`, `tdocs`) and upsert logic, migrated from an initial SQLite prototype once the crawl/extract logic was proven. Also: change-detection (`get_tdoc_file_state`) and cover-page backfill (`backfill_metadata_from_coverpage`, gap-filling only, never overwrites real data).
- **`ingest.py`** — orchestrates discovery → metadata merge → download → extraction → cover-page backfill, idempotent (skips already-downloaded files unless the server-side file changed, tracks per-TDoc extraction status).
- **`main.py`** — CLI: `python main.py --wg ran:RAN2 --meetings-per-wg N [--download --max-files N] [--extract] [--refresh]`.

**Proven results**: RAN2#135 fully ingested — 1,452 TDocs with real metadata (196 CRs, LS in/out with routing, discussion papers, agenda items), 19 zips downloaded and text-extracted (19/19 success), all cross-verified directly in pgAdmin against the crawler's own reported counts.

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

## 10. Remaining phases (unchanged in spirit from v4, adjusted for the real backend)

1. **Widen the crawl** — done for a first pass: all of RAN (RAN1–RAN5) plus SA2 and CT1 now ingesting with real metadata (§8). Remaining: more meetings per group (currently 2 per group), more SA/CT working groups, full historical backfill rather than samples.
2. **Shared service layer** — one Python module for search/read/compare, called by both the future FastAPI backend and MCP server. Never two independent query implementations.
3. **FastAPI backend** — `/api/tdocs`, `/api/tdocs/{id}`, `/api/meetings`, `/api/tdocs/compare`, over the Postgres data already populated. Serves file downloads via direct links (local path now, MinIO public URL later) rather than proxying bytes.
4. **MCP server** — reuse the shared service layer. Needs, beyond local stdio: a shared auth token for remote access, per-tool result limits (max search results, max documents per compare, max returned text length), rate limiting, read-only DB role, no arbitrary SQL/filesystem tools. (These specific hardening requirements are the one part of v4's MCP section worth keeping as-is.)
5. **Frontend** — two-pane UI (document list + filters on the left, preview on the right), matching the confirmed TDocHamster reference screenshot. Compare view for MVP; Diff and AI summaries explicitly post-MVP.
6. **Only if/when real scale demands it**: `pg_cron`/`pgmq` on the existing Postgres for per-job retry isolation and parallel workers across many groups (§6 already covers the basic "what's new/changed" refresh without needing either); MinIO for object storage in place of local disk; connection pooling (PgBouncer) once concurrent public traffic is real.

---

## 11. What to do with the separately-built `tdocstore` package

`tdocstore/` (a fuller package with its own CLI, MCP server, and revision-diff logic) exists in git history but is deleted from the working tree. Its own README states it was *"written against the known layout but could not be verified against the live server from the build sandbox."* That shows in practice: `tdocstore/tdoclist.py` assumes the TDoc list lives in `Docs/` as `TDoc_List_Meeting_<WG>#<n>.xlsx` — confirmed wrong in §2 (real location: `Tdoclists/`, timestamped snapshots). Its crawler/parser layer should not be restored over the verified `crawler/` implementation.

**What is worth reusing from it, later, deliberately** (pulled from git history, not restored wholesale):
- `textnorm.py` — pure text-cleanup logic, doesn't touch site structure, directly relevant to §7.
- Its CLI shape (`search`, `agenda`, `prev --diff`) and MCP tool structure — reasonable design references for the MCP server and Frontend phases in §10 above.
- Its revision-diff approach — relevant once `is_revision_of`/`revised_to` (already captured in `tdocs`) gets a Compare/Diff feature built on top.

---

## 12. Key risks & mitigations (updated)

- **Folder/file naming inconsistency across groups** — confirmed, not just anticipated (§2). `crawler/config.py` hardcodes verified paths per group rather than assuming a pattern; date-from-modified-time rather than date-from-folder-name is already the implemented approach.
- **Running unnecessary infrastructure** — avoided by not adopting Supabase (§3); the stack stays at one Postgres + one file store.
- **Legal/copyright** — unchanged from earlier versions: attribute 3GPP clearly, don't claim ownership, treat 3GPP's explicit anti-AI-crawler `robots.txt` stance as a real signal (identify the crawler honestly, rate-limit it, don't spoof a browser or impersonate a blocked bot name) rather than a footnote.
- **`tdocstore`'s unverified assumptions re-entering the project** — mitigated by this document (§11): reuse specific modules deliberately, never restore the package wholesale as if it were validated.
- **Stale extracted text after a document is replaced server-side** — mitigated by §6's modified-time change detection; verified working end-to-end.
- **Groups without a structured TDoc list producing no usable metadata** — mitigated by §8's cover-page fallback; accepted as heuristic (occasional wrong guess on administrative documents) rather than blocking on a per-TSG parser for every export format.
