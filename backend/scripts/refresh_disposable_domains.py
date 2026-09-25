"""Refresh app/data/disposable_domains.txt from the CC0 disposable-email-domains list.

    python scripts/refresh_disposable_domains.py

Commit the result; the running app reads the file at first use.
"""

import urllib.request
from pathlib import Path

URL = (
    "https://raw.githubusercontent.com/disposable-email-domains/"
    "disposable-email-domains/main/disposable_email_blocklist.conf"
)
TARGET = Path(__file__).resolve().parent.parent / "app" / "data" / "disposable_domains.txt"

with urllib.request.urlopen(URL, timeout=30) as resp:
    body = resp.read().decode("utf-8")
domains = sorted({line.strip().lower() for line in body.splitlines() if line.strip()})
if len(domains) < 1000:
    raise SystemExit(f"refusing to write a suspiciously short list ({len(domains)} domains)")
TARGET.write_text("\n".join(domains) + "\n", encoding="utf-8")
print(f"wrote {len(domains)} domains to {TARGET}")
