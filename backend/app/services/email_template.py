"""Branded HTML version of Ringlite's transactional emails (stdlib only, no I/O).

``render`` turns the plain-text bodies that ``mailer.send`` already receives into a complete
HTML document: a wordmark header, a white card, a real button for the action link, a code box
for one-time codes and a footer. Callers are unchanged - the renderer recognises the bodies by
their shape (see ``email_bodies_facts.md`` for the eight bodies in use).

Only ``html`` and ``re`` are used, there is no I/O, and ``render`` never raises on odd input.
"""

from __future__ import annotations

import html
import re

__all__ = ["render"]

BRAND = "#2a3f75"
INK = "#111827"
MUTED = "#6b7280"
BORDER = "#e5e7eb"
PAGE_BG = "#f4f5f7"
CODE_BG = "#f1f4fa"
CODE_BORDER = "#d6ddee"

FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
)
MONO_STACK = "'SFMono-Regular', Menlo, Consolas, 'Liberation Mono', monospace"

SITE_URL = "https://ringlite.io"

#: A whole paragraph that is a one-time code, e.g. "Your Ringlite code is 304534".
_CODE_RE = re.compile(r"Your .+ code is (\d{6})")
#: A line that is nothing but an https URL.
_URL_LINE_RE = re.compile(r"https://\S+")
#: A one-line paragraph "<Label>: https://..." with a label of at most 40 characters.
_INLINE_LABEL_RE = re.compile(r"([^:\n]{1,40}): *(https://\S+)")
#: An https URL inside running text. http:// and javascript: are never linkified.
_URL_IN_TEXT_RE = re.compile(r"(?<![\w./])https://[^\s<]+")

_NOTE_PREFIXES = (
    "If this wasn't you",
    "If this wasn\u2019t you",
    "If you did not ask for this",
)

#: Button label by URL path fragment - first match wins.
_LABEL_BY_PATH = (
    ("/reset-password", "Reset password"),
    ("/accept-invite", "Accept invitation"),
    ("/confirm-email", "Confirm email"),
    ("/verify", "Confirm email"),
    ("/settings/billing", "Add credit"),
)


class _State:
    """Per-message counters: the first link gets the primary button, one code box only."""

    __slots__ = ("buttons", "code_done")

    def __init__(self) -> None:
        self.buttons = 0
        self.code_done = False


def render(subject: str, body: str, *, app_name: str = "Ringlite") -> str:
    """Return the complete branded HTML document for one transactional email.

    ``subject`` and ``body`` are exactly what ``mailer.send`` receives; ``body`` is not
    modified, so the plain-text part of the message stays as it is today. Never raises:
    odd input still produces a valid document.
    """
    name = (app_name or "Ringlite").strip() or "Ringlite"
    raw_subject = subject if isinstance(subject, str) else ""
    raw_body = _normalise(body)
    heading = _heading(raw_subject, name)

    state = _State()
    blocks = (_block(paragraph, name, state) for paragraph in raw_body.split("\n\n"))
    content = "".join(block for block in blocks if block)
    preheader = html.escape(_preheader(raw_body) or heading)

    card = (
        f'<td style="background-color:#ffffff;border:1px solid {BORDER};'
        f'border-radius:12px;padding:32px;font-family:{FONT_STACK};">'
        f'<div style="font-size:20px;font-weight:bold;color:{INK};line-height:1.3;'
        f'margin:0 0 16px 0;">{html.escape(heading)}</div>'
        f"{content}"
        "</td>"
    )

    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light">',
        f"<title>{html.escape(heading)}</title>",
        "</head>",
        f'<body style="margin:0;padding:0;background-color:{PAGE_BG};">',
        '<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">'
        f"{preheader}</div>",
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        f' width="100%" style="width:100%;background-color:{PAGE_BG};'
        'border-collapse:collapse;">',
        "<tr>",
        '<td align="center" style="padding:24px 12px;">',
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        ' width="560" style="width:100%;max-width:560px;border-collapse:collapse;">',
        "<tr>",
        f'<td style="padding:0 0 16px 4px;font-family:{FONT_STACK};">'
        '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
        f'background-color:{BRAND};margin-right:8px;"></span>'
        f'<span style="font-size:20px;font-weight:bold;color:{BRAND};">'
        f"{html.escape(name)}</span></td>",
        "</tr>",
        "<tr>",
        card,
        "</tr>",
        "<tr>",
        f'<td style="padding:16px 8px 0;text-align:center;font-size:12px;'
        f'line-height:1.6;color:{MUTED};font-family:{FONT_STACK};">'
        f"This is an automated message from {html.escape(name)}.<br>"
        f'{html.escape(name)} &middot; <a href="{SITE_URL}"'
        f' style="color:{MUTED};text-decoration:underline;">ringlite.io</a></td>',
        "</tr>",
        "</table>",
        "</td>",
        "</tr>",
        "</table>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts) + "\n"


def _normalise(body: object) -> str:
    if not isinstance(body, str):
        return ""
    return body.replace("\r\n", "\n").replace("\r", "\n")


def _heading(subject: str, app_name: str) -> str:
    text = " ".join(subject.split())
    if not text:
        return app_name
    stripped = re.sub(
        rf"^{re.escape(app_name)}:\s*", "", text, count=1, flags=re.IGNORECASE
    )
    return stripped.strip() or app_name


def _preheader(body: str) -> str:
    """First plain sentence of the body ("Your code is 123456" for a code paragraph)."""
    for paragraph in body.split("\n\n"):
        text = paragraph.strip()
        if not text:
            continue
        code = _CODE_RE.fullmatch(text)
        if code:
            return f"Your code is {code.group(1)}"
        if "\n" not in text and _URL_LINE_RE.fullmatch(text):
            continue
        if "\n" not in text and _INLINE_LABEL_RE.fullmatch(text):
            continue
        lines = text.split("\n")
        if len(lines) > 1 and _URL_LINE_RE.fullmatch(lines[-1].strip()):
            text = "\n".join(lines[:-1]).strip()
            if not text:
                continue
        return _first_sentence(text)
    return ""


def _first_sentence(text: str) -> str:
    flat = " ".join(text.split())
    match = re.match(r"(.+?[.!?])(?:\s|\Z)", flat)
    sentence = match.group(1) if match else flat
    if len(sentence) > 160:
        sentence = sentence[:157].rstrip() + "..."
    return sentence


def _block(paragraph: str, app_name: str, state: _State) -> str:
    """Render one blank-line separated paragraph of the plain-text body."""
    text = paragraph.strip()
    if not text:
        return ""

    # One-time code: "Your Ringlite code is 304534" - the first such paragraph only.
    code = _CODE_RE.fullmatch(text)
    if code and not state.code_done:
        state.code_done = True
        return _code_block(code.group(1))

    # Inline label: "Add credit: https://ringlite.io/settings/billing".
    if "\n" not in text:
        inline = _INLINE_LABEL_RE.fullmatch(text)
        if inline:
            return _button_block(inline.group(2), inline.group(1).strip(), app_name, state)

    # A paragraph that is only one https URL.
    if "\n" not in text and _URL_LINE_RE.fullmatch(text):
        return _button_block(text, None, app_name, state)

    # An action link on its own line, after at least one line of text.
    lines = text.split("\n")
    if len(lines) > 1 and _URL_LINE_RE.fullmatch(lines[-1].strip()):
        intro = "\n".join(lines[:-1]).rstrip()
        if intro.endswith(":"):
            intro = intro[:-1].rstrip()
        return _paragraph(intro) + _button_block(
            lines[-1].strip(), None, app_name, state
        )

    # Security note: small muted text after a thin divider.
    if text.startswith(_NOTE_PREFIXES):
        return _note_block(text)

    # "Reason: ..." callout.
    if text.startswith("Reason:"):
        return _reason_block(text)

    # Everything else is a normal paragraph.
    return _paragraph(text)


def _paragraph(text: str) -> str:
    return (
        f'<p style="margin:0 0 16px 0;font-size:15px;line-height:1.6;color:{INK};'
        f'font-family:{FONT_STACK};">{_inline(text)}</p>'
    )


def _inline(text: str) -> str:
    """Escape text, turn newlines into <br> and https:// URLs into brand-coloured links."""
    escaped = html.escape(text).replace("\n", "<br>")
    return _URL_IN_TEXT_RE.sub(
        lambda m: (
            f'<a href="{m.group(0)}" style="color:{BRAND};word-break:break-all;">'
            f"{m.group(0)}</a>"
        ),
        escaped,
    )


def _code_block(code: str) -> str:
    return (
        '<div style="margin:0 0 16px 0;">'
        f'<div style="font-size:13px;color:{MUTED};text-transform:uppercase;'
        f'letter-spacing:.06em;font-family:{FONT_STACK};margin-bottom:8px;">Your code</div>'
        f'<div data-role="code" style="font-family:{MONO_STACK};font-size:32px;'
        f'font-weight:bold;letter-spacing:8px;color:{BRAND};background-color:{CODE_BG};'
        f'border:1px solid {CODE_BORDER};border-radius:10px;padding:16px 24px;'
        f'text-align:center;user-select:all;-webkit-user-select:all;">'
        f"{html.escape(code)}</div></div>"
    )


def _note_block(text: str) -> str:
    return (
        f'<div style="border-top:1px solid {BORDER};margin:24px 0 16px 0;"></div>'
        f'<p data-role="note" style="margin:0;font-size:13px;line-height:1.6;'
        f'color:{MUTED};font-family:{FONT_STACK};">{_inline(text)}</p>'
    )


def _reason_block(text: str) -> str:
    rest = text[len("Reason:") :].strip()
    return (
        f'<div data-role="reason" style="background-color:{PAGE_BG};'
        f'border-left:3px solid {BRAND};border-radius:0 8px 8px 0;padding:12px 16px;'
        f'margin:0 0 16px 0;font-size:15px;line-height:1.6;color:{INK};'
        f'font-family:{FONT_STACK};"><strong>Reason</strong>: '
        f"{_inline(rest)}</div>"
    )


def _button_block(url: str, label: str | None, app_name: str, state: _State) -> str:
    """A real button, followed by the small "paste this link" fallback line."""
    if not url.startswith("https://"):
        return _paragraph(url)
    state.buttons += 1
    text = label or _label_for_url(url, app_name)
    href = html.escape(url, quote=True)
    if state.buttons == 1:
        role = "button-primary"
        cell = (
            f"background-color:{BRAND};border-radius:8px;text-align:center;"
            f"font-family:{FONT_STACK};"
        )
        link = (
            "display:inline-block;padding:12px 24px;font-size:15px;font-weight:bold;"
            f"color:#ffffff;text-decoration:none;font-family:{FONT_STACK};"
        )
        bgcolor = f' bgcolor="{BRAND}"'
    else:
        role = "button-secondary"
        cell = (
            f"background-color:#ffffff;border:1px solid {BRAND};border-radius:8px;"
            f"text-align:center;font-family:{FONT_STACK};"
        )
        link = (
            "display:inline-block;padding:12px 24px;font-size:15px;font-weight:bold;"
            f"color:{BRAND};text-decoration:none;font-family:{FONT_STACK};"
        )
        bgcolor = ' bgcolor="#ffffff"'
    return (
        '<div style="margin:24px 0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        ' style="border-collapse:separate;">'
        "<tr>"
        f'<td data-role="{role}" align="center"{bgcolor} style="{cell}">'
        f'<a href="{href}" style="{link}">{html.escape(text)}</a></td>'
        "</tr></table></div>"
        f'<p style="margin:0 0 16px 0;font-size:12px;line-height:1.6;color:{MUTED};'
        f'font-family:{FONT_STACK};word-break:break-all;">'
        "Button not working? Paste this link into your browser:<br>"
        f'<a href="{href}" style="color:{BRAND};word-break:break-all;">'
        f"{html.escape(url)}</a></p>"
    )


def _label_for_url(url: str, app_name: str) -> str:
    path = _url_path(url)
    for marker, label in _LABEL_BY_PATH:
        if marker in path:
            return label
    return f"Open {app_name}"


def _url_path(url: str) -> str:
    rest = url[len("https://") :]
    slash = rest.find("/")
    if slash == -1:
        return ""
    path = rest[slash:]
    end = len(path)
    for separator in ("?", "#"):
        index = path.find(separator)
        if index != -1:
            end = min(end, index)
    return path[:end]
