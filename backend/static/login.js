(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const form = $("login-form"), btn = $("login-btn"), pw = $("password");

  function showError(message) {
    $("login-error-text").textContent = message;
    $("login-error").hidden = !message;
  }

  $("toggle-pw").addEventListener("click", (e) => {
    const show = pw.type === "password";
    pw.type = show ? "text" : "password";
    const b = e.currentTarget;
    b.setAttribute("aria-pressed", String(show));
    b.setAttribute("aria-label", show ? "Hide password" : "Show password");
    b.querySelector("use").setAttribute("href", show ? "#i-eye-off" : "#i-eye");
    pw.focus();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = $("email").value.trim();
    if (!email || !pw.value) {
      showError("Enter your email and password.");
      (email ? pw : $("email")).focus();
      return;
    }
    showError("");
    btn.disabled = true; btn.setAttribute("aria-busy", "true"); btn.textContent = "Signing in…";
    try {
      const resp = await fetch("/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password: pw.value }),
      });
      if (resp.ok) { location.replace("/"); return; }
      const body = await resp.json().catch(() => ({}));
      showError(typeof body.detail === "string" ? body.detail : "Couldn't sign in. Please try again.");
      pw.select();
    } catch {
      showError("Can't reach the server. Check your connection and try again.");
    }
    btn.disabled = false; btn.removeAttribute("aria-busy"); btn.textContent = "Sign in";
  });
})();
