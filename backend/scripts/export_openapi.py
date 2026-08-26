#!/usr/bin/env python
"""Dump the FastAPI OpenAPI schema to frontend/openapi.json.

No running server needed. CI regenerates this and `git diff --exit-code`s it, so the
frontend's generated types can never silently disagree with the backend about a response
shape — the compensating control for having no browser E2E layer yet (plan DR-1).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

OUT = ROOT.parent / "frontend" / "openapi.json"


def build_settings() -> Settings:
    """Minimal settings that satisfy validation without reading the developer's .env."""
    return Settings(
        app_env="test",
        jwt_secret="openapi-export-not-a-real-secret",
        session_secret="openapi-export-not-a-real-secret",
        database_url="sqlite+aiosqlite:///:memory:",
        _env_file=None,
    )


def main() -> int:
    app = create_app(build_settings())
    schema = app.openapi()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys so the file is byte-stable across runs — the diff check depends on it.
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(schema.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
