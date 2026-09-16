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

  // Prefer the rendered PDF (real formatting) over the flat extracted
  // text, matching TDocHamster's preview. /view renders on demand, so any
  // downloaded TDoc gets a PDF attempt — not just ones already rendered;
  // the first click on a given TDoc takes a few seconds (LibreOffice
  // conversion), every one after that is instant (cached on disk). The
  // "rendering" hint has an id so its onload handler can hide it again —
  // previously it stayed visible forever once shown.
  let bodyBlock;
  if (d.local_zip_path) {
    const hintId = `hint-${cssEscape(d.tdoc_id)}`;
    const firstViewHint = d.rendered_path
      ? ""
      : `<div class="no-text" id="${hintId}">Rendering for the first time — may take a few seconds…</div>`;
    bodyBlock = `${firstViewHint}<iframe class="pdf-frame" src="${API}/tdocs/${encodeURIComponent(d.tdoc_id)}/view" onload="document.getElementById('${hintId}')?.remove()"></iframe>`;
  } else if (d.text) {
    bodyBlock = `<div class="detail-text">${escapeHtml(d.text)}</div>`;
  } else {
    bodyBlock = `<div class="no-text">No preview available${d.extraction_status ? ` (${d.extraction_status})` : ""}.</div>`;
  }

  return `
    <div class="tdoc-id">${d.tdoc_id}</div>
    <div class="detail-title">${escapeHtml(d.title || "(no title)")}</div>
    <div class="detail-meta">${metaRows.map(([k, v]) => `<b>${k}:</b> ${escapeHtml(String(v))}`).join("<br>")}</div>
    <div class="detail-actions">${downloadLink}</div>
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

function cssEscape(s) {
  return s.replace(/[^a-zA-Z0-9_-]/g, "_");
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

// Lets the homepage deep-link straight into a filtered result set, e.g.
// /search.html?tsg=ran&wg=RAN1&meeting=TSGR1_126 from a meeting card.
async function initFromUrl() {
  const p = new URLSearchParams(location.search);
  const q = p.get("q");
  const tsg = p.get("tsg");
  const wg = p.get("wg");
  const meeting = p.get("meeting");

  if (q) el("q").value = q;
  if (tsg && GROUPS[tsg]) {
    el("tsg").value = tsg;
    populateSelect(el("wg"), GROUPS[tsg], "All groups");
    if (wg) el("wg").value = wg;
    await refreshMeetingFilter();
    if (meeting) el("meeting").value = meeting;
  }
  search(0);
}

initFromUrl();
