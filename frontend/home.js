const API = "/api";

// Short, hardcoded descriptions — not pulled from 3GPP data (nothing in
// the crawled corpus describes what a WG *does*, only what it produced).
const WG_INFO = {
  ran: [
    ["plenary", "TSG RAN plenary", "Approves and coordinates work across all RAN working groups"],
    ["RAN1", "RAN1", "Radio Layer 1 (physical layer) specifications"],
    ["RAN2", "RAN2", "Radio Layer 2 and Layer 3 radio resource protocols"],
    ["RAN3", "RAN3", "UTRAN / E-UTRAN / NG-RAN architecture and network interfaces"],
    ["RAN4", "RAN4", "Radio performance, RF and demodulation requirements"],
    ["RAN5", "RAN5", "Mobile terminal conformance test specifications"],
    ["RANAH1", "RAN AH1", "Ad-hoc group coordinating with ITU-R"],
  ],
  sa: [
    ["plenary", "TSG SA plenary", "Approves and coordinates work across all SA working groups"],
    ["SA1", "SA1", "Services requirements"],
    ["SA2", "SA2", "System and services architecture"],
    ["SA3", "SA3", "Security and privacy"],
    ["SA4", "SA4", "Codecs, media handling and streaming"],
    ["SA5", "SA5", "Telecom management, charging and orchestration"],
    ["SA6", "SA6", "Mission-critical and application-layer services"],
  ],
  ct: [
    ["plenary", "TSG CT plenary", "Approves and coordinates work across all CT working groups"],
    ["CT1", "CT1", "Mobility management, call control and session management protocols"],
    ["CT3", "CT3", "Interworking with external (fixed/packet) networks"],
    ["CT4", "CT4", "Core network protocols and interworking aspects"],
    ["CT6", "CT6", "Smart card (UICC/USIM) application aspects"],
  ],
};

const TSG_LABELS = {
  ran: "Radio Access Networks (RAN)",
  sa: "Service & System Aspects (SA)",
  ct: "Core Network & Terminals (CT)",
};

const el = (id) => document.getElementById(id);
let activeTsg = "ran";

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function formatOneDate(d) {
  const [, m, day] = d.split("-");
  return `${parseInt(day, 10)} ${MONTHS[parseInt(m, 10) - 1]}`;
}

function formatDateRange(start, end) {
  if (!start) return null;
  const year = start.split("-")[0];
  const startStr = formatOneDate(start);
  const endStr = end ? formatOneDate(end) : null;
  return endStr && endStr !== startStr ? `${startStr} – ${endStr}, ${year}` : `${startStr}, ${year}`;
}

// "TSGC1_162_Prague" -> "#162", "CT6-127_Prague_2026-08" -> "#127" — same
// "first digit run after _ or -" rule the crawler uses to build the
// expected invitation title (crawler/ingest.py:_expected_meeting_titles),
// so the displayed number always matches what parsing looked for.
//
// 0 is the "couldn't parse a number at all" sentinel (e.g. RAN AH1's
// legacy "TSGRT_ALL" aggregate folder) — callers must treat it as "no
// signal", never as a real, low/soonest meeting number.
function meetingNumber(folder) {
  const match = folder.match(/[_-](\d+)/);
  return match ? parseInt(match[1], 10) : 0;
}

// Same suffix markers crawler/ingest.py's _SUFFIX_HINT_RE looks for —
// "126b"/"126bis" and "137-e" are a distinct session from the base
// numbered meeting, and displaying both as just "#126"/"#137" makes two
// different meetings look identical in the UI.
function meetingSuffix(folder) {
  if (/bis/i.test(folder)) return "bis";
  if (/\db(?:[_-]|$)/i.test(folder)) return "bis"; // "126b" shorthand for "126bis"
  if (/\de(?:[_-]|$)/i.test(folder) || /[_-]e(?:[_-]|$)/i.test(folder)) return "e";
  return "";
}

function meetingTitle(tsg, wg, folder) {
  const num = meetingNumber(folder);
  if (!num) return folder;
  const label = wg === "plenary" ? tsg.toUpperCase() : wg;
  return `3GPP${label}#${num}${meetingSuffix(folder)}`;
}

// The crawler now selects which meetings to download by REAL date (a
// wider candidate pool gets date-checked, only the actual next-upcoming +
// most-recent-past ones get downloaded — see
// crawler/ingest.py:_select_meetings_to_download). So the DB can hold
// more than 2 tracked meetings per group, and picking "top 2 by meeting
// number" here would drift back to the exact bug that fixed: a
// numerically-later placeholder meeting (dated further out, or not dated
// at all) outranking the real next one.
//
// This mirrors that same real-date-first logic: meetings with a known
// past date sort latest-first; everything else (future-dated, or
// undated) sorts by date when known, falling back to meeting number only
// when neither candidate has a date yet — so an undated meeting still
// gets a chance to show (never silently dropped), it just ranks behind
// any meeting we do have a real date for.
function classifyMeetings(meetings) {
  const today = new Date().toISOString().slice(0, 10);
  const dateOf = (m) => m.end_date || m.start_date;

  const past = meetings
    .filter((m) => dateOf(m) && dateOf(m) < today)
    .sort((a, b) => dateOf(b).localeCompare(dateOf(a)) || meetingNumber(b.meeting_folder) - meetingNumber(a.meeting_folder));

  // An undated meeting numerically at or below the highest CONFIRMED
  // past meeting can't actually be upcoming — meeting numbers only
  // increase over time, so it's just another past meeting we don't have
  // a date for yet (observed: SA1#114 undated, #115 confirmed past —
  // without this, #114 wins "Upcoming" by default, showing a LOWER
  // number as if it comes AFTER a higher one already marked "Previous").
  const maxPastNumber = past.length
    ? Math.max(...past.map((m) => meetingNumber(m.meeting_folder)))
    : -1;

  const future = meetings
    .filter((m) => !past.includes(m))
    .filter((m) => dateOf(m) || meetingNumber(m.meeting_folder) > maxPastNumber)
    .sort((a, b) => {
      const da = dateOf(a), db_ = dateOf(b);
      if (da && db_) return da.localeCompare(db_);
      if (da) return -1;
      if (db_) return 1;
      // 0 = "couldn't parse a number at all" (e.g. RAN AH1's legacy
      // "TSGRT_ALL" aggregate folder) — must sort LAST, not first, or an
      // unparseable junk folder wins the "soonest" slot by default.
      const na = meetingNumber(a.meeting_folder) || Infinity;
      const nb = meetingNumber(b.meeting_folder) || Infinity;
      return na - nb;
    });

  return { upcoming: future[0] || null, previous: past[0] || null };
}

// The "Upcoming"/"Previous" cell LABEL is a fixed column position, but a
// meeting's actual real-time status changes as its own dates pass —
// once today falls inside [start_date, end_date] it's genuinely
// "Ongoing", not "Upcoming" anymore, even though it still sits in the
// Upcoming column (it hasn't been superseded by a later meeting yet).
// null when we don't have a real start_date at all ("Date TBD" already
// covers that case, no badge to show).
function meetingLiveStatus(m) {
  if (!m.start_date) return null;
  const today = new Date().toISOString().slice(0, 10);
  const end = m.end_date || m.start_date;
  if (today < m.start_date) return "upcoming";
  if (today > end) return "past";
  return "ongoing";
}

const STATUS_BADGE_LABEL = { past: "Past", ongoing: "Ongoing", upcoming: "Upcoming" };

function meetingCell(tsg, wg, m, label, kind) {
  // kind ("upcoming"/"previous") drives the mobile grid-area mapping in
  // CSS — not nth-of-type, since this cell can render as either <a> or
  // <div> depending on whether m exists, which breaks same-tag-type
  // positional counting.
  if (!m) {
    return `
      <div class="wg-cell wg-cell-${kind}">
        <span class="cell-label">${label}</span>
        <div class="cell-empty">—</div>
      </div>`;
  }
  const dateRange = formatDateRange(m.start_date, m.end_date);
  const loc = m.location ? `${m.location}${m.country ? ", " + m.country : ""}` : null;
  const url = `/search?tsg=${tsg}&wg=${encodeURIComponent(wg)}&meeting=${encodeURIComponent(m.meeting_folder)}`;
  const liveStatus = meetingLiveStatus(m);
  const badge = liveStatus
    ? `<span class="status-badge status-${liveStatus}">${STATUS_BADGE_LABEL[liveStatus]}</span>`
    : "";
  // display_title overrides the computed "3GPP<WG>#<n>" title for a
  // meeting-calendar entry with no FTP folder to derive one from (see
  // crawler/dynareport.py) — e.g. RAN5's "TTCN Workshop#75".
  const title = m.display_title || meetingTitle(tsg, wg, m.meeting_folder);
  return `
    <a class="wg-cell wg-cell-link wg-cell-${kind}" href="${url}">
      <span class="cell-label">${label}</span>
      <div class="cell-title">${badge}<span>${escapeHtml(title)}</span></div>
      <div class="cell-sub">${dateRange ? escapeHtml(dateRange) : "Date TBD"}${loc ? " · " + escapeHtml(loc) : ""}</div>
      <div class="cell-count">${m.tdoc_count.toLocaleString()} doc${m.tdoc_count === 1 ? "" : "s"}</div>
    </a>`;
}

const WG_ICON = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 3h9l4 4v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/><path d="M14 3v5h5"/></svg>`;

async function loadGrid(tsg) {
  el("sectionTitle").textContent = TSG_LABELS[tsg];
  el("archiveLink").href = `/search?tsg=${tsg}`;
  el("archiveLink").textContent = `View complete ${tsg.toUpperCase()} archive →`;

  const list = el("wgList");
  list.innerHTML = "<div class='wg-loading'>Loading…</div>";

  const res = await fetch(`${API}/meetings?tsg=${tsg}&limit=100`);
  const data = await res.json();

  const byWg = {};
  for (const m of data.items) {
    (byWg[m.wg_short] = byWg[m.wg_short] || []).push(m);
  }

  const wgList = WG_INFO[tsg] || [];
  el("wgCountLabel").textContent = `${wgList.length} working groups`;

  list.innerHTML = wgList.map(([wg, name, desc]) => {
    const meetings = byWg[wg] || [];
    const totalDocs = meetings.reduce((sum, m) => sum + m.tdoc_count, 0);
    const { previous, upcoming } = classifyMeetings(meetings);

    return `
      <div class="wg-row">
        <div class="wg-icon">${WG_ICON}</div>
        <div class="wg-row-name">
          <div class="wg-row-title">${escapeHtml(name)}</div>
          <div class="wg-row-desc">${escapeHtml(desc)}</div>
        </div>
        ${meetingCell(tsg, wg, upcoming, "Upcoming", "upcoming")}
        ${meetingCell(tsg, wg, previous, "Previous", "previous")}
        <div class="wg-cell wg-cell-count">
          <strong>${totalDocs.toLocaleString()}</strong>
          <span>document${totalDocs === 1 ? "" : "s"}</span>
        </div>
        <a class="wg-view" href="/search?tsg=${tsg}&wg=${encodeURIComponent(wg)}">View &rarr;</a>
      </div>`;
  }).join("");
}

const PARAM_LABELS = { q: "keyword", tsg: "TSG", wg: "group", meeting: "meeting", source: "source", doc_type: "type" };

function describeParams(params) {
  return Object.entries(params).map(([k, v]) => `${PARAM_LABELS[k] || k}: ${v}`).join(", ");
}

// Replaces the old static "Popular searches" row — hidden entirely for
// a signed-out visitor or a signed-in one with no history yet, rather
// than falling back to a hardcoded list (see /search's own copy of
// this same logic for the other place it appears).
async function loadRecentSearchesHome() {
  const container = el("recentSearchesHome");
  const res = await fetch(`${API}/search-history`);
  if (!res.ok) return;
  const items = await res.json();
  if (items.length === 0) return;

  container.hidden = false;
  container.innerHTML = `<span class="recent-searches-label">Recent searches:</span> ` + items.map(item => `
    <a class="recent-search-chip" href="/search?${new URLSearchParams(item.params).toString()}">
      ${escapeHtml(describeParams(item.params))}
    </a>
  `).join("");
}

async function loadStats() {
  const res = await fetch(`${API}/stats`);
  const s = await res.json();
  el("statDocs").textContent = s.total_tdocs.toLocaleString() + "+";
  el("statGroups").textContent = s.total_groups;
  el("statTsgs").textContent = s.total_tsgs;
}

document.querySelectorAll(".tsg-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tsg-tab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    activeTsg = btn.dataset.tsg;
    loadGrid(activeTsg);
  });
});

el("heroSearchForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = el("heroQ").value.trim();
  location.href = `/search${q ? `?q=${encodeURIComponent(q)}` : ""}`;
});

loadStats();
loadGrid(activeTsg);
loadRecentSearchesHome();
