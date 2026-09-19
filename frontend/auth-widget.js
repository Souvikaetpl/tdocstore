async function mountAuthWidget(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const res = await fetch("/auth/me");
  const data = await res.json();

  if (data.signed_in) {
    el.innerHTML = `
      ${data.is_admin ? `<a class="auth-admin-link" href="/admin">Admin</a>` : ""}
      <a class="auth-admin-link" href="/account">Account</a>
      <span class="auth-email">${escapeHtml(data.display_name || data.email)}</span>
      <button type="button" class="auth-signout">Sign out</button>`;
    el.querySelector(".auth-signout").addEventListener("click", async () => {
      await fetch("/auth/logout", { method: "POST" });
      location.reload();
    });
    return;
  }

  el.innerHTML = `
    <a class="auth-signin" href="/auth/google/login">Sign in with Google</a>
    <button type="button" class="auth-testlink">Test account</button>
    <form class="auth-testform" hidden>
      <input type="text" name="username" placeholder="Username" autocomplete="username" required>
      <input type="password" name="password" placeholder="Password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
      <span class="auth-testform-error" hidden>Invalid username or password.</span>
    </form>`;

  const form = el.querySelector(".auth-testform");
  el.querySelector(".auth-testlink").addEventListener("click", () => {
    form.hidden = !form.hidden;
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = form.querySelector(".auth-testform-error");
    errorEl.hidden = true;
    const body = {
      username: form.elements.username.value,
      password: form.elements.password.value,
    };
    const loginRes = await fetch("/auth/local/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (loginRes.ok) {
      location.reload();
    } else {
      errorEl.hidden = false;
    }
  });
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

document.addEventListener("DOMContentLoaded", () => mountAuthWidget("authWidget"));
