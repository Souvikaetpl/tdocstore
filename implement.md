Below is a practical, end‑to‑end implementation plan to replicate a TDocHamster‑like platform: from data ingestion (3GPP TDocs) to storage, search, API, and frontend features (search, read, compare, annotate).

I want a **web app** first, and optionally an **MCP server** later for AI tools.

***

## 0. Scope & MVP definition

**MVP goals:**

- Support TDocs from selected groups (e.g. RAN2, SA2, CT4).
- Cover recent years (e.g. 2020–2026).
- Features:
  - Search by keyword, TDoc number, source, meeting, spec.
  - Filter by TSG, WG, year, doc type.
  - TDoc detail page with metadata + full text + download.
  - Compare 2–3 TDocs side‑by‑side.
  - Basic user accounts + annotations (notes/tags on TDocs).

Non‑MVP (later):

- Advanced analytics (top contributors, timelines).
- Spec‑centric views (all TDocs/CRs affecting a TS).
- MCP server for AI assistants.

***

## 1. Architecture overview

**Components:**

1. **Crawler / Ingestion Service**
   - Walks 3GPP FTP, downloads TDoc ZIPs + TDoc list Excel.
   - Parses metadata + text.
   - Stores raw files, metadata, and indexed text.

2. **Storage Layer**
   - Object storage: raw ZIPs + extracted text/HTML.
   - Relational DB: metadata (TDocs, meetings, specs, users, annotations).
   - Search engine: full‑text index (Elasticsearch/OpenSearch).
   - (Optional) Vector DB: for semantic search / AI.

3. **Backend API**
   - REST/GraphQL API for:
     - Search
     - TDoc detail
     - Compare
     - Annotations
     - Auth

4. **Frontend Web App**
   - Search + filters.
   - TDoc detail page.
   - Compare view.
   - Annotation UI.
   - User auth (login/register).

5. **(Optional) MCP Server**
   - Exposes TDoc search/retrieval tools to AI assistants.

***

## 2. Technology stack (suggested)

You can adjust, but this is a solid, common stack:

- **Backend:** Python + FastAPI
- **DB:** PostgreSQL
- **Search:** Elasticsearch or OpenSearch
- **Object storage:** Local disk (dev) → S3/R2/GCS (prod)
- **Frontend:** Next.js (React) or SvelteKit
- **Auth:** Simple email/password (JWT) or OAuth (Google/GitHub)
- **Task queue / scheduler:** Celery + Redis or simple cron (for crawler)
- **Deployment:** Docker + Docker Compose (dev), then Kubernetes or a managed service (prod)

***

## 3. Data model (core tables)

### 3.1 Meetings

```sql
CREATE TABLE meetings (
  id              BIGSERIAL PRIMARY KEY,
  tsg             TEXT NOT NULL,          -- 'RAN', 'SA', 'CT'
  wg              INT NOT NULL,
  name            TEXT NOT NULL,          -- e.g. 'RAN2#120'
  number          INT,                    -- 120
  date            DATE,
  location        TEXT,
  ftp_path        TEXT                    -- '/ftp/tsg_ran/WG2_RL2/TSGR2_120/'
);
```

### 3.2 TDocs

```sql
CREATE TABLE tdocs (
  t_doc_id        TEXT PRIMARY KEY,       -- 'R2-2506344'
  meeting_id      BIGINT REFERENCES meetings(id),
  tsg             TEXT NOT NULL,
  wg              INT NOT NULL,
  year            INT NOT NULL,
  title           TEXT NOT NULL,
  source          TEXT,
  doc_type        TEXT,                   -- 'Discussion Paper', 'CR', 'LS', ...
  related_specs   TEXT[],                 -- ['TS 38.331', 'TS 23.501']
  work_item_codes TEXT[],
  is_cr           BOOLEAN DEFAULT FALSE,
  cr_number       TEXT,
  cr_affected_spec TEXT,
  cr_release      TEXT,
  cr_clauses      TEXT[],
  cr_category     TEXT,
  file_path       TEXT NOT NULL,          -- 's3://bucket/tdocs/raw/R2/R2-2506344.zip'
  text_path       TEXT,                   -- 's3://bucket/tdocs/text/R2/R2-2506344.txt'
  html_path       TEXT,                   -- optional rendered HTML
  ingested_at     TIMESTAMPTZ DEFAULT NOW()
);
```

### 3.3 Users & Annotations

```sql
CREATE TABLE users (
  id              BIGSERIAL PRIMARY KEY,
  email           TEXT UNIQUE NOT NULL,
  password_hash   TEXT NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE annotations (
  id              BIGSERIAL PRIMARY KEY,
  user_id         BIGINT REFERENCES users(id),
  t_doc_id        TEXT REFERENCES tdocs(t_doc_id),
  text_offset_start INT,
  text_offset_end   INT,
  note            TEXT,
  tags            TEXT[],
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

You can add more tables later (specs, work items, etc.).

***

## 4. Crawler & ingestion pipeline

### 4.1 Crawler design

**Language:** Python (you’re comfortable with it).

**Libraries:**

- HTTP: `httpx` or `requests`
- Excel: `openpyxl` or `pandas`
- ZIP: `zipfile`
- Word: `python-docx`
- PDF: `pypdf` or `PyMuPDF`
- Storage: `boto3` (S3) or local filesystem

**Steps:**

1. **Define scope:**
   - List of WG paths, e.g.:
     - `/ftp/tsg_ran/WG2_RL2/`
     - `/ftp/tsg_sa/WG2/`
     - `/ftp/tsg_ct/WG4/`
   - Year range (e.g. 2020–2026).

2. **Discover meetings:**
   - For each WG path:
     - List subdirectories (meeting folders like `TSGR2_112`, `TSGSA2_150`).
     - Filter by year (from folder name or metadata).
     - Insert into `meetings` table.

3. **Download TDoc list Excel:**
   - For each meeting folder:
     - Fetch `Docs/TDoc_List_Meeting_*.xlsx`.
     - Parse rows:
       - TDoc, Title, Source, Type, Remarks.
     - Use this as the primary metadata source. [3gpp](https://www.3gpp.org/DynaReport/TDocExMtg--R2-112-e--39298.htm)

4. **Download TDoc ZIPs:**
   - For each TDoc entry:
     - Construct expected filename: `<TDocNumber>.zip`.
     - Check if already downloaded (via DB or storage).
     - If not, download and save to:
       - Local: `/data/tdocs/raw/{TSG}/{TDocNumber}.zip`
       - Or S3: `s3://bucket/tdocs/raw/{TSG}/{TDocNumber}.zip`

5. **Parse each TDoc:**
   - Unzip to temp.
   - Identify main document (`.docx` or `.pdf`).
   - Extract:
     - Full text.
     - Optional: structured sections.
   - Optionally parse header tables for:
     - Related specs.
     - CR metadata (if `doc_type == 'CR'`). 

6. **Store processed data:**
   - Upload:
     - Raw ZIP → object storage.
     - Extracted text → object storage (`text/.../*.txt`).
     - Optionally HTML → object storage.
   - Insert/update `tdocs` table with:
     - Metadata (from Excel + parsed doc).
     - Paths to stored files.

7. **Index in Elasticsearch:**
   - Create an index `tdocs` with fields:
     - `t_doc_id` (keyword)
     - `title` (text + keyword)
     - `source` (text + keyword)
     - `tsg`, `wg`, `year` (keyword/int)
     - `meeting_name` (keyword)
     - `doc_type` (keyword)
     - `related_specs` (keyword[])
     - `full_text` (text)
   - For each TDoc, index a document.

8. **Schedule updates:**
   - Run crawler periodically (e.g. weekly) to ingest new meetings.

***

## 5. Backend API design

Use **FastAPI** (or Django/Fiber/etc.):

### 5.1 Core endpoints

- `GET /api/tdocs`
  - Query params: `q`, `tsg`, `wg`, `year_from`, `year_to`, `source`, `doc_type`, `spec`, `meeting`, `page`, `size`.
  - Calls Elasticsearch with:
    - `match` on `q` (title + full_text).
    - Filters on other fields.
  - Returns:
    - List of TDoc summaries (id, title, source, meeting, type, year, snippet).

- `GET /api/tdocs/{t_doc_id}`
  - Lookup in DB by `t_doc_id`.
  - Return:
    - Metadata.
    - Signed URL or proxy endpoint for:
      - Raw ZIP download.
      - Text/HTML view.

- `GET /api/meetings`
  - Filters: `tsg`, `wg`, `year`.
  - Returns list of meetings with counts.

- `GET /api/meetings/{meeting_id}/tdocs`
  - List TDocs for a meeting (with pagination, filters).

- `GET /api/tdocs/compare`
  - Params: `t1`, `t2`, [`t3`].
  - Fetch metadata + text for each.
  - Return structured data for side‑by‑side rendering.

- Auth + annotations:
  - `POST /api/auth/register`
  - `POST /api/auth/login`
  - `GET /api/tdocs/{t_doc_id}/annotations` (auth required)
  - `POST /api/tdocs/{t_doc_id}/annotations`
  - `PUT /api/annotations/{id}`
  - `DELETE /api/annotations/{id}`

You can add more later (specs, work items, stats).

***

## 6. Frontend implementation plan

### 6.1 Pages

1. **Home / Search**
   - Search bar.
   - Filters: TSG, WG, year range, source, doc type, spec, meeting.
   - Results list:
     - TDoc ID, title, source, meeting, type, year.
     - Short snippet from full text.

2. **TDoc Detail**
   - Metadata section:
     - TDoc ID, title, source, meeting, type, related specs.
   - Full text viewer:
     - Rendered HTML or formatted text.
   - Actions:
     - Download original ZIP.
     - Add annotation (if logged in).
     - “Add to compare”.

3. **Compare View**
   - Select 2–3 TDocs (from search or detail pages).
   - Show:
     - Metadata table.
     - Side‑by‑side text panes.
     - Optional diff highlight if texts are similar.

4. **Meeting Browse**
   - List meetings by TSG/WG/year.
   - Click into a meeting → list of TDocs.

5. **User Pages**
   - Login / Register.
   - Profile (optional).
   - “My annotations” list.

### 6.2 Tech

- Next.js (React) or SvelteKit.
- Use your backend API.
- State management: simple (React Query / SWR) is enough.

***

## 7. Step‑by‑step timeline (example)

Assume part‑time work; adjust as needed.

### Week 1–2: Foundation

- Set up repo, Docker Compose (Postgres, Elasticsearch, backend, frontend).
- Implement basic DB schema (meetings, tdocs, users, annotations).
- Implement minimal FastAPI:
  - Health check.
  - Basic CRUD for TDocs (manual insert for testing).

### Week 3–4: Crawler v1

- Implement crawler for 1–2 WGs (e.g. SA2, CT4), recent 2–3 years.
- Download TDoc list Excel + TDoc ZIPs.
- Parse metadata + text.
- Store in local FS + Postgres.
- Index in Elasticsearch.
- Verify via direct ES queries.

### Week 5–6: API + basic frontend

- Implement:
  - `GET /api/tdocs` (search).
  - `GET /api/tdocs/{id}`.
  - `GET /api/meetings`.
- Build frontend:
  - Search page with filters.
  - TDoc detail page (metadata + text).
- End‑to‑end test: search → open TDoc.

### Week 7: Compare + annotations

- Implement:
  - `GET /api/tdocs/compare`.
  - Auth (JWT or session).
  - Annotations endpoints.
- Frontend:
  - Compare view.
  - Annotation UI (highlight + note).
  - Login/register pages.

### Week 8: Polish + deploy

- Improve parsing (better CR handling, related specs).
- Add error handling, logging, monitoring.
- Deploy to a cloud VM or managed service:
  - Docker Compose or Kubernetes.
  - Configure S3/R2 for storage.
- Do a small internal beta with a few users.

***

## 8. Optional: MCP server layer (phase 2)

Once the core platform works:

1. Choose an MCP SDK (Python/TS).
2. Implement MCP tools:
   - `search_tdocs(query, filters)`
   - `get_tdoc(t_doc_id)`
   - `list_meetings(tsg, wg, year)`
   - `compare_tdocs([ids])`
3. Connect MCP server to your existing API/DB.
4. Test with Claude Desktop / other MCP‑capable clients.

This turns your TDoc platform into an AI‑readable knowledge base.

***

## 9. Key risks & mitigations

- **Crawler robustness:** 3GPP folder structures may vary.
  - Mitigation: start with a few WGs, log failures, add retries, make paths configurable.
- **Parsing quality:** TDocs have varied templates.
  - Mitigation: start with metadata from Excel; gradually improve doc parsing; don’t block MVP on perfect parsing.
- **Storage cost:** Many TDocs over many years.
  - Mitigation: start with limited scope; compress text; use cheap object storage.
- **Legal / terms:** 3GPP content has copyright/IPR.
  - Mitigation: clearly attribute 3GPP, don’t claim ownership, review terms if you commercialize.
