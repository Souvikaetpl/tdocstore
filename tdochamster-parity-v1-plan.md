# TDocHamster Parity V1 — Scope-Locked Plan

## Objective

Build a clean-room internal implementation of the TDocHamster behavior already observed. Phase 1 will reproduce the existing meeting, TDoc, Inbox, search, text, analysis, and comparison experience. It will not add enterprise intelligence or unrelated product features.

The implementation may reproduce public behavior and interfaces, but it will not copy TDocHamster source code, branding, text, or proprietary assets.

## Exact V1 scope

### Data coverage

- Public 3GPP documents for 2025–2026.
- RAN plenary and RAN1–RAN5.
- SA plenary and SA1–SA6.
- CT plenary and CT1, CT3, CT4, and CT6.
- Submitted TDocs, meeting agenda items, and mutable meeting Inbox files.
- The official 3GPP website is the document source.

### MCP compatibility

Implement the same 11 public operations:

1. `service_info`
2. `list_meetings`
3. `list_tdocs`
4. `list_tdoc_agenda_items`
5. `search_tdocs`
6. `get_tdoc_text`
7. `get_tdoc_analysis`
8. `get_tdoc_diff_summary`
9. `compare_tdocs`
10. `browse_inbox`
11. `get_inbox_file_text`

Match the observed input parameters, result fields, pagination, status values, limits, and error behavior closely enough that an MCP client can switch endpoints without rewriting its workflow.

### Minimal web interface

- Meeting list with group filters.
- Meeting page with TDoc table and agenda items.
- Search page.
- TDoc metadata and extracted-text viewer.
- Inbox folder/file browser.
- Analysis and document-comparison views.

No additional user-facing modules will be added in V1.

## Explicitly out of scope

- Company-private documents, annotations, or positions.
- Advanced SSO roles or workspace permissions.
- Historical coverage before 2025.
- Patent, agreement, or standards-strategy workflows.
- Collaborative editing.
- Notifications and workflow automation.
- Analytics dashboards beyond basic operational logs.
- Mobile applications.
- New AI features not present in the observed TDocHamster interface.
- Bulk download, because it is disabled in the observed public service.

Infrastructure may still use the basic authentication, secrets, backups, and logging required to deploy safely; these are operational necessities rather than product features.

## Minimal architecture

```text
Official 3GPP meeting pages and files
                  |
          Meeting synchronizer
                  |
      Document cache and extractor
                  |
       PostgreSQL + text search
                  |
        Shared application API
           /              \
      Minimal web UI    MCP server
```

Use a modular application with background jobs. Do not introduce separate microservices, a vector database, or a complex event platform for V1 unless measured load proves they are necessary.

## Required internal components

### 1. Meeting synchronizer

- Discover supported meetings.
- Normalize meeting names and aliases.
- Retrieve the meeting TDoc list and agenda items.
- Browse the current Inbox directory structure.
- Detect new and changed files using path, size, modification time, and content hash.
- Retry temporary 3GPP failures with respectful backoff.

### 2. Document cache and extractor

- Download a document server-side when first needed or during background prefetch.
- Cache the original file so users do not download it manually.
- Extract text from the Office/PDF formats encountered in the selected corpus.
- Record extraction status and source metadata.
- Reprocess failed files after parser improvements.

### 3. Metadata and search

Minimal records:

- groups;
- meetings and meeting aliases;
- agenda items;
- TDocs;
- document files and versions;
- Inbox paths;
- extracted text;
- analysis and comparison cache entries;
- synchronization and extraction status.

Use exact metadata filtering and conventional full-text search first. Semantic/vector search is not required for TDocHamster V1 parity.

### 4. Analysis and comparison

- Generate a document analysis when no cached result exists.
- Cache generated results.
- Compare two selected TDocs.
- Generate a revision/difference summary where a predecessor is known.
- Apply usage limits and return fallback guidance comparable to the observed service.

### 5. Web and MCP adapters

The web interface and MCP server call the same application API. Neither maintains separate data or processing logic.

## Delivery sequence

### Day 1 — vertical slice

- One recent RAN meeting.
- Meeting list and TDoc listing.
- On-demand server-side download and text extraction.
- Basic search.
- Minimal text viewer.
- Initial MCP endpoint with `service_info`, `list_meetings`, `list_tdocs`, `search_tdocs`, and `get_tdoc_text`.

This is a working demonstration, not full parity.

### Week 1 — ingestion parity

- Implement source adapters for representative RAN, SA, and CT meetings.
- Add agenda items, pagination, canonical aliases, and extraction retries.
- Populate recent 2025–2026 meetings in controlled batches.
- Add fixture-based tests using the observed public response shapes.

### Week 2 — interface parity

- Implement Inbox synchronization and browsing.
- Complete all read/retrieval MCP tools.
- Complete the meeting, search, document, and Inbox web screens.
- Match validation errors, empty results, limits, and pagination behavior.

### Week 3 — generated-operation parity

- Implement analysis, comparison, and difference-summary operations.
- Add caching and session/daily usage limits.
- Complete corresponding web views.
- Test cold and warm response behavior.

### Weeks 4–6 — corpus and reliability parity

- Expand and verify all listed RAN, SA, and CT groups for 2025–2026.
- Fix group-specific meeting naming and source-layout cases.
- Test DOCX, PPTX, XLSX, PDF, TXT, and relevant ZIP cases.
- Reconcile sampled meeting and TDoc counts with official lists.
- Run compatibility tests against the captured TDocHamster fixtures.
- Deploy the internal endpoint and perform a controlled client switch.

## Acceptance criteria

V1 is complete when:

- all 11 MCP operations are implemented;
- the six web workflows listed above work end-to-end;
- the same client queries can be run against both endpoints;
- meeting and TDoc pagination is stable and produces no overlap;
- representative meetings from each supported group are discoverable;
- sampled TDoc counts match official 3GPP records or have a documented source discrepancy;
- supported cached documents return extracted text without a user download;
- Inbox navigation returns paths and file metadata comparable to the observed service;
- analysis and comparison results are cached and quota-controlled;
- common invalid inputs produce predictable structured results;
- a regression suite covers the captured response shapes and known naming anomalies.

## Build organization

If parallel coding agents are used, keep only four bounded lanes:

1. Source synchronization and normalization.
2. Extraction, storage, and search.
3. Web UI and application API.
4. MCP compatibility and regression tests.

One lead must own the shared schemas and merge the work daily. No lane may add product features outside this document.

## Estimate

- One-day demonstration: achievable for one meeting and the main retrieval path.
- Useful internal alpha: approximately 2–3 weeks.
- Broad 2025–2026 behavioral parity: approximately 3–6 weeks.

The MCP wrapper itself is small. The schedule is driven by 3GPP meeting normalization, file extraction, Inbox synchronization, and corpus verification.

## Deferred decision

Only after V1 parity is accepted should the company decide whether to add enterprise trust controls, private data, deeper history, or specialized 3GPP intelligence. Those are separate phases and are not part of this build.
