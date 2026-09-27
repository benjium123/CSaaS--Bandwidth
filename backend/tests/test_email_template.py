"""Tests for the branded HTML transactional-email renderer."""

from __future__ import annotations

import re

from app.services.email_template import render

CODE_SUBJECT = "304534 is your Ringlite code"
CODE_BODY = (
    "Your Ringlite code is 304534\n\n"
    "Enter it to finish signing in. It expires in 10 minutes and works once.\n\n"
    "If you did not ask for this, someone may know your password. Change it from "
    "Settings > Security and contact support."
)

RESET_URL = "https://ringlite.io/reset-password?token=abc_-123"
RESET_BODY = (
    "Someone asked to reset the password for a@b.com.\n\n"
    "Reset it here (the link works once, for 30 minutes):\n"
    f"{RESET_URL}\n\n"
    "You will still need your password to sign in on other devices.\n\n"
    "If this wasn't you, reset your password and contact support immediately."
)

INVITE_BODY = (
    "You were invited to join Acme Inc as admin.\n\n"
    "Accept the invitation (the link works once and expires in 7 days):\n"
    "https://ringlite.io/accept-invite?token=abc123"
)

BILLING_BODY = (
    "Your Ringlite balance is $0.00. Outgoing texts, calls and faxes are paused and "
    "incoming calls are being declined until you add credit or buy a bundle.\n\n"
    "Add credit: https://ringlite.io/settings/billing"
)

SUSPENSION_BODY = (
    "Calling, texting and number orders for your business have been suspended "
    "following a compliance review.\n\n"
    "Reason: Spam complaints\n\n"
    "You can still sign in to see your account. Reply to this email or contact "
    "support if you believe this is a mistake."
)


def _preheader(html_text: str) -> str:
    match = re.search(
        r"display:none;max-height:0;overflow:hidden[^>]*>(.*?)</div>", html_text, re.S
    )
    assert match is not None, "no preheader"
    return match.group(1)


def _code_box(html_text: str) -> str:
    match = re.search(r'data-role="code"[^>]*>(.*?)</div>', html_text, re.S)
    assert match is not None, "no code box"
    return match.group(1)


def _note(html_text: str) -> str:
    match = re.search(r'<p data-role="note"[^>]*>(.*?)</p>', html_text, re.S)
    assert match is not None, "no security note"
    return match.group(0)


def _button(html_text: str, role: str) -> str:
    match = re.search(rf'data-role="{role}".*?</table>', html_text, re.S)
    assert match is not None, f"no {role}"
    return match.group(0)


def _hrefs(html_text: str) -> list[str]:
    return re.findall(r'href="([^"]*)"', html_text)


def test_code_body_renders_code_box_and_heading():
    html_text = render(CODE_SUBJECT, CODE_BODY)
    assert html_text.startswith("<!doctype html>")
    assert html_text.rstrip().endswith("</html>")
    assert "304534" in _code_box(html_text)
    assert f"<title>{CODE_SUBJECT}</title>" in html_text
    assert CODE_SUBJECT in html_text


def test_code_body_security_note_is_muted():
    html_text = render(CODE_SUBJECT, CODE_BODY)
    note = _note(html_text)
    assert "If you did not ask for this" in note
    assert "font-size:13px" in note
    assert "#6b7280" in note
    assert "Settings &gt; Security" in html_text


def test_password_reset_body_renders_one_primary_button():
    html_text = render("Ringlite: Reset your password", RESET_BODY)
    assert html_text.count('data-role="button-primary"') == 1
    assert 'data-role="button-secondary"' not in html_text
    button = _button(html_text, "button-primary")
    assert f'href="{RESET_URL}"' in button
    assert ">Reset password</a>" in button
    # Intro keeps the text but loses its trailing colon.
    assert "Reset it here (the link works once, for 30 minutes)<" in html_text
    assert "for 30 minutes):<" not in html_text
    fallback = re.search(r"Button not working\?(.*?)</p>", html_text, re.S)
    assert fallback is not None
    assert f'href="{RESET_URL}"' in fallback.group(1)
    assert "<title>Reset your password</title>" in html_text
    assert "Ringlite:" not in html_text


def test_invite_body_renders_accept_invitation_button():
    html_text = render("You're invited to Acme Inc on Ringlite", INVITE_BODY)
    assert html_text.count('data-role="button-primary"') == 1
    button = _button(html_text, "button-primary")
    assert ">Accept invitation</a>" in button
    assert 'href="https://ringlite.io/accept-invite?token=abc123"' in button


def test_billing_body_renders_add_credit_button():
    subject = "Ringlite: Your balance is empty - calling and texting are paused"
    html_text = render(subject, BILLING_BODY)
    button = _button(html_text, "button-primary")
    assert ">Add credit</a>" in button
    assert 'href="https://ringlite.io/settings/billing"' in button


def test_reason_paragraph_renders_callout():
    html_text = render("Your Ringlite account has been suspended", SUSPENSION_BODY)
    match = re.search(r'<div data-role="reason".*?>(.*?)</div>', html_text, re.S)
    assert match is not None
    assert "<strong>Reason</strong>: Spam complaints" in match.group(1)
    assert "border-left:3px solid #2a3f75" in match.group(0)


def test_html_is_escaped():
    body = (
        "Pay now <script>alert(1)</script> & thanks\n\n"
        "If this wasn't you, ignore this email."
    )
    html_text = render("Ringlite: Action needed", body)
    assert "<script>" not in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert "&amp; thanks" in html_text


def test_settings_breadcrumb_is_escaped_text():
    html_text = render("Ringlite: Verified", "Open Settings > Messaging to register.")
    assert "Settings &gt; Messaging" in html_text
    assert "Settings > Messaging" not in html_text


def test_ampersand_in_url_is_attribute_escaped():
    url = "https://ringlite.io/settings/billing?plan=pro&ref=x"
    html_text = render("Ringlite: Add credit", f"Add credit: {url}")
    assert 'href="https://ringlite.io/settings/billing?plan=pro&amp;ref=x"' in html_text
    assert '?plan=pro&ref=x"' not in html_text
    assert _hrefs(html_text)
    assert all(href.startswith("https://") for href in _hrefs(html_text))


def test_non_https_urls_are_never_links():
    body = (
        "Open http://ringlite.io/reset-password?token=x or javascript:alert(1) "
        "to continue."
    )
    html_text = render("Ringlite: Security check", body)
    assert 'href="http://' not in html_text
    assert 'href="javascript:' not in html_text
    assert 'data-role="button' not in html_text
    assert "http://ringlite.io/reset-password?token=x" in html_text
    assert all(href.startswith("https://") for href in _hrefs(html_text))


def test_empty_body_is_a_valid_document():
    html_text = render("Ringlite: Hello", "")
    assert html_text.startswith("<!doctype html>")
    assert html_text.rstrip().endswith("</html>")
    assert "<title>Hello</title>" in html_text
    assert "max-width:560px" in html_text
    assert 'data-role="button' not in html_text


def test_body_that_is_only_a_url_gets_one_button():
    url = "https://ringlite.io/confirm-email?token=xyz"
    html_text = render("Ringlite: Confirm your email", url)
    assert html_text.count('data-role="button-primary"') == 1
    assert ">Confirm email</a>" in html_text
    assert f'href="{url}"' in html_text


def test_only_the_first_url_is_primary():
    body = (
        "Reset it here:\nhttps://ringlite.io/reset-password?token=aaa\n\n"
        "Confirm your address:\nhttps://ringlite.io/confirm-email?token=bbb"
    )
    html_text = render("Ringlite: Two steps", body)
    assert html_text.count('data-role="button-primary"') == 1
    assert html_text.count('data-role="button-secondary"') == 1
    primary = _button(html_text, "button-primary")
    secondary = _button(html_text, "button-secondary")
    assert ">Reset password</a>" in primary
    assert "reset-password?token=aaa" in primary
    assert ">Confirm email</a>" in secondary
    assert "confirm-email?token=bbb" in secondary
    assert html_text.index('data-role="button-primary"') < html_text.index(
        'data-role="button-secondary"'
    )


def test_preheader_contains_first_sentence():
    html_text = render("Ringlite: Reset your password", RESET_BODY)
    assert "Someone asked to reset the password for a@b.com." in _preheader(html_text)


def test_preheader_for_a_code_is_the_code_sentence():
    html_text = render(CODE_SUBJECT, CODE_BODY)
    assert "Your code is 304534" in _preheader(html_text)


def test_odd_input_never_raises():
    assert render("", "").startswith("<!doctype html>")
    assert render("Ringlite: x", "\n\n\n").startswith("<!doctype html>")
    assert render("Ringlite: x", "   ").startswith("<!doctype html>")
    assert render("Ringlite: x", "y" * 100_000).startswith("<!doctype html>")
