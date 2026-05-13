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

    # Sidebar picks up the new QR
    sidebar_items = page.locator("#my-qrs-list li")
    sidebar_items.first.wait_for(state="visible")
    assert sidebar_items.count() == 1

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

    # --- Delete -----------------------------------------------------
    # confirm() returns true via accept_dialog
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#delete-qr").click()

    # Back to create form, sidebar empty
    page.locator("#create-form").wait_for(state="visible")
    assert not page.locator("#result").is_visible()
    page.wait_for_timeout(200)
    assert sidebar_items.count() == 0


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
