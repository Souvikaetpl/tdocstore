from __future__ import annotations
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = f"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS meetings (
  meeting_ref   TEXT PRIMARY KEY,   -- R2-135, R2-133-bis (TDocHamster-compatible)
  grp           TEXT NOT NULL,      -- RAN2
  number        TEXT NOT NULL,      -- 135, 133-bis
  title         TEXT,
  start_date    TEXT, end_date TEXT,
  location      TEXT, country TEXT,
  ftp_folder    TEXT,               -- TSGR2_135
  docs_url      TEXT,               -- absolute URL of the Docs folder
  doc_count     INTEGER DEFAULT 0,
  status        TEXT,               -- UPCOMING/ONGOING/ENDED (derived from dates)
  ingested_at   TEXT
);

CREATE TABLE IF NOT EXISTS tdocs (
  tdoc_id        TEXT PRIMARY KEY,  -- R2-2604936
  meeting_ref    TEXT NOT NULL REFERENCES meetings(meeting_ref),
  title          TEXT, source TEXT, contact TEXT,
  document_type  TEXT,              -- discussion / CR / LS in / draft / ...  (from TDoc_List 'Type')
  document_for   TEXT,              -- Discussion / Decision / Approval / Information
  agenda_item    TEXT, agenda_desc TEXT,
  tdoc_status    TEXT,              -- available / withdrawn / noted / revised ...
  is_revision_of TEXT, revised_to TEXT,
  release        TEXT, spec TEXT, spec_version TEXT, related_wis TEXT,
  cr_number      TEXT, cr_category TEXT,
  uploaded_at    TEXT,
  file_name      TEXT,              -- name in Docs folder (R2-2604936.zip)
  file_url       TEXT,
  file_sha256    TEXT,
  fetch_status   TEXT DEFAULT 'pending',   -- pending / fetched / missing / error
  extract_status TEXT DEFAULT 'pending',   -- pending / ready / unsupported / error
  source_type    TEXT,              -- docx-fast / pptx-docling / pdf-docling / ...
  extracted_at   TEXT,
  text_chars     INTEGER,
  error          TEXT
);
CREATE INDEX IF NOT EXISTS ix_tdocs_meeting ON tdocs(meeting_ref);
CREATE INDEX IF NOT EXISTS ix_tdocs_agenda ON tdocs(meeting_ref, agenda_item);
CREATE INDEX IF NOT EXISTS ix_tdocs_source ON tdocs(meeting_ref, source);

-- Whole-document markdown-ish rendering (headings, bold, tables). Display/LLM use.
CREATE TABLE IF NOT EXISTS tdoc_text (
  tdoc_id  TEXT PRIMARY KEY REFERENCES tdocs(tdoc_id) ON DELETE CASCADE,
  markdown TEXT NOT NULL,
  truncated INTEGER DEFAULT 0
);

-- Faithful paragraph-level text straight from the DOCX body (no markdown decoration).
-- This is the verbatim-evidence source. Offsets index into the newline-joined concatenation.
CREATE TABLE IF NOT EXISTS paragraphs (
  tdoc_id  TEXT NOT NULL REFERENCES tdocs(tdoc_id) ON DELETE CASCADE,
  idx      INTEGER NOT NULL,
  style    TEXT,                    -- docx style name (Heading 1, Normal, List Paragraph, TableCell...)
  section_idx INTEGER,              -- which section this paragraph belongs to
  char_start INTEGER, char_end INTEGER,
  text     TEXT NOT NULL,
  PRIMARY KEY (tdoc_id, idx)
);

-- Section split on headings; searched via FTS.
CREATE TABLE IF NOT EXISTS sections (
  id          INTEGER PRIMARY KEY,
  tdoc_id     TEXT NOT NULL REFERENCES tdocs(tdoc_id) ON DELETE CASCADE,
  meeting_ref TEXT NOT NULL,
  idx         INTEGER NOT NULL,
  heading     TEXT,
  level       INTEGER,
  body        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sections_tdoc ON sections(tdoc_id);

-- External-content FTS5 over sections; triggers keep it in sync, so writers only touch `sections`.
CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
  heading, body, tdoc_id UNINDEXED, meeting_ref UNINDEXED,
  content='sections', content_rowid='id', tokenize="unicode61 tokenchars '/-_'"
);
CREATE TRIGGER IF NOT EXISTS sections_ai AFTER INSERT ON sections BEGIN
  INSERT INTO sections_fts(rowid, heading, body, tdoc_id, meeting_ref) VALUES (new.id, new.heading, new.body, new.tdoc_id, new.meeting_ref);
END;
CREATE TRIGGER IF NOT EXISTS sections_ad AFTER DELETE ON sections BEGIN
  INSERT INTO sections_fts(sections_fts, rowid, heading, body, tdoc_id, meeting_ref) VALUES ('delete', old.id, old.heading, old.body, old.tdoc_id, old.meeting_ref);
END;
CREATE TRIGGER IF NOT EXISTS sections_au AFTER UPDATE ON sections BEGIN
  INSERT INTO sections_fts(sections_fts, rowid, heading, body, tdoc_id, meeting_ref) VALUES ('delete', old.id, old.heading, old.body, old.tdoc_id, old.meeting_ref);
  INSERT INTO sections_fts(rowid, heading, body, tdoc_id, meeting_ref) VALUES (new.id, new.heading, new.body, new.tdoc_id, new.meeting_ref);
END;

-- Optional cross-meeting link computed by heuristic. Never authoritative.
CREATE TABLE IF NOT EXISTS previous_versions (
  tdoc_id          TEXT PRIMARY KEY REFERENCES tdocs(tdoc_id) ON DELETE CASCADE,
  previous_tdoc_id TEXT,
  strategy         TEXT,   -- revised_from_tdoclist / prev_meeting_source_title
  score            REAL,
  computed_at      TEXT
);

-- LLM-generated artefacts live here and ONLY here. Consumers must treat as generated, not evidence.
CREATE TABLE IF NOT EXISTS analyses (
  tdoc_id     TEXT NOT NULL REFERENCES tdocs(tdoc_id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,   -- summary / proposals / diff
  model       TEXT, provider TEXT,
  payload     TEXT NOT NULL,   -- JSON
  generated_at TEXT,
  PRIMARY KEY (tdoc_id, kind)
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    return con


def delete_tdoc_content(con: sqlite3.Connection, tdoc_id: str) -> None:
    """Remove text/paragraphs/sections for one tdoc. FTS rows follow via trigger."""
    con.execute("DELETE FROM sections WHERE tdoc_id=?", (tdoc_id,))
    con.execute("DELETE FROM paragraphs WHERE tdoc_id=?", (tdoc_id,))
    con.execute("DELETE FROM tdoc_text WHERE tdoc_id=?", (tdoc_id,))


def rebuild_fts(con: sqlite3.Connection) -> None:
    """Full rebuild from the content table (use after bulk imports or if the index looks stale)."""
    con.execute("INSERT INTO sections_fts(sections_fts) VALUES('rebuild')")
    con.commit()
