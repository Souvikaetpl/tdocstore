async function initAdminPage() {
  const gate = document.getElementById("adminGate");
  const me = await (await fetch("/auth/me")).json();

  if (!me.signed_in) {
    gate.innerHTML = `<p class="no-text">Sign in to access this page.</p>`;
    return;
  }
  if (!me.is_admin) {
    gate.innerHTML = `<p class="no-text">Admins only.</p>`;
    return;
  }

  gate.innerHTML = `
    <section class="admin-section">
      <h2>Create test account</h2>
      <form id="createForm" class="admin-create-form">
        <input type="text" name="username" placeholder="Username" required>
        <input type="text" name="display_name" placeholder="Display name (optional)">
        <input type="password" name="password" placeholder="Password" required>
        <button type="submit">Create</button>
      </form>
      <div id="createError" class="admin-error" hidden></div>
    </section>
    <section class="admin-section">
      <h2>Users</h2>
      <table class="admin-users-table">
        <thead><tr><th>Email</th><th>Display name</th><th>Admin</th><th>Status</th><th>MCP access</th><th></th><th></th></tr></thead>
        <tbody id="usersBody"></tbody>
      </table>
    </section>
  `;

  document.getElementById("createForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = {
      username: f.username.value,
      password: f.password.value,
      display_name: f.display_name.value || null,
    };
    const res = await fetch("/auth/admin/test-accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const errEl = document.getElementById("createError");
    if (res.ok) {
      errEl.hidden = true;
      f.reset();
      loadUsers();
    } else {
      const data = await res.json().catch(() => ({}));
      errEl.textContent = data.detail || "Could not create account.";
      errEl.hidden = false;
    }
  });

  loadUsers();
}

async function loadUsers() {
  const users = await (await fetch("/auth/admin/users")).json();
  document.getElementById("usersBody").innerHTML = users.map(u => `
    <tr>
      <td>${escapeHtmlAdmin(u.email)}</td>
      <td>${escapeHtmlAdmin(u.display_name || "")}</td>
      <td>${u.is_admin ? "Yes" : ""}</td>
      <td>${u.status}</td>
      <td>${u.mcp_access ? "on" : "off"}</td>
      <td><button type="button" class="toggle-status" data-id="${u.id}" data-status="${u.status}">
        ${u.status === "active" ? "Disable" : "Enable"}
      </button></td>
      <td><button type="button" class="toggle-mcp-access" data-id="${u.id}" data-enabled="${u.mcp_access}">
        ${u.mcp_access ? "Disable MCP" : "Enable MCP"}
      </button></td>
    </tr>
  `).join("");

  document.querySelectorAll(".toggle-status").forEach(btn => {
    btn.addEventListener("click", async () => {
      const newStatus = btn.dataset.status === "active" ? "disabled" : "active";
      await fetch(`/auth/admin/users/${btn.dataset.id}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: newStatus }),
      });
      loadUsers();
    });
  });

  document.querySelectorAll(".toggle-mcp-access").forEach(btn => {
    btn.addEventListener("click", async () => {
      const enabled = btn.dataset.enabled !== "true";
      await fetch(`/auth/admin/users/${btn.dataset.id}/mcp-access`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      loadUsers();
    });
  });
}

function escapeHtmlAdmin(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

document.addEventListener("DOMContentLoaded", initAdminPage);
