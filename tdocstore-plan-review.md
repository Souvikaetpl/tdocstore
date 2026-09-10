# tdocstore implementation-plan review

**Verdict: clear architectural direction, but not yet a consistent implementation contract. Revise before handing it to an executing agent.**

Reviewed the `tdocstore/` contents of `C:\Users\shrivastavar\Downloads\tdocstore-stage1_1.zip`, especially `IMPLEMENTATION_PLAN.md`, `LIVE_DB_PLAN.md`, `README.md`, the core implementation and acceptance tests. Source references below use paths and line numbers inside the archive. This is a review, not execution of the plans; the original archive was unchanged.

## What is already clear

- The product goal, initial RAN2 scope, SQLite-per-working-group storage and MCP/REST/Python consumers are explicit.
- Separating retrieved evidence from generated analyses is a sound, clearly stated requirement.
- The base plan identifies functions, files, example commands, fixtures and human review checkpoints.
- The live plan turns “live” into measurable properties and requires comparison with independently retrieved source files.
- Live-server validation is explicitly outstanding. Keeping that gate is appropriate.

The main problem is agreement between these requirements, their sequencing and the actual starting code.

## Findings that should be resolved before execution

### 1. High — There is no unambiguous next step or combined stage order

**Evidence:** `IMPLEMENTATION_PLAN.md:28` says start at Stage 2; line 47 says the live run and Checkpoint 1 remain outstanding; line 219 tells the agent to execute Stage 1 and write code that already exists. Stage 2 schedules previous-version matching, but `store.py:296` already implements it. The live plan separately says execute L1–L5 in order, although L2 needs VPS deployment and L3 needs Inbox functionality from base Stage 3.

**Impact:** Different agents could reasonably rebuild completed work, bypass the live-quality gate, or reach a checkpoint before its prerequisites exist.

**Required edit:** Add one authoritative status table and one combined dependency order. Make the immediate task “validate and repair the existing Stage 1 implementation, perform the first live run, then report at Checkpoint 1.” Replace the kickoff prompt. Mark existing previous-version functionality as implemented but requiring review, rather than absent.

Also remove stale interface references: “five existing files”; `tdocstore ingest RAN2 135` versus the actual CLI's `tdocstore ingest R2-135`; and `delete_tdoc_sections()`/`reindex_fts()` versus the actual external-content FTS triggers and helpers in `db.py`.

### 2. High — The freshness target contradicts the scheduler and its measurement

**Evidence:** `LIVE_DB_PLAN.md:22` promises 60 minutes in active windows and 24 hours otherwise. Line 43 schedules ended meetings only weekly and upcoming meetings for discovery only. Line 84 measures freshness using `extracted_at − server_last_modified`, while line 128 says the server timezone is unknown. Line 129 says identical-content reuploads produce no content change.

**Impact:** Weekly checks cannot guarantee 24-hour indexing. An identical-content reupload can legitimately retain its old extraction timestamp and fail the proposed freshness test. Historical backfill will also need different treatment from a newly observed update.

**Required edit:** Specify separate service targets for active meetings, inactive meetings and initial backfill. Define active-window precedence over UPCOMING status and handling of unknown dates. Record successful listing checks, first observation of a new revision, and successful indexing separately. Define how source timestamps are interpreted, and report an unmeasurable check explicitly when the timezone is unverified. Include enough timing budget for downloads, extraction, retries and queued working groups.

### 3. High — Change detection needs a complete state-transition specification

**Evidence:** `LIVE_DB_PLAN.md:30–34` skips downloads when size/mtime match and re-extracts when the SHA changes. The current download path returns any nonempty local file as cached (`fetch.py:131–139`); online ingest uses that default (`ingest.py:263`). Thus current online reruns do not detect server-side replacements of cached files, despite the README's broad changed-file claim.

**Impact:** The L1 implementation must explicitly replace this cache behavior. Matching metadata must not prevent retrying failed extraction, processing a newly supported file, or re-extracting after an extractor upgrade. Missing listing metadata could otherwise become `(None, None)` and appear permanently unchanged. Equal size/mtime also needs a stated residual-risk policy.

**Required edit:** Provide a decision table covering new files, changed validators, unchanged validators, missing validators, identical hashes with changed metadata, extraction failure, withdrawal/removal and reappearance. Define a fallback revalidation policy, update observed metadata even if content hashes match, and specify when forced reprocessing occurs. Decide whether files visible before their TDoc_List row are discovered/indexed: the current file-appearance freshness promise is broader than the list-driven ingestion algorithm.

**Acceptance:** A controlled HTTP fixture changes bytes under the same filename; the next sync fetches and replaces the indexed text. Separate cases verify retries, metadata-only updates, unavailable timestamps and removal/reappearance.

### 4. High — “Verbatim” does not yet have a consistent, satisfied contract

**Evidence:** The base plan promises faithful DOCX paragraph text and exact lookup, while Stage 2 also requires normalized lookup. `extract.py:66` strips paragraph edges and line 147 combines table-cell paragraphs with spaces. The optional Docling path derives paragraph records from markdown (`extract.py:244`), which conflicts with the blanket no-markdown evidence rule. `textnorm.py:36` normalizes the entire string to NFC before recording original offsets.

**Confirmed locally:** With original text `Cafe\u0301 proposal`, the supplied offset mapper locates `proposal` but slicing the original at those offsets returns ` proposa`. A generated DOCX also confirmed stripped edge spaces, merged cell paragraphs and omission of a tracked insertion, despite the documented accepted-text claim.

**Required edit:** Define the canonical text representation, table/member boundaries, whitespace handling and tracked-change policy. Specify offsets as Unicode character indices with an exclusive end, rather than implying byte offsets. Keep raw evidence separate from normalized matching, with mappings back to original spans. Distinguish exact-match and normalized-match behavior; the latter returns the original source span, which may differ from the query. State which extractor outputs qualify as verbatim evidence.

**Acceptance:** Add decomposed Unicode, normalization expansion/deletion, leading/trailing whitespace, multi-paragraph table cells, tracked insertions/deletions and multi-document ZIP cases. Round trips must compare against independently specified source text, not only the index's own text.

### 5. High — Database upgrades and atomic replacement are unspecified

**Evidence:** L1 adds schema columns and a change log after a populated Stage 1 database exists. `db.py:121–127` only creates missing objects and inserts schema version 1; it does not migrate existing tables. `_process_file()` can update the file hash and error status while old text remains after a failed replacement extraction.

**Impact:** Editing the CREATE TABLE schema alone will not upgrade an existing installation. A failed update can associate a new file hash with previously indexed text unless version relationships are explicit.

**Required edit:** Add a versioned migration task with a backup/restore procedure and a populated-v1 upgrade acceptance test. Define atomic publication of content, matching source hash, extraction metadata and change-log event. Retain or label the previous good revision when new extraction fails. Define recovery from a crash between file download and database commit.

### 6. High — The read-only API rule conflicts with actual write behavior

**Evidence:** `IMPLEMENTATION_PLAN.md:17` says ingestion is the only writer. `Store.previous_version()` calls `_compute_previous()`, inserts links and commits (`store.py:296–340`). Store connections also call the schema-initializing `db.connect()`. The live plan requires scheduler/manual-ingest locking, but does not resolve these writes from read endpoints.

**Impact:** HTTP and verifier reads can write to SQLite; a lock around the scheduler alone cannot enforce the stated single-writer architecture. Cached links also need invalidation when TDoc_List revision metadata or predecessor data changes.

**Required edit:** Move persisted link computation into ingestion or an explicit maintenance writer, define invalidation, and make read connections read-only. Alternatively, explicitly revise the architecture and design locking for every writer. Specify the connection lifetime/threading model for REST and MCP request handling.

### 7. Medium — API, RIT and remote-client contracts are still sketches

**Evidence:** `IMPLEMENTATION_PLAN.md:159–167` says one REST route per Store method and proposes a byte-capped batch response with a `next` cursor. It does not define all routes, schemas, error mappings, cursor semantics or handling of one TDoc larger than the cap. The universal provenance rule at line 23 is neither scoped to document-bearing results nor consistently implemented. RIT source code and an input/output fixture are absent from the archive. Auth is mentioned as Stage 2 work at line 113 and Stage 3 work later.

**Required edit:** Add example request/response schemas, required versus nullable provenance fields, stable sort tie-breakers, null ordering, cursor behavior during live changes and oversized-document behavior. Define batch partial failures. Supply the RIT repository/revision and a representative downstream fixture so “keep downstream unchanged” can be tested. Define auth selection, scope enforcement, revocation and client compatibility as explicit tasks before public exposure; keep current client-requirement verification as a prerequisite.

### 8. Medium — Independent verification has contradictory and incomplete pass criteria

**Evidence:** `LIVE_DB_PLAN.md:81` permits at most 0.5% unsupported documents, but the sample PASS at line 93 has 11/1464 = **0.751%**. Update correctness requires comparing old and new content, but old bytes/text are replaced and only hashes are specified for the log. L4 requires all eight checks to pass even if there is no suitable live replacement during the test window. The “signed report” has no defined signature mechanism.

**Required edit:** Correct the numeric example and separate metadata completeness, available-file coverage and supported-format extraction success. Retain revision evidence or use controlled old/new fixtures for update verification. Define PASS/FAIL/NOT TESTED and which untested checks block rollout. Snapshot source manifests and downloaded samples so later server updates do not create false hash mismatches. Define paragraph enumeration independently, including tables and ZIP members. Draw the sample from the independently obtained manifest if resistance to selective reporting is a requirement. Either define report signing or call it an attributed report.

Common phrases can legitimately match many TDocs, so qualify the top-three search criterion with selected distinctive phrases or a documented relevance expectation.

### 9. Medium — “13 passing tests” is insufficient evidence for the declared completion state

**Evidence:** The archive contains 13 test functions, but `tests/test_stage1.py:127` ends an assertion with `or True`, making that assertion incapable of failing for incorrect values. The online test verifies repeated unchanged files, not server-side replacement. The real trimmed XLSX fixture required by the plan is absent. The Unicode and tracked-insertion probes above reveal uncovered cases.

**Required edit:** Distinguish “synthetic tests reportedly passed” from “Stage 1 accepted against live data.” Remove the vacuous assertion, add the missing contract tests, record tested dependency versions and provide a reproducible test installation command. Tie each checkpoint to evidence files and objective pass criteria, including a stated criterion for the 30-document previous-version accuracy sample.

## Suggested consolidated execution order

| Step | Work | Exit evidence |
|---|---|---|
| 0 | Reconcile both plans with the delivered code; settle contracts above | One status table, dependency order and corrected kickoff prompt |
| 1 | Repair baseline correctness and validate real listing/XLSX formats | Focused tests, small live ingest, then Checkpoint 1 report |
| 2 | Migrations, update state machine, revision evidence and writer control | Populated-v1 migration plus controlled replacement/failure tests; L1 |
| 3 | Previous-link hardening, Store/API schemas, REST and RIT integration | Defined accuracy sample and one agenda item consumed end-to-end; Checkpoint 2 |
| 4 | Inbox, sort behavior, deployment/auth and client wiring | Deterministic pagination/sort checks, client calls and demonstrated revocation; L3 and Checkpoint 3 |
| 5 | Scheduler and health against reconciled freshness targets | Unattended 24-hour evidence; L2 |
| 6 | Independent verification, then staged backfill | Reproducible L4 report, then L5 per working group |

Keep Stage 4 LLM enrichment optional and outside the required retrieval rollout. Re-estimate after the live-format check and correctness work: the listed 1–3-hour subtasks do not include all the migration, recovery and acceptance work identified here, and the scheduler gate itself needs 24 hours of observation.

## Validation limits

I inspected the supplied implementation and tests and ran small local probes using the bundled Python runtime and python-docx. I did **not** rerun the full pytest/MCP suite: pytest, httpx and mcp are absent from that runtime. I did not install dependencies, crawl the live meeting, verify claimed TDocHamster behavior, or test VPS/client integration. The review therefore does not certify the claimed 13 passing tests or live interoperability.
