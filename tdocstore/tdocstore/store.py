"""The ONLY read API. MCP, REST and direct imports all go through Store.

Every method returns JSON-serialisable dicts/lists and never raises on bad user input:
it returns {"status": "invalid_argument" | "not_found" | ..., "message": ...} instead.
"""
from __future__ import annotations
import difflib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbm
from .config import Config, GROUP_LAYOUT, split_meeting_ref
from .textnorm import normalise_with_map

TDOC_RE = re.compile(r"^[A-Z]{1,2}[0-9]?-\d{5,7}$")
FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def _agenda_key(a: str | None) -> tuple:
    if not a:
        return (10**6,)
    parts = []
    for p in a.split("."):
        m = re.match(r"^(\d+)(.*)$", p)
        parts.append((int(m.group(1)), m.group(2)) if m else (10**6, p))
    return tuple(parts)


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


class Store:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()
        self._cons: dict[str, sqlite3.Connection] = {}

    # ---------- connections ----------
    def groups(self) -> list[str]:
        if not self.cfg.db_dir.exists():
            return []
        return sorted(p.stem for p in self.cfg.db_dir.glob("*.sqlite"))

    def _con(self, group: str) -> sqlite3.Connection | None:
        group = group.upper()
        if group not in self._cons:
            p = self.cfg.db_dir / f"{group}.sqlite"
            if not p.exists():
                return None
            self._cons[group] = dbm.connect(p)
        return self._cons[group]

    def _con_for_ref(self, meeting_ref: str):
        try:
            group, _ = split_meeting_ref(meeting_ref)
        except ValueError as e:
            return None, {"status": "invalid_argument", "message": str(e)}
        con = self._con(group)
        if con is None:
            return None, {"status": "not_found", "message": f"no data for working group {group}"}
        return con, None

    def _con_for_tdoc(self, tdoc_id: str):
        tdoc_id = (tdoc_id or "").strip().upper()
        if not TDOC_RE.match(tdoc_id):
            return None, tdoc_id, {"status": "invalid_argument", "message": f"'{tdoc_id}' is not a TDoc number (expected e.g. R2-2604936)"}
        prefix = tdoc_id.split("-")[0]
        for g, lay in GROUP_LAYOUT.items():
            if lay["prefix"] == prefix:
                con = self._con(g)
                if con is None:
                    return None, tdoc_id, {"status": "not_found", "message": f"no data for working group {g}"}
                return con, tdoc_id, None
        return None, tdoc_id, {"status": "invalid_argument", "message": f"unknown TDoc prefix {prefix}"}

    # ---------- meetings ----------
    def service_info(self) -> dict:
        info = {"service": "tdocstore", "version": "0.1.0", "groups": {}, "generation": "none (no LLM in the data layer)",
                "verbatim_source": "paragraphs table extracted programmatically from DOCX body; markdown is derived",
                "known_limitations": ["tracked changes: accepted text only (deletions dropped)",
                                       ".doc/.pptx/.pdf need optional docling extra", "text boxes/footnotes not extracted"]}
        for g in self.groups():
            con = self._con(g)
            ms = con.execute("SELECT COUNT(*) c, MIN(start_date) a, MAX(start_date) b FROM meetings").fetchone()
            td = con.execute("SELECT COUNT(*) c, SUM(extract_status='ready') r FROM tdocs").fetchone()
            info["groups"][g] = {"meetings": ms["c"], "earliest": ms["a"], "latest": ms["b"], "tdocs": td["c"], "tdocs_ready": td["r"] or 0}
        return info

    def list_meetings(self, group: str | None = None, status: str | None = None, search: str | None = None, limit: int = 30) -> list[dict]:
        limit = max(1, min(int(limit or 30), 100))
        groups = [group.upper()] if group else self.groups()
        out = []
        for g in groups:
            con = self._con(g)
            if con is None:
                continue
            q = "SELECT meeting_ref, grp AS 'group', number, title, start_date AS start, end_date AS 'end', location, country, doc_count, status, docs_url, ingested_at FROM meetings WHERE 1=1"
            args: list = []
            if status:
                q += " AND status=?"; args.append(status.upper())
            if search:
                q += " AND (title LIKE ? OR meeting_ref LIKE ? OR location LIKE ?)"; args += [f"%{search}%"] * 3
            q += " ORDER BY start_date DESC"
            out += _rows(con.execute(q, args))
        out.sort(key=lambda r: r["start"] or "", reverse=True)
        return out[:limit]

    def list_agenda_items(self, meeting_ref: str) -> dict:
        con, err = self._con_for_ref(meeting_ref)
        if err:
            return err
        rows = _rows(con.execute(
            "SELECT agenda_item, MAX(agenda_desc) description, COUNT(*) tdoc_count FROM tdocs WHERE meeting_ref=? GROUP BY agenda_item",
            (meeting_ref,)))
        if not rows and not con.execute("SELECT 1 FROM meetings WHERE meeting_ref=?", (meeting_ref,)).fetchone():
            return {"status": "not_found", "message": f"meeting {meeting_ref} not registered"}
        rows.sort(key=lambda r: _agenda_key(r["agenda_item"]))
        return {"status": "ready", "meeting_ref": meeting_ref, "agenda_items": rows}

    def list_tdocs(self, meeting_ref: str, agenda_item: str | None = None, source: str | None = None,
                   limit: int = 50, offset: int = 0) -> dict:
        con, err = self._con_for_ref(meeting_ref)
        if err:
            return err
        limit = max(1, min(int(limit or 50), 50)); offset = max(0, int(offset or 0))
        where = "meeting_ref=?"; args: list = [meeting_ref]
        if agenda_item:
            # prefix match on dotted numbering: '8.1' matches 8.1 and 8.1.x but not 8.10
            where += " AND (agenda_item=? OR agenda_item LIKE ?)"; args += [agenda_item, agenda_item + ".%"]
        if source:
            where += " AND source LIKE ?"; args.append(f"%{source}%")
        total = con.execute(f"SELECT COUNT(*) FROM tdocs WHERE {where}", args).fetchone()[0]
        rows = _rows(con.execute(
            f"""SELECT tdoc_id, title, source, agenda_item, document_type, document_for, tdoc_status,
                       is_revision_of, revised_to, extract_status, file_url
                FROM tdocs WHERE {where} ORDER BY tdoc_id LIMIT ? OFFSET ?""", args + [limit, offset]))
        return {"status": "ready", "meeting_ref": meeting_ref, "total": total, "returned": len(rows),
                "offset": offset, "has_more": offset + len(rows) < total, "tdocs": rows}

    # ---------- search ----------
    @staticmethod
    def _fts_query(user_query: str) -> str | None:
        """Quoted phrases kept as phrases; bare tokens quoted individually; implicit AND.
        Operators and punctuation from the user can never reach FTS5 unquoted."""
        q = (user_query or "").strip()
        if len(q) < 2:
            return None
        parts: list[str] = []
        for m in re.finditer(r'"([^"]+)"|(\S+)', q):
            phrase, token = m.group(1), m.group(2)
            if phrase:
                cleaned = phrase.replace('"', " ").strip()
                if cleaned:
                    parts.append('"' + cleaned + '"')
            else:
                t = token.strip('"()*^:').strip()
                if not t or t.upper() in FTS_OPERATORS:
                    continue
                parts.append('"' + t.replace('"', "") + '"')
        return " ".join(parts) or None

    def search(self, meeting_ref: str, query: str, limit: int = 20) -> dict:
        con, err = self._con_for_ref(meeting_ref)
        if err:
            return err
        limit = max(1, min(int(limit or 20), 50))
        fq = self._fts_query(query)
        if fq is None:
            return {"status": "invalid_argument", "message": "query must contain at least two characters"}
        try:
            rows = con.execute(
                """SELECT f.tdoc_id, f.heading, snippet(sections_fts, 1, '<mark>', '</mark>', '…', 24) AS snip,
                          bm25(sections_fts, 2.0, 1.0) AS score
                   FROM sections_fts f WHERE sections_fts MATCH ? AND f.meeting_ref=?
                   ORDER BY score LIMIT 400""", (fq, meeting_ref)).fetchall()
        except sqlite3.OperationalError as e:
            return {"status": "invalid_query", "message": str(e), "fts_query": fq}
        # group by tdoc, keep best score, up to 3 hits per tdoc
        by: dict[str, dict] = {}
        for r in rows:
            d = by.setdefault(r["tdoc_id"], {"tdoc_id": r["tdoc_id"], "score": r["score"], "hits": [], "section_hits": 0})
            d["section_hits"] += 1
            if len(d["hits"]) < 3:
                d["hits"].append({"heading": r["heading"], "snippet": r["snip"]})
        ranked = sorted(by.values(), key=lambda d: d["score"])[:limit]
        if ranked:
            ids = [d["tdoc_id"] for d in ranked]
            meta = {m["tdoc_id"]: dict(m) for m in con.execute(
                f"SELECT tdoc_id, title, source, agenda_item, document_type, file_url FROM tdocs WHERE tdoc_id IN ({','.join('?'*len(ids))})", ids)}
            for d in ranked:
                d.update(meta.get(d["tdoc_id"], {}))
                d["score"] = round(d["score"], 3)
        return {"status": "ready", "meeting_ref": meeting_ref, "query": query, "fts_query": fq,
                "total": len(by), "returned": len(ranked), "results": ranked}

    # ---------- documents ----------
    def get_tdoc(self, tdoc_id: str) -> dict:
        con, tid, err = self._con_for_tdoc(tdoc_id)
        if err:
            return err
        r = con.execute("SELECT * FROM tdocs WHERE tdoc_id=?", (tid,)).fetchone()
        if r is None:
            return {"status": "not_found", "tdoc_id": tid, "message": "TDoc not in index (meeting not ingested, or number not in TDoc_List)"}
        return {"status": "ready", **dict(r)}

    def get_text(self, tdoc_id: str) -> dict:
        meta = self.get_tdoc(tdoc_id)
        if meta["status"] != "ready":
            return meta
        con, tid, _ = self._con_for_tdoc(tdoc_id)
        t = con.execute("SELECT markdown, truncated FROM tdoc_text WHERE tdoc_id=?", (tid,)).fetchone()
        if t is None:
            return {"status": "not_indexed", "tdoc_id": tid, "extract_status": meta["extract_status"],
                    "fetch_status": meta["fetch_status"], "error": meta.get("error"), "file_url": meta.get("file_url"),
                    "message": "metadata known but text not extracted"}
        return {"status": "ready", "tdoc_id": tid, "meeting_ref": meta["meeting_ref"], "title": meta["title"],
                "source": meta["source"], "agenda_item": meta["agenda_item"], "source_type": meta["source_type"],
                "extracted_at": meta["extracted_at"], "file_url": meta["file_url"], "truncated": bool(t["truncated"]),
                "text": t["markdown"]}

    def get_sections(self, tdoc_id: str) -> dict:
        con, tid, err = self._con_for_tdoc(tdoc_id)
        if err:
            return err
        rows = _rows(con.execute("SELECT idx, heading, level, body FROM sections WHERE tdoc_id=? ORDER BY idx", (tid,)))
        if not rows:
            return {"status": "not_indexed", "tdoc_id": tid}
        return {"status": "ready", "tdoc_id": tid, "sections": rows}

    def get_paragraphs(self, tdoc_id: str, start: int | None = None, end: int | None = None) -> dict:
        con, tid, err = self._con_for_tdoc(tdoc_id)
        if err:
            return err
        q = "SELECT idx, style, section_idx, char_start, char_end, text FROM paragraphs WHERE tdoc_id=?"
        args: list = [tid]
        if start is not None:
            q += " AND idx>=?"; args.append(int(start))
        if end is not None:
            q += " AND idx<?"; args.append(int(end))
        rows = _rows(con.execute(q + " ORDER BY idx", args))
        if not rows:
            return {"status": "not_indexed", "tdoc_id": tid}
        return {"status": "ready", "tdoc_id": tid, "paragraphs": rows}

    def _full_text(self, con, tid: str) -> str | None:
        rows = con.execute("SELECT text FROM paragraphs WHERE tdoc_id=? ORDER BY idx", (tid,)).fetchall()
        if not rows:
            return None
        return "\n".join(r["text"] for r in rows)

    def get_verbatim(self, tdoc_id: str, char_start: int, char_end: int) -> dict:
        con, tid, err = self._con_for_tdoc(tdoc_id)
        if err:
            return err
        full = self._full_text(con, tid)
        if full is None:
            return {"status": "not_indexed", "tdoc_id": tid}
        try:
            s, e = int(char_start), int(char_end)
        except (TypeError, ValueError):
            return {"status": "invalid_argument", "message": "char_start/char_end must be integers"}
        if not (0 <= s < e <= len(full)):
            return {"status": "invalid_argument", "message": f"range must satisfy 0 <= start < end <= {len(full)}"}
        paras = _rows(con.execute(
            "SELECT idx, char_start, char_end FROM paragraphs WHERE tdoc_id=? AND char_end>? AND char_start<? ORDER BY idx", (tid, s, e)))
        return {"status": "ready", "tdoc_id": tid, "char_start": s, "char_end": e, "text": full[s:e],
                "paragraph_idx": [p["idx"] for p in paras]}

    def find_verbatim(self, tdoc_id: str, needle: str, max_hits: int = 20) -> dict:
        """Exact-after-normalisation substring search. Returns offsets into the ORIGINAL text,
        so get_verbatim(tdoc_id, start, end) reproduces the evidence byte-for-byte."""
        con, tid, err = self._con_for_tdoc(tdoc_id)
        if err:
            return err
        full = self._full_text(con, tid)
        if full is None:
            return {"status": "not_indexed", "tdoc_id": tid}
        if not needle or len(needle.strip()) < 3:
            return {"status": "invalid_argument", "message": "needle must be at least 3 characters"}
        nfull, amap = normalise_with_map(full)
        nneedle, _ = normalise_with_map(needle)
        hits = []
        i = nfull.find(nneedle)
        while i != -1 and len(hits) < max_hits:
            s = amap[i]
            e = amap[i + len(nneedle) - 1] + 1
            p = con.execute("SELECT idx FROM paragraphs WHERE tdoc_id=? AND char_start<=? ORDER BY idx DESC LIMIT 1", (tid, s)).fetchone()
            hits.append({"char_start": s, "char_end": e, "paragraph_idx": p["idx"] if p else None,
                         "text": full[s:e], "exact": full[s:e] == needle})
            i = nfull.find(nneedle, i + 1)
        return {"status": "ready", "tdoc_id": tid, "needle": needle, "hits": hits, "count": len(hits)}

    # ---------- cross-meeting ----------
    def previous_version(self, tdoc_id: str, compute: bool = True) -> dict:
        con, tid, err = self._con_for_tdoc(tdoc_id)
        if err:
            return err
        r = con.execute("SELECT * FROM previous_versions WHERE tdoc_id=?", (tid,)).fetchone()
        if r is None and compute:
            self._compute_previous(con, tid)
            r = con.execute("SELECT * FROM previous_versions WHERE tdoc_id=?", (tid,)).fetchone()
        if r is None:
            return {"status": "not_computed", "tdoc_id": tid}
        return {"status": "ready" if r["previous_tdoc_id"] else "no_previous", "tdoc_id": tid,
                "previous_tdoc_id": r["previous_tdoc_id"], "strategy": r["strategy"], "score": r["score"],
                "note": "heuristic link unless strategy=revised_from_tdoclist; verify before relying on it"}

    def _compute_previous(self, con, tid: str) -> None:
        t = con.execute("SELECT * FROM tdocs WHERE tdoc_id=?", (tid,)).fetchone()
        if t is None:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # 1. authoritative within-meeting revision link from TDoc_List
        if t["is_revision_of"] and TDOC_RE.match(t["is_revision_of"].strip().upper()):
            con.execute("INSERT OR REPLACE INTO previous_versions VALUES(?,?,?,?,?)",
                        (tid, t["is_revision_of"].strip().upper(), "revised_from_tdoclist", 1.0, now))
            con.commit(); return
        # 2. previous meeting, same source, similar title
        m = con.execute("SELECT start_date FROM meetings WHERE meeting_ref=?", (t["meeting_ref"],)).fetchone()
        prev = con.execute("SELECT meeting_ref FROM meetings WHERE start_date<? ORDER BY start_date DESC LIMIT 1",
                           (m["start_date"] or "9999",)).fetchone() if m else None
        if prev:
            src = _norm_source(t["source"])
            best, best_score = None, 0.0
            for c in con.execute("SELECT tdoc_id, title, source, agenda_item FROM tdocs WHERE meeting_ref=?", (prev["meeting_ref"],)):
                if _norm_source(c["source"]) != src:
                    continue
                ratio = difflib.SequenceMatcher(None, (t["title"] or "").lower(), (c["title"] or "").lower()).ratio()
                if c["agenda_item"] == t["agenda_item"]:
                    ratio += 0.05
                if ratio > best_score:
                    best, best_score = c["tdoc_id"], ratio
            if best and best_score >= 0.6:
                con.execute("INSERT OR REPLACE INTO previous_versions VALUES(?,?,?,?,?)",
                            (tid, best, "prev_meeting_source_title", round(best_score, 3), now))
                con.commit(); return
        con.execute("INSERT OR REPLACE INTO previous_versions VALUES(?,?,?,?,?)", (tid, None, "none", 0.0, now))
        con.commit()

    def diff_previous(self, tdoc_id: str, context: int = 2) -> dict:
        """Deterministic paragraph-level unified diff vs the linked previous version. No LLM."""
        pv = self.previous_version(tdoc_id)
        if pv["status"] != "ready":
            return pv
        con, tid, _ = self._con_for_tdoc(tdoc_id)
        a = self._full_text(con, pv["previous_tdoc_id"]) ; b = self._full_text(con, tid)
        if a is None or b is None:
            return {"status": "not_indexed", "tdoc_id": tid, "previous_tdoc_id": pv["previous_tdoc_id"]}
        diff = list(difflib.unified_diff(a.split("\n"), b.split("\n"), fromfile=pv["previous_tdoc_id"], tofile=tid, lineterm="", n=context))
        added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        return {"status": "ready", "tdoc_id": tid, "previous_tdoc_id": pv["previous_tdoc_id"], "strategy": pv["strategy"],
                "paragraphs_added": added, "paragraphs_removed": removed, "unified_diff": "\n".join(diff)}

    def close(self):
        for c in self._cons.values():
            c.close()
        self._cons.clear()


def _norm_source(s: str | None) -> str:
    s = (s or "").lower()
    s = s.split(",")[0]
    s = re.sub(r"\(.*?\)", "", s)
    return re.sub(r"[^a-z0-9]", "", s)
