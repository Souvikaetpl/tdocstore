import logging
import re
from datetime import datetime
from pathlib import Path

from . import config, db, discover, dynareport
from .coverpage import parse_cover_page
from .extract import extract_text_from_zip
from .http_client import HttpClient
from .meeting_info import fetch_meeting_info
from .render import render_to_pdf
from .tdoclist import parse_tdoc_list, pick_latest_tdoc_list

log = logging.getLogger("crawler.ingest")


def _local_meeting_dir(tsg, wg_short, meeting_folder):
    return config.RAW_DIR / tsg / wg_short / meeting_folder


# "\d[eb](?:[_\-]|$)": a directly-attached suffix letter, e.g. "126b".
# "[_\-]e(?:[_\-]|$)": a hyphen/underscore-separated "-e", e.g. "137-e"
# (SA4's electronic pre-session) — a bare digit-adjacency check misses
# this since there's a separator between the number and the "e".
_SUFFIX_HINT_RE = re.compile(r"bis|\d[eb](?:[_\-]|$)|[_\-]e(?:[_\-]|$)", re.IGNORECASE)


def _expected_meeting_titles(wg_short: str, meeting_folder: str, tsg: str = None) -> list:
    """'TSGC1_162_Prague' + wg_short 'CT1' -> ['CT1#162', '3GPPCT1#162']
    — acceptable forms of the string used in an Invitation document's
    TITLE column. Returns a list, not one guaranteed-right string,
    because real invitations vary independently of the folder name in
    ways that can't be predicted from it alone:
      - the "3GPP" prefix is sometimes present, sometimes omitted
        entirely ("RAN1#126-bis" vs "3GPPRAN1#126-bis", both observed)
      - a folder suffix like "126b" doesn't reveal the invitation's own
        spelling of it ("126-bis" observed, not "126b")
    parse_invitation_text/_normalize_title already ignores whitespace and
    hyphen differences, so only prefix-presence and suffix-spelling need
    to be enumerated here, not every cosmetic variant.

    Two naming styles seen across groups for the base number:
      - "TSG<code>_<number>..." (most groups): first digit run after an
        underscore.
      - "{wg_short}-<number>..." (CT6: "CT6-127_Prague_2026-08"): the
        number sits right after wg_short, before the first underscore —
        the generic "_(\\d+)" rule would otherwise grab a trailing date
        fragment like "_2026" instead of the real meeting number "127".

    Leading zeros are stripped: invitation titles use the natural,
    unpadded meeting number (observed "TDoc_List_Meeting_RANAHG-ITU#9",
    not "#09"), so "TSGS6_074_Prague" must resolve to "#74", not "#074".

    wg_short is the literal config.GROUPS key, which for a TSG plenary is
    just "plenary" — not a real label the invitation ever prints. There
    the title uses the TSG's own code instead ("3GPPRAN#113", not
    "3GPPplenary#113"), hence the tsg fallback."""
    label = tsg.upper() if wg_short == "plenary" and tsg else wg_short
    hyphen_match = re.match(rf"^{re.escape(wg_short)}-(\d+)", meeting_folder, re.IGNORECASE)
    if hyphen_match:
        number = hyphen_match.group(1)
    else:
        m = re.search(r"_(\d+)", meeting_folder)
        if not m:
            return []
        number = m.group(1)

    base = f"{label}#{int(number)}"
    titles = {base, f"3GPP{base}"}
    if _SUFFIX_HINT_RE.search(meeting_folder):
        titles |= {f"{base}bis", f"3GPP{base}bis", f"{base}e", f"3GPP{base}e"}
    return list(titles)


def _fetch_and_store_meeting_info(client, conn, meeting_id, tsg, wg_short, wg_path, meeting_entry):
    if not db.meeting_needs_info(conn, meeting_id):
        return
    expected_titles = _expected_meeting_titles(wg_short, meeting_entry.name, tsg)
    if not expected_titles:
        return

    info = None
    try:
        root_entries = discover.list_folder(client, meeting_entry.url)
        # Case varies by group — SA1 uses "invitation" (lowercase), not
        # "Invitation"; an exact-match check silently treated that as "no
        # invitation folder" and left the meeting undated.
        invitation_dir = next((e for e in root_entries if e.is_dir and e.name.lower() == "invitation"), None)
        if invitation_dir is not None:
            invitation_entries = discover.list_folder(client, invitation_dir.url)
            info = fetch_meeting_info(client, invitation_entries, expected_titles)
    except Exception:
        log.exception("Failed to fetch meeting info for %s/%s/%s", tsg, wg_short, meeting_entry.name)

    if info is None:
        # No Invitation document at all, or it couldn't be parsed
        # (observed: SA4#137-e has ~480 real downloaded TDocs but no
        # Invitation folder anywhere on the FTP, so it stayed permanently
        # dateless and lost the "Previous" slot to an older, near-empty
        # meeting). 3GPP's own meeting-calendar page carries real
        # start/end dates for every numbered meeting regardless of
        # whether an Invitation was ever published — tried here as a
        # fallback rather than leaving a meeting with real content
        # unclassifiable.
        try:
            rows = dynareport.fetch_meeting_table(client, tsg, wg_short)
            info = dynareport.find_dated_meeting(rows, expected_titles)
        except Exception:
            log.exception("Failed dynareport date fallback for %s/%s/%s", tsg, wg_short, meeting_entry.name)

    if info is not None:
        db.update_meeting_info(conn, meeting_id, info)


def sync_calendar_only_meetings(client, conn, tsg, wg_short, wg_path):
    """Picks up meeting-calendar entries with no FTP folder at all (see
    dynareport.py's module docstring, case 1) — a separate, cheap sync (one
    HTML page fetch, no document crawl) run alongside the normal FTP-based
    ingest for every WG that has a dynareport page."""
    try:
        rows = dynareport.fetch_meeting_table(client, tsg, wg_short)
        entries = dynareport.calendar_only_entries(rows, tsg, wg_short)
    except Exception:
        log.exception("Failed to sync dynareport calendar for %s/%s", tsg, wg_short)
        return
    for folder_key, title, row in entries:
        try:
            db.upsert_calendar_only_meeting(
                conn, tsg, wg_short, wg_path, folder_key, row.portal_url,
                title, row.start_date, row.end_date, row.town,
            )
            conn.commit()
        except Exception:
            log.exception("Failed to store calendar-only meeting %s/%s/%s", tsg, wg_short, folder_key)


def _fetch_metadata_by_tdoc(client, meeting_url, docs_entries, tsg, wg_short, meeting_folder) -> dict:
    # RAN2 has a separate Tdoclists/ folder with intra-week snapshots —
    # more current when present, so checked first. Every other group
    # observed so far (RAN1/3/4/5, CT1, and RAN2 too) keeps its export as
    # TDoc_List_Meeting_*.xlsx directly in Docs/ instead — docs_entries is
    # the already-fetched listing, not a second HTTP round-trip.
    latest = pick_latest_tdoc_list(discover.list_tdoclists(client, meeting_url))
    if latest is None:
        latest = pick_latest_tdoc_list(discover.find_tdoc_list_in_docs(docs_entries))

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
    _fetch_and_store_meeting_info(client, conn, meeting_id, tsg, wg_short, wg_path, meeting_entry)

    docs_entries = discover.list_docs_folder(client, meeting_entry.url)
    doc_entries = [e for e in docs_entries if not e.is_dir and e.name.lower().endswith(".zip")]
    metadata_by_id = _fetch_metadata_by_tdoc(client, meeting_entry.url, docs_entries, tsg, wg_short, meeting_folder)
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

            previous = db.get_tdoc_file_state(conn, tdoc_id)
            previously_downloaded = previous is not None and previous[1] is not None
            content_changed = (
                previously_downloaded and previous[0] != record["file_modified_at"]
            )

            # Displayed FTP sizes are rounded (e.g. "84.6 KB"), so they never
            # exactly match the real byte count — modified-time is the
            # reliable change signal, not size.
            #
            # A failed download here must not abort the rest of this
            # meeting's batch (previously it did: one persistent network
            # failure mid-meeting silently aborted every file after it in
            # the listing). Caught per-file, logged, left for a future
            # --download/--refresh run to retry — record["local_zip_path"]
            # is only set on actual success, so upsert_tdoc below leaves
            # the existing DB value (NULL, or the prior path) untouched.
            try:
                if content_changed:
                    log.info("%s changed on the server (was modified %s, now %s) — re-downloading",
                              tdoc_id, previous[0], record["file_modified_at"])
                    client.download(entry.url, local_path)
                    db.reset_extraction(conn, tdoc_id)
                    downloaded += 1
                    record["local_zip_path"] = str(local_path)
                elif not local_path.exists():
                    client.download(entry.url, local_path)
                    downloaded += 1
                    record["local_zip_path"] = str(local_path)
                else:
                    record["local_zip_path"] = str(local_path)
            except Exception as exc:
                log.warning("Download failed for %s, will retry on a future run: %s", tdoc_id, exc)

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
                # Malformed source documents occasionally yield text that
                # isn't safely round-trippable: NUL bytes (seen in
                # LibreOffice .doc->.txt output — Postgres text columns
                # reject them outright) or lone UTF-16 surrogates (seen
                # from pypdf misdecoding a PDF's custom/broken font
                # encoding — Python's UTF-8 encoder refuses those
                # entirely). Sanitized once here, before this text is
                # written to disk, cover-page-parsed, or stored to the DB,
                # rather than patching each call site separately.
                result.text = result.text.replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")
                text_path = config.TEXT_DIR / tsg / wg_short / meeting_folder / f"{tdoc_id}.txt"
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text(result.text, encoding="utf-8")
                text_path = str(text_path)

                cover_fields = parse_cover_page(result.text)
                if cover_fields:
                    db.backfill_metadata_from_coverpage(conn, tdoc_id, cover_fields)
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


def run_rendering(limit=None):
    with db.session() as conn:
        pending = db.tdocs_pending_render(conn)
        if limit:
            pending = pending[:limit]
        log.info("Rendering %d pending TDocs", len(pending))

        for tdoc_id, tsg, wg_short, local_zip_path in pending:
            zip_path = Path(local_zip_path)
            meeting_folder = zip_path.parent.name
            dest = config.RENDER_DIR / tsg / wg_short / meeting_folder / f"{tdoc_id}.pdf"
            result = render_to_pdf(zip_path, tdoc_id, dest)

            rendered_path = str(dest) if result.status == "success" else None
            if result.status != "success":
                log.warning("Render %s for %s (%s)", result.status, tdoc_id, result.error or "")

            db.save_render_result(
                conn, tdoc_id,
                rendered_path=rendered_path,
                render_status=result.status,
                render_error=result.error,
            )
            conn.commit()


def refresh_known_groups(meetings_per_wg=3, max_files=200):
    """Daily-refresh entry point: re-sync every group already tracked in
    the DB (no separate scope list to maintain), picking up new meetings,
    new TDocs, and content changes to already-downloaded ones, then
    extract anything newly downloaded or reset by a detected change.

    max_files defaults to a modest cap, not unbounded: in steady state a
    day only produces a handful of new/changed files per group, but while
    a group's historical backlog isn't fully backfilled yet, unbounded
    would silently turn a "check what's new" run into a full backfill.
    Pass max_files=None explicitly to intentionally do that backfill.
    """
    with db.session() as conn:
        pairs = db.known_groups(conn)
    if not pairs:
        log.warning("No known groups yet — run an initial --wg crawl before using --refresh")
        return

    log.info("Refreshing %d known group(s): %s", len(pairs), pairs)
    ingest_scope(pairs, meetings_per_wg=meetings_per_wg, download=True, max_files=max_files)
    run_extraction()


def _meeting_number(tsg: str, wg_short: str, name: str) -> int:
    """Best-effort meeting number extracted from a folder name, reusing
    the same patterns as _expected_meeting_title. Used only to build a
    candidate pool (see _select_meetings_to_download) — -1 (lowest
    priority) for anything unparseable, e.g. a stray utility folder that
    slipped past discover.list_meetings's filter."""
    hyphen_match = re.match(rf"^{re.escape(wg_short)}-(\d+)", name, re.IGNORECASE)
    if hyphen_match:
        return int(hyphen_match.group(1))
    tsg_match = re.match(rf"^{re.escape(tsg)}_(\d+)", name, re.IGNORECASE)
    if tsg_match:
        return int(tsg_match.group(1))
    m = re.search(r"_(\d+)", name)
    return int(m.group(1)) if m else -1


_CANDIDATE_POOL_SIZE = 6


def _select_meetings_to_download(client, conn, tsg, wg_short, wg_path, all_meetings, meetings_per_wg):
    """Picks which meetings actually get their documents downloaded, by
    REAL date rather than by folder-modified-time or meeting number alone.

    Folder-modified-time (the old sort key) isn't a reliable proxy for
    "which meeting is happening next": 3GPP admins sometimes touch a
    future placeholder folder (creating Docs/, adding a template) more
    recently than they touch the folder for the meeting that's actually
    imminent — which previously made us skip the real next meeting
    entirely (confirmed for RAN1, SA3, SA4, SA5, SA6). Meeting number
    alone isn't enough either: a numerically-later folder can exist for a
    meeting scheduled well beyond the next real one.

    So this takes the N numerically-most-recent candidates (a wide-enough
    net to include the real next/previous meeting even when an even-later
    placeholder folder exists), cheaply fetches just each one's real date
    (no document downloads yet), then picks by that real date: the
    nearest meeting still in the future ("Upcoming"), plus however many
    of the most recent already-past meetings fill the remaining
    meetings_per_wg slots ("Previous", and further back if
    meetings_per_wg > 2 — e.g. to reach a real meeting when the 1-2 most
    recent are cancelled/empty, as seen for CT3)."""
    pool_size = max(_CANDIDATE_POOL_SIZE, meetings_per_wg + 3)
    candidates = sorted(all_meetings, key=lambda e: _meeting_number(tsg, wg_short, e.name), reverse=True)
    pool = candidates[:pool_size]

    dated = []
    for entry in pool:
        meeting_id = db.upsert_meeting(conn, tsg, wg_short, wg_path, entry.name, entry.url, entry.modified_at)
        conn.commit()
        _fetch_and_store_meeting_info(client, conn, meeting_id, tsg, wg_short, wg_path, entry)
        conn.commit()
        start_date, end_date = db.get_meeting_dates(conn, meeting_id)
        dated.append((entry, end_date or start_date))

    today = datetime.utcnow().date().isoformat()
    past = sorted([(e, d) for e, d in dated if d and d < today], key=lambda pair: pair[1], reverse=True)
    # Dated candidates first (soonest date), THEN undated ones by the
    # LOWEST meeting number (a numerically-closer, not-yet-dated meeting
    # is more likely the real next one than a further-out placeholder).
    # A plain (has_no_date, date_or_empty) key got this backwards: every
    # undated candidate ties on "", so the tie was broken by scan order
    # (descending by number) — which put the FURTHEST-out undated
    # candidate first, exactly the bug this function exists to fix.
    future = sorted(
        [(e, d) for e, d in dated if not d or d >= today],
        key=lambda pair: (pair[1] is None, pair[1] or "", _meeting_number(tsg, wg_short, pair[0].name) if pair[1] is None else 0),
    )

    selected = []
    if future:
        selected.append(future[0][0])
    remaining_slots = meetings_per_wg - len(selected) if meetings_per_wg else len(past)
    selected += [e for e, _ in past[:remaining_slots]]

    if meetings_per_wg and len(selected) < meetings_per_wg:
        # Pool didn't have enough dated past/future candidates (a brand-new
        # or very small group) — pad with whatever's left, by number.
        remaining = [e for e in candidates if e not in selected]
        selected += remaining[: meetings_per_wg - len(selected)]

    return selected


def ingest_scope(tsg_wg_pairs, meetings_per_wg=1, download=False, max_files=None):
    client = HttpClient()
    try:
        with db.session() as conn:
            for tsg, wg_short in tsg_wg_pairs:
                wg_path = config.GROUPS[tsg][wg_short]
                meetings = discover.list_meetings(client, tsg, wg_path, wg_short)
                sync_calendar_only_meetings(client, conn, tsg, wg_short, wg_path)

                if download and meetings_per_wg:
                    selected = _select_meetings_to_download(
                        client, conn, tsg, wg_short, wg_path, meetings, meetings_per_wg
                    )
                else:
                    # Metadata-only / full-crawl (meetings_per_wg=0) modes
                    # aren't where the selection bug showed up — left on
                    # the simpler modified-time ordering.
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
