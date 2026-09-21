const API = "/api";

const GROUPS = {
  ran: ["plenary", "RAN1", "RAN2", "RAN3", "RAN4", "RAN5", "RANAH1"],
  sa: ["plenary", "SA1", "SA2", "SA3", "SA4", "SA5", "SA6"],
  ct: ["plenary", "CT1", "CT3", "CT4", "CT6"],
};
const DOC_TYPES = ["agenda", "report", "discussion", "CR", "draftCR", "draft TR",
                    "LS in", "LS out", "Work Plan", "pCR", "response", "other"];

const state = { offset: 0, limit: 20, total: 0, items: [], selected: new Set() };

const el = (id) => document.getElementById(id);

function populateSelect(select, values, placeholder) {
  select.innerHTML = `<option value="">${placeholder}</option>` +
    values.map(v => `<option value="${v}">${v}</option>`).join("");
}

async function refreshMeetingFilter() {
  const tsg = el("tsg").value;
  const wg = el("wg").value;
  const meetingSelect = el("meeting");
  const previouslySelected = meetingSelect.value;

  if (!tsg || !wg) {
    populateSelect(meetingSelect, [], "All meetings");
    meetingSelect.disabled = true;
    return;
  }

  meetingSelect.disabled = false;
  const p = new URLSearchParams({ tsg, wg, limit: "100" });
  const res = await fetch(`${API}/meetings?${p}`);
  const data = await res.json();
  const names = data.items.map(m => m.meeting_folder);
  populateSelect(meetingSelect, names, "All meetings");
  if (names.includes(previouslySelected)) meetingSelect.value = previouslySelected;
}

el("tsg").addEventListener("change", () => {
  const tsg = el("tsg").value;
  populateSelect(el("wg"), GROUPS[tsg] || [], "All groups");
  refreshMeetingFilter();
});
el("wg").addEventListener("change", refreshMeetingFilter);
populateSelect(el("doc_type"), DOC_TYPES, "All types");
el("meeting").disabled = true;

function currentParams(offset) {
  const p = new URLSearchParams();
  const q = el("q").value.trim();
  const tsg = el("tsg").value;
  const wg = el("wg").value;
  const meeting = el("meeting").value;
  const source = el("source").value.trim();
  const docType = el("doc_type").value;
  if (q) p.set("q", q);
  if (tsg) p.set("tsg", tsg);
  if (wg) p.set("wg", wg);
  if (meeting) p.set("meeting", meeting);
  if (source) p.set("source", source);
  if (docType) p.set("doc_type", docType);
  p.set("offset", offset);
  p.set("limit", state.limit);
  return p;
}

async function search(offset = 0) {
  state.offset = offset;
  state.selected.clear();
  updateCompareBtn();
  el("listMeta").textContent = "Searching…";
  el("resultsBody").innerHTML = "";

  const res = await fetch(`${API}/tdocs?${currentParams(offset)}`);
  const data = await res.json();
  state.items = data.items;
  state.total = data.total;
  renderResults();
  if (offset === 0) loadRecentSearches();
}

function renderResults() {
  el("listMeta").textContent = state.total
    ? `${state.total} result(s)`
    : "No results";

  el("resultsBody").innerHTML = state.items.map(item => `
    <div class="results-grid-row result-row" data-id="${item.tdoc_id}">
      <div><input type="checkbox" class="sel" data-id="${item.tdoc_id}"></div>
      <div class="tdoc-id">${item.tdoc_id}</div>
      <div>${escapeHtml(item.title || "(no title)")}</div>
      <div>${escapeHtml(item.source || "")}</div>
      <div>${item.status ? `<span class="status-badge">${item.status}</span>` : ""}</div>
      <div>${formatDate(item.uploaded_at)}</div>
    </div>
  `).join("");

  document.querySelectorAll("#resultsBody .result-row").forEach(row => {
    row.addEventListener("click", (e) => {
      if (e.target.classList.contains("sel")) return;
      loadDetail(row.dataset.id);
    });
  });

  document.querySelectorAll("#resultsBody .sel").forEach(cb => {
    cb.addEventListener("change", () => {
      if (cb.checked) state.selected.add(cb.dataset.id);
      else state.selected.delete(cb.dataset.id);
      updateCompareBtn();
    });
  });

  el("prevBtn").disabled = state.offset === 0;
  el("nextBtn").disabled = state.offset + state.limit >= state.total;
  const page = Math.floor(state.offset / state.limit) + 1;
  const pages = Math.max(1, Math.ceil(state.total / state.limit));
  el("pageInfo").textContent = `Page ${page} / ${pages}`;
}

function updateCompareBtn() {
  const n = state.selected.size;
  const btn = el("compareBtn");
  btn.textContent = `Compare selected (${n})`;
  btn.disabled = n < 2 || n > 3;
}

async function loadDetail(tdocId) {
  document.querySelectorAll("#resultsBody .result-row").forEach(row =>
    row.classList.toggle("selected", row.dataset.id === tdocId));

  showPreviewHtml(`<div class="no-text">Loading ${escapeHtml(tdocId)}…</div>`);

  const res = await fetch(`${API}/tdocs/${encodeURIComponent(tdocId)}`);
  if (!res.ok) {
    showPreviewHtml(`<div class="no-text">Not found: ${escapeHtml(tdocId)}</div>`);
    return;
  }
  const d = await res.json();
  showPreviewHtml(renderDetailCard(d));
}

function renderDetailCard(d) {
  const metaRows = [
    ["Source", d.source], ["Type", d.doc_type], ["Status", d.status],
    ["Meeting", `${d.tsg}/${d.wg_short}/${d.meeting_folder}`],
    ["Specification", d.specification], ["Release", d.release],
    ["Agenda item", d.agenda_item], ["Work item(s)", d.related_wis],
    ["CR", d.cr_number ? `${d.cr_number} rev ${d.cr_revision || "-"} (${d.cr_category || "?"})` : null],
  ].filter(([, v]) => v);

  const downloadLink = d.local_zip_path
    ? `<a href="${API}/tdocs/${encodeURIComponent(d.tdoc_id)}/download">Download original</a>`
    : `<span class="no-text">Not downloaded yet</span>`;

  // d.bookmarked is only present when a signed-in user's cookie was
  // sent with the /api/tdocs/{id} request (see api/main.py) — omitted
  // entirely for a signed-out visitor, so the button just doesn't
  // render rather than showing something that would 401 on click.
  const bookmarkBtn = d.bookmarked === undefined
    ? ""
    : `<button type="button" class="bookmark-toggle-btn" data-id="${escapeHtml(d.tdoc_id)}"
         data-bookmarked="${d.bookmarked}">${d.bookmarked ? "★ Bookmarked" : "☆ Bookmark"}</button>`;

  // Prefer the rendered PDF (real formatting) over the flat extracted
  // text, matching TDocHamster's preview. /view renders on demand, so any
  // downloaded TDoc gets a PDF attempt — not just ones already rendered;
  // the first click on a given TDoc takes a few seconds (LibreOffice
  // conversion), every one after that is instant (cached on disk).
  // The preview itself is pdf.js's own prebuilt viewer app (viewer.html),
  // not a hand-rolled canvas renderer — it's loaded in an <iframe>, which
  // is what gives it the full toolbar (find, page nav, rotate, scroll/
  // spread modes, print, download) for free, and also means several
  // instances can sit side by side in the compare view without any of
  // them sharing state, since each iframe is its own document.
  let bodyBlock;
  if (d.local_zip_path) {
    const viewUrl = `${API}/tdocs/${encodeURIComponent(d.tdoc_id)}/view`;
    const viewerSrc = `/static/vendor/pdfjs-viewer/web/viewer.html?file=${encodeURIComponent(viewUrl)}`;
    bodyBlock = `<iframe class="pdfv-frame" src="${viewerSrc}" title="Preview of ${escapeHtml(d.tdoc_id)}"></iframe>`;
  } else if (d.text) {
    bodyBlock = `<div class="detail-text">${escapeHtml(d.text)}</div>`;
  } else {
    bodyBlock = `<div class="no-text">No preview available${d.extraction_status ? ` (${d.extraction_status})` : ""}.</div>`;
  }

  return `
    <div class="tdoc-id">${d.tdoc_id}</div>
    <div class="detail-title">${escapeHtml(d.title || "(no title)")}</div>
    <div class="detail-meta">${metaRows.map(([k, v]) => `<b>${k}:</b> ${escapeHtml(String(v))}`).join("<br>")}</div>
    <div class="detail-actions">${downloadLink} ${bookmarkBtn}</div>
    ${bodyBlock}
  `;
}

async function compareSelected() {
  const ids = [...state.selected];
  showPreviewHtml(`<div class="no-text">Loading comparison…</div>`);
  const p = new URLSearchParams();
  ids.forEach(id => p.append("ids", id));
  const res = await fetch(`${API}/tdocs/compare?${p}`);
  const data = await res.json();
  showPreviewHtml(`<div class="compare-grid">${
    data.items.map(d => `<div class="compare-col">${renderDetailCard(d)}</div>`).join("")
  }</div>`);
}

function showPreviewHtml(html) {
  el("previewEmpty").hidden = true;
  el("previewContent").hidden = false;
  el("previewContent").innerHTML = html;
}

function formatDate(isoLike) {
  if (!isoLike) return "";
  return String(isoLike).split("T")[0];
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function clearFilters() {
  el("q").value = "";
  el("tsg").value = "";
  populateSelect(el("wg"), [], "All groups");
  populateSelect(el("meeting"), [], "All meetings");
  el("meeting").disabled = true;
  el("source").value = "";
  el("doc_type").value = "";
  search(0);
}

el("searchBtn").addEventListener("click", () => search(0));
el("clearBtn").addEventListener("click", clearFilters);
el("q").addEventListener("keydown", (e) => { if (e.key === "Enter") search(0); });
el("source").addEventListener("keydown", (e) => { if (e.key === "Enter") search(0); });
el("prevBtn").addEventListener("click", () => search(Math.max(0, state.offset - state.limit)));
el("nextBtn").addEventListener("click", () => search(state.offset + state.limit));
el("compareBtn").addEventListener("click", compareSelected);

const PARAM_LABELS = { q: "keyword", tsg: "TSG", wg: "group", meeting: "meeting", source: "source", doc_type: "type" };

function describeParams(params) {
  return Object.entries(params).map(([k, v]) => `${PARAM_LABELS[k] || k}: ${v}`).join(", ");
}

// Automatic, not an explicit save — the server records history itself
// on every fresh search (see api/main.py's search_tdocs route), so
// this just displays whatever's already there. Called after every
// search(0) below, not just on page load, so a search you just ran
// shows up immediately rather than only after a reload.
async function loadRecentSearches() {
  const container = el("recentSearches");
  const res = await fetch(`${API}/search-history`);
  if (!res.ok) {
    container.hidden = true;
    return;
  }
  const items = await res.json();
  if (items.length === 0) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  container.innerHTML = `<span class="recent-searches-label">Recent:</span> ` + items.map(item => `
    <a class="recent-search-chip" href="/search?${new URLSearchParams(item.params).toString()}">
      ${escapeHtml(describeParams(item.params))}
    </a>
  `).join("");
}

// Delegated rather than wired per-render: renderDetailCard runs for
// both the single-document view and each column of the compare view,
// and returns a plain HTML string (no post-insertion mount step),
// so one listener on the shared container covers every instance.
el("previewContent").addEventListener("click", async (e) => {
  const btn = e.target.closest(".bookmark-toggle-btn");
  if (!btn) return;
  const tdocId = btn.dataset.id;
  const nowBookmarked = btn.dataset.bookmarked !== "true";
  await fetch(`${API}/bookmarks/${encodeURIComponent(tdocId)}`, {
    method: nowBookmarked ? "POST" : "DELETE",
  });
  btn.dataset.bookmarked = String(nowBookmarked);
  btn.textContent = nowBookmarked ? "★ Bookmarked" : "☆ Bookmark";
});

// Lets the homepage deep-link straight into a filtered result set, e.g.
// /search.html?tsg=ran&wg=RAN1&meeting=TSGR1_126 from a meeting card.
async function initFromUrl() {
  const p = new URLSearchParams(location.search);
  const q = p.get("q");
  const tsg = p.get("tsg");
  const wg = p.get("wg");
  const meeting = p.get("meeting");
  const source = p.get("source");
  const docType = p.get("doc_type");

  if (q) el("q").value = q;
  if (tsg && GROUPS[tsg]) {
    el("tsg").value = tsg;
    populateSelect(el("wg"), GROUPS[tsg], "All groups");
    if (wg) el("wg").value = wg;
    await refreshMeetingFilter();
    if (meeting) el("meeting").value = meeting;
  }
  // These two were missing before saved searches needed a full,
  // lossless round-trip through the URL — a saved search with a
  // source or doc_type filter would otherwise silently drop it on Run.
  if (source) el("source").value = source;
  if (docType) el("doc_type").value = docType;
  search(0);
}

initFromUrl();
