(() => {
  "use strict";

  // ══ Utilities ═══════════════════════════════════════════════════════════
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 5000;
  const NOT_CONNECTED_AFTER_MS = 3 * 60 * 1000;

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c != null && c !== false) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return node;
  }
  function icon(name, cls = "icon") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", cls); svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`); svg.append(use);
    return svg;
  }
  const store = {
    get(k, fallback) { try { return localStorage.getItem(`tt.${k}`) ?? fallback; } catch { return fallback; } },
    set(k, v) { try { localStorage.setItem(`tt.${k}`, v); } catch { /* storage unavailable */ } },
  };

  const digits = (s) => String(s ?? "").replace(/\D/g, "");
  function local10(raw) {
    let d = digits(raw);
    if (d.length === 12 && d.startsWith("91")) d = d.slice(2);
    else if (d.length === 11 && d.startsWith("0")) d = d.slice(1);
    return d;
  }
  function fmtPhone(raw) {
    const d = local10(raw);
    return d.length === 10 ? `+91 ${d.slice(0, 5)} ${d.slice(5)}` : (raw || "—");
  }
  const fmtAmount = (d) => (d ? Number(d).toLocaleString("en-IN") : "");
  const showAmount = (a) => {
    const [whole, frac] = String(a ?? "").replace(/[^\d.]/g, "").split(".");
    return whole ? `₹${fmtAmount(whole)}${frac && Number(frac) ? `.${frac}` : ""}` : "—";
  };
  const fullTime = (iso) => new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  const shortTime = (iso) => new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  function relTime(iso) {
    const diff = (Date.now() - new Date(iso)) / 1000;
    if (diff < 45) return "Just now";
    if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
    const d = new Date(iso), now = new Date();
    const time = d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return `Today, ${time}`;
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return `Yesterday, ${time}`;
    return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
  }
  const callTime = (c) => c.dialed_at || c.created_at;
  const durationSeconds = (c) => (c.started_at && c.ended_at ? Math.max(0, (new Date(c.ended_at) - new Date(c.started_at)) / 1000) : null);
  function fmtSeconds(s) {
    s = Math.round(s);
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  }
  function periodLabel(value) {
    const [y, m] = value.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleString("en-IN", { month: "long", year: "numeric" });
  }
  const capital = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

  async function api(path, opts) {
    const resp = await fetch(path, opts);
    if (resp.status === 401) {  // session expired or signed out elsewhere
      location.replace("/login");
      throw new Error("Please sign in again");
    }
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (HTTP ${resp.status})`);
    return body;
  }

  function toast(title, message, kind = "ok") {
    const t = el("div", { class: `toast ${kind === "error" ? "toast-error" : ""}`, role: kind === "error" ? "alert" : "status" },
      icon(kind === "error" ? "alert" : "check"),
      el("div", { class: "toast-body" }, el("div", { class: "toast-title" }, title), message ? el("div", {}, message) : null),
      el("button", { class: "btn-icon", type: "button", "aria-label": "Dismiss", onclick: () => t.remove() }, icon("close")));
    $("toasts").append(t);
    setTimeout(() => t.remove(), kind === "error" ? 9000 : 5000);
  }

  function confirmDialog({ title, text, ok = "Confirm", danger = false }) {
    const dlg = $("confirm");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    const okBtn = $("confirm-ok");
    okBtn.textContent = ok;
    okBtn.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
    return new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      dlg.returnValue = "";
      dlg.showModal();
      okBtn.focus();
    });
  }

  // ══ Call status ═════════════════════════════════════════════════════════
  const STATUS = {
    queued:     { label: "Waiting", group: "live" },
    initiated:  { label: "Ringing", group: "live" },
    active:     { label: "On call", group: "live" },
    completed:  { label: "Completed", group: "completed" },
    failed:     { label: "Failed", group: "failed" },
    unanswered: { label: "Not connected", group: "failed" },
    cancelled:  { label: "Cancelled", group: "cancelled" },
  };
  function statusOf(c) {
    // A dialed call that never streamed to the bot within this window was not answered or not placed.
    if (c.status === "initiated" && Date.now() - new Date(callTime(c)) > NOT_CONNECTED_AFTER_MS) return "unanswered";
    return c.status;
  }
  const groupOf = (c) => (STATUS[statusOf(c)] || {}).group || "other";
  const statusLabel = (c) => (STATUS[statusOf(c)] || {}).label || c.status;
  const pill = (c) => el("span", { class: `pill pill-${statusOf(c)}` }, statusLabel(c));

  // ══ App state ═══════════════════════════════════════════════════════════
  const state = {
    me: null, users: [],
    page: null,
    config: { call_gap_seconds: 2, batch_max_rows: 500, default_voice: "shubh" },
    catalog: null,
    defaultVoice: store.get("defaultVoice", "shubh"),
    recent: [], recentLoaded: false,
    calls: [], callsLoaded: false, limit: 100, filter: "all", query: "", batchFilter: "", userFilter: "",
    batches: [],
    preview: null,
    openRef: null, lastFocus: null, highlight: null, lastOk: 0,
    dashStats: null, dashSearch: "", dashUserFilter: "",
    calYear: 2026, calMonth: 9, calEvents: [],
    adminOverview: null,
    tickets: [], ticketStats: null, ticketsSearch: "", ticketsStatusFilter: "", ticketsCatFilter: "", ticketsUserFilter: "", activeResolveTicketId: null,
    callbacksData: null, cbTypeFilter: "", cbSearch: "", cbUserFilter: "",
  };
  const voiceName = (id) => (state.catalog?.voices.find((v) => v.id === id)?.name) || capital(id);
  const isAdmin = () => state.me?.role === "admin";

  // ══ Router ══════════════════════════════════════════════════════════════
  const ROUTES = {
    "/": "start",
    "/dashboard": "dashboard",
    "/calendar": "calendar",
    "/tickets": "tickets",
    "/callbacks": "callbacks",
    "/admin-dashboard": "admin-dashboard",
    "/calls": "calls",
    "/voices": "voices",
    "/users": "users",
    "/settings": "settings",
  };
  const TITLES = {
    start: "Start calls",
    dashboard: "Agent Dashboard",
    calendar: "Follow-up Calendar",
    tickets: "Tickets & Incidents",
    callbacks: "Callbacks Scheduled",
    "admin-dashboard": "Admin Overview",
    calls: "Call logs",
    voices: "Voice library",
    users: "Users",
    settings: "Settings",
  };
  const ADMIN_PAGES = new Set(["admin-dashboard", "users", "settings"]);

  function navigate(href) {
    history.pushState(null, "", href);
    route();
    window.scrollTo(0, 0);
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-link]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || a.target) return;
    e.preventDefault();
    if (state.openRef) closeDrawer(false);
    navigate(a.getAttribute("href"));
  });
  window.addEventListener("popstate", route);

  function route() {
    let page = ROUTES[location.pathname] || "start";
    if (ADMIN_PAGES.has(page) && !isAdmin()) { history.replaceState(null, "", "/"); page = "start"; }
    const changed = page !== state.page;
    if (page !== "voices") player.stop();
    state.page = page;
    document.querySelectorAll("[data-page]").forEach((p) => (p.hidden = p.dataset.page !== page));
    document.querySelectorAll(".nav a").forEach((a) => {
      if (ROUTES[a.getAttribute("href")] === page) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    document.title = `${TITLES[page]} · Collection Voice Agent`;
    if (page === "dashboard") {
      const uParam = new URLSearchParams(location.search).get("user");
      if (uParam !== null) state.dashUserFilter = uParam;
      loadAgentStats();
    }
    if (page === "calendar") loadCalendar();
    if (page === "tickets") loadTickets();
    if (page === "callbacks") loadCallbacks();
    if (page === "admin-dashboard") loadAdminOverview();
    if (page === "calls") {
      const batch = new URLSearchParams(location.search).get("batch") || "";
      if (batch !== state.batchFilter) { state.batchFilter = batch; state.callsLoaded = false; state.calls = []; }
      renderCalls();
    }
    if (page === "voices") renderVoices();
    if (page === "users") loadUsers();
    if (changed || page === "calls") refresh();
    openFromHash();
  }

  // ══ Voice player (shared by form + library) ════════════════════════════
  const player = {
    audio: new Audio(),
    key: null, status: "idle", url: null, listeners: new Set(),
    keyOf: (voice, lang, text) => `${voice}|${lang}|${text}`,
    notify() { this.listeners.forEach((fn) => fn()); },
    stop() {
      this.audio.pause();
      if (this.key) { this.key = null; this.status = "idle"; this.notify(); }
    },
    async toggle(voice, lang, text) {
      const key = this.keyOf(voice, lang, text);
      if (this.key === key) { this.stop(); return; }
      this.stop();
      this.key = key; this.status = "loading"; this.notify();
      const sample = state.catalog?.samples?.[lang];
      const qs = new URLSearchParams({ language: lang });
      if (text && text !== sample) qs.set("text", text);
      try {
        const resp = await fetch(`/voices/${encodeURIComponent(voice)}/preview?${qs}`);
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${resp.status}`);
        }
        const blob = await resp.blob();
        if (this.key !== key) return;
        if (this.url) URL.revokeObjectURL(this.url);
        this.url = URL.createObjectURL(blob);
        this.audio.src = this.url;
        await this.audio.play();
        this.status = "playing"; this.notify();
      } catch (err) {
        if (this.key === key) { this.key = null; this.status = "idle"; this.notify(); }
        toast(`Couldn't play ${voiceName(voice)}`, err.message, "error");
      }
    },
  };
  player.audio.addEventListener("ended", () => { player.key = null; player.status = "idle"; player.notify(); });
  player.audio.addEventListener("timeupdate", () => player.notify());

  function setDefaultVoice(id, announce = true) {
    state.defaultVoice = id;
    store.set("defaultVoice", id);
    $("voice").value = id;
    $("vl-default").textContent = voiceName(id);
    $("bulk-default-voice").textContent = voiceName(id);
    if (state.page === "voices") renderVoices();
    if (announce) toast("Default voice updated", `New calls will use ${voiceName(id)}.`);
  }

  // ══ Start calls · single call ═══════════════════════════════════════════
  const form = $("call-form");
  const phone = $("phone"), amount = $("amount"), period = $("period"), nameIn = $("name");

  function setFieldError(field, message) {
    const wrap = form.querySelector(`[data-field="${field}"]`);
    const hint = wrap.querySelector(".hint");
    if (hint.dataset.default === undefined) hint.dataset.default = hint.textContent;
    const input = wrap.querySelector("input");
    if (message) { wrap.dataset.invalid = ""; hint.textContent = message; input.setAttribute("aria-invalid", "true"); }
    else { delete wrap.dataset.invalid; hint.textContent = hint.dataset.default; input.removeAttribute("aria-invalid"); }
  }
  function validateForm() {
    const errors = {};
    const p = local10(phone.value);
    if (!p) errors.phone_number = "Enter the customer's phone number";
    else if (p.length !== 10) errors.phone_number = "Phone number must have 10 digits";
    if (!nameIn.value.trim()) errors.customer_name = "Enter the customer's name";
    if (!(Number(digits(amount.value)) > 0)) errors.amount = "Enter the amount due";
    if (!period.value) errors.billing_period = "Select the billing month";
    const days = daysFromDue($("due").value);
    const predue = form.querySelector('input[name="call_type"]:checked').value === "predue";
    if (days !== null && predue && days >= 0) errors.due_date = days ? `This date passed ${plural(days, "day")} ago. Use Overdue collection, or pick a future date.` : "This is due today. Use Overdue collection, or pick a future date.";
    if (days !== null && !predue && days < 0) errors.due_date = `Not due for ${plural(-days, "day")}. Use Pre-due reminder, or check the date.`;
    ["phone_number", "customer_name", "amount", "billing_period", "due_date"].forEach((f) => setFieldError(f, errors[f]));
    return errors;
  }
  function updateCallLabel() {
    const p = local10(phone.value);
    $("call-btn-text").textContent = p.length === 10 ? `Call ${p.slice(0, 5)} ${p.slice(5)}` : "Call customer";
  }

  phone.addEventListener("input", () => {
    let d = digits(phone.value).slice(0, 12);
    if (d.length === 12 && d.startsWith("91")) d = d.slice(2);
    else if (d.length === 11 && d.startsWith("0")) d = d.slice(1);
    phone.value = d.length > 5 && d.length <= 10 ? `${d.slice(0, 5)} ${d.slice(5)}` : d;
    if (form.dataset.tried) validateForm();
    updateCallLabel();
  });
  amount.addEventListener("input", () => {
    amount.value = fmtAmount(digits(amount.value).slice(0, 10));
    if (form.dataset.tried) validateForm();
  });
  [nameIn, period, $("due")].forEach((i) => i.addEventListener("input", () => form.dataset.tried && validateForm()));
  document.querySelectorAll('input[name="call_type"]').forEach((r) => r.addEventListener("change", () => form.dataset.tried && validateForm()));
  (() => { const d = new Date(); d.setMonth(d.getMonth() - 1); period.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; })();

  $("voice").addEventListener("change", () => player.stop());
  $("voice-preview-btn").addEventListener("click", () => {
    player.toggle($("voice").value, "English", state.catalog?.samples?.English || "");
  });
  player.listeners.add(() => {
    const btn = $("voice-preview-btn");
    const mine = player.key === player.keyOf($("voice").value, "English", state.catalog?.samples?.English || "");
    btn.replaceChildren(icon(mine && player.status !== "idle" ? "stop" : "headphones"), mine && player.status === "loading" ? "Loading…" : mine ? "Stop" : "Listen");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    form.dataset.tried = "1";
    if (Object.keys(validateForm()).length) {
      form.querySelector("[data-invalid] input")?.focus();
      return;
    }
    const btn = $("call-btn");
    const payload = {
      phone_number: local10(phone.value),
      customer_name: nameIn.value.trim(),
      amount: fmtAmount(digits(amount.value)),
      billing_period: periodLabel(period.value),
      service_name: $("service").value.trim(),
      voice_id: $("voice").value,
      call_type: form.querySelector('input[name="call_type"]:checked').value,
      invoice_number: $("invoice").value.trim(),
      due_date: $("due").value,
    };
    btn.disabled = true; btn.setAttribute("aria-busy", "true");
    btn.replaceChildren(el("span", { class: "spinner", "aria-hidden": "true" }), el("span", {}, "Placing call…"));
    try {
      const data = await api("/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      toast("Call placed", `${payload.customer_name}'s phone (${fmtPhone(payload.phone_number)}) will ring shortly.`);
      state.highlight = data.ref_id;
      phone.value = ""; nameIn.value = ""; amount.value = ""; $("invoice").value = ""; $("due").value = "";
      delete form.dataset.tried;
      phone.focus();
    } catch (err) {
      toast("Could not place the call", err.message, "error");
    } finally {
      btn.disabled = false; btn.removeAttribute("aria-busy");
      btn.replaceChildren(icon("phone"), el("span", { id: "call-btn-text" }, "Call customer"));
      updateCallLabel();
      loadRecent();
    }
  });

  // ══ Start calls · tabs ══════════════════════════════════════════════════
  function selectTab(which, remember = true) {
    const single = which !== "bulk";
    $("tab-single").setAttribute("aria-selected", String(single));
    $("tab-bulk").setAttribute("aria-selected", String(!single));
    $("panel-single").hidden = !single;
    $("panel-bulk").hidden = single;
    if (remember) store.set("startTab", single ? "single" : "bulk");
  }
  $("tab-single").addEventListener("click", () => selectTab("single"));
  $("tab-bulk").addEventListener("click", () => selectTab("bulk"));
  document.querySelector(".tabs").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const next = $("tab-single").getAttribute("aria-selected") === "true" ? "bulk" : "single";
    selectTab(next); $(`tab-${next}`).focus();
  });

  // ══ Start calls · bulk upload ═══════════════════════════════════════════
  const fileInput = $("file-input"), dropzone = $("dropzone");
  const bulk = { rows: [], fileName: "", format: "", sort: { key: "amount", dir: "asc" }, view: "all", filters: {}, minAmount: 100 };
  const FIELD_LABELS = { customer_name: "Customer", phone_number: "Phone", invoice_number: "Invoice number", billing_period: "Billing period", due_date: "Due date", amount: "Amount" };
  const bulkType = () => document.querySelector('input[name="bulk_type"]:checked').value;
  const amountValue = (s) => Number(String(s ?? "").replace(/[^\d.]/g, "")) || 0;
  function amountText(s) {
    const [whole, frac] = String(s ?? "").replace(/[^\d.]/g, "").split(".");
    return whole ? `${fmtAmount(whole)}${frac && Number(frac) ? `.${frac.slice(0, 2)}` : ""}` : "";
  }
  function phoneText(s) {
    const d = local10(s);
    return d.length === 10 ? `${d.slice(0, 5)} ${d.slice(5)}` : String(s ?? "");
  }
  function daysFromDue(iso) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(iso || "")) return null;
    const [y, m, d] = iso.split("-").map(Number);
    const t = new Date();
    return Math.round((Date.UTC(t.getFullYear(), t.getMonth(), t.getDate()) - Date.UTC(y, m - 1, d)) / 86400000);
  }
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  function dueText(days) {
    if (days === null) return "No due date";
    if (days > 0) return `${plural(days, "day")} overdue`;
    if (days === 0) return "Due today";
    return `Due in ${plural(-days, "day")}`;
  }

  function bulkView(view) {
    $("bulk-idle").hidden = view !== "idle";
    $("bulk-loading").hidden = view !== "loading";
    $("bulk-preview").hidden = view !== "preview";
    $("start-grid").classList.toggle("expanded", view === "preview");
  }

  // ── Row model ──
  function toModel(r, i) {
    const data = { ...r.input, amount: amountText(r.input.amount), phone_number: phoneText(r.input.phone_number) };
    return { id: i, rowNo: r.row, data, original: { ...data }, serverErrors: r.errors || [], edited: false,
      skipReason: r.skip_reason, filters: r.filters || {}, source: r.source || {}, override: null };
  }
  function rowErrors(r) {
    if (!r.edited && r.serverErrors.length) return r.serverErrors.map((msg) => ({ field: /phone/i.test(msg) ? "phone_number" : /amount/i.test(msg) ? "amount" : /name/i.test(msg) ? "customer_name" : /period/i.test(msg) ? "billing_period" : /due/i.test(msg) ? "due_date" : null, msg }));
    const errs = [];
    if (local10(r.data.phone_number).length !== 10) errs.push({ field: "phone_number", msg: "Phone number must have 10 digits" });
    if (!String(r.data.customer_name || "").trim()) errs.push({ field: "customer_name", msg: "Customer name is missing" });
    if (!(amountValue(r.data.amount) > 0)) errs.push({ field: "amount", msg: "Amount must be greater than 0" });
    if (!String(r.data.billing_period || "").trim()) errs.push({ field: "billing_period", msg: "Billing period is missing" });
    if (r.data.due_date && daysFromDue(r.data.due_date) === null) errs.push({ field: "due_date", msg: "Due date is not valid" });
    return errs;
  }
  function autoSkip(r) {
    const days = daysFromDue(r.data.due_date);
    if (bulkType() === "overdue" && days !== null && days < 0) return `Not due yet (due in ${plural(-days, "day")}). Switch to Pre-due reminder to call.`;
    if (bulkType() === "predue" && days !== null && days >= 0) return days ? `Already ${plural(days, "day")} overdue. Switch to Overdue collection to call.` : "Due today. Switch to Overdue collection to call.";
    if (r.skipReason) return r.skipReason;
    if (amountValue(r.data.amount) > 0 && amountValue(r.data.amount) < bulk.minAmount) return `Below the ₹${fmtAmount(bulk.minAmount)} minimum`;
    return null;
  }
  const included = (r) => (r.override ?? !autoSkip(r));
  const matchesFilters = (r) => Object.entries(bulk.filters).every(([k, v]) => !v || r.filters[k] === v);
  const rowState = (r) => (!included(r) ? "skipped" : rowErrors(r).length ? "fix" : "ready");

  function sortedRows() {
    const { key, dir } = bulk.sort;
    const sign = dir === "asc" ? 1 : -1;
    const val = (r) => key === "amount" ? amountValue(r.data.amount) : key === "due" ? (r.data.due_date || "9999") : String(r.data.customer_name || "").toLowerCase();
    return [...bulk.rows].sort((a, b) => {
      const x = val(a), y = val(b);
      return (x < y ? -1 : x > y ? 1 : a.rowNo - b.rowNo) * sign;
    });
  }
  const visibleRows = () => sortedRows().filter((r) => matchesFilters(r) && (bulk.view === "all" || rowState(r) === bulk.view));

  // ── Rendering ──
  function statusCell(r) {
    const state = rowState(r);
    if (state === "skipped") return el("span", { class: "st-skip" }, r.override === false ? "Excluded by you" : autoSkip(r));
    if (state === "fix") return el("span", { class: "st-error" }, rowErrors(r).map((e) => e.msg).join(" · "));
    return el("div", {}, el("span", { class: "st-ready" }, icon("check"), "Ready"), el("span", { class: "st-meta" }, dueText(daysFromDue(r.data.due_date))));
  }

  function refreshRow(tr, r) {
    const state = rowState(r);
    tr.classList.toggle("excluded", state === "skipped");
    tr.querySelector(".pv-include").checked = included(r);
    const errFields = new Set(state === "fix" ? rowErrors(r).map((e) => e.field) : []);
    tr.querySelectorAll(".cell").forEach((c) => {
      c.classList.toggle("invalid", errFields.has(c.dataset.field));
      c.classList.toggle("edited", c.value !== String(r.original[c.dataset.field] ?? ""));
    });
    tr.querySelector(".status-cell").replaceChildren(statusCell(r));
  }

  function rowEl(r) {
    const tr = el("tr", { dataset: { id: r.id } });
    const cell = (field, cls, attrs = {}) => {
      const input = el("input", { class: `cell ${cls}`, value: r.data[field] ?? "", dataset: { field }, "aria-label": `${FIELD_LABELS[field]}, row ${r.rowNo}`, autocomplete: "off", ...attrs });
      input.addEventListener("input", () => {
        r.data[field] = input.value;
        r.edited = true;
        refreshRow(tr, r);
        updateSummary();
      });
      input.addEventListener("blur", () => {
        const fmt = field === "phone_number" ? phoneText(input.value) : field === "amount" ? amountText(input.value) : input.value.trim();
        if (fmt !== input.value) { input.value = fmt; r.data[field] = fmt; refreshRow(tr, r); updateSummary(); }
      });
      return input;
    };
    const include = el("input", { type: "checkbox", class: "pv-include", "aria-label": `Include row ${r.rowNo}` });
    include.addEventListener("change", () => { r.override = include.checked; refreshRow(tr, r); updateSummary(); });

    const sub = [r.data.account_number && `A/c ${r.data.account_number}`, r.data.service_name, r.data.amount_paid && `₹${amountText(r.data.amount_paid)} already paid`].filter(Boolean).join(" · ");
    tr.append(
      el("td", { class: "col-check" }, include),
      el("td", {}, cell("customer_name", "name", { maxlength: "80", title: r.data.customer_name || "" }), sub ? el("div", { class: "row-sub", title: sub }, sub) : null),
      el("td", {}, cell("phone_number", "phone", { inputmode: "numeric", maxlength: "14" })),
      el("td", {}, cell("invoice_number", "invoice", { maxlength: "30", placeholder: "—" })),
      el("td", {}, cell("billing_period", "period", { maxlength: "40" })),
      el("td", {}, cell("due_date", "due", { type: "date" })),
      el("td", { class: "col-amount" }, el("span", { class: "amount-wrap" }, "₹", cell("amount", "amount", { inputmode: "decimal", maxlength: "14" }))),
      el("td", { class: "status-cell" }));
    refreshRow(tr, r);
    return tr;
  }

  function renderFilters() {
    const labels = state.config.filter_labels || {};
    const fields = Object.keys(labels).filter((k) => bulk.rows.some((r) => r.filters[k]));
    const wrap = $("pv-filters");
    wrap.querySelectorAll(".pv-filter").forEach((n) => n.remove());
    const min = wrap.querySelector(".pv-min");
    for (const k of fields) {
      const counts = {};
      bulk.rows.forEach((r) => { if (r.filters[k]) counts[r.filters[k]] = (counts[r.filters[k]] || 0) + 1; });
      const sel = el("select", { id: `pv-f-${k}`, onchange: (e) => { bulk.filters[k] = e.target.value; renderPreviewRows(); } },
        el("option", { value: "" }, `All (${bulk.rows.length})`),
        ...Object.keys(counts).sort().map((v) => el("option", { value: v }, `${v} (${counts[v]})`)));
      sel.value = bulk.filters[k] || "";
      wrap.insertBefore(el("div", { class: "field pv-filter" }, el("label", { for: `pv-f-${k}` }, labels[k]), el("div", { class: "control" }, sel)), min);
    }
  }

  function renderPreviewRows() {
    const rows = visibleRows();
    $("pv-rows").replaceChildren(...rows.map(rowEl));
    if (!rows.length) $("pv-rows").replaceChildren(el("tr", {}, el("td", { colspan: "8", class: "muted", style: "text-align:center;padding:28px" }, "No rows match this view.")));
    document.querySelectorAll("#pv-table .sort").forEach((b) => {
      if (b.dataset.sort === bulk.sort.key) b.dataset.dir = bulk.sort.dir; else delete b.dataset.dir;
    });
    updateSummary();
  }

  function updateSummary() {
    const inScope = bulk.rows.filter(matchesFilters);
    const counts = { ready: 0, fix: 0, skipped: 0 };
    inScope.forEach((r) => counts[rowState(r)]++);
    const hidden = bulk.rows.length - inScope.length;

    $("pv-counts").replaceChildren(...[
      el("span", { class: "count-pill ok" }, icon("check"), `${counts.ready} ready`),
      counts.fix ? el("span", { class: "count-pill bad" }, icon("alert"), `${counts.fix} need fixing`) : null,
      counts.skipped ? el("span", { class: "count-pill", style: "background:var(--grey-02);color:var(--text-2)" }, `${counts.skipped} skipped`) : null,
    ].filter(Boolean));

    $("pv-view").replaceChildren(...[["all", "All", inScope.length], ["ready", "Ready", counts.ready], ["fix", "Needs fixing", counts.fix], ["skipped", "Skipped", counts.skipped]]
      .map(([key, label, n]) => el("button", { class: "chip", type: "button", "aria-pressed": String(bulk.view === key), onclick: () => { bulk.view = key; renderPreviewRows(); } }, label, el("span", { class: "count" }, n))));
    $("pv-note").textContent = `Sorted by ${bulk.sort.key === "amount" ? "amount" : bulk.sort.key === "due" ? "due date" : "customer"}, ${bulk.sort.dir === "asc" ? "lowest first" : "highest first"} · calls are placed in this order · click any cell to edit${hidden ? ` · ${hidden} hidden by filters` : ""}`;

    const visible = visibleRows();
    const all = $("pv-all");
    const sel = visible.filter(included).length;
    all.checked = visible.length > 0 && sel === visible.length;
    all.indeterminate = sel > 0 && sel < visible.length;

    const n = counts.ready;
    const secs = n * state.config.call_gap_seconds;
    const typeLabel = bulkType() === "predue" ? "reminder" : "collection";
    $("pv-eta").textContent = n
      ? `${plural(n, `${typeLabel} call`)} · one every ${state.config.call_gap_seconds}s · about ${secs < 90 ? `${Math.max(1, Math.round(secs))} seconds` : `${Math.round(secs / 60)} minutes`} to place them all`
      : counts.fix ? "Fix the highlighted rows or untick them to continue." : "No rows ready to call.";
    const start = $("pv-start");
    start.disabled = !n || counts.fix > 0;
    start.title = counts.fix ? "Some selected rows need fixing" : "";
    start.querySelector("span").textContent = n ? `Start ${plural(n, "call")}` : "Start calls";
  }

  function renderPreview() {
    $("pv-file").replaceChildren(bulk.fileName, el("span", { class: "format-badge" }, bulk.format === "smartflo" ? "Smartflo export" : "Template"));
    $("pv-total").textContent = ` ${plural(bulk.rows.length, bulk.format === "smartflo" ? "invoice" : "row")} found`;
    $("pv-min").value = fmtAmount(bulk.minAmount);
    renderFilters();
    renderPreviewRows();
  }

  async function handleFile(file) {
    if (!file) return;
    if (!/\.(xlsx|csv)$/i.test(file.name)) { toast("Unsupported file", "Upload an Excel (.xlsx) or CSV (.csv) file.", "error"); return; }
    if (file.size > 5 * 1024 * 1024) { toast("File too large", "Files must be 5 MB or smaller.", "error"); return; }
    bulkView("loading");
    const body = new FormData();
    body.append("file", file);
    try {
      const data = await api(`/batches/preview?default_voice=${encodeURIComponent(state.defaultVoice)}`, { method: "POST", body });
      Object.assign(bulk, { rows: data.rows.map(toModel), fileName: data.file_name, format: data.format,
        sort: { key: "amount", dir: "asc" }, view: "all", filters: {}, minAmount: state.config.min_amount ?? 100 });
      renderPreview();
      bulkView("preview");
    } catch (err) {
      bulkView("idle");
      toast("We couldn't use this file", err.message, "error");
    } finally {
      fileInput.value = "";
    }
  }

  fileInput.addEventListener("change", () => handleFile(fileInput.files[0]));
  dropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  ["dragenter", "dragover"].forEach((t) => dropzone.addEventListener(t, (e) => { e.preventDefault(); dropzone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((t) => dropzone.addEventListener(t, (e) => { e.preventDefault(); dropzone.classList.remove("dragging"); }));
  dropzone.addEventListener("drop", (e) => handleFile(e.dataTransfer.files[0]));
  $("pv-reset").addEventListener("click", async () => {
    if (bulk.rows.some((r) => r.edited) && !(await confirmDialog({ title: "Discard your edits?", text: "Changes made in the preview will be lost.", ok: "Discard", danger: true }))) return;
    bulk.rows = [];
    bulkView("idle");
  });
  // Rows the user ticked or unticked by hand keep that choice when the call type changes.
  document.querySelectorAll('input[name="bulk_type"]').forEach((r) => r.addEventListener("change", () => {
    if (bulk.rows.length) renderPreviewRows();
  }));
  document.querySelectorAll("#pv-table .sort").forEach((b) => b.addEventListener("click", () => {
    const key = b.dataset.sort;
    bulk.sort = { key, dir: bulk.sort.key === key && bulk.sort.dir === "asc" ? "desc" : "asc" };
    renderPreviewRows();
  }));
  $("pv-all").addEventListener("change", (e) => {
    visibleRows().forEach((r) => (r.override = e.target.checked));
    renderPreviewRows();
  });
  let minTimer;
  $("pv-min").addEventListener("input", (e) => {
    e.target.value = fmtAmount(digits(e.target.value).slice(0, 9));
    clearTimeout(minTimer);
    minTimer = setTimeout(() => { bulk.minAmount = Number(digits(e.target.value)) || 0; renderPreviewRows(); }, 250);
  });

  $("pv-start").addEventListener("click", async () => {
    const rows = sortedRows().filter((r) => matchesFilters(r) && rowState(r) === "ready");
    if (!rows.length) return;
    const predue = bulkType() === "predue";
    const ok = await confirmDialog({
      title: `Start ${plural(rows.length, predue ? "reminder call" : "collection call")}?`,
      text: `Arjun will call ${plural(rows.length, "customer")} from "${bulk.fileName}" in the order shown, one every ${state.config.call_gap_seconds} seconds, starting now. A customer with several invoices is called once per invoice, never two calls at the same time. You can stop the remaining calls at any time.`,
      ok: "Start calls",
    });
    if (!ok) return;
    const btn = $("pv-start");
    btn.disabled = true; btn.setAttribute("aria-busy", "true");
    btn.querySelector("span").textContent = "Starting…";
    try {
      const batch = await api("/batches", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          file_name: bulk.fileName,
          call_type: bulkType(),
          default_voice: state.defaultVoice,
          rows: rows.map((r) => ({ ...r.data, row: r.rowNo, phone_number: local10(r.data.phone_number),
            amount: String(r.data.amount).replace(/,/g, ""), source: r.source })),
        }),
      });
      toast("Calling started", `${plural(batch.total, "call")} from ${batch.file_name} are queued.`);
      bulk.rows = [];
      bulkView("idle");
      loadBatches(); loadRecent();
    } catch (err) {
      toast("Could not start the calls", err.message, "error");
    } finally {
      btn.removeAttribute("aria-busy");
      if (bulk.rows.length) updateSummary();
    }
  });

  // ══ Start calls · batches + recent ══════════════════════════════════════
  function batchProgress(b) {
    const total = b.total || 1;
    const live = b.dialing + b.active;
    const seg = (cls, n, label) => (n ? el("span", { class: cls, style: `width:${(n / total) * 100}%`, title: `${n} ${label}` }) : null);
    return el("div", { class: "progress", role: "img", "aria-label": `${b.completed} completed, ${b.failed} failed, ${live} in progress, ${b.queued} waiting` },
      seg("p-done", b.completed, "completed"), seg("p-bad", b.failed, "failed"), seg("p-cancel", b.cancelled, "cancelled"), seg("p-live", live, "in progress"));
  }

  function renderBatches() {
    const card = $("batches-card");
    const list = state.batches.slice(0, 3);
    card.hidden = !list.length;
    $("batches").replaceChildren(...list.map((b) => {
      const pending = b.queued + b.dialing + b.active;
      const headline = pending ? "In progress" : b.cancelled_at ? "Stopped" : "Finished";
      const stat = (n, label) => (n ? el("span", {}, el("b", {}, n), ` ${label}`) : null);
      return el("div", { class: "batch" },
        el("div", { class: "batch-top" },
          el("div", { style: "flex:1;min-width:0" },
            el("div", { class: "batch-name" }, b.file_name),
            el("div", { class: "batch-time" }, `${headline} · uploaded ${relTime(b.created_at).toLowerCase()}`,
              isAdmin() && b.created_by_email ? ` by ${b.created_by_email}` : "")),
          el("span", { class: `pill ${pending ? "pill-active" : !b.completed && b.failed ? "pill-failed" : b.cancelled_at ? "pill-cancelled" : "pill-completed"}`,
            title: `${b.completed} of ${b.total} completed` }, `${b.completed}/${b.total}`)),
        batchProgress(b),
        el("div", { class: "batch-stats" },
          stat(b.completed, "completed"), stat(b.failed, "failed"), stat(b.dialing + b.active, "in progress"),
          stat(b.queued, "waiting"), stat(b.cancelled, "cancelled")),
        el("div", { class: "batch-actions" },
          el("a", { class: "btn btn-secondary btn-sm", href: `/calls?batch=${encodeURIComponent(b.batch_id)}`, "data-link": true }, "View calls"),
          b.queued ? el("button", { class: "btn btn-danger btn-sm", type: "button", onclick: () => stopBatch(b) }, "Stop remaining") : null));
    }));
  }

  async function stopBatch(b) {
    const ok = await confirmDialog({
      title: "Stop the remaining calls?",
      text: `${b.queued} customer${b.queued === 1 ? "" : "s"} from "${b.file_name}" won't be called. Calls already ringing or in progress continue.`,
      ok: "Stop calls", danger: true,
    });
    if (!ok) return;
    try {
      const r = await api(`/batches/${encodeURIComponent(b.batch_id)}/cancel`, { method: "POST" });
      toast("Remaining calls stopped", `${r.cancelled} call${r.cancelled === 1 ? "" : "s"} cancelled.`);
    } catch (err) {
      toast("Could not stop the calls", err.message, "error");
    }
    loadBatches();
  }

  function renderRecent() {
    const list = $("recent");
    if (!state.recentLoaded) {
      list.replaceChildren(...[1, 2, 3].map(() => el("li", { style: "padding:14px 24px" }, el("div", { class: "skeleton", style: "width:70%" }))));
      return;
    }
    if (!state.recent.length) {
      list.replaceChildren(el("li", {}, el("div", { class: "state compact" }, el("h3", {}, "No calls yet"), "Calls you place appear here.")));
      return;
    }
    list.replaceChildren(...state.recent.map((c) => el("li", {},
      el("button", { type: "button", onclick: () => openCall(c.ref_id), "aria-label": `${c.customer_name}, ${statusLabel(c)}. Open details` },
        el("span", { class: "cust-name" }, c.customer_name), pill(c),
        el("span", { class: "cust-num" }, fmtPhone(c.phone_number)), el("span", { class: "when" }, relTime(callTime(c)))))));
  }

  // ══ Call logs ═══════════════════════════════════════════════════════════
  const FILTERS = [
    { key: "all", label: "All" }, { key: "completed", label: "Completed" }, { key: "live", label: "In progress" },
    { key: "failed", label: "Failed" }, { key: "cancelled", label: "Cancelled" },
  ];

  function visibleCalls() {
    const q = state.query.trim().toLowerCase();
    const qd = digits(q);
    return state.calls.filter((c) =>
      (state.filter === "all" || groupOf(c) === state.filter) &&
      (!q || (c.customer_name || "").toLowerCase().includes(q) || (qd && digits(c.phone_number).includes(qd)) ||
        (isAdmin() && (c.created_by_email || "").includes(q))));
  }

  function renderKpis() {
    const calls = state.calls;
    const done = calls.filter((c) => c.status === "completed");
    const durs = done.map(durationSeconds).filter((d) => d != null);
    $("k-total").textContent = calls.length;
    $("k-total-sub").textContent = state.batchFilter ? "In this upload" : calls.length >= state.limit ? `Latest ${state.limit}` : "All calls";
    $("k-done").textContent = done.length;
    $("k-done-bar").style.width = calls.length ? `${(done.length / calls.length) * 100}%` : "0";
    $("k-live").textContent = calls.filter((c) => groupOf(c) === "live").length;
    $("k-avg").textContent = durs.length ? fmtSeconds(durs.reduce((a, b) => a + b, 0) / durs.length) : "–";
  }

  function renderChips() {
    const counts = Object.fromEntries(FILTERS.map((f) => [f.key, 0]));
    counts.all = state.calls.length;
    state.calls.forEach((c) => { const g = groupOf(c); if (g in counts) counts[g]++; });
    $("chips").replaceChildren(...FILTERS.filter((f) => f.key !== "cancelled" || counts.cancelled).map((f) =>
      el("button", { class: "chip", type: "button", "aria-pressed": String(state.filter === f.key), onclick: () => { state.filter = f.key; renderCalls(); } },
        f.label, el("span", { class: "count" }, counts[f.key]))));
  }

  function renderBatchFilter() {
    const sel = $("batch-filter");
    const opts = [el("option", { value: "" }, "All calls"),
      ...state.batches.map((b) => el("option", { value: b.batch_id }, `${b.file_name} · ${shortTime(b.created_at)}`))];
    if (state.batchFilter && !state.batches.some((b) => b.batch_id === state.batchFilter)) opts.push(el("option", { value: state.batchFilter }, "Selected upload"));
    sel.replaceChildren(...opts);
    sel.value = state.batchFilter;
  }

  function renderRows() {
    const empty = $("empty");
    if (!state.callsLoaded) {
      $("rows").replaceChildren(...[1, 2, 3, 4].map(() =>
        el("tr", {}, ...(isAdmin() ? [160, 120, 70, 80, 50, 90] : [160, 70, 80, 50, 90]).map((w) => el("td", {}, el("div", { class: "skeleton", style: `width:${w}px` }))))));
      empty.replaceChildren();
      $("load-more").hidden = true;
      return;
    }
    const rows = visibleCalls();
    $("rows").replaceChildren(...rows.map((c) => {
      const dur = durationSeconds(c);
      const tr = el("tr", { tabindex: "0", "aria-label": `${c.customer_name}, ${statusLabel(c)}. Open details`,
          onclick: () => openCall(c.ref_id),
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCall(c.ref_id); } } },
        el("td", { dataset: { col: "customer" } },
          el("div", { class: "cust-name" }, c.customer_name), el("div", { class: "cust-num" }, fmtPhone(c.phone_number)),
          c.call_type === "predue" ? el("span", { class: "batch-tag", style: "margin-right:4px" }, "Reminder") : null,
          c.invoice_number ? el("span", { class: "batch-tag", style: "margin-right:4px" }, `Inv ${c.invoice_number}`) : null,
          c.batch_file_name && !state.batchFilter ? el("span", { class: "batch-tag", title: `Uploaded in ${c.batch_file_name}` }, c.batch_file_name) : null),
        isAdmin() ? el("td", { class: "muted owner", dataset: { col: "owner" }, title: c.created_by_email || "" },
          c.created_by_email || (c.direction === "inbound" ? "Inbound call" : "—")) : null,
        el("td", { class: "num", dataset: { col: "amount" } }, showAmount(c.amount)),
        el("td", { dataset: { col: "status" } }, pill(c)),
        el("td", { class: "num muted", dataset: { col: "duration" } }, dur != null ? fmtSeconds(dur) : "—"),
        el("td", { dataset: { col: "time" }, title: fullTime(callTime(c)) },
          el("div", { class: "time-rel" }, relTime(callTime(c))), el("div", { class: "time-abs" }, shortTime(callTime(c)))));
      if (c.ref_id === state.highlight) { tr.classList.add("flash"); state.highlight = null; }
      return tr;
    }));
    if (rows.length) empty.replaceChildren();
    else if (!state.calls.length) empty.replaceChildren(el("div", { class: "state" }, icon("inbox"), el("h3", {}, state.batchFilter ? "No calls in this upload" : "No calls yet"), el("div", {}, "Calls appear here with their status, recording and transcript.")));
    else empty.replaceChildren(el("div", { class: "state" }, el("h3", {}, "No matching calls"), el("div", {}, "Try a different search or status filter.")));
    $("load-more").hidden = !(state.calls.length >= state.limit && state.limit < 1000);
  }

  function renderUserFilter() {
    if (!isAdmin()) return;
    const sel = $("user-filter");
    sel.replaceChildren(el("option", { value: "" }, "Everyone"),
      ...state.users.map((u) => el("option", { value: String(u.id) }, u.email)));
    sel.value = state.userFilter;
  }

  function renderCalls() { renderKpis(); renderChips(); renderUserFilter(); renderBatchFilter(); renderRows(); }

  $("q").addEventListener("input", (e) => { state.query = e.target.value; renderRows(); });
  $("refresh").addEventListener("click", () => { loadCalls(); loadBatches(); });
  $("batch-filter").addEventListener("change", (e) => {
    const id = e.target.value;
    history.replaceState(null, "", id ? `/calls?batch=${encodeURIComponent(id)}` : "/calls");
    state.batchFilter = id; state.callsLoaded = false; state.calls = [];
    renderCalls(); loadCalls();
  });
  $("user-filter").addEventListener("change", (e) => {
    state.userFilter = e.target.value; state.callsLoaded = false; state.calls = [];
    renderCalls(); loadCalls();
  });
  $("load-more").querySelector("button").addEventListener("click", () => { state.limit = Math.min(1000, state.limit + 100); loadCalls(); });

  $("export").addEventListener("click", () => {
    const rows = visibleCalls();
    if (!rows.length) { toast("Nothing to export", "No calls match the current filters.", "error"); return; }
    const safe = (v) => {
      let s = String(v ?? "");
      if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;  // keep spreadsheet formulas from executing
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const header = ["Time", "Call type", "Customer", "Phone", "Account number", "Invoice number", "Amount", "Already paid", "Billing period",
      "Due date", "Days overdue", "Service", "Voice", "Status", "Duration (s)", "Region", "Partner", "Upload", "Initiated by", "Error", "Reference", "Smartflo call ID"];
    const lines = rows.map((c) => [fullTime(callTime(c)), c.call_type === "predue" ? "Pre-due reminder" : "Overdue collection",
      c.customer_name, fmtPhone(c.phone_number), c.account_number || "", c.invoice_number || "", showAmount(c.amount).replace("₹", ""),
      c.amount_paid ? showAmount(c.amount_paid).replace("₹", "") : "", c.billing_period, c.due_date || "", c.days_overdue || "",
      c.service_name, voiceName(c.voice_id), statusLabel(c), durationSeconds(c) ?? "",
      c.source?.REGION || "", c.source?.["Partner Name"] || "",
      c.batch_file_name || "", c.created_by_email || "", c.error || "", c.ref_id, c.call_sid || ""].map(safe).join(","));
    const blob = new Blob(["﻿" + [header.join(","), ...lines].join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `call-logs-${new Date().toISOString().slice(0, 10)}.csv` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });

  // ══ Drawer ══════════════════════════════════════════════════════════════
  const drawer = $("drawer");
  const findCall = (ref) => state.calls.find((x) => x.ref_id === ref) || state.recent.find((x) => x.ref_id === ref);

  function openCall(ref) {
    if (!state.openRef) state.lastFocus = document.activeElement;
    state.openRef = ref;
    document.body.classList.add("drawer-open");
    drawer.setAttribute("aria-hidden", "false");
    const hash = `#call=${encodeURIComponent(ref)}`;
    if (location.hash !== hash) history.replaceState(null, "", location.pathname + location.search + hash);
    renderDetailHead(findCall(ref));
    $("d-body").replaceChildren(el("div", { class: "skeleton", style: "width:60%" }), el("div", { class: "skeleton", style: "width:90%" }), el("div", { class: "skeleton", style: "width:75%" }));
    loadDetail(ref);
    setTimeout(() => $("d-close").focus(), 50);
  }

  function closeDrawer(restoreFocus = true) {
    state.openRef = null;
    document.body.classList.remove("drawer-open");
    drawer.setAttribute("aria-hidden", "true");
    if (location.hash) history.replaceState(null, "", location.pathname + location.search);
    if (restoreFocus) state.lastFocus?.focus?.();
  }

  function renderDetailHead(c) {
    $("d-name").textContent = c ? c.customer_name : "Call details";
    $("d-num").textContent = c ? fmtPhone(c.phone_number) : "";
    $("d-status").replaceChildren(c ? pill(c) : "");
  }

  async function loadDetail(ref, quiet = false) {
    let data;
    try {
      data = await api(`/logs/${encodeURIComponent(ref)}`);
    } catch (err) {
      if (!quiet && state.openRef === ref) $("d-body").replaceChildren(el("div", { class: "error-box" }, `Couldn't load this call: ${err.message}`));
      return;
    }
    if (state.openRef !== ref) return;
    const audio = $("d-body").querySelector("audio");
    if (quiet && audio && !audio.paused) return;  // don't interrupt playback on background refresh
    const { call: c, transcript } = data;
    renderDetailHead(c);
    const live = groupOf(c) === "live";
    const dur = durationSeconds(c);
    const item = (label, value, wide) => el("div", { class: wide ? "wide" : null }, el("dt", {}, label), el("dd", {}, value || "—"));

    const copyBtn = (value) => el("button", { class: "copy", type: "button", onclick: async (e) => {
      try { await navigator.clipboard.writeText(value); e.target.textContent = "Copied"; setTimeout(() => (e.target.textContent = "Copy"), 1500); } catch { /* clipboard unavailable */ }
    } }, "Copy");

    const sections = [];

    // Call Intelligence, Customer Quote, and Commitments
    if (c.customer_quote || c.commitment_eta || c.summary) {
      const intelChildren = [];
      if (c.customer_quote) {
        intelChildren.push(
          el("div", { class: "drawer-quote-card" },
            el("div", { class: "drawer-quote-label" }, "What Customer Said (Highlighted Quote)"),
            el("div", { class: "drawer-quote-text" }, `“${c.customer_quote}”`))
        );
      }
      if (c.commitment_eta || c.callback_date) {
        const dateStr = c.callback_date ? new Date(c.callback_date).toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "short", year: "numeric" }) : "—";
        intelChildren.push(
          el("div", { class: "drawer-eta-card" },
            el("div", {},
              el("div", { class: "drawer-eta-title" }, "Commitment ETA"),
              el("div", { class: "drawer-eta-val" }, c.commitment_eta || "—")),
            el("div", {},
              el("div", { class: "drawer-eta-title" }, "Scheduled Callback (Working Day)"),
              el("div", { class: "drawer-eta-val" }, dateStr)),
            el("span", { class: `sentiment-badge ${sentimentClass(c.sentiment)}` }, c.sentiment || "Committed"))
        );
      }
      if (c.summary) {
        intelChildren.push(
          el("div", { class: "drawer-summary-box" }, el("b", {}, "Call Summary: "), c.summary)
        );
      }
      sections.push(
        el("section", { class: "drawer-intelligence" },
          el("div", { class: "section-title" }, "Call Intelligence & Follow-up"),
          ...intelChildren)
      );
    }

    sections.push(
      el("section", {}, el("div", { class: "section-title" }, "Call details"),
        el("dl", { class: "meta" },
          item("Amount due", showAmount(c.amount)), item("Billing period", c.billing_period),
          item("Call type", c.call_type === "predue" ? "Pre-due reminder" : "Overdue collection"),
          item("Voice", voiceName(c.voice_id)),
          c.invoice_number ? item("Invoice number", c.invoice_number) : null,
          c.account_number ? item("Account number", c.account_number) : null,
          c.due_date ? item("Due date", `${c.due_date}${c.days_overdue !== "" ? ` · ${dueText(Number(c.days_overdue))}` : ""}`) : null,
          c.amount_paid ? item("Already paid", showAmount(c.amount_paid)) : null,
          c.source?.REGION ? item("Region", c.source.REGION) : null,
          c.source?.["Partner Name"] ? item("Partner", c.source["Partner Name"]) : null,
          item("Duration", dur != null ? fmtSeconds(dur) : live ? "In progress" : null),
          item(c.status === "queued" ? "Queued" : "Dialed", fullTime(callTime(c))),
          c.started_at ? item("Answered", new Date(c.started_at).toLocaleTimeString("en-IN")) : null,
          c.batch_file_name ? item("Upload", c.batch_file_name) : null,
          isAdmin() ? item("Initiated by", c.created_by_email || (c.direction === "inbound" ? "Inbound call" : null)) : null,
          item("Service", c.service_name, true)))
    );
    if (c.error) sections.push(el("div", { class: "error-box", role: "alert" }, c.error));
    sections.push(el("section", {}, el("div", { class: "section-title" }, "Recording"),
      c.recording_path ? el("audio", { controls: true, preload: "metadata", src: `/recordings/${encodeURIComponent(c.ref_id)}` })
        : el("div", { class: "muted" }, live ? "Available once the call ends." : "No recording for this call.")));
    sections.push(el("section", {}, el("div", { class: "section-title" }, "Transcript"),
      transcript.length
        ? el("div", { class: "transcript" }, ...transcript.map((t) =>
            el("div", { class: `turn turn-${t.role === "assistant" ? "assistant" : "user"}` },
              el("div", { class: "turn-role" }, t.role === "assistant" ? "Arjun (bot)" : "Customer"), t.text)))
        : el("div", { class: "muted" }, live ? "The transcript appears once the call ends." : "No conversation was recorded.")));

    if (c.status === "completed" && c.initiator_email) sections.push(emailSection(c));

    sections.push(el("div", { class: "ids" },
      el("div", {}, "Reference ", el("code", {}, c.ref_id), copyBtn(c.ref_id)),
      c.call_sid ? el("div", {}, "Smartflo call ", el("code", {}, c.call_sid), copyBtn(c.call_sid)) : null));
    $("d-body").replaceChildren(...sections);
  }

  function emailSection(c) {
    const to = c.initiator_email;
    let status, cls = "email-status";
    if (c.email_sent_at) { status = `Sent to ${to} · ${shortTime(c.email_sent_at)}`; cls += " ok"; }
    else if (!state.config.email_enabled) status = "Email isn't set up on the server yet, so no summary was sent.";
    else if (c.email_error) { status = `Not sent yet: ${c.email_error}`; cls += " bad"; }
    else status = `Sending the summary to ${to}…`;

    const btn = state.config.email_enabled ? el("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: async (e) => {
      const b = e.currentTarget;
      b.disabled = true; b.textContent = "Sending…";
      try {
        const res = await api(`/logs/${encodeURIComponent(c.ref_id)}/send-email`, { method: "POST" });
        toast("Email sent", `Summary and transcript sent to ${res.recipient}.`);
        loadDetail(c.ref_id, true);
      } catch (err) {
        toast("Couldn't send the email", err.message, "error");
        b.disabled = false; b.textContent = c.email_sent_at ? "Send again" : "Send now";
      }
    } }, icon("mail"), c.email_sent_at ? "Send again" : "Send now") : null;

    return el("section", {}, el("div", { class: "section-title" }, "Summary email"),
      el("div", { class: "email-row" }, el("div", { class: cls }, status), btn));
  }

  $("d-close").addEventListener("click", () => closeDrawer());
  $("backdrop").addEventListener("click", () => closeDrawer());
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.openRef && !$("confirm").open) closeDrawer(); });
  function openFromHash() {
    const m = location.hash.match(/^#call=(.+)$/);
    if (m && state.openRef !== decodeURIComponent(m[1])) openCall(decodeURIComponent(m[1]));
  }
  window.addEventListener("hashchange", openFromHash);

  // ══ Sentiment helper ══════════════════════════════════════════════════
  function sentimentClass(s) {
    const raw = String(s || "").toLowerCase();
    if (raw.includes("commit")) return "committed";
    if (raw.includes("callback")) return "callback-requested";
    if (raw.includes("paid")) return "paid";
    if (raw.includes("disput")) return "disputed";
    if (raw.includes("approv")) return "pending-approval";
    return "other";
  }

  // ══ Agent Dashboard ═══════════════════════════════════════════════════
  function selectDashUser(userId) {
    state.dashUserFilter = userId ? String(userId) : "";
    const u = new URL(location.href);
    if (state.dashUserFilter) {
      u.searchParams.set("user", state.dashUserFilter);
    } else {
      u.searchParams.delete("user");
    }
    history.replaceState(null, "", u.pathname + u.search);
    loadAgentStats();
  }

  async function loadAgentStats() {
    try {
      const qs = state.dashUserFilter ? `?user_id=${encodeURIComponent(state.dashUserFilter)}` : "";
      const stats = await api(`/api/agent/stats${qs}`);
      state.dashStats = stats;
      renderAgentDashboard();
    } catch (err) {
      toast("Couldn't load dashboard", err.message, "error");
    }
  }

  function renderAgentDashboard() {
    const stats = state.dashStats;
    if (!stats) return;

    const admin = isAdmin();
    const userFilterWrap = $("dash-user-filter-wrap");
    const userSelect = $("dash-user-select");
    const usersCard = $("dash-users-card");
    const thAgent = $("dash-th-agent");

    if (thAgent) thAgent.style.display = admin ? "" : "none";

    if (admin) {
      if (userFilterWrap) userFilterWrap.style.display = "flex";
      if (usersCard) usersCard.style.display = "";

      // Populate user selector dropdown
      if (userSelect && stats.users_breakdown) {
        const currentVal = state.dashUserFilter || "";
        userSelect.replaceChildren(
          el("option", { value: "" }, "All Team Members (Company View)"),
          ...stats.users_breakdown.map((u) =>
            el("option", { value: String(u.id), selected: String(u.id) === currentVal },
              `${u.email} (${u.role.toUpperCase()} · ${u.total_calls} calls · ${u.amount_formatted})`
            )
          )
        );
        userSelect.value = currentVal;
      }

      // Update subtitle
      if (stats.selected_user) {
        $("dash-sub").textContent = `Showing user view for ${stats.selected_user.email} (${stats.selected_user.role.toUpperCase()}) · Assigned portfolio & commitments`;
      } else {
        $("dash-sub").textContent = `Company-wide collection operations across all ${stats.users_breakdown?.length || 3} team users. Select a user to drill down.`;
      }

      // Render Users Breakdown side card
      const usersList = $("dash-users-list");
      const usersCount = $("dash-users-count");
      if (usersCount && stats.users_breakdown) {
        usersCount.textContent = `${stats.users_breakdown.length} Team Members`;
      }
      if (usersList && stats.users_breakdown) {
        usersList.replaceChildren(
          ...stats.users_breakdown.map((u) => {
            const isSelected = String(u.id) === (state.dashUserFilter || "");
            const initials = (u.email.slice(0, 2) || "AG").toUpperCase();
            return el("div", {
              class: `user-breakdown-item ${isSelected ? "selected" : ""}`,
              onclick: () => selectDashUser(isSelected ? "" : u.id),
              title: isSelected ? "Click to clear filter and show all" : `Click to view dashboard for ${u.email}`,
            },
              el("div", { style: "display:flex;justify-content:space-between;align-items:center;margin-bottom:6px" },
                el("div", { style: "display:flex;align-items:center;gap:8px" },
                  el("div", { class: "dash-agent-avatar" }, initials),
                  el("span", { style: "font-weight:600;font-size:13px;max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap", title: u.email }, u.email)
                ),
                el("span", { class: `pill pill-${u.role === "admin" ? "completed" : "active"}`, style: "font-size:10px;padding:1px 6px" }, u.role)
              ),
              el("div", { style: "display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;font-size:12px;padding-top:6px;border-top:1px dashed #E5E7EB" },
                el("div", {},
                  el("div", { class: "muted", style: "font-size:11px" }, "Calls"),
                  el("div", { style: "font-weight:600" }, `${u.total_calls} (${u.completed_calls} done)`)
                ),
                el("div", {},
                  el("div", { class: "muted", style: "font-size:11px" }, "Amount"),
                  el("div", { style: "font-weight:600" }, u.amount_formatted)
                ),
                el("div", {},
                  el("div", { class: "muted", style: "font-size:11px" }, "Promised"),
                  el("div", { style: "font-weight:600;color:#059669" }, u.commitments_count)
                )
              )
            );
          })
        );
      }
    } else {
      if (userFilterWrap) userFilterWrap.style.display = "none";
      if (usersCard) usersCard.style.display = "none";
      $("dash-sub").textContent = "Personal collection performance, handled call volume, and customer commitments.";
    }

    $("dash-total-calls").textContent = stats.total_calls;
    $("dash-calls-sub").textContent = `${stats.completed_calls} completed · ${stats.active_calls} active`;
    $("dash-total-amount").textContent = stats.total_amount_formatted;
    $("dash-commitments-count").textContent = stats.commitments_count;
    $("dash-commitments-amount").textContent = `${stats.commitments_amount_formatted} promised`;

    const topSvc = stats.services[0];
    $("dash-top-service").textContent = topSvc ? (topSvc.name.length > 28 ? topSvc.name.slice(0, 26) + "…" : topSvc.name) : "—";
    $("dash-service-sub").textContent = `${stats.services.length} services in portfolio`;

    renderDashCommitments();

    // Services breakdown
    const svcContainer = $("dash-services-list");
    if (!stats.services.length) {
      svcContainer.replaceChildren(el("div", { class: "muted" }, "No services recorded yet."));
    } else {
      const maxAmt = Math.max(...stats.services.map((s) => s.amount), 1);
      svcContainer.replaceChildren(
        ...stats.services.map((s) => {
          const pct = Math.max(5, Math.round((s.amount / maxAmt) * 100));
          return el("div", { class: "service-item" },
            el("div", { class: "service-item-top" },
              el("span", { class: "service-item-name", title: s.name }, s.name),
              el("span", { class: "service-item-amt" }, s.amount_formatted)),
            el("div", { class: "service-bar-wrap" },
              el("div", { class: "service-bar-fill", style: `width:${pct}%` })),
            el("div", { class: "service-item-sub" }, `${s.count} ${plural(s.count, "call")}`));
        })
      );
    }
  }

  function renderDashCommitments() {
    const stats = state.dashStats;
    if (!stats) return;
    const q = (state.dashSearch || "").toLowerCase().trim();
    let rows = stats.commitments;
    if (q) {
      rows = rows.filter((c) =>
        (c.customer_name || "").toLowerCase().includes(q) ||
        (c.service_name || "").toLowerCase().includes(q) ||
        (c.customer_quote || "").toLowerCase().includes(q) ||
        (c.commitment_eta || "").toLowerCase().includes(q)
      );
    }

    const tbody = $("dash-commitments-rows");
    const empty = $("dash-commitments-empty");
    if (!rows.length) {
      tbody.replaceChildren();
      empty.replaceChildren(
        el("div", { class: "state", style: "padding:24px 0" },
          icon("inbox"),
          el("h3", {}, q ? "No matching commitments" : "No customer commitments recorded yet"),
          el("div", {}, q ? "Try another search term." : "When customers make commitments like 'I will pay in 2 days', they are highlighted here."))
      );
      return;
    }
    empty.replaceChildren();

    tbody.replaceChildren(
      ...rows.map((c) => {
        const quoteElem = c.customer_quote
          ? el("blockquote", { class: "customer-quote-box" }, `“${c.customer_quote}”`)
          : el("span", { class: "muted" }, "—");

        const dateStr = c.callback_date
          ? new Date(c.callback_date).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", year: "numeric" })
          : null;

        const etaPill = el("div", {},
          c.commitment_eta ? el("span", { class: "eta-chip" }, icon("clock"), c.commitment_eta) : null,
          dateStr ? el("span", { class: "date-pill" }, dateStr) : null
        );

        const agentCell = isAdmin() ? el("td", { class: "dash-agent-cell" },
          c.created_by_email ? el("div", {
            class: "dash-agent-badge",
            style: "cursor:pointer",
            title: `Filter dashboard to ${c.created_by_email}`,
            onclick: (e) => {
              e.stopPropagation();
              selectDashUser(c.created_by);
            }
          },
            el("span", { class: "dash-agent-avatar" }, (c.created_by_email.slice(0, 2) || "U").toUpperCase()),
            el("span", { style: "max-width:130px;overflow:hidden;text-overflow:ellipsis" }, c.created_by_email)
          ) : el("span", { class: "muted" }, "Unassigned")
        ) : null;

        return el("tr", { tabindex: "0", onclick: () => openCall(c.ref_id) },
          ...(isAdmin() ? [agentCell] : []),
          el("td", {},
            el("div", { class: "cust-name" }, c.customer_name),
            el("div", { class: "cust-num" }, fmtPhone(c.phone_number))),
          el("td", { class: "num", style: "font-weight:600" }, c.amount_formatted),
          el("td", { style: "font-size:13px;max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis", title: c.service_name }, c.service_name),
          el("td", {}, quoteElem),
          el("td", {}, etaPill),
          el("td", {}, el("span", { class: `sentiment-badge ${sentimentClass(c.sentiment)}` }, c.sentiment || "Committed")),
          el("td", {},
            el("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: (e) => { e.stopPropagation(); openCall(c.ref_id); } },
              icon("eye"), "Details"))
        );
      })
    );
  }

  // ══ Calendar View ═══════════════════════════════════════════════════════
  async function loadCalendar() {
    try {
      const qs = new URLSearchParams({
        year: String(state.calYear),
        month: String(state.calMonth + 1),
      });
      state.calEvents = await api(`/api/agent/calendar?${qs}`);
      renderCalendar();
    } catch (err) {
      toast("Couldn't load calendar", err.message, "error");
    }
  }

  function renderCalendar() {
    const y = state.calYear;
    const m = state.calMonth;
    const monthDate = new Date(y, m, 1);
    $("cal-month-title").textContent = monthDate.toLocaleDateString("en-IN", { month: "long", year: "numeric" });

    // Reference today: Friday, 2026-10-09
    const now = new Date();
    const todayStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    $("cal-today-indicator").textContent = `Reference Today: ${now.toLocaleDateString("en-IN", { weekday: "long", day: "2-digit", month: "short", year: "numeric" })}`;

    const eventsByDate = {};
    for (const ev of state.calEvents) {
      if (!ev.callback_date) continue;
      const dStr = String(ev.callback_date).split("T")[0];
      if (!eventsByDate[dStr]) eventsByDate[dStr] = [];
      eventsByDate[dStr].push(ev);
    }

    // Days calculation (Mon = 0 .. Sun = 6)
    const firstDayWd = (new Date(y, m, 1).getDay() + 6) % 7;
    const daysInMonth = new Date(y, m + 1, 0).getDate();
    const daysInPrevMonth = new Date(y, m, 0).getDate();
    const totalCells = Math.ceil((firstDayWd + daysInMonth) / 7) * 7;

    const grid = $("cal-days-grid");
    const cells = [];

    for (let i = 0; i < totalCells; i++) {
      const colWd = i % 7;
      const isWeekend = colWd >= 5; // Saturday=5, Sunday=6

      let cellYear = y;
      let cellMonth = m;
      let dayNum;
      let isOtherMonth = false;

      if (i < firstDayWd) {
        isOtherMonth = true;
        dayNum = daysInPrevMonth - (firstDayWd - i - 1);
        cellMonth = m - 1;
        if (cellMonth < 0) { cellMonth = 11; cellYear = y - 1; }
      } else if (i >= firstDayWd + daysInMonth) {
        isOtherMonth = true;
        dayNum = i - (firstDayWd + daysInMonth) + 1;
        cellMonth = m + 1;
        if (cellMonth > 11) { cellMonth = 0; cellYear = y + 1; }
      } else {
        dayNum = i - firstDayWd + 1;
      }

      const cellDateStr = `${cellYear}-${String(cellMonth + 1).padStart(2, "0")}-${String(dayNum).padStart(2, "0")}`;
      const isToday = cellDateStr === todayStr;

      let cellClasses = "cal-day-cell";
      if (isOtherMonth) cellClasses += " other-month";
      if (isToday) cellClasses += " today";
      if (isWeekend) cellClasses += " weekend";

      const header = el("div", { class: "cal-day-header" },
        el("span", { class: "cal-day-num" }, dayNum),
        isWeekend ? el("span", { class: "cal-weekend-badge" }, "Non-working") : null
      );

      const cellContent = [header];

      if (!isWeekend) {
        // Working days (Mon-Fri) only
        const dayEvents = eventsByDate[cellDateStr] || [];
        if (dayEvents.length) {
          const chips = dayEvents.map((ev) =>
            el("button", {
              class: "cal-event-chip",
              type: "button",
              title: `${ev.customer_name} (${ev.amount_formatted}): ${ev.commitment_eta || "Callback"}\nQuote: "${ev.customer_quote || ""}"`,
              onclick: (e) => {
                e.stopPropagation();
                openCall(ev.ref_id);
              },
            },
              el("div", { class: "cal-event-cust" }, ev.customer_name),
              el("div", { class: "cal-event-amt" }, `${ev.amount_formatted} · ${ev.commitment_eta || "Follow-up"}`))
          );
          cellContent.push(el("div", { class: "cal-events-wrap" }, ...chips));
        }
      }

      const cellElem = el("div", {
        class: cellClasses,
        onclick: () => {
          if (!isWeekend) {
            const dayEvents = eventsByDate[cellDateStr] || [];
            if (dayEvents.length) renderDayDetail(cellDateStr, dayEvents);
          }
        },
      }, ...cellContent);

      cells.push(cellElem);
    }

    grid.replaceChildren(...cells);
  }

  function renderDayDetail(dateStr, dayEvents) {
    const card = $("cal-day-detail-card");
    card.hidden = false;
    const formattedDate = new Date(dateStr).toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    $("cal-day-title").textContent = `Follow-ups on ${formattedDate} (${plural(dayEvents.length, "callback")})`;

    const tbody = $("cal-day-rows");
    tbody.replaceChildren(
      ...dayEvents.map((c) =>
        el("tr", { tabindex: "0", onclick: () => openCall(c.ref_id) },
          el("td", {}, el("div", { class: "cust-name" }, c.customer_name)),
          el("td", {}, fmtPhone(c.phone_number)),
          el("td", { class: "num", style: "font-weight:600" }, c.amount_formatted),
          el("td", { style: "font-size:13px" }, c.service_name),
          el("td", {}, c.customer_quote ? el("blockquote", { class: "customer-quote-box" }, `“${c.customer_quote}”`) : "—"),
          el("td", {}, el("span", { class: "eta-chip" }, icon("clock"), c.commitment_eta || "Callback")),
          el("td", {}, el("button", { class: "btn btn-secondary btn-sm", onclick: (e) => { e.stopPropagation(); openCall(c.ref_id); } }, icon("eye"), "Details"))
        )
      )
    );
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ══ Admin Overview ════════════════════════════════════════════════════
  async function loadAdminOverview() {
    try {
      const data = await api("/api/admin/overview");
      state.adminOverview = data;
      renderAdminOverview();
    } catch (err) {
      toast("Couldn't load admin overview", err.message, "error");
    }
  }

  function renderAdminOverview() {
    const data = state.adminOverview;
    if (!data) return;
    const k = data.kpis;
    $("adm-total-users").textContent = k.total_users;
    $("adm-total-calls").textContent = k.total_calls;
    $("adm-calls-sub").textContent = `${k.completed_calls} completed · ${k.failed_calls} failed`;
    $("adm-total-amount").textContent = k.total_amount_formatted;
    $("adm-total-commitments").textContent = k.total_commitments;
    $("adm-commitments-amount").textContent = `${k.commitments_amount_formatted} in pipeline`;

    // Team table
    const tbody = $("adm-team-rows");
    tbody.replaceChildren(
      ...data.team.map((u) => {
        const nextEtaStr = u.next_callback_date
          ? new Date(u.next_callback_date).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" })
          : "None scheduled";

        return el("tr", {},
          el("td", {},
            el("div", { class: "cust-name" }, u.email),
            el("div", { class: "muted", style: "font-size:12px" }, u.last_login_at ? `Last sign-in: ${relTime(u.last_login_at)}` : "Never signed in")),
          el("td", {}, el("span", { class: `pill pill-${u.role === "admin" ? "completed" : "active"}` }, u.role)),
          el("td", { class: "num" }, u.total_calls),
          el("td", { class: "num" }, u.completed_calls),
          el("td", { class: "num", style: "font-weight:600" }, u.amount_formatted),
          el("td", {},
            el("div", { style: "font-weight:600" }, `${u.commitments_count} commitments`),
            el("div", { class: "muted", style: "font-size:12px" }, u.commitments_amount_formatted)),
          el("td", {},
            u.next_callback_date ? el("span", { class: "eta-chip" }, icon("clock"), nextEtaStr) : el("span", { class: "muted" }, "—")),
          el("td", { class: "num" }, `${u.services_count} services`),
          el("td", { style: "display:flex;gap:6px" },
            u.id ? el("button", { class: "btn btn-secondary btn-sm", onclick: () => { state.dashUserFilter = String(u.id); navigate(`/dashboard?user=${u.id}`); } }, "View Dashboard") : null,
            u.id ? el("button", { class: "btn btn-secondary btn-sm", onclick: () => { state.userFilter = String(u.id); navigate("/calls"); } }, "View Calls") : null)
        );
      })
    );

    // Company services breakdown
    const svcContainer = $("adm-services-list");
    if (!data.services.length) {
      svcContainer.replaceChildren(el("div", { class: "muted" }, "No services recorded."));
    } else {
      const maxAmt = Math.max(...data.services.map((s) => s.amount), 1);
      svcContainer.replaceChildren(
        ...data.services.map((s) => {
          const pct = Math.max(5, Math.round((s.amount / maxAmt) * 100));
          return el("div", { class: "service-item" },
            el("div", { class: "service-item-top" },
              el("span", { class: "service-item-name", title: s.name }, s.name),
              el("span", { class: "service-item-amt" }, s.amount_formatted)),
            el("div", { class: "service-bar-wrap" },
              el("div", { class: "service-bar-fill", style: `width:${pct}%` })),
            el("div", { class: "service-item-sub" }, `${s.count} calls company-wide`));
        })
      );
    }

    // Recent fleet commitments stream
    const streamContainer = $("adm-commitments-stream");
    if (!data.recent_commitments.length) {
      streamContainer.replaceChildren(el("div", { class: "muted" }, "No commitments recorded yet."));
    } else {
      const items = data.recent_commitments.slice(0, 8).map((c) =>
        el("div", { class: "stream-item", onclick: () => openCall(c.ref_id), style: "cursor:pointer" },
          el("div", { class: "stream-item-top" },
            el("span", { style: "font-weight:700" }, c.customer_name),
            el("span", { class: "stream-item-amt" }, c.amount_formatted)),
          c.customer_quote ? el("blockquote", { class: "customer-quote-box", style: "max-width:100%" }, `“${c.customer_quote}”`) : null,
          el("div", { style: "display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-top:2px" },
            el("span", { class: "stream-item-agent" }, `By: ${c.created_by_email || "System"}`),
            c.callback_date ? el("span", { class: "eta-chip" }, icon("clock"), new Date(c.callback_date).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" })) : null)
        )
      );
      streamContainer.replaceChildren(...items);
    }
  }

  // Dashboard & Calendar Event Listeners
  $("dash-refresh").addEventListener("click", () => loadAgentStats());
  const dashUserSel = $("dash-user-select");
  if (dashUserSel) {
    dashUserSel.addEventListener("change", (e) => selectDashUser(e.target.value));
  }
  $("dash-search").addEventListener("input", (e) => {
    state.dashSearch = e.target.value;
    renderDashCommitments();
  });
  $("cal-prev").addEventListener("click", () => {
    state.calMonth--;
    if (state.calMonth < 0) { state.calMonth = 11; state.calYear--; }
    loadCalendar();
  });
  $("cal-next").addEventListener("click", () => {
    state.calMonth++;
    if (state.calMonth > 11) { state.calMonth = 0; state.calYear++; }
    loadCalendar();
  });
  $("cal-today").addEventListener("click", () => {
    const now = new Date();
    state.calYear = now.getFullYear();
    state.calMonth = now.getMonth();
    loadCalendar();
  });
  $("cal-day-close").addEventListener("click", () => {
    $("cal-day-detail-card").hidden = true;
  });
  $("admin-dash-refresh").addEventListener("click", () => loadAdminOverview());

  // ══ Tickets & Incidents ═══════════════════════════════════════════════
  async function loadTickets() {
    try {
      const params = new URLSearchParams();
      if (state.ticketsStatusFilter) params.set("status", state.ticketsStatusFilter);
      if (state.ticketsCatFilter) params.set("category", state.ticketsCatFilter);
      if (isAdmin() && state.ticketsUserFilter) params.set("user_id", state.ticketsUserFilter);

      const data = await api(`/api/tickets?${params.toString()}`);
      state.tickets = data.tickets || [];
      state.ticketStats = data.stats || null;
      renderTickets();
    } catch (err) {
      toast("Couldn't load tickets", err.message, "error");
    }
  }

  function renderTickets() {
    // Admin user filter dropdown
    if (isAdmin()) {
      const wrap = $("tickets-user-filter-wrap");
      if (wrap) {
        wrap.style.display = "flex";
        const sel = $("tickets-user-select");
        if (sel && sel.options.length <= 1) {
          sel.replaceChildren(el("option", { value: "" }, "All Assigned Agents"));
          for (const u of state.users || []) {
            sel.appendChild(el("option", { value: String(u.id) }, `${u.email} (${u.role})`));
          }
          sel.value = state.ticketsUserFilter || "";
        }
      }
    }

    // Stats
    const s = state.ticketStats;
    if (s) {
      $("tck-total").textContent = s.total;
      $("tck-open").textContent = s.open;
      $("tck-urgent").textContent = s.urgent;
      $("tck-resolved").textContent = s.resolved;
    }

    const q = (state.ticketsSearch || "").toLowerCase().trim();
    let rows = state.tickets || [];
    if (q) {
      rows = rows.filter((t) =>
        (t.ticket_number || "").toLowerCase().includes(q) ||
        (t.customer_name || "").toLowerCase().includes(q) ||
        (t.service_name || "").toLowerCase().includes(q) ||
        (t.title || "").toLowerCase().includes(q) ||
        (t.customer_quote || "").toLowerCase().includes(q)
      );
    }

    const tbody = $("tickets-rows");
    const empty = $("tickets-empty");
    if (!rows.length) {
      tbody.replaceChildren();
      empty.replaceChildren(
        el("div", { class: "state", style: "padding:32px 0" },
          icon("ticket"),
          el("h3", {}, q ? "No matching tickets" : "No incident tickets in this view"),
          el("div", {}, q ? "Try another search filter." : "When customer issues, human callback requests, or complaints occur, tickets are automatically assigned here."))
      );
      return;
    }
    empty.replaceChildren();

    tbody.replaceChildren(
      ...rows.map((t) => {
        const isResolved = t.status === "resolved";

        const catMap = {
          service_issue: { label: "Service Outage", cls: "badge-cat-service" },
          human_callback: { label: "Human Callback", cls: "badge-cat-human" },
          billing_dispute: { label: "Billing Dispute", cls: "badge-cat-dispute" },
          callback_request: { label: "Callback Request", cls: "badge-cat-callback" },
        };
        const catInfo = catMap[t.category] || { label: capital(t.category || "Issue"), cls: "badge-cat-service" };

        const catElem = el("div", { style: "display:flex;flex-direction:column;gap:4px" },
          el("span", { class: `badge-cat ${catInfo.cls}` }, catInfo.label),
          el("span", { class: `badge-priority badge-priority-${t.priority || "medium"}` }, capital(t.priority || "medium"))
        );

        const quoteElem = el("div", {},
          el("div", { style: "font-weight:600;font-size:13px;margin-bottom:2px" }, t.title),
          t.customer_quote ? el("blockquote", { class: "customer-quote-box", style: "margin:4px 0 0" }, `“${t.customer_quote}”`) : null,
          isResolved && t.resolution_notes ? el("div", { class: "muted", style: "font-size:12px;margin-top:4px" }, `Resolution: ${t.resolution_notes}`) : null
        );

        const assignedElem = t.assigned_to_email
          ? el("div", { class: "dash-agent-badge" },
              el("span", { class: "dash-agent-avatar" }, t.assigned_to_email[0]),
              el("span", {}, t.assigned_to_email.split("@")[0]))
          : el("span", { class: "muted" }, "Unassigned");

        const statusPill = el("span", { class: `ticket-status-pill ticket-status-${t.status}` },
          isResolved ? icon("check") : null,
          capital(t.status.replace("_", " "))
        );

        const actionElem = isResolved
          ? el("span", { class: "pill pill-completed", style: "font-size:11px" }, "Completed")
          : el("button", {
              class: "btn btn-primary btn-sm",
              type: "button",
              onclick: (e) => { e.stopPropagation(); openResolveTicketDialog(t); },
            }, icon("check"), "Resolve");

        return el("tr", { tabindex: "0", onclick: () => { if (t.call_ref_id) openCall(t.call_ref_id); } },
          el("td", {}, el("span", { class: "ticket-num" }, t.ticket_number)),
          el("td", {},
            el("div", { class: "cust-name" }, t.customer_name || "Customer"),
            el("div", { class: "cust-num" }, fmtPhone(t.phone_number))),
          el("td", {}, catElem),
          el("td", {}, quoteElem),
          el("td", {}, assignedElem),
          el("td", {}, statusPill),
          el("td", { class: "muted", style: "font-size:12px;white-space:nowrap" }, relTime(t.created_at)),
          el("td", {}, actionElem)
        );
      })
    );
  }

  function openResolveTicketDialog(ticket) {
    state.activeResolveTicketId = ticket.id;
    $("tr-cust-info").textContent = `Ticket ${ticket.ticket_number} · ${ticket.customer_name} (${ticket.service_name || "Service"})`;
    $("tr-notes").value = "";
    $("ticket-resolve-dialog").showModal();
    $("tr-notes").focus();
  }

  async function submitResolveTicket() {
    const ticketId = state.activeResolveTicketId;
    if (!ticketId) return;
    const notes = $("tr-notes").value.trim();
    if (!notes) {
      return toast("Resolution notes required", "Please describe what action was taken to resolve this ticket.", "error");
    }
    try {
      await api(`/api/tickets/${ticketId}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution_notes: notes }),
      });
      $("ticket-resolve-dialog").close();
      toast("Ticket resolved", "The ticket has been marked completed.");
      loadTickets();
    } catch (err) {
      toast("Failed to resolve ticket", err.message, "error");
    }
  }

  // ══ Callbacks Requested ══════════════════════════════════════════════
  async function loadCallbacks() {
    try {
      const params = new URLSearchParams();
      if (state.cbTypeFilter) params.set("cb_type", state.cbTypeFilter);
      if (isAdmin() && state.cbUserFilter) params.set("user_id", state.cbUserFilter);

      const data = await api(`/api/callbacks?${params.toString()}`);
      state.callbacksData = data;
      renderCallbacks();
    } catch (err) {
      toast("Couldn't load callbacks", err.message, "error");
    }
  }

  function renderCallbacks() {
    // Admin user filter dropdown
    if (isAdmin()) {
      const wrap = $("cb-user-filter-wrap");
      if (wrap) {
        wrap.style.display = "flex";
        const sel = $("cb-user-select");
        if (sel && sel.options.length <= 1) {
          sel.replaceChildren(el("option", { value: "" }, "All Team Members"));
          for (const u of state.users || []) {
            sel.appendChild(el("option", { value: String(u.id) }, `${u.email} (${u.role})`));
          }
          sel.value = state.cbUserFilter || "";
        }
      }
    }

    const data = state.callbacksData;
    if (!data) return;
    const k = data.kpis;
    $("cb-total").textContent = k.total_callbacks;
    $("cb-human").textContent = k.human_callbacks;
    $("cb-auto").textContent = k.auto_callbacks;
    $("cb-today").textContent = `${k.scheduled_today} today (${k.overdue_count} overdue)`;

    const q = (state.cbSearch || "").toLowerCase().trim();
    let rows = data.callbacks || [];
    if (q) {
      rows = rows.filter((c) =>
        (c.customer_name || "").toLowerCase().includes(q) ||
        (c.phone_number || "").includes(q) ||
        (c.service_name || "").toLowerCase().includes(q) ||
        (c.customer_quote || "").toLowerCase().includes(q) ||
        (c.commitment_eta || "").toLowerCase().includes(q)
      );
    }

    const tbody = $("callbacks-rows");
    const empty = $("callbacks-empty");
    if (!rows.length) {
      tbody.replaceChildren();
      empty.replaceChildren(
        el("div", { class: "state", style: "padding:32px 0" },
          icon("callback"),
          el("h3", {}, q ? "No matching callbacks" : "No callbacks scheduled in this view"),
          el("div", {}, q ? "Try another search filter." : "When customers request a callback or give payment commitments, they are scheduled here."))
      );
      return;
    }
    empty.replaceChildren();

    tbody.replaceChildren(
      ...rows.map((c) => {
        const isHuman = c.callback_type === "human";
        const typeBadge = el("span", { class: `cb-type-badge cb-type-${c.callback_type}` },
          icon(isHuman ? "headphones" : "retry"),
          isHuman ? "Human Callback" : "Auto Bot Reminder"
        );

        const dateStr = c.callback_date
          ? new Date(c.callback_date).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", year: "numeric" })
          : "—";

        const etaPill = el("div", {},
          c.commitment_eta ? el("span", { class: "eta-chip" }, icon("clock"), c.commitment_eta) : null,
          el("div", { style: "font-weight:600;font-size:12px;margin-top:2px" }, dateStr)
        );

        const assignedElem = c.assigned_to_email
          ? el("div", { class: "dash-agent-badge" },
              el("span", { class: "dash-agent-avatar" }, c.assigned_to_email[0]),
              el("span", {}, c.assigned_to_email.split("@")[0]))
          : el("span", { class: "muted" }, "System");

        return el("tr", { tabindex: "0", onclick: () => openCall(c.ref_id) },
          el("td", {},
            el("div", { class: "cust-name" }, c.customer_name || "Customer"),
            el("div", { class: "cust-num" }, fmtPhone(c.phone_number))),
          el("td", { class: "num", style: "font-weight:600" }, c.amount_formatted),
          el("td", { style: "font-size:13px;max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis", title: c.service_name }, c.service_name),
          el("td", {}, typeBadge),
          el("td", {}, etaPill),
          el("td", {}, c.customer_quote ? el("blockquote", { class: "customer-quote-box" }, `“${c.customer_quote}”`) : "—"),
          el("td", {}, assignedElem),
          el("td", {}, el("span", { class: `pill pill-${c.status === "completed" ? "completed" : "active"}` }, c.status)),
          el("td", {}, el("button", { class: "btn btn-secondary btn-sm", onclick: (e) => { e.stopPropagation(); openCall(c.ref_id); } }, icon("eye"), "Details"))
        );
      })
    );
  }

  // Tickets & Callbacks Event Listeners
  $("tickets-refresh").addEventListener("click", () => loadTickets());
  $("tickets-search").addEventListener("input", (e) => {
    state.ticketsSearch = e.target.value;
    renderTickets();
  });
  $("tickets-status-filter").addEventListener("change", (e) => {
    state.ticketsStatusFilter = e.target.value;
    loadTickets();
  });
  $("tickets-cat-filter").addEventListener("change", (e) => {
    state.ticketsCatFilter = e.target.value;
    loadTickets();
  });
  const tckUserSel = $("tickets-user-select");
  if (tckUserSel) {
    tckUserSel.addEventListener("change", (e) => {
      state.ticketsUserFilter = e.target.value;
      loadTickets();
    });
  }
  $("tr-cancel").addEventListener("click", () => $("ticket-resolve-dialog").close());
  $("tr-submit").addEventListener("click", () => submitResolveTicket());

  $("cb-refresh").addEventListener("click", () => loadCallbacks());
  $("cb-search").addEventListener("input", (e) => {
    state.cbSearch = e.target.value;
    renderCallbacks();
  });
  $("cb-type-filter").addEventListener("change", (e) => {
    state.cbTypeFilter = e.target.value;
    loadCallbacks();
  });
  const cbUserSel = $("cb-user-select");
  if (cbUserSel) {
    cbUserSel.addEventListener("change", (e) => {
      state.cbUserFilter = e.target.value;
      loadCallbacks();
    });
  }

  // ══ Voice library ═══════════════════════════════════════════════════════
  const vlText = $("vl-text");
  let vlGender = "All";
  const vlLang = () => document.querySelector('input[name="vl-lang"]:checked').value;

  function renderVoices() {
    const cat = state.catalog;
    const grid = $("voice-grid");
    if (!cat) {
      grid.replaceChildren(...Array.from({ length: 8 }, () => el("div", { class: "card voice-card" }, el("div", { class: "skeleton", style: "width:140px;grid-column:1/-1" }))));
      return;
    }
    const counts = { All: cat.voices.length, Female: 0, Male: 0 };
    cat.voices.forEach((v) => counts[v.gender]++);
    $("vl-gender").replaceChildren(...["All", "Female", "Male"].map((g) =>
      el("button", { class: "chip", type: "button", "aria-pressed": String(vlGender === g), onclick: () => { vlGender = g; renderVoices(); } }, g, el("span", { class: "count" }, counts[g]))));
    $("vl-count").textContent = vlText.value.length;

    const lang = vlLang(), text = vlText.value.trim();
    const voices = cat.voices.filter((v) => vlGender === "All" || v.gender === vlGender);
    grid.replaceChildren(...voices.map((v) => {
      const key = player.keyOf(v.id, lang, text);
      const mine = player.key === key;
      const isDefault = v.id === state.defaultVoice;
      const progress = mine && player.status === "playing" && player.audio.duration ? (player.audio.currentTime / player.audio.duration) * 100 : 0;
      return el("div", { class: `card voice-card${isDefault ? " is-default" : ""}${mine ? " is-playing" : ""}`, dataset: { voice: v.id } },
        el("span", { class: `avatar ${v.gender === "Male" ? "male" : ""}`, "aria-hidden": "true" }, v.name[0]),
        el("div", {}, el("div", { class: "voice-name" }, v.name),
          el("div", { class: "voice-meta" }, v.gender, isDefault ? el("span", { class: "badge-default" }, "Default") : null)),
        el("button", { class: `play${mine ? " on" : ""}`, type: "button", "aria-label": `${mine ? "Stop" : "Play"} ${v.name} in ${lang}`,
            onclick: () => player.toggle(v.id, lang, text) },
          mine && player.status === "loading" ? el("span", { class: "spinner" }) : icon(mine ? "stop" : "play", "icon fill")),
        el("div", { class: "voice-foot" },
          el("div", { class: "voice-progress" }, el("span", { style: `width:${progress}%` })),
          el("button", { class: "use-voice", type: "button", disabled: isDefault, onclick: () => setDefaultVoice(v.id) }, isDefault ? "In use" : "Use for calls")));
    }));
  }

  function updateVoiceProgress() {
    if (state.page !== "voices") return;
    // Light update while playing; full re-render only when the playing card changes.
    const playingKey = player.key;
    document.querySelectorAll(".voice-card").forEach((card) => {
      const mine = playingKey === player.keyOf(card.dataset.voice, vlLang(), vlText.value.trim());
      const bar = card.querySelector(".voice-progress span");
      if (bar) bar.style.width = mine && player.audio.duration ? `${(player.audio.currentTime / player.audio.duration) * 100}%` : "0";
    });
  }
  let lastPlayerSig = "";
  player.listeners.add(() => {
    const sig = `${player.key}|${player.status}`;
    if (sig !== lastPlayerSig) { lastPlayerSig = sig; if (state.page === "voices") renderVoices(); }
    else updateVoiceProgress();
  });

  document.querySelectorAll('input[name="vl-lang"]').forEach((r) => r.addEventListener("change", () => {
    player.stop();
    const samples = state.catalog?.samples || {};
    if (Object.values(samples).includes(vlText.value.trim()) || !vlText.value.trim()) vlText.value = samples[vlLang()] || "";
    renderVoices();
  }));
  let textTimer;
  vlText.addEventListener("input", () => {
    $("vl-count").textContent = vlText.value.length;
    clearTimeout(textTimer);
    textTimer = setTimeout(() => { player.stop(); renderVoices(); }, 300);
  });
  $("vl-reset").addEventListener("click", () => { vlText.value = state.catalog?.samples?.[vlLang()] || ""; player.stop(); renderVoices(); });

  // ══ Data loading ════════════════════════════════════════════════════════
  async function loadCatalog() {
    try {
      state.catalog = await api("/voices/catalog");
    } catch (err) {
      toast("Couldn't load voices", err.message, "error");
      return;
    }
    const ids = new Set(state.catalog.voices.map((v) => v.id));
    if (!ids.has(state.defaultVoice)) state.defaultVoice = state.catalog.default_voice;
    $("voice").replaceChildren(...state.catalog.voices.map((v) => el("option", { value: v.id }, `${v.name} · ${v.gender}`)));
    setDefaultVoice(state.defaultVoice, false);
    if (!vlText.value) vlText.value = state.catalog.samples[vlLang()] || "";
    renderVoices();
  }

  async function loadConfig() {
    try {
      state.config = { ...state.config, ...(await api("/config")) };
      $("max-rows").textContent = state.config.batch_max_rows;
    } catch { /* keep defaults */ }
  }

  let busy = { recent: false, calls: false, batches: false };
  async function guarded(name, fn) {
    if (busy[name]) return;
    busy[name] = true;
    try { await fn(); } finally { busy[name] = false; }
  }

  const loadRecent = () => guarded("recent", async () => {
    try {
      state.recent = await api("/logs?limit=6");
      state.recentLoaded = true;
      renderRecent();
      $("recent-updated").textContent = "";
    } catch (err) {
      if (!state.recentLoaded) { state.recentLoaded = true; renderRecent(); }
      $("recent-updated").textContent = "Couldn't refresh";
    }
  });

  const loadBatches = () => guarded("batches", async () => {
    try {
      state.batches = await api(`/batches?limit=${state.page === "calls" ? 50 : 5}`);
      renderBatches();
      if (state.page === "calls") renderBatchFilter();
    } catch { /* batches are secondary; keep the last view */ }
  });

  const loadCalls = () => guarded("calls", async () => {
    const qs = new URLSearchParams({ limit: state.limit });
    if (state.batchFilter) qs.set("batch_id", state.batchFilter);
    if (state.userFilter && isAdmin()) qs.set("user_id", state.userFilter);
    try {
      state.calls = await api(`/logs?${qs}`);
      state.callsLoaded = true;
      state.lastOk = Date.now();
      $("banner").replaceChildren();
      renderCalls();
      renderUpdated();
    } catch (err) {
      $("banner").replaceChildren(el("div", { class: "banner", role: "alert" }, icon("alert"), `Couldn't refresh calls: ${err.message}`));
      if (!state.callsLoaded) { state.callsLoaded = true; renderCalls(); }
    }
  });

  function renderUpdated() {
    if (!state.lastOk || state.page !== "calls") return;
    const s = Math.round((Date.now() - state.lastOk) / 1000);
    $("updated").textContent = s < 5 ? "Updated just now" : `Updated ${s}s ago`;
  }

  function refresh() {
    if (state.page === "start") { loadRecent(); loadBatches(); }
    if (state.page === "calls") { loadCalls(); loadBatches(); }
    if (state.openRef) {
      const c = findCall(state.openRef);
      if (!c || groupOf(c) === "live") loadDetail(state.openRef, true);
    }
  }

  async function checkHealth() {
    const h = $("health");
    try {
      const r = await fetch("/health", { cache: "no-store" });
      if (!r.ok) throw new Error();
      h.dataset.state = "ok"; $("health-text").textContent = "Service online";
    } catch {
      h.dataset.state = "down"; $("health-text").textContent = "Service unreachable";
    }
  }

  // ══ Signed-in user ══════════════════════════════════════════════════════
  function renderMe() {
    const me = state.me;
    $("me").hidden = false;
    $("me-avatar").textContent = me.email[0].toUpperCase();
    $("me-email").textContent = me.email; $("me-email").title = me.email;
    $("me-role").textContent = me.role === "admin" ? "Admin" : "User";
    document.querySelectorAll(".admin-only").forEach((n) => (n.hidden = me.role !== "admin"));
    $("calls-sub").textContent = me.role === "admin"
      ? "Every call by every user, with its outcome, recording and transcript."
      : "Every call you started, with its outcome, recording and transcript.";
  }

  $("logout").addEventListener("click", async () => {
    try { await fetch("/auth/logout", { method: "POST" }); } finally { location.replace("/login"); }
  });

  // ══ Users (admin) ═══════════════════════════════════════════════════════
  function generatePassword() {
    const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
    const bytes = crypto.getRandomValues(new Uint32Array(12));
    return Array.from(bytes, (n) => chars[n % chars.length]).join("");
  }

  const loadUsers = () => guarded("users", async () => {
    if (!isAdmin()) return;
    try {
      state.users = await api("/users/list");
      renderUsers();
      if (state.page === "calls") renderUserFilter();
    } catch (err) {
      if (state.page === "users") toast("Couldn't load users", err.message, "error");
    }
  });

  function renderUsers() {
    $("ul-count").textContent = plural(state.users.length, "account");
    $("user-rows").replaceChildren(...state.users.map((u) => {
      const self = u.id === state.me.id;
      const disabled = Boolean(u.disabled_at);
      return el("tr", { class: disabled ? "is-disabled" : null },
        el("td", {},
          el("div", { class: "user-cell" },
            el("span", { class: "avatar", "aria-hidden": "true" }, u.email[0].toUpperCase()),
            el("div", { style: "min-width:0" },
              el("div", { class: "cust-name", title: u.email }, u.email, self ? el("span", { class: "you" }, "You") : null),
              el("div", { class: "cust-num", title: u.created_by_email ? `Added by ${u.created_by_email}` : "" },
                disabled ? `Disabled ${relTime(u.disabled_at).toLowerCase()}` : `Added ${relTime(u.created_at).toLowerCase()}`)))),
        el("td", {}, el("span", { class: `role-pill role-${u.role}` }, u.role === "admin" ? "Admin" : "User")),
        el("td", { class: "num" }, u.calls),
        el("td", { class: "muted" }, u.last_login_at ? relTime(u.last_login_at) : "Never"),
        el("td", { class: "row-actions" },
          el("button", { class: "btn btn-secondary btn-sm", type: "button", title: "Reset password", "aria-label": `Reset password for ${u.email}`, onclick: () => resetPassword(u) }, icon("key"), "Reset"),
          self ? null : el("button", { class: `btn btn-sm ${disabled ? "btn-secondary" : "btn-danger"}`, type: "button", onclick: () => toggleUser(u) },
            disabled ? "Enable" : "Disable")));
    }));
  }

  function fieldError(form, name, message) {
    const f = form.querySelector(`[data-field="${name}"]`);
    if (!f) return;
    const hint = f.querySelector(".hint");
    if (!hint.dataset.base) hint.dataset.base = hint.textContent;
    if (message) { f.setAttribute("data-invalid", ""); hint.textContent = message; }
    else { f.removeAttribute("data-invalid"); hint.textContent = hint.dataset.base; }
  }

  $("nu-generate").addEventListener("click", () => { $("nu-password").value = generatePassword(); fieldError($("new-user"), "password", ""); });
  $("nu-email").addEventListener("input", () => fieldError($("new-user"), "email", ""));
  $("nu-password").addEventListener("input", () => fieldError($("new-user"), "password", ""));

  $("new-user").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const email = $("nu-email").value.trim().toLowerCase(), password = $("nu-password").value;
    const role = form.querySelector('input[name="nu-role"]:checked').value;
    const emailBad = !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email) ? (email ? "Enter a valid email address" : "Email is required") : "";
    const pwBad = password.length < 8 ? "Password must be at least 8 characters" : "";
    fieldError(form, "email", emailBad); fieldError(form, "password", pwBad);
    if (emailBad || pwBad) { (emailBad ? $("nu-email") : $("nu-password")).focus(); return; }

    const btn = $("nu-submit");
    btn.disabled = true; btn.setAttribute("aria-busy", "true");
    try {
      await api("/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password, role }) });
      toast("User created", `${email} can now sign in with the password you set.`);
      form.reset();
      loadUsers();
    } catch (err) {
      if (/already exists/i.test(err.message)) { fieldError(form, "email", err.message); $("nu-email").focus(); }
      else toast("Couldn't create the user", err.message, "error");
    } finally {
      btn.disabled = false; btn.removeAttribute("aria-busy");
    }
  });

  async function toggleUser(u) {
    const disabling = !u.disabled_at;
    if (disabling) {
      const ok = await confirmDialog({
        title: `Disable ${u.email}?`,
        text: "They're signed out immediately and can't sign in until you enable the account again. Their calls stay in the logs.",
        ok: "Disable", danger: true,
      });
      if (!ok) return;
    }
    try {
      await api(`/users/${u.id}/${disabling ? "disable" : "enable"}`, { method: "POST" });
      toast(disabling ? "User disabled" : "User enabled", u.email);
    } catch (err) {
      toast("Couldn't update the user", err.message, "error");
    }
    loadUsers();
  }

  function resetPassword(u) {
    const dlg = $("pw-dialog"), input = $("pw-new"), form = $("pw-form");
    $("pw-text").textContent = `Set a new password for ${u.email}.`;
    input.value = generatePassword();
    fieldError(form, "password", "");
    dlg.showModal();
    input.select();
    const onSubmit = async (e) => {
      e.preventDefault();
      if (input.value.length < 8) { fieldError(form, "password", "Password must be at least 8 characters"); input.focus(); return; }
      try {
        await api(`/users/${u.id}/password`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: input.value }) });
        dlg.close();
        toast("Password reset", `Share the new password with ${u.email}.`);
      } catch (err) {
        fieldError(form, "password", err.message);
      }
    };
    form.addEventListener("submit", onSubmit);
    dlg.addEventListener("close", () => form.removeEventListener("submit", onSubmit), { once: true });
  }
  $("pw-cancel").addEventListener("click", () => $("pw-dialog").close());
  $("pw-generate").addEventListener("click", () => { $("pw-new").value = generatePassword(); $("pw-new").select(); });
  $("pw-new").addEventListener("input", () => fieldError($("pw-form"), "password", ""));

  // ══ Settings (demo UI — values are kept in this browser only) ═══════════
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const loadSettings = () => {
    const defaults = { from: "09:00", to: "21:00", days: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], retry: true, wait: 3, unit: "hours", max: 3 };
    try { return { ...defaults, ...JSON.parse(store.get("settings", "{}")) }; } catch { return defaults; }
  };
  let settings = loadSettings();
  let editDays = [];

  function selectSettingsTab(which) {
    [["hours", "st-tab-hours"], ["retry", "st-tab-retry"]].forEach(([panel, tab]) => {
      $(tab).setAttribute("aria-selected", String(panel === which));
      $(`st-${panel}`).hidden = panel !== which;
    });
  }
  $("st-tab-hours").addEventListener("click", () => selectSettingsTab("hours"));
  $("st-tab-retry").addEventListener("click", () => selectSettingsTab("retry"));

  function renderHours(editing) {
    $("st-from").value = settings.from; $("st-to").value = settings.to;
    $("st-from").disabled = $("st-to").disabled = !editing;
    $("st-hours-actions").hidden = !editing;
    $("st-hours-edit").setAttribute("aria-pressed", String(editing));
    editDays = [...settings.days];
    $("st-days").replaceChildren(...DAYS.map((d) => el("button", {
      type: "button", "aria-pressed": String(editDays.includes(d)), disabled: !editing,
      onclick: (e) => {
        editDays = editDays.includes(d) ? editDays.filter((x) => x !== d) : [...editDays, d];
        e.currentTarget.setAttribute("aria-pressed", String(editDays.includes(d)));
      },
    }, d)));
  }
  $("st-hours-edit").addEventListener("click", () => { renderHours(true); $("st-from").focus(); });
  $("st-hours-cancel").addEventListener("click", () => renderHours(false));
  $("st-hours-save").addEventListener("click", () => {
    const from = $("st-from").value, to = $("st-to").value;
    if (!from || !to || from >= to) return toast("Check the calling window", "The end time must be after the start time.", "error");
    if (!editDays.length) return toast("Pick at least one calling day", "", "error");
    settings = { ...settings, from, to, days: DAYS.filter((d) => editDays.includes(d)) };
    store.set("settings", JSON.stringify(settings));
    renderHours(false);
    toast("Calling window saved", `Bulk calls will be placed between ${from} and ${to} IST.`);
  });

  function renderRetry() {
    $("st-retry-on").checked = settings.retry;
    $("st-wait").value = settings.wait; $("st-wait-unit").value = settings.unit; $("st-max").value = settings.max;
    $("st-retry-opts").setAttribute("aria-disabled", String(!settings.retry));
  }
  $("st-retry-on").addEventListener("change", (e) => $("st-retry-opts").setAttribute("aria-disabled", String(!e.target.checked)));
  $("st-retry-save").addEventListener("click", () => {
    const wait = Number($("st-wait").value), max = Number($("st-max").value);
    if (!Number.isInteger(wait) || wait < 1) return toast("Check the wait time", "Enter a whole number of 1 or more.", "error");
    if (!Number.isInteger(max) || max < 1 || max > 10) return toast("Check the retry attempts", "Enter a number from 1 to 10.", "error");
    settings = { ...settings, retry: $("st-retry-on").checked, wait, unit: $("st-wait-unit").value, max };
    store.set("settings", JSON.stringify(settings));
    toast("Callback settings saved", settings.retry ? `Unanswered calls retry after ${wait} ${settings.unit}, up to ${plural(max, "time")}.` : "Auto-retry is off.");
  });
  renderHours(false); renderRetry();

  // ══ Boot ════════════════════════════════════════════════════════════════
  (async () => {
    try {
      state.me = await api("/auth/me");  // redirects to /login if the session has ended
    } catch {
      return;
    }
    renderMe();
    selectTab(store.get("startTab", "single"), false);
    renderRecent(); renderCalls(); renderVoices();
    updateCallLabel();
    await loadConfig();
    route();
    loadCatalog();
    loadUsers();
    checkHealth();
    setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
    setInterval(() => { if (!document.hidden) checkHealth(); }, 30000);
    setInterval(renderUpdated, 1000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); checkHealth(); } });
  })();
})();
