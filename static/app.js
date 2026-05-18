// Front-end glue: vanilla fetch + DOM, no build step.
//
// Flows:
//   1. CREATE — submit a URL, get back token + edit_token + QR image.
//   2. EDIT   — same QR, change destination. PATCHes /api/qr/{token}
//              using the edit_token captured at creation time.
//   3. AUTH   — Sign in via magic link. /api/auth/me on page load
//              decides whether the top-right shows "Sign in" or
//              "Signed in as X / Sign out". The auth state is purely
//              cosmetic for now — it shows the QR ownership story is
//              coming, but doesn't gate any current functionality.

const $ = (id) => document.getElementById(id);

const createForm = $("create-form");
const editForm = $("edit-form");
const errorBox = $("error");
const resultPanel = $("result");
const editStatus = $("edit-status");
const editError = $("edit-error");

// In-memory state for the currently-displayed QR. The edit form
// authenticates via the browser's session cookie (owner shortcut),
// so we only track the public 7-char token.
let currentToken = null;
// Whether the currently-open QR is soft-deleted. Drives the
// Restore button visibility and the Delete button's disabled state.
let currentIsDeleted = false;
// Cached current redirect_status (302 or 301). Controls the
// promote-to-301 button's disabled state so we don't have to
// re-read the DOM.
let currentRedirectStatus = 302;
// Selection state for bulk delete — Set of tokens. Persists across
// re-renders of the sidebar so a search/sort doesn't lose your picks.
const bulkSelection = new Set();

createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideError();

  const url = $("url-input").value.trim();
  if (!url) return;

  // <input type="datetime-local"> yields a string like
  // "2099-12-31T23:59" — Pydantic parses that as a naive datetime,
  // which `_to_naive_utc` then leaves alone. Empty input => skip the
  // field so the API treats it as "no expiry".
  const expiresRaw = $("create-expires-input").value.trim();
  const body = { url };
  if (expiresRaw) body.expires_at = expiresRaw;

  try {
    const resp = await fetch("/api/qr/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ detail: resp.statusText }));
      showError(formatError(body, resp.status));
      return;
    }

    const data = await resp.json();
    renderResult(data);
    // Refresh the sidebar so a newly-owned create appears immediately.
    refreshMyQRs();
  } catch (err) {
    showError(`Network error: ${err.message}`);
  }
});

editForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideEditFeedback();

  const newUrl = $("new-url-input").value.trim();
  const newExpires = $("edit-expires-input").value.trim();

  // At least one of the two fields must be filled.
  if (!newUrl && !newExpires) {
    showEditError("Enter a new URL, a new expiry, or both.");
    return;
  }
  if (!currentToken) {
    showEditError("Lost edit context — please create a new QR.");
    return;
  }

  const patch = {};
  if (newUrl) patch.url = newUrl;
  if (newExpires) patch.expires_at = newExpires;

  // Auth is the session cookie (owner shortcut). credentials:
  // "same-origin" makes fetch send the cookie along.
  try {
    const resp = await fetch(`/api/qr/${currentToken}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(patch),
    });

    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ detail: resp.statusText }));
      showEditError(formatError(body, resp.status));
      return;
    }

    const data = await resp.json();
    // Update both displayed fields. The QR image (which encodes
    // /r/{token}) stays unchanged — that's the whole point of dynamic
    // QR codes.
    $("original-url").value = data.original_url;
    $("current-expires").value = _formatExpires(data.expires_at);
    $("new-url-input").value = "";
    $("edit-expires-input").value = "";

    const parts = [];
    if (newUrl) parts.push(`destination → ${data.original_url}`);
    if (newExpires) parts.push(`expires_at → ${data.expires_at ?? "(none)"}`);
    showEditOK(`Updated: ${parts.join(", ")}`);

    // Re-fetch the sidebar so the listed entry shows the new
    // destination. Otherwise the next click on this token in the
    // sidebar reloads the stale `item.original_url` into the result
    // panel and the user thinks the change reverted.
    refreshMyQRs();
    // History gained a new entry (patch_url and/or patch_expires);
    // analytics is unchanged but we refresh anyway to keep both
    // panels in sync with the server's view.
    refreshAuditTimeline(currentToken);
  } catch (err) {
    showEditError(`Network error: ${err.message}`);
  }
});

// Two entry points share this helper:
//   - the sidebar × button on each My-QRs row (item.token known)
//   - the big "Delete this QR" button in the result-panel footer
//     (acts on currentToken, the open mapping)
async function _softDeleteQR(token) {
  const ok = confirm(
    `Delete ${token}? Subsequent scans will return HTTP 410. The ` +
    "row stays in the database and the action is recorded in audit_logs " +
    "(soft-delete, not erased)."
  );
  if (!ok) return;

  try {
    const resp = await fetch(`/api/qr/${token}`, {
      method: "DELETE",
      credentials: "same-origin",
    });
    if (!resp.ok) {
      alert(`Delete failed: HTTP ${resp.status}`);
      return;
    }
    // If we just deleted the QR open in the result panel, reset
    // the view — otherwise the UI lies about what's editable.
    if (currentToken === token) {
      resetCreateView();
    }
    refreshMyQRs();
  } catch (err) {
    alert(`Network error: ${err.message}`);
  }
}

// Result-panel "Delete this QR" button. Same flow as sidebar ×,
// acting on the QR currently open in the result panel.
$("delete-qr").addEventListener("click", () => {
  if (!currentToken) return;
  _softDeleteQR(currentToken);
});

$("restore-qr").addEventListener("click", async () => {
  if (!currentToken) return;
  try {
    const r = await fetch(`/api/qr/${currentToken}/restore`, {
      method: "POST",
      credentials: "same-origin",
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({ detail: r.statusText }));
      alert(`Restore failed: ${formatError(body, r.status)}`);
      return;
    }
    const data = await r.json();
    _applyDeletedState(false);
    showEditOK(`Restored ${data.token}. Redirect resumes from the next scan.`);
    refreshMyQRs();
    refreshAuditTimeline(currentToken);
  } catch (err) {
    alert(`Network error: ${err.message}`);
  }
});

$("promote-301-btn").addEventListener("click", async () => {
  if (!currentToken || currentRedirectStatus === 301) return;
  const ok = confirm(
    "Promote to a permanent 301 redirect?\n\n" +
    "After promotion:\n" +
    "  • Browsers cache the redirect for up to 5 minutes\n" +
    "    (we send Cache-Control: max-age=300).\n" +
    "  • Destination changes will take up to 5 minutes to propagate\n" +
    "    to scanners whose browser is still caching the old value.\n" +
    "  • Analytics will under-count by the same window: cached scans\n" +
    "    skip our server entirely.\n" +
    "  • This is a ONE-WAY operation. The API rejects demoting back\n" +
    "    to 302 — the right move if you regret it is to delete this\n" +
    "    QR and create a fresh one.\n\n" +
    "Only promote QRs whose destination really won't change.\n\n" +
    "Continue?"
  );
  if (!ok) return;
  try {
    const r = await fetch(`/api/qr/${currentToken}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ redirect_status: 301 }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({ detail: r.statusText }));
      showEditError(formatError(body, r.status));
      return;
    }
    const data = await r.json();
    _applyRedirectStatus(data.redirect_status);
    showEditOK("Promoted to 301. Subsequent edits may not propagate.");
    refreshAuditTimeline(currentToken);
  } catch (err) {
    showEditError(`Network error: ${err.message}`);
  }
});

// Analytics date-range filter — change either input to re-fetch.
$("analytics-from").addEventListener("change", () => {
  if (currentToken) refreshAnalytics(currentToken);
});
$("analytics-to").addEventListener("change", () => {
  if (currentToken) refreshAnalytics(currentToken);
});
$("analytics-reset").addEventListener("click", () => {
  $("analytics-from").value = "";
  $("analytics-to").value = "";
  $("analytics-from").max = "";
  $("analytics-to").min = "";
  if (currentToken) refreshAnalytics(currentToken);
});

$("reset").addEventListener("click", () => {
  // No "did you save edit_token?" prompt needed — the UI doesn't
  // expose it. The QR remains editable by this user via the
  // session cookie and the My-QRs sidebar.
  resetCreateView();
  $("url-input").focus();
});

// Copy buttons (delegated).
document.addEventListener("click", async (event) => {
  const btn = event.target.closest("button.copy");
  if (!btn) return;
  const target = $(btn.dataset.target);
  if (!target) return;
  try {
    await navigator.clipboard.writeText(target.value);
    btn.classList.add("copied");
    btn.textContent = "Copied";
    setTimeout(() => {
      btn.classList.remove("copied");
      btn.textContent = "Copy";
    }, 1200);
  } catch {
    target.select();
    document.execCommand("copy"); // legacy fallback
  }
});

// Server stores naive UTC. The trailing "Z" makes Date parse as
// UTC; toLocaleString then renders in the browser's locale + TZ.
// Returns "(never)" for null / empty so the UI never shows a bare
// empty input that looks like a render bug.
function _formatExpires(isoString) {
  if (!isoString) return "(never)";
  return new Date(isoString + "Z").toLocaleString();
}

function renderResult(data) {
  $("qr-image").src = data.qr_code_url;
  $("short-url").value = data.short_url;
  $("original-url").value = data.original_url;
  $("token").value = data.token;
  $("current-expires").value = _formatExpires(data.expires_at);
  _applyRedirectStatus(data.redirect_status ?? 302);
  _applyDeletedState(false);
  _setDownloadLink(data.token);
  currentToken = data.token;
  // We intentionally ignore data.edit_token here — the UI doesn't
  // expose it. The API still returns it for programmatic clients.
  createForm.hidden = true;
  resultPanel.hidden = false;
  $("qr-stats").hidden = false;
  hideEditFeedback();
  _resetAnalyticsRange();
  // Kick off the secondary fetches (analytics + audit). Fresh QR
  // means 0 scans + 1 "create" audit row, but rendering them keeps
  // the layout consistent across fresh-create and sidebar-open paths.
  refreshAnalytics(data.token);
  refreshAuditTimeline(data.token);
}

function _applyRedirectStatus(status) {
  currentRedirectStatus = status;
  const label = status === 301 ? "301 (permanent — cached)" : "302 (temporary)";
  $("current-redirect-status").value = label;
  // The collapsed <details> summary mirrors the current state so the
  // user can read it without expanding — they only need to expand
  // when they actually want to promote.
  $("redirect-status-summary-value").textContent = label;
  // Always re-collapse the section when (re-)rendering a QR. Otherwise
  // opening a 302-state QR after just-promoting a different one would
  // inherit the previous open-state and surface the dangerous button.
  $("redirect-status-details").open = false;
  // Once promoted, the button can't take it back. Disable + relabel so
  // the user understands the state without having to re-read the hint.
  const btn = $("promote-301-btn");
  if (status === 301) {
    btn.disabled = true;
    btn.textContent = "Already 301";
    btn.title = "This link is already 301. Promotion is one-way.";
  } else {
    btn.disabled = false;
    btn.textContent = "Promote to 301";
    btn.title =
      "Promote to a permanent 301 redirect. Browsers will cache the result aggressively; subsequent edits may not propagate. ONE-WAY operation.";
  }
}

function _applyDeletedState(isDeleted) {
  currentIsDeleted = isDeleted;
  // Restore is only meaningful for deleted rows; Delete is hidden for
  // them so the user doesn't click "delete an already-deleted thing".
  $("restore-qr").hidden = !isDeleted;
  $("delete-qr").hidden = isDeleted;
  // Editing a deleted row would 404 because PATCH goes through
  // _get_mapping_or_404. Block the form so we surface that up-front.
  $("edit-form").querySelectorAll("input, button").forEach((el) => {
    el.disabled = isDeleted;
  });
}

function _setDownloadLink(token) {
  const link = $("qr-download-link");
  link.href = `/api/qr/${token}/image?download=1`;
  link.setAttribute("download", `qr-${token}.png`);
}

function _resetAnalyticsRange() {
  $("analytics-from").value = "";
  $("analytics-to").value = "";
  // Clear the cross-constraints too so opening a new QR doesn't
  // inherit the previous QR's date bounds.
  $("analytics-from").max = "";
  $("analytics-to").min = "";
  $("analytics-summary-suffix").textContent = "total scans";
}

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.hidden = false;
}

function hideError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

function showEditOK(msg) {
  editStatus.textContent = msg;
  editStatus.hidden = false;
  editError.hidden = true;
}

function showEditError(msg) {
  editError.textContent = msg;
  editError.hidden = false;
  editStatus.hidden = true;
}

function hideEditFeedback() {
  editStatus.hidden = true;
  editError.hidden = true;
}

// ---------------------------------------------------------------------
// History timeline + Analytics chart (lazy-loaded into the result panel).
// ---------------------------------------------------------------------

const _FRIENDLY_ACTION = {
  create: "Created",
  patch_url: "Changed destination",
  patch_expires: "Updated expiration",
  delete: "Deleted",
  rotate_edit_token: "Rotated edit token",
  promote_to_301: "Promoted to 301",
  restore: "Restored",
};

async function refreshAuditTimeline(token) {
  const list = $("audit-timeline");
  list.innerHTML = '<li class="empty">Loading history…</li>';
  try {
    const r = await fetch(`/api/qr/${token}/audit`, {
      credentials: "same-origin",
    });
    if (!r.ok) {
      // 403 (not owner) or 401 (no session) — show nothing
      // intentionally; we don't want to leak the trail.
      list.innerHTML = '<li class="empty">History unavailable.</li>';
      return;
    }
    const items = (await r.json()).items;
    if (items.length === 0) {
      list.innerHTML = '<li class="empty">No history yet.</li>';
      return;
    }
    list.innerHTML = "";
    for (const e of items) {
      // Server stores naive UTC; the trailing "Z" makes Date parse
      // it as UTC, then toLocaleString renders in the browser's TZ.
      const t = new Date(e.created_at + "Z").toLocaleString();
      const label = _FRIENDLY_ACTION[e.action] || e.action;

      const li = document.createElement("li");

      const time = document.createElement("time");
      time.textContent = t;
      const action = document.createElement("span");
      action.className = "action";
      action.textContent = label;

      li.appendChild(time);
      li.appendChild(action);

      if (e.before_value || e.after_value) {
        const diff = document.createElement("div");
        diff.className = "diff";
        if (e.before_value) {
          const before = document.createElement("span");
          before.className = "before";
          before.textContent = e.before_value;
          diff.appendChild(before);
          const arrow = document.createElement("span");
          arrow.className = "arrow";
          arrow.textContent = "→";
          diff.appendChild(arrow);
        }
        if (e.after_value) {
          const after = document.createElement("span");
          after.className = "after";
          after.textContent = e.after_value;
          diff.appendChild(after);
        }
        li.appendChild(diff);
      }
      list.appendChild(li);
    }
  } catch {
    list.innerHTML = '<li class="empty">Failed to load history.</li>';
  }
}

async function refreshAnalytics(token) {
  const chart = $("analytics-chart");
  const from = $("analytics-from").value.trim();
  const to = $("analytics-to").value.trim();

  // Cross-constrain the two pickers so the calendar UI greys out
  // invalid choices (e.g. picking a `from` later than the current
  // `to`). Strict typing/paste can still slip past this — the
  // explicit check below catches that case.
  $("analytics-from").max = to || "";
  $("analytics-to").min = from || "";

  // Validate BEFORE updating the suffix or firing the fetch. A user
  // who flips from/to should see a clear inline message, not a
  // generic "Analytics unavailable." from the 422.
  if (from && to && from > to) {
    chart.innerHTML =
      '<li class="empty">Date range invalid: <strong>From</strong> ' +
      "must be on or before <strong>To</strong>.</li>";
    $("analytics-total").textContent = "—";
    $("analytics-summary-suffix").textContent = "—";
    return;
  }

  chart.innerHTML = '<li class="empty">Loading…</li>';
  $("analytics-total").textContent = "—";
  const qs = new URLSearchParams();
  if (from) qs.set("from", from);
  if (to) qs.set("to", to);
  const url = `/api/qr/${token}/analytics${qs.toString() ? "?" + qs : ""}`;
  // Suffix mirrors the active filter so "0 total scans" doesn't look
  // like a bug when the user has narrowed to an empty window.
  $("analytics-summary-suffix").textContent =
    from || to
      ? `scans in ${from || "earliest"} → ${to || "latest"}`
      : "total scans";
  try {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) {
      // Surface the server's detail string if any — that's more useful
      // than a generic "Analytics unavailable.". 422s on this endpoint
      // are validation messages worth showing verbatim.
      const body = await r.json().catch(() => ({ detail: r.statusText }));
      const msg = formatError(body, r.status);
      // textContent (not innerHTML) so a future server message can't
      // turn into a markup injection. The static "Analytics
      // unavailable: " prefix keeps the surface readable.
      chart.innerHTML = "";
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = `Analytics unavailable: ${msg}`;
      chart.appendChild(li);
      return;
    }
    const data = await r.json();
    $("analytics-total").textContent = data.total_scans;

    if (data.scans_by_day.length === 0) {
      chart.innerHTML = '<li class="empty">No scans in this range.</li>';
      return;
    }
    const max = Math.max(...data.scans_by_day.map((d) => d.count));
    chart.innerHTML = "";
    for (const d of data.scans_by_day) {
      const li = document.createElement("li");
      const date = document.createElement("span");
      date.className = "date";
      date.textContent = d.date;
      const bar = document.createElement("span");
      bar.className = "bar";
      bar.style.width = `${(d.count / max) * 100}%`;
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = d.count;
      li.appendChild(date);
      li.appendChild(bar);
      li.appendChild(count);
      chart.appendChild(li);
    }
  } catch {
    chart.innerHTML = '<li class="empty">Failed to load analytics.</li>';
  }
}

// Pydantic 422s come back as { detail: [{loc, msg, ...}, ...] };
// our own 422s use { detail: "string" }. Normalize for display.
function formatError(body, status) {
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    return body.detail.map((d) => d.msg).join("; ");
  }
  return `Request failed (HTTP ${status})`;
}

// ---------------------------------------------------------------------
// Auth flow: magic link via email.
// ---------------------------------------------------------------------

const authAnon = $("auth-anon");
const authSignedIn = $("auth-signed-in");
const loginModal = $("login-modal");
const loginForm = $("login-form");
const loginStatus = $("login-status");
const loginError = $("login-error");

// On page load, ask the server who we are. Updates the auth bar.
refreshAuthState();

// Also detect whether GitHub OAuth is configured server-side. The
// route is conditionally registered in app/main.py based on env, so
// a HEAD probe is enough: 200/302 means available, 404 means hide.
detectGitHubLogin();

async function detectGitHubLogin() {
  // /api/auth/github/available is only registered when the OAuth
  // routes are. 200 = configured, 404 = not. Cheap probe with no
  // side effects (the /login endpoint, by contrast, would set a
  // state cookie even if we don't end up using it).
  try {
    const r = await fetch("/api/auth/github/available");
    if (r.ok) {
      $("github-login-section").hidden = false;
    }
  } catch {
    // Network error — leave the section hidden, the user can still
    // use the email path.
  }
}

async function refreshAuthState() {
  try {
    const r = await fetch("/api/auth/me", { credentials: "same-origin" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    if (data.user) {
      authAnon.hidden = true;
      authSignedIn.hidden = false;
      $("auth-email").textContent = data.user.email;
      // Reveal the Create form; hide the anon prompt.
      $("create-form").hidden = false;
      $("signin-prompt").hidden = true;
      await refreshMyQRs();
    } else {
      authAnon.hidden = false;
      authSignedIn.hidden = true;
      $("my-qrs").hidden = true;
      $("qr-stats").hidden = true;
      // API now requires auth to create — show the prompt instead of
      // a Create form that would 401 the moment a user clicked it.
      $("create-form").hidden = true;
      $("signin-prompt").hidden = false;
    }
  } catch {
    // If /me fails (network down etc.), show the anon UI as a safe
    // default — better to invite sign-in than to hide it.
    authAnon.hidden = false;
    authSignedIn.hidden = true;
    $("my-qrs").hidden = true;
    $("create-form").hidden = true;
    $("signin-prompt").hidden = false;
  }
}

async function refreshMyQRs() {
  const sidebar = $("my-qrs");
  const list = $("my-qrs-list");
  const empty = $("my-qrs-empty");
  // Build the query from the current control state. Any of the three
  // can be empty/default; we only include non-defaults in the URL so
  // the network tab stays readable.
  const search = $("my-qrs-search").value.trim();
  const sort = $("my-qrs-sort").value;
  const includeDeleted = $("my-qrs-include-deleted").checked;
  const qs = new URLSearchParams();
  if (search) qs.set("search", search);
  if (sort && sort !== "created_desc") qs.set("sort", sort);
  if (includeDeleted) qs.set("include_deleted", "1");
  const url = `/api/qr/mine${qs.toString() ? "?" + qs : ""}`;
  try {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    sidebar.hidden = false;
    list.innerHTML = "";
    // Prune selection of tokens that fell out of the current view
    // (e.g. after a search narrows the list). Otherwise the bulk-bar
    // count includes ghosts the user can't see.
    const visibleTokens = new Set(data.items.map((i) => i.token));
    for (const tok of [...bulkSelection]) {
      if (!visibleTokens.has(tok)) bulkSelection.delete(tok);
    }
    _refreshBulkBar();
    if (data.items.length === 0) {
      // Distinct empty-states: "no QRs at all" vs "filter excluded them all".
      empty.textContent =
        search || includeDeleted || sort !== "created_desc"
          ? "No QRs match the current filter."
          : "You haven't created any QR codes yet.";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    for (const item of data.items) {
      list.appendChild(_renderQrRow(item));
    }
  } catch {
    sidebar.hidden = true;
  }
}

function _renderQrRow(item) {
  const li = document.createElement("li");
  if (item.is_deleted) li.classList.add("deleted");

  // Bulk-select checkbox. Stops propagation so toggling doesn't also
  // fire the row's open handler. Deleted rows don't get a checkbox —
  // bulk-delete on already-deleted rows is a no-op; offering it is
  // just visual noise.
  if (!item.is_deleted) {
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "qr-row-check";
    cb.setAttribute("aria-label", `Select ${item.token} for bulk action`);
    cb.checked = bulkSelection.has(item.token);
    cb.addEventListener("click", (event) => event.stopPropagation());
    cb.addEventListener("change", () => {
      if (cb.checked) bulkSelection.add(item.token);
      else bulkSelection.delete(item.token);
      _refreshBulkBar();
    });
    li.appendChild(cb);
  } else {
    // Spacer so checkbox-less rows still align with checkboxed ones.
    const spacer = document.createElement("span");
    spacer.className = "qr-row-check-spacer";
    li.appendChild(spacer);
  }

  // Click-target column: token + destination.
  const main = document.createElement("div");
  main.className = "qr-row";
  const tokenSpan = document.createElement("span");
  tokenSpan.className = "token";
  tokenSpan.textContent = item.token;
  if (item.redirect_status === 301) {
    const pill = document.createElement("span");
    pill.className = "status-pill status-301";
    pill.textContent = "301";
    pill.title = "Promoted to permanent 301 redirect.";
    tokenSpan.appendChild(document.createTextNode(" "));
    tokenSpan.appendChild(pill);
  }
  if (item.is_deleted) {
    const pill = document.createElement("span");
    pill.className = "status-pill status-deleted";
    pill.textContent = "deleted";
    tokenSpan.appendChild(document.createTextNode(" "));
    tokenSpan.appendChild(pill);
  }
  const destSpan = document.createElement("span");
  destSpan.className = "destination";
  destSpan.textContent = item.original_url;
  main.appendChild(tokenSpan);
  main.appendChild(destSpan);
  main.addEventListener("click", () => openOwnedQR(item));

  // Per-row × delete (live rows) OR restore (deleted rows). Both
  // hidden until row hover via CSS; stop propagation so they don't
  // also trigger the row's open handler.
  const actionBtn = document.createElement("button");
  actionBtn.className = "qr-row-delete";
  actionBtn.type = "button";
  if (item.is_deleted) {
    actionBtn.title = `Restore ${item.token}`;
    actionBtn.setAttribute("aria-label", `Restore ${item.token}`);
    actionBtn.textContent = "↻";
    actionBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      _restoreQR(item.token);
    });
  } else {
    actionBtn.title = `Delete ${item.token}`;
    actionBtn.setAttribute("aria-label", `Delete ${item.token}`);
    actionBtn.textContent = "×";
    actionBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      _softDeleteQR(item.token);
    });
  }

  li.appendChild(main);
  li.appendChild(actionBtn);
  return li;
}

async function _restoreQR(token) {
  try {
    const r = await fetch(`/api/qr/${token}/restore`, {
      method: "POST",
      credentials: "same-origin",
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({ detail: r.statusText }));
      alert(`Restore failed: ${formatError(body, r.status)}`);
      return;
    }
    // If the restored QR is the one open in the result panel, sync
    // its delete-state so the buttons swap back.
    if (currentToken === token) {
      _applyDeletedState(false);
      refreshAuditTimeline(token);
    }
    refreshMyQRs();
  } catch (err) {
    alert(`Network error: ${err.message}`);
  }
}

function _refreshBulkBar() {
  const bar = $("my-qrs-bulk-bar");
  const count = bulkSelection.size;
  $("my-qrs-bulk-count").textContent = count;
  bar.hidden = count === 0;
}

// Sidebar control wiring.
$("my-qrs-search").addEventListener("input", _debounce(refreshMyQRs, 200));
$("my-qrs-sort").addEventListener("change", refreshMyQRs);
$("my-qrs-include-deleted").addEventListener("change", refreshMyQRs);

$("my-qrs-bulk-clear").addEventListener("click", () => {
  bulkSelection.clear();
  refreshMyQRs();
});

$("my-qrs-bulk-delete").addEventListener("click", async () => {
  if (bulkSelection.size === 0) return;
  const tokens = [...bulkSelection];
  const ok = confirm(
    `Delete ${tokens.length} QR${tokens.length === 1 ? "" : "s"}? ` +
    "Each becomes a 410 on next scan; rows stay in the audit log."
  );
  if (!ok) return;
  try {
    const r = await fetch("/api/qr/bulk-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ tokens }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({ detail: r.statusText }));
      alert(`Bulk delete failed: ${formatError(body, r.status)}`);
      return;
    }
    bulkSelection.clear();
    // If the currently-open QR got deleted in the batch, reset the view.
    if (currentToken && tokens.includes(currentToken)) {
      resetCreateView();
    }
    refreshMyQRs();
  } catch (err) {
    alert(`Network error: ${err.message}`);
  }
});

// Tiny debounce so typing in the search box doesn't fire a request
// per keystroke. 200 ms feels instant but absorbs a fast typist.
function _debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function openOwnedQR(item) {
  // Render the result panel against an existing owned QR.
  // Auth for subsequent PATCH/DELETE flows via the session cookie.
  $("qr-image").src = `/api/qr/${item.token}/image`;
  $("short-url").value = item.short_url;
  $("original-url").value = item.original_url;
  $("token").value = item.token;
  $("current-expires").value = _formatExpires(item.expires_at);
  _applyRedirectStatus(item.redirect_status ?? 302);
  _applyDeletedState(Boolean(item.is_deleted));
  _setDownloadLink(item.token);
  currentToken = item.token;
  $("create-form").hidden = true;
  $("result").hidden = false;
  $("qr-stats").hidden = false;
  hideEditFeedback();
  _resetAnalyticsRange();
  refreshAnalytics(item.token);
  refreshAuditTimeline(item.token);
}

$("login-btn").addEventListener("click", openLoginModal);
$("signin-prompt-btn").addEventListener("click", openLoginModal);

function openLoginModal() {
  loginModal.hidden = false;
  $("login-email").focus();
  hideLoginFeedback();
}

$("login-cancel").addEventListener("click", () => {
  loginModal.hidden = true;
  hideLoginFeedback();
});

// Click outside the modal content closes the modal too.
loginModal.addEventListener("click", (event) => {
  if (event.target === loginModal) {
    loginModal.hidden = true;
    hideLoginFeedback();
  }
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideLoginFeedback();

  const email = $("login-email").value.trim();
  if (!email) return;

  try {
    const resp = await fetch("/api/auth/request-link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ detail: resp.statusText }));
      showLoginError(formatError(body, resp.status));
      return;
    }
    // Server response is intentionally vague. In dev the magic link
    // also lands in the uvicorn console, so we tell the user where
    // to look.
    showLoginOK(
      "Check your email for the sign-in link. " +
      "(Dev mode: link is printed to the server console.)"
    );
    $("login-email").value = "";
  } catch (err) {
    showLoginError(`Network error: ${err.message}`);
  }
});

$("logout-btn").addEventListener("click", async () => {
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
    });
  } catch {
    // Best-effort on the client side. Even if the server call fails
    // we still refresh; the cookie path-delete may have worked.
  }
  // Reset the visible UI back to the anonymous landing state.
  // Without this, a result panel from an owned QR keeps rendering
  // after sign-out — its Update button no longer works (no session)
  // but the UI still suggests it does, which is misleading.
  resetCreateView();
  await refreshAuthState();
});

function resetCreateView() {
  resultPanel.hidden = true;
  $("qr-stats").hidden = true;
  // Reveal the create form. This is the right state for "Start over"
  // (caller is definitely signed in -- result panel implies it). For
  // logout the caller chains refreshAuthState() after this, which
  // re-hides the form and shows the signin-prompt instead.
  createForm.hidden = false;
  $("signin-prompt").hidden = true;
  hideEditFeedback();
  currentToken = null;
  $("url-input").value = "";
  $("create-expires-input").value = "";
  $("new-url-input").value = "";
  $("edit-expires-input").value = "";
}

// ---------------------------------------------------------------------
// API-token modal: wraps POST /api/qr/{token}/rotate-edit-token in a
// "click Generate to issue a one-time bearer + show a curl example"
// flow. Surfaces the otherwise-hidden edit_token mechanism to
// programmatic clients without polluting the day-to-day UI.
// ---------------------------------------------------------------------

const apiTokenModal = $("api-token-modal");

$("open-api-token").addEventListener("click", () => {
  if (!currentToken) return;
  $("api-token-target").textContent = currentToken;
  // Reset to the "pre-generate" state every open.
  $("api-token-prompt").hidden = false;
  $("api-token-result").hidden = true;
  $("api-token-error").hidden = true;
  $("api-token-value").value = "";
  $("api-token-curl").textContent = "";
  apiTokenModal.hidden = false;
});

$("api-token-close").addEventListener("click", () => {
  apiTokenModal.hidden = true;
});

// Click outside the modal-content closes (same UX as the sign-in modal).
apiTokenModal.addEventListener("click", (event) => {
  if (event.target === apiTokenModal) apiTokenModal.hidden = true;
});

$("api-token-generate").addEventListener("click", async () => {
  if (!currentToken) return;
  $("api-token-error").hidden = true;
  try {
    const resp = await fetch(
      `/api/qr/${currentToken}/rotate-edit-token`,
      { method: "POST", credentials: "same-origin" },
    );
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ detail: resp.statusText }));
      $("api-token-error").textContent = formatError(body, resp.status);
      $("api-token-error").hidden = false;
      return;
    }
    const data = await resp.json();
    $("api-token-value").value = data.edit_token;
    // Build a copy-pastable curl example using the current origin so
    // it works whether they're on http://127.0.0.1:8001 or a deployed
    // domain. Multi-line + line continuations for readability.
    const origin = window.location.origin;
    $("api-token-curl").textContent =
      `# Change destination\n` +
      `curl -X PATCH ${origin}/api/qr/${currentToken} \\\n` +
      `  -H "Authorization: Bearer ${data.edit_token}" \\\n` +
      `  -H "Content-Type: application/json" \\\n` +
      `  -d '{"url": "https://new-destination.example/"}'\n` +
      `\n` +
      `# Soft-delete\n` +
      `curl -X DELETE ${origin}/api/qr/${currentToken} \\\n` +
      `  -H "Authorization: Bearer ${data.edit_token}"`;
    $("api-token-prompt").hidden = true;
    $("api-token-result").hidden = false;
    // History gained a "rotate_edit_token" entry; refresh so the user
    // can see it appear immediately on close.
    refreshAuditTimeline(currentToken);
  } catch (err) {
    $("api-token-error").textContent = `Network error: ${err.message}`;
    $("api-token-error").hidden = false;
  }
});

function showLoginOK(msg) {
  loginStatus.textContent = msg;
  loginStatus.hidden = false;
  loginError.hidden = true;
}

function showLoginError(msg) {
  loginError.textContent = msg;
  loginError.hidden = false;
  loginStatus.hidden = true;
}

function hideLoginFeedback() {
  loginStatus.hidden = true;
  loginError.hidden = true;
}
