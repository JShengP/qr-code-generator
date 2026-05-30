"""End-to-end UI coverage for the stretch-feature batch:
302→301 promote, PNG download, sidebar search/sort, analytics range,
soft-delete restore, bulk delete.

Patterns reused from test_ui_flows.py: signed_in_page fixture,
relative count assertions (session-scoped DB), confirm-dialog auto-accept.
"""
import re


def _create_qr(page, url: str = "https://example.com") -> None:
    """Submit a fresh URL and wait for the result panel."""
    page.locator("#url-input").fill(url)
    page.locator("#create-form button[type=submit]").click()
    page.locator("#result").wait_for(state="visible")


# ---------------------------------------------------------------------
# 302 → 301 promote
# ---------------------------------------------------------------------


def _expand_redirect_status(page):
    """The Promote-to-301 button is wrapped in a collapsed <details>
    on purpose (it's a one-way action, easy to misclick if it sits
    next to Copy / API token). Tests have to expand it explicitly,
    mirroring what a real user does."""
    page.evaluate(
        "() => { document.getElementById('redirect-status-details').open = true; }"
    )


def test_promote_to_301_disables_button_and_updates_status(signed_in_page):
    page, _ = signed_in_page
    _create_qr(page)

    # Default-collapsed summary already reflects state; no expand needed
    # for the read-only check.
    assert "302" in page.locator("#redirect-status-summary-value").text_content()

    # Open the advanced section to reach the field + button.
    _expand_redirect_status(page)
    assert "302" in page.locator("#current-redirect-status").input_value()
    promote_btn = page.locator("#promote-301-btn")
    assert promote_btn.is_enabled()
    assert "Promote" in promote_btn.text_content()

    # Confirm dialog must be accepted for the PATCH to fire.
    page.on("dialog", lambda d: d.accept())
    promote_btn.click()

    # Button disabled + relabeled, status field reads "301".
    page.wait_for_function(
        "() => document.getElementById('promote-301-btn').disabled === true"
    )
    assert "Already 301" in promote_btn.text_content()
    assert "301" in page.locator("#current-redirect-status").input_value()
    # Summary value reflects the new state even after re-collapse.
    assert "301" in page.locator("#redirect-status-summary-value").text_content()

    # Audit timeline gains the "Promoted to 301" entry (the friendly
    # label, not the raw `promote_to_301` action name).
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#audit-timeline li .action'))"
        ".some(el => el.textContent.includes('Promoted to 301'))"
    )


def test_promote_to_301_can_be_cancelled_via_dialog(signed_in_page):
    """If the user dismisses the confirm(), no PATCH should fire and
    the button must stay enabled at 302."""
    page, _ = signed_in_page
    _create_qr(page)
    _expand_redirect_status(page)

    page.on("dialog", lambda d: d.dismiss())
    page.locator("#promote-301-btn").click()
    page.wait_for_timeout(200)

    assert page.locator("#promote-301-btn").is_enabled()
    assert "302" in page.locator("#current-redirect-status").input_value()


def test_promote_to_301_details_collapsed_by_default(signed_in_page):
    """Regression for the misclick concern: a fresh QR must NOT show
    the Promote button — the <details> section is collapsed, summary
    shows just the current 302 state."""
    page, _ = signed_in_page
    _create_qr(page)

    # <details>.open === false; the button is in the DOM but its
    # parent is collapsed so it's display:none.
    is_open = page.evaluate(
        "() => document.getElementById('redirect-status-details').open"
    )
    assert is_open is False
    # Summary surface is the only thing the user sees, and it reads
    # the current status — no button anywhere near the readonly
    # affordances above.
    assert page.locator("#redirect-status-summary-value").is_visible()
    assert not page.locator("#promote-301-btn").is_visible()


# ---------------------------------------------------------------------
# PNG download
# ---------------------------------------------------------------------


def test_download_png_link_points_at_attachment_endpoint(signed_in_page):
    page, _ = signed_in_page
    _create_qr(page)

    link = page.locator("#qr-download-link")
    assert link.is_visible()
    href = link.get_attribute("href")
    # Should target /api/qr/{token}/image?download=1 (the attachment
    # variant). The token segment is dynamic; just lock the shape.
    assert "/api/qr/" in href
    assert "/image?download=1" in href

    # The `download` attribute is what makes the browser save vs.
    # navigate — without it, clicking would just open the PNG inline.
    assert (link.get_attribute("download") or "").startswith("qr-")


# ---------------------------------------------------------------------
# Sidebar search + sort
# ---------------------------------------------------------------------


def test_sidebar_search_narrows_the_list(signed_in_page):
    """Create three QRs with distinguishable hostnames, then type a
    fragment into the search box and assert only matching rows remain.
    Uses relative-state assertions because earlier tests share the DB."""
    page, _ = signed_in_page

    # Create 3 unique destinations using a per-test random tag so this
    # test's rows don't collide with anything seeded by earlier tests.
    import secrets
    tag = secrets.token_hex(4)
    for host in (f"alpha-{tag}.example", f"beta-{tag}.example", f"gamma-{tag}.example"):
        _create_qr(page, f"https://{host}/")
        page.locator("#reset").click()
        page.locator("#create-form").wait_for(state="visible")

    # Wait for the sidebar to reflect all three creations.
    page.wait_for_function(
        f"() => document.querySelectorAll('#my-qrs-list li').length >= 3"
    )

    # Type the per-test tag plus "beta" — only the beta row should remain.
    page.locator("#my-qrs-search").fill(f"beta-{tag}")
    page.wait_for_function(
        f"() => document.querySelectorAll('#my-qrs-list li').length === 1"
    )
    item_text = page.locator("#my-qrs-list li").first.text_content()
    assert f"beta-{tag}" in item_text

    # Clear search — the rows come back.
    page.locator("#my-qrs-search").fill("")
    page.wait_for_function(
        f"() => document.querySelectorAll('#my-qrs-list li').length >= 3"
    )


def test_sidebar_sort_destination_orders_alphabetically(signed_in_page):
    """Switch the sort dropdown to 'destination'; the rendered rows
    must be alphabetical by destination url among this test's tagged set."""
    page, _ = signed_in_page

    import secrets
    tag = secrets.token_hex(4)
    for host in (f"zeta-{tag}.example", f"alpha-{tag}.example", f"mu-{tag}.example"):
        _create_qr(page, f"https://{host}/")
        page.locator("#reset").click()
        page.locator("#create-form").wait_for(state="visible")

    # Narrow the view to our 3 tagged rows so the assertion is stable.
    page.locator("#my-qrs-search").fill(tag)
    page.wait_for_function(
        f"() => document.querySelectorAll('#my-qrs-list li').length === 3"
    )

    # Sort by destination.
    page.locator("#my-qrs-sort").select_option("destination")
    page.wait_for_timeout(200)  # let refreshMyQRs round-trip land

    destinations = page.locator("#my-qrs-list .destination").all_text_contents()
    assert destinations == sorted(destinations)


# ---------------------------------------------------------------------
# Analytics date-range filter
# ---------------------------------------------------------------------


def test_analytics_range_clear_button_resets_dates(signed_in_page):
    """We can't easily seed scan_events across days in an e2e test
    without back-doors, so this test focuses on the UX state machine:
    the From/To inputs accept dates, the summary suffix updates to
    reflect the filtered window, and Clear range puts it back to
    'total scans'."""
    page, _ = signed_in_page
    _create_qr(page)

    # Initial suffix is the all-time copy.
    suffix = page.locator("#analytics-summary-suffix")
    assert suffix.text_content().strip() == "total scans"

    # Fill in a range — the suffix mirrors it.
    page.locator("#analytics-from").fill("2025-12-01")
    page.locator("#analytics-from").dispatch_event("change")
    page.locator("#analytics-to").fill("2025-12-31")
    page.locator("#analytics-to").dispatch_event("change")
    page.wait_for_function(
        "() => document.getElementById('analytics-summary-suffix')"
        ".textContent.includes('2025-12-01')"
    )
    suffix_text = suffix.text_content()
    assert "2025-12-01" in suffix_text and "2025-12-31" in suffix_text

    # Clear range — back to all-time copy + empty inputs.
    page.locator("#analytics-reset").click()
    page.wait_for_function(
        "() => document.getElementById('analytics-summary-suffix')"
        ".textContent.trim() === 'total scans'"
    )
    assert page.locator("#analytics-from").input_value() == ""
    assert page.locator("#analytics-to").input_value() == ""


# ---------------------------------------------------------------------
# Soft-delete restore
# ---------------------------------------------------------------------


def test_show_deleted_surfaces_soft_deleted_row_with_restore_button(signed_in_page):
    """Delete a QR via the result-panel button, toggle 'Show deleted',
    confirm the row appears with the .deleted styling + restore (↻)
    affordance, then click restore and confirm the row goes back to
    live state."""
    page, _ = signed_in_page

    import secrets
    tag = secrets.token_hex(4)
    _create_qr(page, f"https://restore-me-{tag}.example/")
    # Capture the token from the result panel; we'll need it to find
    # the row across re-renders.
    token = page.locator("#token").input_value()
    assert token

    # Delete via result-panel button.
    page.on("dialog", lambda d: d.accept())
    page.locator("#delete-qr").click()
    page.locator("#create-form").wait_for(state="visible")

    # Default sidebar view excludes it. fill() dispatches the input
    # event which is debounced 200 ms; wait_for_function rides past
    # the fetch round-trip rather than racing it with a fixed delay.
    page.locator("#my-qrs-search").fill(tag)
    page.wait_for_function(
        "() => document.querySelectorAll('#my-qrs-list li').length === 0"
    )

    # Flip "Show deleted" — row reappears with the deleted pill.
    page.locator("#my-qrs-include-deleted").check()
    page.wait_for_function(
        f"() => document.querySelectorAll('#my-qrs-list li').length === 1"
    )
    row = page.locator("#my-qrs-list li").first
    assert "deleted" in (row.get_attribute("class") or "")
    # The status pill is part of the row text.
    assert "deleted" in row.text_content().lower()

    # Click the per-row restore affordance (now ↻, not ×).
    row.locator(".qr-row-delete").click(force=True)

    # Reset the filter; the row is back in the default view.
    page.locator("#my-qrs-include-deleted").uncheck()
    page.wait_for_function(
        f"() => document.querySelectorAll('#my-qrs-list li').length === 1"
    )
    assert "deleted" not in page.locator("#my-qrs-list li").first.text_content().lower()


# ---------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------


def test_bulk_delete_bar_appears_on_check_and_deletes_selection(signed_in_page):
    """Create two QRs, tick both checkboxes, the bulk-bar count goes
    to 2, click Delete selected, accept the confirm — both rows leave
    the default sidebar view."""
    page, _ = signed_in_page

    import secrets
    tag = secrets.token_hex(4)
    for i in range(2):
        _create_qr(page, f"https://bulk-{tag}-{i}.example/")
        page.locator("#reset").click()
        page.locator("#create-form").wait_for(state="visible")

    # Narrow to our two tagged rows for a stable count.
    page.locator("#my-qrs-search").fill(tag)
    page.wait_for_function(
        "() => document.querySelectorAll('#my-qrs-list li').length === 2"
    )

    # Bulk-bar hidden until first check.
    bar = page.locator("#my-qrs-bulk-bar")
    assert not bar.is_visible()

    # Check both rows.
    checkboxes = page.locator("#my-qrs-list .qr-row-check")
    assert checkboxes.count() == 2
    checkboxes.nth(0).check()
    checkboxes.nth(1).check()

    page.wait_for_function(
        "() => document.getElementById('my-qrs-bulk-count').textContent === '2'"
    )
    assert bar.is_visible()

    # Delete selected.
    page.on("dialog", lambda d: d.accept())
    page.locator("#my-qrs-bulk-delete").click()

    # Default-view list shrinks to 0 for our tagged set.
    page.wait_for_function(
        "() => document.querySelectorAll('#my-qrs-list li').length === 0"
    )
    # Bulk bar hides itself when selection drains.
    assert not bar.is_visible()


def test_bulk_clear_button_drains_selection(signed_in_page):
    """Check a row, click Clear — selection count goes back to 0 and
    the bar hides without any deletions happening."""
    page, _ = signed_in_page

    import secrets
    tag = secrets.token_hex(4)
    _create_qr(page, f"https://clearme-{tag}.example/")
    page.locator("#reset").click()
    page.locator("#create-form").wait_for(state="visible")

    page.locator("#my-qrs-search").fill(tag)
    page.wait_for_function(
        "() => document.querySelectorAll('#my-qrs-list li').length === 1"
    )

    page.locator("#my-qrs-list .qr-row-check").first.check()
    page.wait_for_function(
        "() => document.getElementById('my-qrs-bulk-count').textContent === '1'"
    )

    page.locator("#my-qrs-bulk-clear").click()
    page.wait_for_function(
        "() => document.getElementById('my-qrs-bulk-bar').hidden === true"
    )
    # Row is still alive (we only cleared the selection, not deleted).
    assert page.locator("#my-qrs-list li").count() == 1


# ---------------------------------------------------------------------
# QR appearance customizer (color / resolution / quiet-zone / ECC)
# ---------------------------------------------------------------------


def _set_input(page, selector: str, value: str) -> None:
    """Set an <input type=color/range> value and fire the `input` event
    the live-preview handler listens for. Playwright's .fill() doesn't
    drive color/range inputs, so set value + dispatch the event directly."""
    page.locator(selector).evaluate(
        "(el, v) => { el.value = v; "
        "el.dispatchEvent(new Event('input', {bubbles: true})); }",
        value,
    )


def test_qr_styling_rewrites_preview_and_download_urls(signed_in_page):
    """Changing a style control rewrites BOTH the preview <img> src and
    the Download link href with the matching query param, and the styled
    PNG actually loads from the server (naturalWidth > 0)."""
    page, _ = signed_in_page
    _create_qr(page, "https://example.com")

    # Default render: bare /image path, no style params yet.
    page.wait_for_function(
        "() => { const s = document.querySelector('#qr-image').getAttribute('src');"
        " return s && s.includes('/image') && !s.includes('fill='); }"
    )
    # The customizer is open by default — the feature should be discoverable.
    assert page.locator(".qr-style").get_attribute("open") is not None

    # Foreground color -> src + download href both gain fill=%23ff0000.
    _set_input(page, "#qr-fill", "#ff0000")
    page.wait_for_function(
        "() => document.querySelector('#qr-image')"
        ".getAttribute('src').includes('fill=%23ff0000')"
    )
    dl = page.locator("#qr-download-link").get_attribute("href")
    assert "fill=%23ff0000" in dl
    assert "download=1" in dl

    # The styled image must actually decode — proves the server served a
    # valid PNG for the styled URL, not a 422/500.
    page.wait_for_function(
        "() => { const i = document.querySelector('#qr-image');"
        " return i.complete && i.naturalWidth > 0; }"
    )

    # Resolution slider -> readout + scale= param update together.
    _set_input(page, "#qr-scale", "20")
    page.wait_for_function(
        "() => document.querySelector('#qr-image')"
        ".getAttribute('src').includes('scale=20')"
    )
    assert page.locator("#qr-scale-value").text_content().strip() == "20"

    # Reset clears every style param back to the bare /image URL.
    page.locator("#qr-style-reset").click()
    page.wait_for_function(
        "() => { const s = document.querySelector('#qr-image').getAttribute('src');"
        " return !s.includes('fill=') && !s.includes('scale='); }"
    )
    assert page.locator("#qr-fill").input_value() == "#000000"


def test_qr_styling_does_not_leak_between_qrs(signed_in_page):
    """Opening / creating another QR resets the controls so a previously
    chosen palette doesn't silently carry over to a different code."""
    page, _ = signed_in_page
    _create_qr(page, "https://first.example")

    _set_input(page, "#qr-fill", "#00ff00")
    page.wait_for_function(
        "() => document.querySelector('#qr-image')"
        ".getAttribute('src').includes('fill=%2300ff00')"
    )

    # Start over and create a second QR — controls must be back to default.
    page.locator("#reset").click()
    page.locator("#create-form").wait_for(state="visible")
    _create_qr(page, "https://second.example")

    assert page.locator("#qr-fill").input_value() == "#000000"
    page.wait_for_function(
        "() => !document.querySelector('#qr-image')"
        ".getAttribute('src').includes('fill=')"
    )


def test_qr_module_shape_and_gradient_render(signed_in_page):
    """Module-shape + gradient selects rewrite the GET image URL and the
    styled QR still decodes; the gradient-end color row reveals on demand."""
    page, _ = signed_in_page
    _create_qr(page, "https://shapes.example")

    # Gradient-end row is hidden until a gradient is chosen.
    assert not page.locator("#qr-fill2-row").is_visible()

    page.locator("#qr-module").select_option("rounded")
    page.wait_for_function(
        "() => document.querySelector('#qr-image')"
        ".getAttribute('src').includes('module=rounded')"
    )

    page.locator("#qr-gradient").select_option("radial")
    page.wait_for_function(
        "() => { const s = document.querySelector('#qr-image').getAttribute('src');"
        " return s.includes('gradient=radial') && s.includes('fill2='); }"
    )
    assert page.locator("#qr-fill2-row").is_visible()

    # Styled QR actually decodes, and the download link mirrors the style.
    page.wait_for_function(
        "() => { const i = document.querySelector('#qr-image');"
        " return i.complete && i.naturalWidth > 0; }"
    )
    dl = page.locator("#qr-download-link").get_attribute("href")
    assert "module=rounded" in dl
    assert "gradient=radial" in dl


def test_qr_logo_upload_previews_via_blob_then_clears(signed_in_page, tmp_path):
    """Selecting a logo switches the preview to a POST-rendered blob: URL
    that decodes; removing it returns to the cacheable GET preview."""
    from PIL import Image

    page, _ = signed_in_page
    _create_qr(page, "https://logo.example")

    logo = tmp_path / "logo.png"
    Image.new("RGB", (80, 80), (255, 0, 0)).save(logo)
    page.locator("#qr-logo").set_input_files(str(logo))

    # Preview becomes a blob (POST render) and decodes.
    page.wait_for_function(
        "() => document.querySelector('#qr-image').getAttribute('src').startsWith('blob:')"
    )
    page.wait_for_function(
        "() => { const i = document.querySelector('#qr-image');"
        " return i.complete && i.naturalWidth > 0; }"
    )
    assert page.locator("#qr-download-link").get_attribute("href").startswith("blob:")
    # Logo-size slider + remove button reveal once a logo is attached.
    assert page.locator("#qr-logo-size-row").is_visible()
    assert page.locator("#qr-logo-clear").is_visible()

    # Remove the logo -> back to the GET (non-blob) preview.
    page.locator("#qr-logo-clear").click()
    page.wait_for_function(
        "() => !document.querySelector('#qr-image').getAttribute('src').startsWith('blob:')"
    )
    assert not page.locator("#qr-logo-size-row").is_visible()
