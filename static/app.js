// Front-end glue: submit URL to /api/qr/create, render the QR + short link.
// No build step, no framework — vanilla fetch + DOM.

const $ = (id) => document.getElementById(id);

const form = $("create-form");
const errorBox = $("error");
const resultPanel = $("result");

form.addEventListener("submit", async (event) => {
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

$("reset").addEventListener("click", () => {
  resultPanel.hidden = true;
  form.hidden = false;
  $("url-input").value = "";
  $("url-input").focus();
});

// Copy buttons (delegated, since there's only one for now).
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

function renderResult(data) {
  $("qr-image").src = data.qr_code_url;
  $("short-url").value = data.short_url;
  $("original-url").value = data.original_url;
  $("token").value = data.token;
  // edit_token is shown ONCE here; the API will never return it again.
  $("edit-token").value = data.edit_token;
  form.hidden = true;
  resultPanel.hidden = false;
}

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.hidden = false;
}

function hideError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
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
