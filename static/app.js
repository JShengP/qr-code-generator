// Front-end glue: vanilla fetch + DOM, no build step.
//
// Two flows:
//   1. CREATE — submit a URL, get back token + edit_token + QR image.
//   2. EDIT   — same QR, change destination. PATCHes /api/qr/{token}
//              using the edit_token captured at creation time. The
//              printed QR keeps working unchanged because it encodes
//              `/r/{token}`, which we never re-issue here.

const $ = (id) => document.getElementById(id);

const createForm = $("create-form");
const editForm = $("edit-form");
const errorBox = $("error");
const resultPanel = $("result");
const editStatus = $("edit-status");
const editError = $("edit-error");

// In-memory state for the currently-displayed QR. Populated by the
// create response; consumed by the edit form. Holding this in memory
// (not localStorage) means refresh = forget, which matches the
// "edit_token is shown once" contract — if the user lost the tab,
// they need the saved edit_token to come back via the API directly.
let currentToken = null;
let currentEditToken = null;

// Whether the user has taken the action to save the edit_token via the
// Copy button. Used to decide whether to nudge them with a confirm()
// before they wipe the edit context by clicking "Start over." If they
// already copied it, we trust they have it and skip the prompt.
let editTokenCopied = false;

createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideError();

  const url = $("url-input").value.trim();
  if (!url) return;

  try {
    const resp = await fetch("/api/qr/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });

    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ detail: resp.statusText }));
      showError(formatError(body, resp.status));
      return;
    }

    const data = await resp.json();
    renderResult(data);
  } catch (err) {
    showError(`Network error: ${err.message}`);
  }
});

editForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideEditFeedback();

  const newUrl = $("new-url-input").value.trim();
  if (!newUrl) return;
  if (!currentToken || !currentEditToken) {
    showEditError("Lost edit context — please create a new QR.");
    return;
  }

  try {
    const resp = await fetch(`/api/qr/${currentToken}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${currentEditToken}`,
      },
      body: JSON.stringify({ url: newUrl }),
    });

    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ detail: resp.statusText }));
      showEditError(formatError(body, resp.status));
      return;
    }

    const data = await resp.json();
    // Update the displayed destination. The QR image (which encodes
    // /r/{token}) stays unchanged — that's the whole point of dynamic
    // QR codes.
    $("original-url").value = data.original_url;
    $("new-url-input").value = "";
    showEditOK(`Destination updated → ${data.original_url}`);
  } catch (err) {
    showEditError(`Network error: ${err.message}`);
  }
});

$("reset").addEventListener("click", () => {
  // The edit_token only exists in memory and the only place it's
  // *visible* on screen is in the readonly input. If the user hasn't
  // copied it yet, "Start over" is destructive — they lose the
  // ability to edit this QR forever. Nudge them, but only once.
  if (!editTokenCopied) {
    const proceed = confirm(
      "You haven't copied the edit token yet. Without it, this QR " +
      "can't be edited later. Start over anyway?"
    );
    if (!proceed) return;
  }
  resultPanel.hidden = true;
  createForm.hidden = false;
  $("url-input").value = "";
  $("url-input").focus();
  hideEditFeedback();
  currentToken = null;
  currentEditToken = null;
  editTokenCopied = false;
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
  // Track whether the edit_token specifically was copied — that's the
  // signal we use to skip the "lose edit context" warning on reset.
  if (btn.dataset.target === "edit-token") {
    editTokenCopied = true;
  }
});

function renderResult(data) {
  $("qr-image").src = data.qr_code_url;
  $("short-url").value = data.short_url;
  $("original-url").value = data.original_url;
  $("token").value = data.token;
  // edit_token is shown ONCE here; the API will never return it again.
  $("edit-token").value = data.edit_token;
  currentToken = data.token;
  currentEditToken = data.edit_token;
  editTokenCopied = false;  // fresh QR; user hasn't copied yet
  createForm.hidden = true;
  resultPanel.hidden = false;
  hideEditFeedback();
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

// Pydantic 422s come back as { detail: [{loc, msg, ...}, ...] };
// our own 422s use { detail: "string" }. Normalize for display.
function formatError(body, status) {
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    return body.detail.map((d) => d.msg).join("; ");
  }
  return `Request failed (HTTP ${status})`;
}
