"""Render simple ``{{ path.to.value }}`` SMS message templates.

This module deliberately does not use a template engine such as Jinja2. A real
template engine is a server-side template injection surface and a dependency
this code path does not need. The supported syntax is intentionally narrow:
dotted tokens only, resolved against a plain nested dictionary.
"""

import re
from dataclasses import dataclass

ALLOWED_ROOTS: frozenset[str] = frozenset({"contact", "org"})

_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


@dataclass(frozen=True)
class RenderResult:
    body: str
    warnings: list[str]


class UnknownTokenError(ValueError):
    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(f"Unknown token: {token}")


def extract_tokens(body: str) -> list[str]:
    """Return the dotted token names found in *body* in match order."""
    return _TOKEN_RE.findall(body)


def render(body: str, namespace: dict) -> RenderResult:
    """Render *body* using *namespace*, raising UnknownTokenError for bad tokens."""
    warnings: list[str] = []

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        value = _resolve(token, namespace)
        if value is None or value == "":
            warnings.append(token)
            return ""
        return str(value)

    rendered = _TOKEN_RE.sub(replace, body)
    return RenderResult(body=rendered, warnings=warnings)


def _resolve(token: str, namespace: dict) -> object:
    parts = token.split(".")
    if not parts or parts[0] not in ALLOWED_ROOTS:
        raise UnknownTokenError(token)

    current: object = namespace
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise UnknownTokenError(token)
        current = current[part]

    if current is None:
        return None
    if type(current) not in (str, int, float):
        raise UnknownTokenError(token)
    return current
