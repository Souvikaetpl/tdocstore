# tdocstore — Plan: a TDocHamster-equivalent database that updates and sorts live, verified independently by Astra (OpenAI model)

Status of the base: Stage 1 of `tdocstore` is built and tested (see README.md, IMPLEMENTATION_PLAN.md). This plan
covers what turns that index into a *live* database and how an independent model verifies it. Execute in order;
stop at each **CHECKPOINT**.

Terminology used below: "ground truth" = the 3GPP file server (`www.3gpp.org/ftp/...`) and the MCC `TDoc_List` xlsx.
Nothing else is authoritative; TDocHamster is a reference implementation, not ground truth.

---

## 0. What "live" means, decomposed (so it can be verified)

| Property | Definition (testable) | TDocHamster evidence |
|---|---|---|
| Freshness | A file that appears in a meeting's Docs folder is indexed within **F** minutes | in-meeting report `R2-2606030` extracted 2026-08-27 14:31, same afternoon it was uploaded |
| Completeness | For an ENDED meeting, `tdocs` rows == rows in `TDoc_List`; `extract_status=ready` for every file that exists on the server and is DOCX | `doc_count` 1,464 for R2-135 matches TDoc_List |
| Revision tracking | A re-uploaded file (same TDoc number, new content) is re-extracted and the old text replaced; the change is logged | not observable from Hamster's interface — we do better |
| Sort | Every list endpoint accepts a `sort` key and is deterministic: `tdoc_id`, `uploaded_at`, `last_modified`, `agenda_item` (numeric tuple), `source`, `title`; Inbox listing newest-first | Hamster: Inbox files "newest first"; TDoc lists in tdoc_id order |
| Provenance | Every row carries `file_url`, `file_sha256`, `server_last_modified`, `extracted_at`, `source_type` | Hamster exposes `extracted_at`, `source_type`, `hamster_url` |

Targets for v1: **F = 60 min during active windows, 24 h otherwise**. Active window = from TDoc deadline − 2 days to meeting end + 7 days (revisions, reports and chair notes keep arriving). Confidence that Hamster polls hourly-or-better: medium (inferred from one timestamp pair).

---

## 1. Stage L1 — Change detection without re-downloading (2–3 h of work)

Goal: a `sync` pass on an active meeting costs one listing request plus one download per *changed* file.

1. Extend `fetch.parse_listing()` to return `(name, size, last_modified)` when the listing page has date/size columns (both 3GPP front-ends show them). Keep the names-only path as fallback. **Verify the column format on the live server first and record it in NOTES.md.**
2. Add columns to `tdocs`: `server_size INTEGER`, `server_last_modified TEXT`, `first_seen_at TEXT`, `last_checked_at TEXT`, `revision_count INTEGER DEFAULT 0`.
3. In `ingest_meeting`: for each listed file, if `(size, last_modified)` equals the stored pair → skip download (`skipped_unchanged`); else download, sha256, and if the sha changed → re-extract, `revision_count += 1`, append a row to a new `change_log(tdoc_id, meeting_ref, event, old_sha, new_sha, server_last_modified, at)` table (`event` ∈ new / updated / removed / tdoclist_metadata_changed).
4. TDoc_List diff: on every pass, compare the parsed xlsx against `tdocs`; log metadata changes (status → withdrawn/revised, `revised_to` filled in, agenda item moved) as `tdoclist_metadata_changed`. This is the cheapest and most valuable "live" signal during a meeting.
5. `Store.changes(meeting_ref, since=None, limit=100)` and MCP tool `list_changes` — the feed a chat client or RIT uses to ask "what is new since 10:00".
6. Tests: mock server serves listing with dates; modify one file's bytes and one xlsx row between two `sync` calls; assert exactly one re-extract, one metadata change, correct `change_log`.

**CHECKPOINT L1** — show `tdocstore sync RAN2 --meeting R2-135` twice with a file modified in between; the report must say `fetched: 1, skipped_unchanged: N-1`.

---

## 2. Stage L2 — Scheduler (1–2 h)

1. `tdocstore schedule` command: reads `meetings.start_date/end_date` (plus a per-WG TDoc-deadline offset, default start − 10 days) and decides per meeting: **active** → sync every `TDOCSTORE_ACTIVE_INTERVAL` (default 30 min); **ended > 7 days** → weekly integrity pass (listing only, no downloads unless changed); **upcoming** → discover-only daily.
2. systemd service + timer on the VPS (`tdocstore-sync.timer`, `OnUnitActiveSec=30min`), single-instance lock (`flock`) so passes never overlap; one WG per process, run WGs sequentially to stay polite to the 3GPP server.
3. Meeting calendar: dates are not on the file server. v1: maintain `meetings.yaml` (group, number, start, end, location, tdoc_deadline) committed to the repo and imported by `tdocstore meeting import meetings.yaml`. v2 (optional): parse the 3GPP portal meeting list. Do not scrape the portal blindly; check its terms and whether an export exists.
4. Health: `tdocstore health` prints, per active meeting, `last_sync_at`, `files_on_server`, `files_indexed`, `ready`, `unsupported`, `errors`, `minutes_since_last_change`. Exposed as MCP `service_info()["health"]` and REST `/health` (plain 200/503 for uptime monitors).

**CHECKPOINT L2** — the timer has run unattended for 24 h on the VPS with an active meeting (or a mock server simulating uploads); `health` shows freshness ≤ 60 min at every check.

---

## 3. Stage L3 — Sorting and listing parity with TDocHamster (1 h)

1. `list_tdocs(..., sort="tdoc_id"|"uploaded_at"|"last_modified"|"agenda_item"|"source"|"title", order="asc"|"desc")`. Agenda sort uses the numeric tuple already implemented in `_agenda_key`. Default stays `tdoc_id asc` (matches Hamster and the MCC list).
2. `list_meetings(sort="start_date" desc)` default; add `status` derivation from dates on every read, not only at ingest (a meeting flips UPCOMING → ONGOING → ENDED without a sync).
3. Inbox (Stage 3 of the base plan): `browse_inbox` newest-first by `last_modified` from the listing; no per-file HEAD requests.
4. `search_tdocs(..., sort="relevance"|"tdoc_id"|"uploaded_at")` — relevance stays default.
5. Tests for every sort key on the fixture set, including the `8.9 < 8.10` case and ties.

---

## 4. Stage L4 — Verification by Astra (independent OpenAI model)

Principle: the verifier must not trust tdocstore's own claims. It gets **two independent views** — tdocstore (via MCP or REST) and ground truth (the 3GPP server, fetched by the verifier itself or via a tiny read-only "ground-truth proxy" that only lists and downloads, no database). It compares them and produces a signed report. tdocstore has no access to the verifier's results, so it cannot game them.

### 4.1 Give Astra access
- Connect Astra (ChatGPT custom connector / OpenAI Agents SDK / Responses API with MCP tool) to `https://tdocs.<domain>/mcp` with a **read-only verifier key** (scopes: read). Verify OpenAI's current connector requirements before relying on any auth mode.
- Ground truth: Astra fetches `https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_135/Docs/` itself (its browsing tool) or via `GET /truth/listing?meeting=R2-135` and `GET /truth/tdoclist?meeting=R2-135` on the ground-truth proxy — a separate 60-line service on the VPS that streams from 3gpp.org and caches for 5 min, with **no** access to the SQLite files.

### 4.2 Verification tools tdocstore exposes (deterministic, cheap; added to `store.py` + MCP)
| Tool | Returns |
|---|---|
| `verify_manifest(meeting_ref)` | every `tdoc_id` with `file_name, file_sha256, server_size, server_last_modified, extract_status, text_chars, extracted_at` — one page of ≤500 rows with cursor |
| `verify_sample(meeting_ref, n=20, seed)` | a seeded pseudo-random sample of TDocs with their first 300 chars of paragraph text and the sha256 of the full paragraph concatenation |
| `verify_change_log(meeting_ref, since)` | the change feed |
| `health()` | as in L2 |

Seeded sampling means Astra can pick the seed, so tdocstore cannot pre-select easy documents.

### 4.3 Checks Astra performs (each is pass/fail with numbers)
1. **Completeness**: set difference between `TDoc_List` ids (ground truth) and `verify_manifest` ids; both directions. Pass = 0 missing, ≤ 0.5 % `unsupported` explained by non-DOCX file types.
2. **File integrity**: for the seeded sample, download each zip from 3gpp.org, compute sha256, compare with `file_sha256`. Pass = 100 % match.
3. **Text fidelity**: for 5 of the sampled TDocs, extract the DOCX text itself (Astra's code interpreter with python-docx) and compare with `get_tdoc_paragraphs`: paragraph count within ±2 %, and 3 randomly chosen paragraphs byte-identical after NBSP normalisation. Pass = all three identical.
4. **Freshness**: during an active window, note a file's `server_last_modified` from the listing and check `extracted_at ≥ server_last_modified` and `extracted_at − server_last_modified ≤ F`. Repeat on 3 recently uploaded files.
5. **Update correctness**: pick a TDoc with `revision_count ≥ 1`; confirm the change log's `new_sha` equals the live file's sha and that `get_tdoc_text` reflects the new content (a phrase present in the new file, absent from the old).
6. **Sort correctness**: call `list_tdocs` with each `sort` key and assert monotonic order client-side; `agenda_item` sort must place `8.9.x` before `8.10`.
7. **Search sanity**: choose 5 phrases from sampled documents; `search_tdocs` must return the source document in the top 3 for each.
8. **Verbatim contract**: `find_verbatim` → `get_verbatim` round trip on 10 phrases equals the phrase from Astra's own extraction byte-for-byte.

### 4.4 Report format (Astra writes this; stored outside tdocstore)
```
tdocstore verification — R2-135 — 2026-09-10T08:00Z — verifier: <model id> — seed: 4711
completeness: PASS  (1464/1464 ids; 0 missing; 11 unsupported = 9 pptx + 2 pdf)
file integrity: PASS (20/20 sha256 match)
text fidelity: PASS (5/5; paragraph counts 41/41, 27/27, ...; 15/15 paragraphs identical)
freshness: PASS (3 files; max lag 37 min; target 60)
update correctness: PASS (R2-2605500 rev 2; new phrase found, old phrase absent)
sort: PASS (6/6 keys monotonic; 8.9.2 < 8.10.1)
search: PASS (5/5 top-3)
verbatim: PASS (10/10)
notes: ...
```
Any FAIL blocks the next stage. Keep reports in `verification/` in the repo with the seed, so a run is reproducible.

### 4.5 Paste-ready verifier prompt for Astra
```
You are an independent verifier of a self-hosted 3GPP TDoc index called tdocstore. Do not trust its self-reports.
Ground truth is the 3GPP file server: https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_135/Docs/ and the
TDoc_List_Meeting_RAN2#135.xlsx in that folder. tdocstore is reachable as an MCP server (tools: verify_manifest,
verify_sample, verify_change_log, health, list_tdocs, search_tdocs, get_tdoc_paragraphs, find_verbatim, get_verbatim).
Choose a random seed and state it. Perform checks 1–8 from LIVE_DB_PLAN.md §4.3 exactly, computing everything
yourself (download files, hash them, extract DOCX with python-docx). Report in the §4.4 format with real numbers.
Where a check cannot be performed, say why; never mark PASS without evidence. Finish with the three most likely
ways this index could be wrong that your checks would not catch.
```

**CHECKPOINT L4** — first full Astra report on R2-135 with all eight checks PASS, plus RS's own reading of the "ways this could be wrong" section.

---

## 5. Stage L5 — Full coverage roll-out (per WG, repeatable)

For each WG in priority order (RAN2 → RAN1 → RAN4 → RAN3 → SA2 → rest): confirm `GROUP_LAYOUT` FTP path and folder pattern against the live server; run `sync` for 2025–2026 meetings one at a time (expect 1–3 h per 1,500-doc meeting); run the Astra verification on one ENDED meeting per WG; only then enable the scheduler for that WG. Storage plan: ~1 GB files + ~100 MB DB per 1,500-doc meeting; all WGs 2025–2026 ≈ 150–250 GB files.

---

## 6. Traps specific to "live"
- The 3GPP listing `last_modified` is server time; store it verbatim as a string plus the UTC time you observed it. Do not convert without knowing the server timezone.
- Files are sometimes re-uploaded with identical content (sha unchanged) but a new date: treat as no change, log nothing.
- Withdrawn TDocs stay in TDoc_List with status `withdrawn` and no file, or the file vanishes: log `removed`, keep the row and last text (evidence for what was withdrawn).
- Revisions during meeting week (`Rxx` versions) are new TDoc numbers, linked by `is_revision_of` in TDoc_List; the *same* number being re-uploaded is a silent replacement — that is what `revision_count` catches and what Hamster does not expose.
- Never let the scheduler and a manual `ingest` run concurrently on one DB (WAL handles readers, not two writers with long transactions) — the `flock` in L2 is mandatory.
- A verifier that only reads tdocstore's own numbers verifies nothing. The ground-truth proxy must not import `tdocstore`.

## 7. Effort
L1 2–3 h · L2 1–2 h · L3 1 h · L4 3–4 h (tools + proxy + first run) · L5 compute-bound. Roughly two working days of build, then the crawl and verification cycles.
