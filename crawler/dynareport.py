"""3GPP's own meeting-calendar pages (https://www.3gpp.org/dynareport?code=Meetings-<code>.htm),
one per working group — a second, independent data source from the FTP
document archive the rest of this crawler is built on.

Used for two things, both cases the Invitation-document approach
(meeting_info.py) can never cover on its own:

1. Calendar-only entries with no FTP folder at all, e.g. RAN5's "TTCN
   Workshop#75" — a real, dated event with no Invitation/Docs directory to
   crawl (its "Town" column link points at a broken placeholder instead of
   a real /ftp/... path, same as any not-yet-backfilled future meeting —
   the only reliable signal is the meeting-code format: a real meeting is
   "<code>-<number>", e.g. "R5-113"; a calendar-only entry uses a double
   hyphen and a non-numeric title, e.g. "R5--TTCN Workshop#75").
2. A real numbered meeting that has actual downloaded documents but no
   Invitation document of its own (observed: SA4#137-e — an electronic
   pre-session with ~480 real TDocs and its own row on this page, Aug
   24-28 2026, but no Invitation folder anywhere on the FTP, so it stayed
   permanently dateless and lost the "Previous" slot to an older, mostly-
   empty meeting). Tried only as a fallback after the Invitation-document
   approach fails, using the exact same title strings.
"""
import logging
import re

from bs4 import BeautifulSoup

log = logging.getLogger("crawler.dynareport")

BASE_URL = "https://www.3gpp.org/dynareport"

# tsg:wg_short -> dynareport code. Plenary and WG codes follow different
# conventions (verified against the live pages, not guessable from the
# FTP wg_path): plenary is "<T>P" (RP/SP/CP), a WG is "<T><n>" (R1..R5,
# S1..S6, C1/C3/C4/C6). RANAH1 has no dynareport page found — skipped.
DYNAREPORT_CODES = {
    ("ran", "plenary"): "RP",
    ("ran", "RAN1"): "R1",
    ("ran", "RAN2"): "R2",
    ("ran", "RAN3"): "R3",
    ("ran", "RAN4"): "R4",
    ("ran", "RAN5"): "R5",
    ("sa", "plenary"): "SP",
    ("sa", "SA1"): "S1",
    ("sa", "SA2"): "S2",
    ("sa", "SA3"): "S3",
    ("sa", "SA4"): "S4",
    ("sa", "SA5"): "S5",
    ("sa", "SA6"): "S6",
    ("ct", "plenary"): "CP",
    ("ct", "CT1"): "C1",
    ("ct", "CT3"): "C3",
    ("ct", "CT4"): "C4",
    ("ct", "CT6"): "C6",
}

_DATE_RE = re.compile(r"(\d{4})\D(\d{2})\D(\d{2})")
_MTGID_RE = re.compile(r"MtgId=(\d+)", re.IGNORECASE)
# A double-hyphen row covers far more than workshops — the same dynareport
# pages list internal ad-hoc SWG calls, conference calls, social events and
# hotel/coach bookings under the identical table structure (confirmed:
# SA4's page alone had ~25 of these, several dated in the future — one
# would have silently hijacked SA4's "Upcoming" WG-meeting slot). None of
# those are real attend-able meetings the way a workshop is, and
# TDocHamster's own data (the only concrete case checked) treats only the
# "Workshop" category as a real calendar meeting — so this is deliberately
# narrow rather than accepting every double-hyphen row.
_WORKSHOP_RE = re.compile(r"workshop", re.IGNORECASE)
# 3GPP keeps a cancelled meeting's row on the calendar page indefinitely,
# still carrying its originally-planned dates (observed: SA4#137, title
# "3GPPSA4#137 CANCELLED" — TDocHamster shows this as a real "Upcoming"
# meeting, which is exactly the mistake this guards against).
_CANCELLED_RE = re.compile(r"cancel", re.IGNORECASE)


class MeetingRow:
    def __init__(self, label, title, town, start_date, end_date, portal_url):
        self.label = label
        self.title = title
        self.town = town
        self.start_date = start_date
        self.end_date = end_date
        self.portal_url = portal_url


def _parse_cell_date(text: str):
    m = _DATE_RE.search(text)
    if not m:
        return None
    year, month, day = m.groups()
    return f"{year}-{month}-{day}"


def fetch_meeting_table(client, tsg: str, wg_short: str) -> list:
    """Every row of this WG's dynareport page, raw — both real numbered
    meetings and calendar-only entries. One HTTP fetch; callers filter."""
    code = DYNAREPORT_CODES.get((tsg, wg_short))
    if code is None:
        return []

    url = f"{BASE_URL}?code=Meetings-{code}.htm"
    resp = client.get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if table is None:
        return []

    rows = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        label = cells[0].get_text(strip=True)
        title = cells[1].get_text(strip=True)
        town = cells[2].get_text(strip=True) or None
        start_date = _parse_cell_date(cells[3].get_text(strip=True))
        end_date = _parse_cell_date(cells[4].get_text(strip=True)) or start_date
        portal_link = cells[0].find("a")
        portal_url = portal_link.get("href") if portal_link else url
        rows.append(MeetingRow(label, title, town, start_date, end_date, portal_url))
    return rows


def calendar_only_entries(rows: list, tsg: str, wg_short: str) -> list:
    """Filters fetch_meeting_table's rows down to workshop-style entries
    with no FTP folder of their own (see module docstring, case 1)."""
    code = DYNAREPORT_CODES.get((tsg, wg_short))
    if code is None:
        return []
    special_re = re.compile(rf"^{re.escape(code)}--(.+)$")

    entries = []
    for row in rows:
        m = special_re.match(row.label)
        if not m:
            continue  # a normal "<code>-<number>" meeting
        title = row.title or m.group(1).strip()
        if not _WORKSHOP_RE.search(title) and not _WORKSHOP_RE.search(row.label):
            continue  # a conference call / social event / ad-hoc SWG call etc. — not a real meeting
        if row.start_date is None:
            continue

        # The "Meeting" column's visible text is sometimes truncated by
        # 3GPP's own page for a long label (observed on other, non-workshop
        # rows) — using it as-is risks two different entries colliding on
        # the same truncated key and silently overwriting each other.
        # 3GPP's own numeric meeting id (embedded in the portal link) is
        # stable and guaranteed unique; fall back to the label only if
        # that id can't be found.
        mtgid_m = _MTGID_RE.search(row.portal_url or "")
        folder_key = f"{code}--workshop-{mtgid_m.group(1)}" if mtgid_m else row.label
        entries.append((folder_key, title, row))
    return entries


def find_dated_meeting(rows: list, expected_titles):
    """Matches a real numbered meeting by the exact same title strings
    meeting_info.py tries against an Invitation document — dynareport's
    Title column uses the identical '3GPP<WG>#<n>[suffix]' format, so no
    separate title-construction logic is needed here. Skips any row whose
    title flags it as cancelled (see module docstring, case 2)."""
    from .meeting_info import MeetingInfo, _normalize_title

    targets = {_normalize_title(t) for t in expected_titles}
    for row in rows:
        if not row.title or row.start_date is None:
            continue
        if _CANCELLED_RE.search(row.title):
            continue
        if _normalize_title(row.title) in targets:
            return MeetingInfo(start_date=row.start_date, end_date=row.end_date, location=row.town, country=None)
    return None
