import logging
import re

from . import config
from .listing import parse_directory_listing, Entry

log = logging.getLogger("crawler.discover")


def wg_url(tsg: str, wg_path: str) -> str:
    return f"{config.BASE_URL}/tsg_{tsg}/{wg_path}"


_ALL_SUFFIX_RE = re.compile(r"_ALL$", re.IGNORECASE)


def list_meetings(client, tsg: str, wg_path: str, wg_short: str | None = None) -> list[Entry]:
    """List meeting folders under a working group. Most real meeting
    folders observed across RAN/SA/CT start with "TSG" (TSGR2_135,
    TSGS2_176_Prague_2026-08, TSGC1_162_Prague, ...); working groups also
    carry non-meeting utility folders alongside them (PRD,
    Working_documents, Workshop, Specifications, Templates) that don't.
    Without this filter those utility folders can outrank real meetings
    when sorted by modified-time and starve out actual meeting crawls —
    observed happening to RAN5 (both --meetings-per-wg slots went to
    Working_documents/PRD, zero real meetings) before this filter existed.

    CT6 is one group observed so far that names meetings differently
    ("CT6-127_Prague_2026-08", not "TSGC6...") — its actual meetings would
    otherwise all be filtered out, leaving only the stray "tsg_ct" utility
    folder. wg_short (when passed) also matches that "{wg_short}-<digits>"
    style.

    CT's plenary drifted the same way partway through its history: up to
    ~2024 it used "TSGC_105_Melbourne" (caught by the "TSG" prefix), then
    switched to "CT_113_Madrid-2026-09" with no "TSG" at all — silently
    stranding a crawl on 2024-era meetings while RAN/SA plenary (still
    "TSGR_"/"TSGS_") kept working. tsg (when passed) matches that
    "{TSG}_<digits>" style generally, not just for plenary.

    CT4 drifted the same way too, but with wg_short + underscore rather
    than tsg + underscore: "TSGCT4_124_Maastricht" (caught by "TSG") then
    "CT4_125_Hefei" onward (2025-02 through at least 2027-02 observed) —
    matches neither the "TSG" prefix nor the "{wg_short}-<digits>" hyphen
    style already handled for CT6. Confirmed live against 3GPP's own
    dynareport calendar and a second independent TDoc archive (TDocHamster)
    before concluding this was a real gap and not a data limitation — CT4
    was silently stuck on 2024-era meetings for 14+ real, documented
    meetings while every sibling CT group kept working. wg_short (when
    passed) also matches that "{wg_short}_<digits>" underscore style.

    Also drops "..._ALL"-suffixed folders (e.g. RAN AH1's "TSGRT_ALL"): a
    legacy aggregate/index folder, not an individual meeting, which would
    otherwise burn a --meetings-per-wg slot for 0 real docs."""
    url = wg_url(tsg, wg_path) + "/"
    resp = client.get(url)
    all_entries = [e for e in parse_directory_listing(resp.text) if e.is_dir]

    def is_meeting(name: str) -> bool:
        if _ALL_SUFFIX_RE.search(name):
            return False
        if name.upper().startswith("TSG"):
            return True
        if wg_short and re.match(rf"^{re.escape(wg_short)}-\d", name, re.IGNORECASE):
            return True
        if wg_short and re.match(rf"^{re.escape(wg_short)}_\d", name, re.IGNORECASE):
            return True
        if re.match(rf"^{re.escape(tsg)}_\d", name, re.IGNORECASE):
            return True
        return False

    entries = [e for e in all_entries if is_meeting(e.name)]
    skipped = len(all_entries) - len(entries)
    if skipped:
        log.info("Skipped %d non-meeting folder(s) under %s", skipped, url)
    log.info("Found %d meeting folders under %s", len(entries), url)
    return entries


def list_folder(client, folder_url: str) -> list[Entry]:
    if not folder_url.endswith("/"):
        folder_url += "/"
    resp = client.get(folder_url)
    return parse_directory_listing(resp.text)


def list_docs_folder(client, meeting_url: str) -> list[Entry]:
    """Raw listing of a meeting's Docs/ folder — every entry, unfiltered.
    Returns [] if there's no Docs/ folder yet (e.g. an upcoming meeting).
    Fetched once and reused for both the TDoc zip list and the
    TDoc_List_Meeting_*.xlsx lookup, rather than fetching Docs/ twice."""
    entries = list_folder(client, meeting_url)
    docs_dir = next((e for e in entries if e.is_dir and e.name == "Docs"), None)
    if docs_dir is None:
        return []
    return list_folder(client, docs_dir.url)


def list_docs(client, meeting_url: str) -> list[Entry]:
    """TDoc zip files in a meeting's Docs/ folder — see list_docs_folder.

    Filtered to .zip only: Docs/ can also contain stray non-TDoc files —
    e.g. the TDoc_List_Meeting_*.xlsx export itself sits right alongside
    the real zips in every group — which would otherwise be ingested as
    a fake TDoc once its extension was stripped."""
    return [e for e in list_docs_folder(client, meeting_url) if not e.is_dir and e.name.lower().endswith(".zip")]


_TDOC_LIST_IN_DOCS_RE = re.compile(r"^tdoc_list_meeting.*\.xlsx$", re.IGNORECASE)


def find_tdoc_list_in_docs(docs_entries: list[Entry]) -> list[Entry]:
    """Pick TDoc_List_Meeting_*.xlsx candidates out of an already-fetched
    Docs/ listing. This is where most groups keep their TDoc list export
    (confirmed for RAN1/RAN3/RAN4/RAN5/CT1 — initially missed because
    only RAN2's separate Tdoclists/ folder, checked below, was assumed to
    be the only mechanism). A group can have more than one candidate
    (e.g. RAN1 had a venue-suffixed duplicate); pick_latest_tdoc_list
    resolves that the same way it resolves Tdoclists/'s multiple
    snapshots."""
    return [e for e in docs_entries if not e.is_dir and _TDOC_LIST_IN_DOCS_RE.match(e.name)]


def list_tdoclists(client, meeting_url: str) -> list[Entry]:
    """RAN2-specific (so far): a separate folder with intra-week
    timestamped snapshots, checked before find_tdoc_list_in_docs since
    it's the more current/authoritative source when present."""
    entries = list_folder(client, meeting_url)
    tdoclists_dir = next((e for e in entries if e.is_dir and e.name == "Tdoclists"), None)
    if tdoclists_dir is None:
        return []
    return list_folder(client, tdoclists_dir.url)
