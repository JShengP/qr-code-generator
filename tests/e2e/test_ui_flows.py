"""End-to-end UI flows driven by Playwright.

These run a real Chromium against a live uvicorn subprocess; they
catch the rendering / state-management bugs that the in-process
pytest suite can't see (e.g. HTML `hidden` losing to `display: flex`).

Slow path:
  pytest tests/e2e -v
"""


# ---------------------------------------------------------------------
# Anonymous landing state
# ---------------------------------------------------------------------


def test_anonymous_shows_signin_prompt_not_create_form(anonymous_page):
    page, _ = anonymous_page

    # "Sign in to create" CTA visible
    prompt = page.locator("#signin-prompt")
    assert prompt.is_visible()
    assert "Sign in" in prompt.text_content()

    # Create form must be hidden (this is exactly the CSS-vs-`hidden`
    # bug that bit us repeatedly before the global rule landed).
    assert not page.locator("#create-form").is_visible()

    # The My-QRs sidebar shouldn't render when signed out
    assert not page.locator("#my-qrs").is_visible()

    # Auth bar shows the Sign-in button, not the signed-in row
    assert page.locator("#auth-anon").is_visible()
    assert not page.locator("#auth-signed-in").is_visible()


def test_anonymous_clicking_signin_opens_modal(anonymous_page):
    page, _ = anonymous_page

    modal = page.locator("#login-modal")
    assert not modal.is_visible()  # closed initially

    page.locator("#signin-prompt-btn").click()
    assert modal.is_visible()

    page.locator("#login-cancel").click()
    assert not modal.is_visible()


# ---------------------------------------------------------------------
# Signed-in landing state
# ---------------------------------------------------------------------


def test_signed_in_shows_create_form_and_sidebar(signed_in_page):
    page, _ = signed_in_page

    # Create form visible, signin prompt hidden
    assert page.locator("#create-form").is_visible()
    assert not page.locator("#signin-prompt").is_visible()

    # Auth bar reflects the user
    assert page.locator("#auth-signed-in").is_visible()
    assert "e2e@example.com" in page.locator("#auth-email").text_content()

    # Sidebar visible (empty until we create something)
    assert page.locator("#my-qrs").is_visible()


# ---------------------------------------------------------------------
# Create → Edit → Delete cycle
# ---------------------------------------------------------------------


def test_full_create_edit_delete_lifecycle(signed_in_page):
    page, _ = signed_in_page

    # --- Create -----------------------------------------------------
    page.locator("#url-input").fill("https://example.com")
    page.locator("#create-form button[type=submit]").click()

    # Result panel appears
    page.locator("#result").wait_for(state="visible")
    assert not page.locator("#create-form").is_visible()
    assert "example.com" in page.locator("#original-url").input_value()

    # Sidebar picks up the new QR. Use relative count — earlier
    # tests in the same session may have left QRs in the shared DB.
    sidebar_items = page.locator("#my-qrs-list li")
    sidebar_items.first.wait_for(state="visible")
    before_count = sidebar_items.count()
    assert before_count >= 1, "the newly-created QR should be present"

    # --- Edit -------------------------------------------------------
    page.locator("#new-url-input").fill("https://new-target.example")
    page.locator("#edit-form button[type=submit]").click()

    # "Updated" status appears
    status = page.locator("#edit-status")
    status.wait_for(state="visible")
    assert "new-target.example" in status.text_content()

    # Current-destination field reflects the change
    assert "new-target.example" in page.locator("#original-url").input_value()

    # Sidebar's row also updates (this was the stale-sidebar bug we
    # fixed earlier — opening the same token shouldn't revert)
    page.wait_for_timeout(200)  # give refreshMyQRs() time to land
    item_text = sidebar_items.first.text_content()
    assert "new-target.example" in item_text

    # --- Delete via the result-panel button. -----------------------
    # (The sidebar × is the alternative path, covered by a separate
    # test below.)
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#delete-qr").click()

    # Back to create form; sidebar shrinks by 1.
    page.locator("#create-form").wait_for(state="visible")
    assert not page.locator("#result").is_visible()
    page.wait_for_function(
        f"() => document.querySelectorAll('#my-qrs-list li').length === "
        f"{before_count - 1}"
    )


def test_current_expires_field_updates_after_patch(signed_in_page):
    """Regression for the bug spotted via visual inspection: PATCHing
    only `expires_at` worked end-to-end (DB updated, audit_log entry,
    History timeline showed it) but the top-of-panel info section had
    no `Expires at` field, so the user couldn't tell what they'd set
    without scrolling to History. Lock the new field's render in."""
    page, _ = signed_in_page

    # Fresh QR, no expiry yet.
    page.locator("#url-input").fill("https://example.com")
    page.locator("#create-form button[type=submit]").click()
    page.locator("#result").wait_for(state="visible")

    # Initial: no expiry -> "(never)" placeholder, not blank.
    assert page.locator("#current-expires").input_value() == "(never)"

    # PATCH expires only. datetime-local format is "YYYY-MM-DDTHH:MM".
    page.locator("#edit-expires-input").fill("2099-12-31T23:59")
    page.locator("#edit-form button[type=submit]").click()
    page.locator("#edit-status").wait_for(state="visible")

    # Top field reflects the new value. Locale + timezone formatting
    # both vary by where the test runs (UTC vs UTC+8 turns
    # `2099-12-31T23:59` into either Dec 31 2099 or Jan 1 2100), so
    # we just assert "no longer the empty placeholder, contains a
    # 4-digit year" — that's enough to lock the regression.
    import re as _re

    expires_text = page.locator("#current-expires").input_value()
    assert expires_text != "(never)"
    assert expires_text != ""
    assert _re.search(r"\b(2099|2100)\b", expires_text), (
        f"Expected the new expiry (2099 or 2100 depending on TZ) "
        f"to surface in #current-expires, got: {expires_text!r}"
    )


def test_api_token_modal_generates_and_displays_bearer(signed_in_page):
    """Settings flow: result panel "API token" button -> modal ->
    Generate -> show plaintext + curl example. Locks the modal
    state machine (prompt -> result) and the History refresh."""
    page, _ = signed_in_page

    # Open a fresh QR
    page.locator("#url-input").fill("https://example.com")
    page.locator("#create-form button[type=submit]").click()
    page.locator("#result").wait_for(state="visible")

    # Open the API-token modal
    page.locator("#open-api-token").click()
    modal = page.locator("#api-token-modal")
    assert modal.is_visible()
    # Initial state: prompt visible, result hidden
    assert page.locator("#api-token-prompt").is_visible()
    assert not page.locator("#api-token-result").is_visible()

    # Generate
    page.locator("#api-token-generate").click()

    # Result populated
    page.locator("#api-token-result").wait_for(state="visible")
    token_value = page.locator("#api-token-value").input_value()
    assert len(token_value) >= 32, f"unexpected token shape: {token_value!r}"
    # curl example must include the actual token AND the QR's path
    curl_text = page.locator("#api-token-curl").text_content()
    assert token_value in curl_text
    assert "/api/qr/" in curl_text
    assert "Authorization: Bearer" in curl_text

    # Close
    page.locator("#api-token-close").click()
    assert not modal.is_visible()


def test_sidebar_x_button_deletes_qr(signed_in_page):
    """Alternative delete path: click × on a My-QRs row instead of
    opening the QR and using the result-panel Delete.

    Uses RELATIVE count comparisons because earlier tests in the
    same session may have left QRs in the shared (session-scoped)
    test DB. Absolute `count == 1` would be order-of-tests-dependent."""
    page, _ = signed_in_page

    sidebar_items = page.locator("#my-qrs-list li")
    # Wait for any prior sidebar state to settle (the initial
    # /api/qr/mine fetch on page load).
    page.wait_for_timeout(150)
    before_create = sidebar_items.count()

    page.locator("#url-input").fill("https://example.com")
    page.locator("#create-form button[type=submit]").click()
    page.locator("#result").wait_for(state="visible")

    # Sidebar must grow by exactly one (the new QR is at the top —
    # sorted by created_at DESC server-side).
    page.wait_for_function(
        f"() => document.querySelectorAll('#my-qrs-list li').length === "
        f"{before_create + 1}"
    )

    # opacity:0 until hover; force=True bypasses Playwright's
    # actionability check.
    page.on("dialog", lambda dialog: dialog.accept())
    sidebar_items.first.locator(".qr-row-delete").click(force=True)

    # After delete: sidebar shrinks back to its pre-create size.
    page.wait_for_function(
        f"() => document.querySelectorAll('#my-qrs-list li').length === "
        f"{before_create}"
    )

    # Currently-open QR was the one we deleted; result panel must
    # have been reset.
    assert not page.locator("#result").is_visible()
    assert page.locator("#create-form").is_visible()


# ---------------------------------------------------------------------
# Start over leaves the page in a sane state
# ---------------------------------------------------------------------


def test_start_over_returns_to_create_form(signed_in_page):
    """Regression for the bug where resetCreateView delegated to
    refreshAuthState (which doesn't run on Start-over) and the page
    ended up blank below the title."""
    page, _ = signed_in_page

    page.locator("#url-input").fill("https://example.com")
    page.locator("#create-form button[type=submit]").click()
    page.locator("#result").wait_for(state="visible")

    page.locator("#reset").click()

    assert page.locator("#create-form").is_visible()
    assert not page.locator("#result").is_visible()
    assert page.locator("#url-input").input_value() == ""


# ---------------------------------------------------------------------
# Logout resets the page (regression for the bug where result panel
# kept showing after sign-out)
# ---------------------------------------------------------------------


def test_history_and_analytics_panels_populate_after_create(signed_in_page):
    """Fresh-create should yield one Created entry in History and 0
    scans in Analytics. After a PATCH, History gains a
    "Changed destination" entry."""
    page, _ = signed_in_page

    page.locator("#url-input").fill("https://example.com")
    page.locator("#create-form button[type=submit]").click()
    page.locator("#result").wait_for(state="visible")

    # Analytics shows 0 total scans
    page.locator("#analytics-total").wait_for(state="visible")
    assert page.locator("#analytics-total").text_content().strip() == "0"

    # History shows one "Created" entry
    timeline_items = page.locator("#audit-timeline li")
    # Wait for the loading placeholder to be replaced
    page.wait_for_function(
        "() => !document.querySelector('#audit-timeline li.empty')"
    )
    assert timeline_items.count() == 1
    first_entry = timeline_items.first.locator(".action").text_content()
    assert "Created" in first_entry

    # PATCH adds a history entry
    page.locator("#new-url-input").fill("https://updated.example")
    page.locator("#edit-form button[type=submit]").click()
    page.locator("#edit-status").wait_for(state="visible")

    # Wait for history to refresh past 1 entry
    page.wait_for_function(
        "() => document.querySelectorAll('#audit-timeline li').length >= 2"
    )
    actions = page.locator("#audit-timeline li .action").all_text_contents()
    assert any("Changed destination" in a for a in actions)
    assert any("Created" in a for a in actions)


def test_logout_clears_result_panel_and_shows_signin_prompt(signed_in_page):
    page, _ = signed_in_page

    # Create a QR so the result panel is open
    page.locator("#url-input").fill("https://example.com")
    page.locator("#create-form button[type=submit]").click()
    page.locator("#result").wait_for(state="visible")

    # Sign out
    page.locator("#logout-btn").click()

    # Sign-in prompt visible, result panel + create form hidden
    page.locator("#signin-prompt").wait_for(state="visible")
    assert not page.locator("#result").is_visible()
    assert not page.locator("#create-form").is_visible()
    # Sidebar gone
    assert not page.locator("#my-qrs").is_visible()
