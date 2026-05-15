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
  currentToken = data.token;
  // We intentionally ignore data.edit_token here — the UI doesn't
  // expose it. The API still returns it for programmatic clients.
  createForm.hidden = true;
  resultPanel.hidden = false;
  hideEditFeedback();
  // Kick off the secondary fetches (analytics + audit). Fresh QR
  // means 0 scans + 1 "create" audit row, but rendering them keeps
  // the layout consistent across fresh-create and sidebar-open paths.
  refreshAnalytics(data.token);
  refreshAuditTimeline(data.token);
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
  chart.innerHTML = '<li class="empty">Loading…</li>';
  $("analytics-total").textContent = "—";
  try {
    const r = await fetch(`/api/qr/${token}/analytics`, {
      credentials: "same-origin",
    });
    if (!r.ok) {
      chart.innerHTML = '<li class="empty">Analytics unavailable.</li>';
      return;
    }
    const data = await r.json();
    $("analytics-total").textContent = data.total_scans;

    if (data.scans_by_day.length === 0) {
      chart.innerHTML = '<li class="empty">No scans yet.</li>';
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
  try {
    const r = await fetch("/api/qr/mine", { credentials: "same-origin" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    sidebar.hidden = false;
    list.innerHTML = "";
    if (data.items.length === 0) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    for (const item of data.items) {
      const li = document.createElement("li");

      // Click-target column: token + destination. Held inside its
      // own <div> so the × button can sit next to it without being
      // part of the click area that opens the QR.
      const main = document.createElement("div");
      main.className = "qr-row";
      const tokenSpan = document.createElement("span");
      tokenSpan.className = "token";
      tokenSpan.textContent = item.token;
      const destSpan = document.createElement("span");
      destSpan.className = "destination";
      destSpan.textContent = item.original_url;
      main.appendChild(tokenSpan);
      main.appendChild(destSpan);
      main.addEventListener("click", () => openOwnedQR(item));

      // Per-row × delete button. Hidden by default, revealed on
      // row hover via CSS; stops propagation so clicking it doesn't
      // also trigger the row's openOwnedQR handler.
      const delBtn = document.createElement("button");
      delBtn.className = "qr-row-delete";
      delBtn.type = "button";
      delBtn.title = `Delete ${item.token}`;
      delBtn.setAttribute("aria-label", `Delete ${item.token}`);
      delBtn.textContent = "×";
      delBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        _softDeleteQR(item.token);
      });

      li.appendChild(main);
      li.appendChild(delBtn);
      list.appendChild(li);
    }
  } catch {
    sidebar.hidden = true;
  }
}

function openOwnedQR(item) {
  // Render the result panel against an existing owned QR.
  // Auth for subsequent PATCH/DELETE flows via the session cookie.
  $("qr-image").src = `/api/qr/${item.token}/image`;
  $("short-url").value = item.short_url;
  $("original-url").value = item.original_url;
  $("token").value = item.token;
  $("current-expires").value = _formatExpires(item.expires_at);
  currentToken = item.token;
  $("create-form").hidden = true;
  $("result").hidden = false;
  hideEditFeedback();
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
