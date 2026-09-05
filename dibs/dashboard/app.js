"use strict";

/* dibs dashboard — vanilla JS, no build step, no external deps. Polls GET /v1/state (2s),
 * GET /v1/screenshot.png (1s), GET /v1/audit (2s). apiFetch() is the single choke point for
 * auth (bearer token) and errors (toast area / token gate on 401). */

const TOKEN_KEY = "dibs_admin_token";
const STATE_POLL_MS = 2000;
const SHOT_POLL_MS = 1000;
const AUDIT_POLL_MS = 2000;

let currentScreen = null;
let shotObjectUrl = null;
let auditObjectUrls = [];
let leaseExpiresAt = null;
let consentExpiresAt = null;
let consentRequestedAt = null;
let lastState = null;
let confirmingRevoke = null; // {id, timer} — survives the next renderAgents() rebuild

// Pause reasons caused by the human touching the mouse/keyboard, not a manual/admin pause.
// Mirrors tray.py's HUMAN_PAUSE_REASONS — keep the two in sync.
const HUMAN_PAUSE_REASONS = new Set(["human_took_the_mouse", "human_release"]);
/* ---------- fetch helper ---------- */
function authHeaders() {
  const token = localStorage.getItem(TOKEN_KEY);
  return token ? { Authorization: "Bearer " + token } : {};
}

async function apiFetch(path, opts = {}) {
  const headers = Object.assign({}, opts.headers, authHeaders());
  let resp;
  try {
    resp = await fetch(path, Object.assign({ cache: "no-store" }, opts, { headers }));
  } catch (err) {
    toast("Network error: " + err.message, "error");
    throw err;
  }
  if (resp.status === 401) {
    showTokenGate();
    throw new Error("unauthorized");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.clone().json();
      detail = body.detail || body.error || detail;
    } catch (_) { /* body wasn't json */ }
    toast(`${path.split("?")[0]}: ${detail}`, "error");
    throw new Error(detail);
  }
  return resp;
}

async function apiJson(path, opts) {
  return (await apiFetch(path, opts)).json();
}

async function apiBlobUrl(path) {
  const blob = await (await apiFetch(path)).blob();
  return URL.createObjectURL(blob);
}
/* ---------- toasts (slide in from the bottom-right via CSS; see style.css) ---------- */
function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " error" : "");
  el.textContent = message;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}
/* ---------- token gate ---------- */
function showTokenGate() { document.getElementById("token-gate").hidden = false; }
function hideTokenGate() { document.getElementById("token-gate").hidden = true; }

document.getElementById("token-save").addEventListener("click", () => {
  const val = document.getElementById("token-input").value.trim();
  if (!val) return;
  localStorage.setItem(TOKEN_KEY, val);
  hideTokenGate();
  refreshState();
  refreshAudit();
  refreshScreenshot();
});
/* ---------- formatting helpers ---------- */
function formatUptime(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${m}m ${s}s`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatRelative(iso) {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "?";
  const diff = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// A "12s ago"/"waiting 3m" span that ticks in place — tickRelativeTimes() (1/s) rewrites
// every one of these from its data-iso, so tables stay live without a full re-render.
function relSpan(iso, prefix = "") {
  return `<span class="rel-time" data-iso="${escapeHtml(iso || "")}" data-prefix="${escapeHtml(prefix)}">${prefix}${formatRelative(iso)}</span>`;
}

function tickRelativeTimes() {
  document.querySelectorAll(".rel-time").forEach((el) => {
    el.textContent = (el.dataset.prefix || "") + formatRelative(el.dataset.iso);
  });
}

function formatCountdown(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "?";
  const diff = Math.floor((then - Date.now()) / 1000);
  if (diff <= 0) return "expired";
  if (diff < 60) return `${diff}s`;
  return `${Math.floor(diff / 60)}m ${diff % 60}s`;
}

function countdownExpiring(iso) {
  return !!iso && new Date(iso).getTime() - Date.now() < 10000;
}

// Format a raw "seconds ago" number (not an ISO string) — used for human.last_input_ago_s.
function formatAgoSeconds(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return "?";
  seconds = Math.max(0, Math.floor(seconds));
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} m`;
  return `${Math.floor(seconds / 3600)} h`;
}

function truncate(str, n) {
  if (str == null) return "";
  str = String(str);
  return str.length > n ? str.slice(0, n) + "…" : str;
}

function fmtTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? (iso || "") : d.toLocaleTimeString([], { hour12: false });
}

function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Compact rendering of an audit row's input, e.g. `left_click [715,402]`, `type "hello…"`.
function formatAction(row) {
  const a = row.action;
  const input = row.input || {};
  const coord = input.coordinate ? `[${input.coordinate[0]},${input.coordinate[1]}]` : "";
  switch (a) {
    case "left_click": case "right_click": case "middle_click": case "double_click": case "triple_click":
      return coord ? `${a} ${coord}` : a;
    case "left_click_drag": return `drag [${(input.start_coordinate || []).join(",")}]→[${(input.coordinate || []).join(",")}]`;
    case "mouse_move": return `move ${coord}`;
    case "scroll": return `scroll ${input.scroll_direction || "?"} x${input.scroll_amount || "?"}${coord ? " " + coord : ""}`;
    case "type": return `type "${truncate(input.text, 40)}"`;
    case "key": return `key ${input.text || ""}${input.repeat > 1 ? " x" + input.repeat : ""}`;
    case "hold_key": return `hold ${input.text || ""} ${input.duration || ""}s`;
    case "wait": return `wait ${input.duration || ""}s`;
    case "zoom": return `zoom [${(input.region || []).join(",")}]`;
    case "focus_window": return `focus ${input.title || input.hwnd || ""}`;
    case "set_clipboard": return `set_clipboard "${truncate(input.text, 40)}"`;
    case "launch": return `launch ${truncate(input.command, 40)}`;
    default: return ""; // read-only, no params (screenshot, cursor_position, …) — Action column already names it
  }
}
/* ---------- header / status pill (the hero) ---------- */

// Mirrors dibs/tray.py's derive_state() precedence exactly (paused > human > consent >
// agent > locked > idle) with the friendlier labels from SPEC-v0.3-visual.md §3, so the
// pill always agrees with the tray icon and overlay banner (closes review finding #8).
function deriveDashboardState(state) {
  const paused = !!state.paused;
  const reason = state.pause_reason;
  const holder = (state.lease || {}).holder;
  const pending = (state.consent || {}).pending;
  const mode = state.mode || (state.config && state.config.mode) || "ask";
  if (paused && HUMAN_PAUSE_REASONS.has(reason)) return { name: "human", label: "You have the desk" };
  if (paused) return { name: "paused", label: "Paused" + (reason ? ` (${reason})` : "") };
  if (pending) return { name: "consent", label: `${pending.name || pending.agent_id || "an agent"} is asking…` };
  if (holder) return { name: "agent", label: `${holder.name || holder.agent_id || "an agent"} has dibs` };
  if (mode === "locked") return { name: "locked", label: "Locked" };
  return { name: "idle", label: "All yours" };
}

// The screen preview's border glows in the same colour as the pill (SPEC §3).
function applyScreenState(name) {
  const wrap = document.getElementById("screenshot-wrap");
  if (wrap) wrap.className = `screenshot-wrap state-${name}`;
}

function renderHeader(state) {
  const cfg = state.config || {};
  document.getElementById("host-port").textContent = `${cfg.host || "?"}:${cfg.port || "?"}`;
  document.getElementById("uptime").textContent = "up " + formatUptime(state.uptime_s);
  document.getElementById("config-strip").textContent =
    `mode: ${cfg.mode || state.mode || "?"} · overlay: ${cfg.overlay === false ? "off" : "on"}`;

  const { name, label } = deriveDashboardState(state);
  const pill = document.getElementById("status-pill");
  pill.textContent = label;
  pill.className = `pill pill-${name}`;
  applyScreenState(name);

  const paused = !!state.paused;
  document.getElementById("btn-pause").hidden = paused;
  document.getElementById("btn-resume").hidden = !paused;
}
/* ---------- mode selector ---------- */
function renderMode(state) {
  const mode = state.mode || (state.config && state.config.mode);
  document.querySelectorAll(".mode-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === mode));
}

document.getElementById("mode-selector").addEventListener("click", async (e) => {
  const btn = e.target.closest(".mode-btn");
  if (!btn) return;
  const mode = btn.dataset.mode;
  try {
    await apiFetch("/v1/admin/mode", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }),
    });
    toast(`Mode: ${mode}`);
    refreshState();
  } catch (_) { /* already toasted */ }
});
/* ---------- human presence chip ---------- */
function renderHuman(state) {
  const chip = document.getElementById("human-chip");
  const human = state.human;
  if (!human) {
    chip.textContent = "Human: …";
    chip.className = "chip chip-unknown";
    return;
  }
  chip.textContent = human.active
    ? `Human: active (${formatAgoSeconds(human.last_input_ago_s)} ago)`
    : `Human: idle ${formatAgoSeconds(human.last_input_ago_s)}`;
  chip.className = human.active ? "chip chip-active" : "chip chip-idle";
}
/* ---------- take the desk back ---------- */
document.getElementById("btn-release").addEventListener("click", async () => {
  try {
    await apiFetch("/v1/admin/release", { method: "POST" });
    toast("Desk taken back");
    refreshState();
  } catch (_) { /* already toasted */ }
});
/* ---------- desk / lease card ---------- */
function renderDesk(state) {
  const lease = state.lease || {};
  const holderEl = document.getElementById("desk-holder");
  const btnForce = document.getElementById("btn-force-release");

  if (lease.holder) {
    const h = lease.holder;
    leaseExpiresAt = h.expires_at;
    holderEl.innerHTML = `
      <div><span class="holder-name">${escapeHtml(h.name)}</span><span class="holder-id">${escapeHtml(h.agent_id)}</span></div>
      <div class="holder-times">acquired ${fmtTime(h.acquired_at)} &middot; expires in
        <span class="holder-countdown${countdownExpiring(h.expires_at) ? " expiring" : ""}">${formatCountdown(h.expires_at)}</span>
      </div>`;
    btnForce.disabled = false;
  } else {
    leaseExpiresAt = null;
    holderEl.innerHTML = `<p class="empty">🐑 Nobody has dibs. The desk is all yours.</p>`;
    btnForce.disabled = true;
  }

  const queueEl = document.getElementById("queue-list");
  const queue = lease.queue || [];
  queueEl.innerHTML = queue.length
    ? queue.map(q => `<li>${escapeHtml(q.name)} <span class="meta">${escapeHtml(q.agent_id)} &middot; ${relSpan(q.since, "waiting ")}</span></li>`).join("")
    : `<li class="empty">empty</li>`;
}

// The consent countdown bar: fraction of the grant window remaining (SPEC §3: "the
// countdown as a thin ring or bar").
function updateConsentBar() {
  const bar = document.getElementById("consent-bar");
  if (!bar) return;
  if (!consentExpiresAt || !consentRequestedAt) { bar.style.width = "0%"; return; }
  const total = new Date(consentExpiresAt) - new Date(consentRequestedAt);
  const remain = new Date(consentExpiresAt) - Date.now();
  bar.style.width = (total > 0 ? Math.max(0, Math.min(100, (remain / total) * 100)) : 0) + "%";
}

function tickCountdown() {
  const leaseEl = document.querySelector(".holder-countdown");
  if (leaseEl && leaseExpiresAt) {
    leaseEl.textContent = formatCountdown(leaseExpiresAt);
    leaseEl.classList.toggle("expiring", countdownExpiring(leaseExpiresAt));
  }
  const consentEl = document.getElementById("consent-countdown");
  if (consentEl && consentExpiresAt) {
    consentEl.textContent = formatCountdown(consentExpiresAt);
    consentEl.classList.toggle("expiring", countdownExpiring(consentExpiresAt));
  }
  updateConsentBar();
  tickRelativeTimes();
}
/* ---------- consent card ---------- */
function renderConsent(state) {
  const consent = state.consent || {};
  const pending = consent.pending;
  const wrap = document.getElementById("consent-wrap");

  if (pending) {
    consentExpiresAt = pending.expires_at;
    consentRequestedAt = pending.requested_at;
    wrap.hidden = false;
    document.getElementById("consent-agent").textContent = pending.name || pending.agent_id || "an agent";
    document.getElementById("consent-purpose").textContent = pending.purpose || "";
    document.getElementById("consent-countdown").textContent = formatCountdown(pending.expires_at);
    wrap.dataset.requestId = pending.request_id;
    updateConsentBar();
  } else {
    consentExpiresAt = consentRequestedAt = null;
    wrap.hidden = true;
    delete wrap.dataset.requestId;
  }

  const recentEl = document.getElementById("consent-recent-list");
  const recent = consent.recent || [];
  recentEl.innerHTML = recent.length
    ? recent.slice(0, 5).map(r => `<li>${escapeHtml(r.agent_id)} <span class="decision decision-${escapeHtml(r.decision)}">${escapeHtml(r.decision)}</span> <span class="meta">${relSpan(r.at)}</span></li>`).join("")
    : `<li class="empty">none yet</li>`;
}

async function decideConsent(decision) {
  const wrap = document.getElementById("consent-wrap");
  const requestId = wrap.dataset.requestId;
  if (!requestId) return;
  try {
    await apiFetch(`/v1/admin/consent/${encodeURIComponent(requestId)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision }),
    });
    toast(decision === "allow" ? "Allowed" : "Denied");
    refreshState();
  } catch (_) { /* already toasted */ }
}

document.getElementById("btn-consent-allow").addEventListener("click", () => decideConsent("allow"));
document.getElementById("btn-consent-deny").addEventListener("click", () => decideConsent("deny"));
/* ---------- agents table ---------- */
function renderAgents(state) {
  const body = document.getElementById("agents-body");
  const agents = state.agents || [];
  const windows = (state.consent && state.consent.windows) || [];
  body.innerHTML = agents.map(a => {
    const win = windows.find(w => w.agent_id === a.agent_id);
    const badges = [
      a.holding ? '<span class="badge badge-holding">holding</span>' : "",
      win ? `<span class="badge badge-consent">consent ${formatCountdown(win.consent_until)}</span>` : "",
      a.revoked ? '<span class="badge badge-revoked">revoked</span>' : "",
    ].join(" ");
    const pending = confirmingRevoke && confirmingRevoke.id === a.agent_id;
    const action = a.revoked ? "" : `<button class="btn btn-small ${pending ? "btn-confirm" : "btn-outline"}" data-revoke="${escapeHtml(a.agent_id)}">${pending ? "Confirm?" : "Revoke"}</button>`;
    return `<tr>
      <td>${escapeHtml(a.name)}</td>
      <td class="code">${escapeHtml(a.agent_id)}</td>
      <td class="wrap">${escapeHtml(a.purpose || "")}</td>
      <td>${relSpan(a.last_seen)}</td>
      <td>${a.action_count ?? 0}</td>
      <td>${badges}</td>
      <td>${action}</td>
    </tr>`;
  }).join("");
}

// Inline two-step confirm for Revoke — never window.confirm (blocks automation). State
// lives in `confirmingRevoke`, not the DOM, so it survives the next renderAgents() rebuild.
document.getElementById("agents-body").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-revoke]");
  if (!btn) return;
  const id = btn.dataset.revoke;
  if (confirmingRevoke && confirmingRevoke.id === id) {
    clearTimeout(confirmingRevoke.timer);
    confirmingRevoke = null;
    doRevoke(id);
    return;
  }
  if (confirmingRevoke) clearTimeout(confirmingRevoke.timer);
  const timer = setTimeout(() => { confirmingRevoke = null; if (lastState) renderAgents(lastState); }, 4000);
  confirmingRevoke = { id, timer };
  if (lastState) renderAgents(lastState);
});

async function doRevoke(id) {
  try {
    await apiFetch(`/v1/agents/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast(`Revoked ${id}`);
    refreshState();
  } catch (_) { /* already toasted */ }
}
/* ---------- stats strip ---------- */
function renderStats(state) {
  const s = state.stats || {};
  document.getElementById("stat-total").textContent = s.actions_total ?? "–";
  const failedEl = document.getElementById("stat-failed");
  failedEl.textContent = s.actions_failed ?? "–";
  failedEl.classList.toggle("nonzero", !!s.actions_failed);
  document.getElementById("stat-5m").textContent = s.actions_last_5m ?? "–";
}
/* ---------- screen selector + screenshot meta ---------- */
function renderScreenSelect(display) {
  const sel = document.getElementById("screen-select");
  const screens = (display && display.screens) || [];
  if (currentScreen == null) currentScreen = (display && display.default_screen) ?? (screens[0] ? screens[0].index : 0);
  const key = screens.map(s => s.index).join(",");
  if (sel.dataset.key !== key) {
    sel.innerHTML = screens.map(s => `<option value="${s.index}">screen ${s.index}${s.primary ? " (primary)" : ""} — ${s.width}×${s.height}</option>`).join("");
    sel.dataset.key = key;
    sel.value = String(currentScreen);
  }
  const shot = display && display.screenshot;
  document.getElementById("screenshot-meta").textContent = shot ? `${shot.width}×${shot.height} @ ${shot.scale}x` : "";
}

document.getElementById("screen-select").addEventListener("change", (e) => {
  currentScreen = Number(e.target.value);
  refreshScreenshot();
});
/* ---------- live screenshot ---------- */
async function refreshScreenshot() {
  const screen = currentScreen == null ? 0 : currentScreen;
  try {
    const url = await apiBlobUrl(`/v1/screenshot.png?screen=${screen}&scale=0.5&_=${Date.now()}`);
    const img = document.getElementById("screenshot");
    const old = shotObjectUrl;
    img.src = url;
    shotObjectUrl = url;
    if (old) URL.revokeObjectURL(old);
  } catch (_) { /* already toasted */ }
}
/* ---------- audit tail ---------- */
async function refreshAudit() {
  const filter = document.getElementById("audit-filter").value.trim();
  const qs = new URLSearchParams({ limit: "50" });
  if (filter) qs.set("agent_id", filter);
  try {
    await renderAudit(await apiJson(`/v1/audit?${qs.toString()}`), filter);
  } catch (_) { /* already toasted */ }
}

async function renderAudit(rows, filter) {
  const needle = filter.toLowerCase();
  const filtered = filter
    ? rows.filter(r => (r.agent_id || "").toLowerCase().includes(needle) || (r.agent_name || "").toLowerCase().includes(needle))
    : rows;

  auditObjectUrls.forEach(u => URL.revokeObjectURL(u));
  auditObjectUrls = [];

  const body = document.getElementById("audit-body");
  body.innerHTML = filtered.map((r) => `
    <tr>
      <td>${fmtTime(r.ts)}</td>
      <td>${escapeHtml(r.agent_name || r.agent_id || "")}</td>
      <td class="code">${escapeHtml(r.action)}</td>
      <td class="code wrap">${escapeHtml(formatAction(r))}</td>
      <td>${r.screenshot_url ? `<img class="thumb" data-shot="${escapeHtml(r.screenshot_url)}">` : ""}</td>
      <td class="${r.ok ? "ok-yes" : "ok-no"}">${r.ok ? "ok" : escapeHtml(r.error || "error")}</td>
      <td>${r.duration_ms ?? ""}</td>
    </tr>`).join("");

  for (const img of body.querySelectorAll("img[data-shot]")) {
    const path = img.dataset.shot;
    try {
      const url = await apiBlobUrl(path);
      img.src = url;
      auditObjectUrls.push(url);
      img.addEventListener("click", async () => {
        try { window.open(await apiBlobUrl(path), "_blank"); } catch (_) { /* already toasted */ }
      });
    } catch (_) { /* skip broken thumb */ }
  }
}

let auditDebounce;
document.getElementById("audit-filter").addEventListener("input", () => {
  clearTimeout(auditDebounce);
  auditDebounce = setTimeout(refreshAudit, 300);
});
/* ---------- state poll + admin buttons ---------- */
async function refreshState() {
  try {
    const state = await apiJson("/v1/state");
    lastState = state;
    renderHeader(state);
    renderMode(state);
    renderHuman(state);
    renderConsent(state);
    renderDesk(state);
    renderAgents(state);
    renderStats(state);
    renderScreenSelect(state.display);
  } catch (_) { /* already toasted */ }
}

document.getElementById("btn-pause").addEventListener("click", async () => {
  try {
    await apiFetch("/v1/admin/pause", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: "dashboard" }),
    });
    toast("Paused");
    refreshState();
  } catch (_) { /* already toasted */ }
});

document.getElementById("btn-resume").addEventListener("click", async () => {
  try {
    await apiFetch("/v1/admin/resume", { method: "POST" });
    toast("Resumed");
    refreshState();
  } catch (_) { /* already toasted */ }
});

document.getElementById("btn-force-release").addEventListener("click", async () => {
  try {
    await apiFetch("/v1/lease?force=true", { method: "DELETE" });
    toast("Dibs revoked");
    refreshState();
  } catch (_) { /* already toasted */ }
});
/* ---------- boot ---------- */
// The page's own load already picked up the loopback-exemption cookie the server sets on
// GET / — this script tag runs after that, so it's safe to fetch state right away.
refreshState();
refreshAudit();
refreshScreenshot();
setInterval(refreshState, STATE_POLL_MS);
setInterval(refreshAudit, AUDIT_POLL_MS);
setInterval(refreshScreenshot, SHOT_POLL_MS);
setInterval(tickCountdown, 1000);
