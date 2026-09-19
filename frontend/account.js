async function initAccountPage() {
  const gate = document.getElementById("accountGate");
  const me = await (await fetch("/auth/me")).json();

  if (!me.signed_in) {
    gate.innerHTML = `<p class="no-text">Sign in to access this page.</p>`;
    return;
  }

  const config = await (await fetch("/auth/config")).json();

  const generateBlock = me.mcp_access
    ? `<button type="button" id="newTokenBtn">Generate new token</button>
       <div id="newTokenBox" class="account-token-box" hidden>
         <div class="account-token-warning">Copy this now — it won't be shown again.</div>
         <code id="newTokenValue"></code>
       </div>
       <div id="newTokenError" class="admin-error" hidden></div>`
    : `<p class="account-hint" style="color:#b3413d;">
         MCP access has been disabled for your account by an administrator.
         Your website login is unaffected, but no new MCP token can be generated
         right now, and any existing ones no longer work.
       </p>`;

  gate.innerHTML = `
    <section class="admin-section">
      <h2>MCP server URL</h2>
      <p class="account-hint">
        Paste this into any MCP client (Claude.ai connectors, Claude Code, Claude Desktop, etc.)
        to connect it to this server. It's the same URL for every account — not secret, and not
        specific to you. What identifies you is signing in (for a client that only accepts a URL,
        you'll be redirected to log in and approve access) or the personal token below (for a
        client that lets you set a request header).
      </p>
      <div class="account-token-box">
        <code>${escapeHtmlAccount(config.mcp_server_url)}</code>
      </div>
    </section>
    <section class="admin-section">
      <h2>Personal MCP token</h2>
      <p class="account-hint">
        Lets an MCP client (Claude Code, Claude Desktop, etc.) connect to this
        server's data over the network, as you. Independent of your website
        login — revoking a token here does not sign you out of the site, and
        signing out of the site does not revoke any token.
      </p>
      ${generateBlock}
    </section>
    <section class="admin-section">
      <h2>Your tokens</h2>
      <table class="admin-users-table">
        <thead><tr><th>Name</th><th>Created</th><th>Status</th><th></th></tr></thead>
        <tbody id="tokensBody"></tbody>
      </table>
    </section>
  `;

  const newTokenBtn = document.getElementById("newTokenBtn");
  if (newTokenBtn) {
    newTokenBtn.addEventListener("click", async () => {
      const errEl = document.getElementById("newTokenError");
      errEl.hidden = true;
      const res = await fetch("/auth/mcp-tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!res.ok) {
        errEl.textContent = data.detail || "Could not generate a token.";
        errEl.hidden = false;
        return;
      }
      document.getElementById("newTokenValue").textContent = data.token;
      document.getElementById("newTokenBox").hidden = false;
      loadTokens();
    });
  }

  loadTokens();
}

async function loadTokens() {
  const tokens = await (await fetch("/auth/mcp-tokens")).json();
  document.getElementById("tokensBody").innerHTML = tokens.map(t => `
    <tr>
      <td>${escapeHtmlAccount(t.name || "(unnamed)")}</td>
      <td>${escapeHtmlAccount((t.created_at || "").split("T")[0])}</td>
      <td>${t.revoked_at ? "revoked" : "active"}</td>
      <td>${t.revoked_at ? "" : `<button type="button" class="revoke-token" data-id="${t.id}">Revoke</button>`}</td>
    </tr>
  `).join("");

  document.querySelectorAll(".revoke-token").forEach(btn => {
    btn.addEventListener("click", async () => {
      await fetch(`/auth/mcp-tokens/${btn.dataset.id}/revoke`, { method: "POST" });
      loadTokens();
    });
  });
}

function escapeHtmlAccount(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

document.addEventListener("DOMContentLoaded", initAccountPage);
