import logging
from datetime import datetime
from pathlib import Path

from . import config, db, discover
from .extract import extract_text_from_zip
from .http_client import HttpClient
from .tdoclist import parse_tdoc_list, pick_latest_tdoc_list

log = logging.getLogger("crawler.ingest")


def _local_meeting_dir(tsg, wg_short, meeting_folder):
    return config.RAW_DIR / tsg / wg_short / meeting_folder


def _fetch_metadata_by_tdoc(client, meeting_url, tsg, wg_short, meeting_folder) -> dict:
    tdoclist_entries = discover.list_tdoclists(client, meeting_url)
    latest = pick_latest_tdoc_list(tdoclist_entries)
    if latest is None:
        log.warning("No TDoc list Excel found for %s", meeting_url)
        return {}

    dest = _local_meeting_dir(tsg, wg_short, meeting_folder) / "tdoclist.xlsx"
    client.download(latest.url, dest)
    records = parse_tdoc_list(dest)
    return {r["tdoc_id"]: r for r in records if r.get("tdoc_id")}


def ingest_meeting(client, conn, tsg, wg_short, wg_path, meeting_entry, download=False, max_files=None):
    meeting_folder = meeting_entry.name
    meeting_id = db.upsert_meeting(
        conn, tsg, wg_short, wg_path, meeting_folder, meeting_entry.url, meeting_entry.modified_at
    )

    metadata_by_id = _fetch_metadata_by_tdoc(client, meeting_entry.url, tsg, wg_short, meeting_folder)
    doc_entries = discover.list_docs(client, meeting_entry.url)
    log.info("%s/%s/%s: %d metadata rows, %d doc files", tsg, wg_short, meeting_folder,
              len(metadata_by_id), len(doc_entries))

    downloaded = 0
    for entry in doc_entries:
        tdoc_id = entry.name.rsplit(".", 1)[0]
        record = dict(metadata_by_id.get(tdoc_id, {}))
        record["tdoc_id"] = tdoc_id
        record["meeting_id"] = meeting_id
        record["tsg"] = tsg
        record["wg_short"] = wg_short
        record["file_url"] = entry.url
        record["file_size_bytes"] = entry.size_bytes
        record["file_modified_at"] = entry.modified_at.isoformat() if entry.modified_at else None

        if download and (max_files is None or downloaded < max_files):
            local_path = _local_meeting_dir(tsg, wg_short, meeting_folder) / entry.name
            # Displayed FTP sizes are rounded (e.g. "84.6 KB"), so they never
            # exactly match the real byte count — treat existence as final.
            if not local_path.exists():
                client.download(entry.url, local_path)
                downloaded += 1
            record["local_zip_path"] = str(local_path)

        # normalize datetime/other non-primitive values for sqlite
        for key in ("reservation_date", "uploaded_at"):
            if isinstance(record.get(key), datetime):
                record[key] = record[key].isoformat()
        for key, val in list(record.items()):
            if val is not None and not isinstance(val, (str, int, float)):
                record[key] = str(val)

        db.upsert_tdoc(conn, record)

    conn.commit()


def run_extraction(limit=None):
    with db.session() as conn:
        pending = db.tdocs_pending_extraction(conn)
        if limit:
            pending = pending[:limit]
        log.info("Extracting text for %d pending TDocs", len(pending))

        for tdoc_id, tsg, wg_short, local_zip_path in pending:
            zip_path = Path(local_zip_path)
            meeting_folder = zip_path.parent.name
            result = extract_text_from_zip(zip_path, tdoc_id)

            text_path = None
            if result.status == "success":
                text_path = config.TEXT_DIR / tsg / wg_short / meeting_folder / f"{tdoc_id}.txt"
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text(result.text, encoding="utf-8")
                text_path = str(text_path)
            else:
                log.warning("Extraction %s for %s (%s)", result.status, tdoc_id, result.error or "")

            db.save_extraction_result(
                conn,
                tdoc_id,
                text_path=text_path,
                extraction_status=result.status,
                extraction_error=result.error,
                extracted_source_file=result.source_filename,
            )
            conn.commit()


def ingest_scope(tsg_wg_pairs, meetings_per_wg=1, download=False, max_files=None):
    client = HttpClient()
    try:
        with db.session() as conn:
            for tsg, wg_short in tsg_wg_pairs:
                wg_path = config.GROUPS[tsg][wg_short]
                meetings = discover.list_meetings(client, tsg, wg_path)
                meetings.sort(key=lambda e: e.modified_at or datetime.min, reverse=True)
                selected = meetings[:meetings_per_wg] if meetings_per_wg else meetings

                for meeting_entry in selected:
                    log.info("Ingesting %s %s %s", tsg, wg_short, meeting_entry.name)
                    try:
                        ingest_meeting(client, conn, tsg, wg_short, wg_path, meeting_entry,
                                        download=download, max_files=max_files)
                    except Exception:
                        log.exception("Failed to ingest %s %s %s", tsg, wg_short, meeting_entry.name)
    finally:
        client.close()
