async function initBookmarksPage() {
  const gate = document.getElementById("bookmarksGate");
  const me = await (await fetch("/auth/me")).json();

  if (!me.signed_in) {
    gate.innerHTML = `<p class="no-text">Sign in to see your bookmarks.</p>`;
    return;
  }

  gate.innerHTML = `
    <section class="admin-section">
      <h2>Your bookmarks</h2>
      <table class="admin-users-table">
        <thead><tr><th>TDoc #</th><th>Title</th><th>Source</th><th>Bookmarked</th><th></th></tr></thead>
        <tbody id="bookmarksBody"></tbody>
      </table>
      <p id="bookmarksEmpty" class="no-text" hidden>No bookmarks yet — open any TDoc and click "Bookmark" to save it here.</p>
    </section>
  `;

  loadBookmarks();
}

async function loadBookmarks() {
  const data = await (await fetch("/api/bookmarks?limit=100")).json();
  const body = document.getElementById("bookmarksBody");
  const empty = document.getElementById("bookmarksEmpty");

  if (data.items.length === 0) {
    body.innerHTML = "";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  body.innerHTML = data.items.map(item => `
    <tr>
      <td><a href="/search?q=${encodeURIComponent(item.tdoc_id)}">${escapeHtmlBookmarks(item.tdoc_id)}</a></td>
      <td>${escapeHtmlBookmarks(item.title || "(no title)")}</td>
      <td>${escapeHtmlBookmarks(item.source || "")}</td>
      <td>${formatDateBookmarks(item.uploaded_at)}</td>
      <td><button type="button" class="remove-bookmark" data-id="${item.tdoc_id}">Remove</button></td>
    </tr>
  `).join("");

  document.querySelectorAll(".remove-bookmark").forEach(btn => {
    btn.addEventListener("click", async () => {
      await fetch(`/api/bookmarks/${encodeURIComponent(btn.dataset.id)}`, { method: "DELETE" });
      loadBookmarks();
    });
  });
}

function formatDateBookmarks(isoLike) {
  if (!isoLike) return "";
  return String(isoLike).split("T")[0];
}

function escapeHtmlBookmarks(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

document.addEventListener("DOMContentLoaded", initBookmarksPage);
