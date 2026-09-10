"""Write path: register meetings, fetch TDoc lists + files, extract, index.

Two modes:
  online : list the 3GPP Docs folder over HTTPS, download TDoc_List + zips.
  offline: point at a local folder that already contains TDoc_List*.xlsx and the zips/docx
           (e.g. RS's existing downloads, or test fixtures). No network.
Idempotent: re-running re-extracts only files whose sha256 changed unless force=True.
"""
from __future__ import annotations
import asyncio
import hashlib
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db as dbm
from .config import Config, GROUP_LAYOUT, meeting_folder, meeting_ref, split_meeting_ref
from .extract import extract_any
from .fetch import Fetcher
from .tdoclist import parse_tdoc_list

log = logging.getLogger("tdocstore.ingest")

TDOCLIST_RE = re.compile(r"^tdoc_?list.*\.xlsx$", re.I)
DOCFILE_RE = re.compile(r"^([A-Z]{1,2}[0-9]?-\d{5,7})(?:[_\-][^.]*)?\.(zip|docx|doc|pptx|pdf|xlsx)$", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class IngestReport:
    meeting_ref: str
    listed: int = 0
    in_tdoclist: int = 0
    fetched: int = 0
    cached: int = 0
    missing: int = 0
    fetch_errors: int = 0
    extracted: int = 0
    skipped_unchanged: int = 0
    unsupported: int = 0
    extract_errors: int = 0
    source_types: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def derive_status(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "UNKNOWN"
    today = datetime.now(timezone.utc).date().isoformat()
    if today < start[:10]:
        return "UPCOMING"
    if today > end[:10]:
        return "ENDED"
    return "ONGOING"


def register_meeting(cfg: Config, group: str, number: str, *, title=None, start=None, end=None,
                     location=None, country=None) -> str:
    group = group.upper()
    ref = meeting_ref(group, number)
    folder = meeting_folder(group, number)
    docs_url = f"{cfg.base_url}/{GROUP_LAYOUT[group]['ftp']}/{folder}/Docs/"
    con = dbm.connect(cfg.db_path(group))
    con.execute(
        """INSERT INTO meetings(meeting_ref, grp, number, title, start_date, end_date, location, country, ftp_folder, docs_url, status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(meeting_ref) DO UPDATE SET
             title=COALESCE(excluded.title, meetings.title),
             start_date=COALESCE(excluded.start_date, meetings.start_date),
             end_date=COALESCE(excluded.end_date, meetings.end_date),
             location=COALESCE(excluded.location, meetings.location),
             country=COALESCE(excluded.country, meetings.country),
             ftp_folder=excluded.ftp_folder, docs_url=excluded.docs_url, status=excluded.status""",
        (ref, group, number, title or f"3GPP {group}#{number}", start, end, location, country, folder, docs_url,
         derive_status(start, end)),
    )
    con.commit()
    con.close()
    return ref


def _upsert_tdocs(con, ref: str, records: list[dict]) -> int:
    n = 0
    for r in records:
        con.execute(
            """INSERT INTO tdocs(tdoc_id, meeting_ref, title, source, contact, document_type, document_for,
                 agenda_item, agenda_desc, tdoc_status, is_revision_of, revised_to, release, spec, spec_version,
                 related_wis, cr_number, cr_category, uploaded_at)
               VALUES(:tdoc_id, :meeting_ref, :title, :source, :contact, :document_type, :document_for,
                 :agenda_item, :agenda_desc, :tdoc_status, :is_revision_of, :revised_to, :release, :spec, :spec_version,
                 :related_wis, :cr_number, :cr_category, :uploaded_at)
               ON CONFLICT(tdoc_id) DO UPDATE SET
                 meeting_ref=excluded.meeting_ref, title=excluded.title, source=excluded.source, contact=excluded.contact,
                 document_type=excluded.document_type, document_for=excluded.document_for,
                 agenda_item=excluded.agenda_item, agenda_desc=excluded.agenda_desc, tdoc_status=excluded.tdoc_status,
                 is_revision_of=excluded.is_revision_of, revised_to=excluded.revised_to, release=excluded.release,
                 spec=excluded.spec, spec_version=excluded.spec_version, related_wis=excluded.related_wis,
                 cr_number=excluded.cr_number, cr_category=excluded.cr_category, uploaded_at=excluded.uploaded_at""",
            {**r, "meeting_ref": ref},
        )
        n += 1
    return n


def index_extracted(con, tdoc_id: str, ex, *, max_text_chars: int) -> None:
    """Write tdoc_text / paragraphs / sections / FTS for one TDoc. Removes previous rows first."""
    dbm.delete_tdoc_content(con, tdoc_id)
    mref = con.execute("SELECT meeting_ref FROM tdocs WHERE tdoc_id=?", (tdoc_id,)).fetchone()[0]
    md = ex.markdown
    truncated = 0
    if len(md) > max_text_chars:
        md = md[:max_text_chars]
        truncated = 1
    con.execute("INSERT INTO tdoc_text(tdoc_id, markdown, truncated) VALUES(?,?,?)", (tdoc_id, md, truncated))

    # sections first so paragraphs can reference section_idx
    sec_rows = [(tdoc_id, mref, i, h, lvl, body) for i, (h, lvl, body) in enumerate(ex.sections)]
    con.executemany("INSERT INTO sections(tdoc_id, meeting_ref, idx, heading, level, body) VALUES(?,?,?,?,?,?)", sec_rows)

    # paragraph offsets over '\n'.join(text)
    pos = 0
    sec_idx = 0
    heading_positions = {h: i for i, (h, _, _) in enumerate(ex.sections)}
    rows = []
    for p in ex.paragraphs:
        if p.level and not p.is_table_cell and p.text in heading_positions:
            sec_idx = heading_positions[p.text]
        start = pos
        end = pos + len(p.text)
        rows.append((tdoc_id, p.idx, p.style, sec_idx, start, end, p.text))
        pos = end + 1
    con.executemany(
        "INSERT INTO paragraphs(tdoc_id, idx, style, section_idx, char_start, char_end, text) VALUES(?,?,?,?,?,?,?)", rows
    )


def _process_file(con, cfg: Config, tdoc_id: str, path: Path, *, force: bool, allow_docling: bool, rep: IngestReport):
    sha = _sha(path)
    row = con.execute("SELECT file_sha256, extract_status FROM tdocs WHERE tdoc_id=?", (tdoc_id,)).fetchone()
    if row and row["file_sha256"] == sha and row["extract_status"] == "ready" and not force:
        rep.skipped_unchanged += 1
        return
    workdir = cfg.files_dir / "_extracted" / tdoc_id   # never inside an offline source folder
    if workdir.exists():
        shutil.rmtree(workdir)
    try:
        ex = extract_any(path, workdir, allow_docling=allow_docling)
    except Exception as e:  # corrupt zip/docx etc.
        rep.extract_errors += 1
        con.execute("UPDATE tdocs SET fetch_status='fetched', file_name=?, file_sha256=?, extract_status='error', error=? WHERE tdoc_id=?",
                    (path.name, sha, f"{type(e).__name__}: {e}"[:500], tdoc_id))
        return
    rep.source_types[ex.source_type] = rep.source_types.get(ex.source_type, 0) + 1
    if ex.warnings:
        rep.warnings.append({"tdoc_id": tdoc_id, "warnings": ex.warnings[:5]})
    if not ex.paragraphs:
        rep.unsupported += 1
        con.execute("UPDATE tdocs SET fetch_status='fetched', file_name=?, file_sha256=?, extract_status='unsupported', source_type=?, error=? WHERE tdoc_id=?",
                    (path.name, sha, ex.source_type, "; ".join(ex.warnings)[:500] or None, tdoc_id))
        return
    index_extracted(con, tdoc_id, ex, max_text_chars=cfg.max_text_chars)
    con.execute(
        "UPDATE tdocs SET fetch_status='fetched', file_name=?, file_sha256=?, extract_status='ready', source_type=?, extracted_at=?, text_chars=?, error=NULL WHERE tdoc_id=?",
        (path.name, sha, ex.source_type, _now(), len(ex.markdown), tdoc_id),
    )
    rep.extracted += 1
    log.info("%s %s %d paragraphs %d sections", tdoc_id, ex.source_type, len(ex.paragraphs), len(ex.sections))


def ingest_meeting(cfg: Config, ref_or_group: str, number: str | None = None, *, limit: int | None = None,
                   only_agenda: str | None = None, offline_dir: Path | str | None = None,
                   force: bool = False, allow_docling: bool = True) -> IngestReport:
    """Ingest one meeting. `ref_or_group` is 'R2-135' or ('RAN2', '135')."""
    if number is None:
        group, number = split_meeting_ref(ref_or_group)
    else:
        group = ref_or_group.upper()
    ref = meeting_ref(group, number)
    con = dbm.connect(cfg.db_path(group))
    m = con.execute("SELECT * FROM meetings WHERE meeting_ref=?", (ref,)).fetchone()
    if m is None:
        con.close()
        register_meeting(cfg, group, number)
        con = dbm.connect(cfg.db_path(group))
        m = con.execute("SELECT * FROM meetings WHERE meeting_ref=?", (ref,)).fetchone()
    rep = IngestReport(meeting_ref=ref)
    files_dir = cfg.files_dir / group / ref
    files_dir.mkdir(parents=True, exist_ok=True)

    # 1. listing
    if offline_dir:
        src = Path(offline_dir)
        names = [p.name for p in sorted(src.iterdir()) if p.is_file()]
        fetcher = None
    else:
        fetcher = Fetcher(cfg)
        names = asyncio.run(fetcher.list_dir(m["docs_url"]))
    rep.listed = len(names)

    # 2. TDoc_List
    tl_names = [n for n in names if TDOCLIST_RE.match(n)]
    if not tl_names:
        raise RuntimeError(f"no TDoc_List*.xlsx in listing for {ref} ({len(names)} entries)")
    tl_name = sorted(tl_names)[-1]
    if offline_dir:
        tl_path = Path(offline_dir) / tl_name
    else:
        res = asyncio.run(fetcher.fetch_files(m["docs_url"], [tl_name], files_dir, skip_existing=False))
        if res[0].status not in ("fetched", "cached"):
            raise RuntimeError(f"TDoc_List download failed: {res[0].error}")
        tl_path = res[0].path
    records = list(parse_tdoc_list(tl_path))
    if only_agenda:
        records = [r for r in records if (r.get("agenda_item") or "").startswith(only_agenda)]
    if limit:
        records = records[:limit]
    rep.in_tdoclist = _upsert_tdocs(con, ref, records)
    con.commit()

    # 3. map tdoc_id -> file name from the listing
    by_id: dict[str, str] = {}
    for n in names:
        mm = DOCFILE_RE.match(n)
        if mm:
            tid = mm.group(1).upper()
            # prefer .zip over loose files if both exist
            if tid not in by_id or n.lower().endswith(".zip"):
                by_id[tid] = n
    wanted = [(r["tdoc_id"], by_id.get(r["tdoc_id"])) for r in records]
    for tid, fname in wanted:
        if fname is None:
            rep.missing += 1
            con.execute("UPDATE tdocs SET fetch_status='missing' WHERE tdoc_id=?", (tid,))
        else:
            con.execute("UPDATE tdocs SET file_name=?, file_url=? WHERE tdoc_id=?", (fname, m["docs_url"] + fname, tid))
    con.commit()

    # 4. fetch
    to_fetch = [(tid, fn) for tid, fn in wanted if fn]
    local: dict[str, Path] = {}
    if offline_dir:
        for tid, fn in to_fetch:
            local[tid] = Path(offline_dir) / fn
            rep.cached += 1
    else:
        results = asyncio.run(fetcher.fetch_files(m["docs_url"], [fn for _, fn in to_fetch], files_dir))
        name_to_id = {fn: tid for tid, fn in to_fetch}
        for r in results:
            tid = name_to_id[r.name]
            if r.status in ("fetched", "cached"):
                local[tid] = r.path
                if r.status == "fetched":
                    rep.fetched += 1
                else:
                    rep.cached += 1
            elif r.status == "missing":
                rep.missing += 1
                con.execute("UPDATE tdocs SET fetch_status='missing' WHERE tdoc_id=?", (tid,))
            else:
                rep.fetch_errors += 1
                con.execute("UPDATE tdocs SET fetch_status='error', error=? WHERE tdoc_id=?", (r.error, tid))
        con.commit()

    # 5. extract + index
    for tid, path in local.items():
        _process_file(con, cfg, tid, path, force=force, allow_docling=allow_docling, rep=rep)
        con.commit()

    # 6. meeting counters
    total = con.execute("SELECT COUNT(*) FROM tdocs WHERE meeting_ref=?", (ref,)).fetchone()[0]
    con.execute("UPDATE meetings SET doc_count=?, ingested_at=?, status=? WHERE meeting_ref=?",
                (total, _now(), derive_status(m["start_date"], m["end_date"]), ref))
    con.commit()
    con.close()
    return rep


MEETING_FOLDER_RE = re.compile(r"^TSG[A-Z]{1,2}\d?_(\d{1,3}(?:bis|-e|_e)?)(?:[-_]?e)?$", re.I)


def discover_meetings(cfg: Config, group: str) -> list[str]:
    """List the WG folder on the 3GPP server and register meeting folders not yet known.
    Dates/locations are not on the file server; add them with `meeting add` or from the 3GPP calendar later."""
    group = group.upper()
    url = f"{cfg.base_url}/{GROUP_LAYOUT[group]['ftp']}/"
    names = asyncio.run(Fetcher(cfg).list_dir(url))
    con = dbm.connect(cfg.db_path(group))
    known = {r[0] for r in con.execute("SELECT ftp_folder FROM meetings")}
    con.close()
    added = []
    for n in names:
        m = MEETING_FOLDER_RE.match(n)
        if not m or n in known:
            continue
        number = m.group(1).lower().replace("_e", "-e")
        if number.endswith("bis"):
            number = number[:-3] + "-bis"
        try:
            added.append(register_meeting(cfg, group, number))
        except Exception as e:  # unusual folder name
            log.warning("skip folder %s: %s", n, e)
    return added


def sync_group(cfg: Config, group: str, *, meetings: list[str] | None = None, **kw) -> list[IngestReport]:
    """Discover + ingest. With `meetings` given, ingest only those refs (e.g. the current meeting during meeting week)."""
    discover_meetings(cfg, group)
    con = dbm.connect(cfg.db_path(group.upper()))
    refs = meetings or [r[0] for r in con.execute("SELECT meeting_ref FROM meetings WHERE status!='UPCOMING' ORDER BY start_date")]
    con.close()
    out = []
    for ref in refs:
        try:
            out.append(ingest_meeting(cfg, ref, **kw))
        except Exception as e:
            log.error("%s: %s", ref, e)
            rep = IngestReport(meeting_ref=ref); rep.warnings.append(str(e)); out.append(rep)
    return out
