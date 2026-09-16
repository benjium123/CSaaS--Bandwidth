"""P41 sanctions screening against the official, free government lists.

  - US   OFAC Specially Designated Nationals (SDN) list, CSV
  - UK   OFSI Consolidated List of Financial Sanctions Targets, CSV
  - CA   Special Economic Measures Act (SEMA) consolidated list, XML

The sweeper downloads each list daily into SECURITY_DATA_DIR/sanctions/<source>.txt as one
normalised name per line; screening reads those files. A list that has never downloaded
makes the check answer ``error`` ("could not screen") - never ``pass``.

Matching is deliberately simple and explainable to an operator:
  exact   - every word matches, in any order        -> fail (block until reviewed)
  partial - every word of a 2+ word listed name appears in the candidate -> warn
Anything fuzzier belongs to a paid screening vendor, behind the same screen() interface.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.config import Settings

log = structlog.get_logger(__name__)

SOURCES: dict[str, str] = {
    "ofac_sdn": "https://www.treasury.gov/ofac/downloads/sdn.csv",
    "uk_ofsi": "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv",
    "ca_sema": (
        "https://www.international.gc.ca/world-monde/assets/office_docs/"
        "international_relations-relations_internationales/sanctions/sema-lmes.xml"
    ),
}

_STOPWORDS = frozenset({"the", "of", "and", "llc", "ltd", "limited", "inc", "co", "corp", "plc"})
_cache: dict[str, tuple[float, list[frozenset[str]]]] = {}


def normalize_name(name: str) -> frozenset[str]:
    decomposed = unicodedata.normalize("NFKD", name or "")
    ascii_only = decomposed.encode("ascii", "ignore").decode().lower()
    words = re.split(r"[^a-z0-9]+", ascii_only)
    return frozenset(w for w in words if w and w not in _STOPWORDS)


def list_dir(settings: Settings) -> Path:
    return Path(settings.security_data_dir) / "sanctions"


# --------------------------------------------------------------------------------------
# Parsers (each returns raw names)
# --------------------------------------------------------------------------------------
def parse_ofac_sdn(text: str) -> list[str]:
    names = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) > 1 and row[1].strip() and row[1].strip() != "-0-":
            names.append(row[1].strip())
    return names


def parse_uk_ofsi(text: str) -> list[str]:
    rows = list(csv.reader(io.StringIO(text)))
    header_index = next(
        (i for i, r in enumerate(rows[:5]) if any(c.strip() == "Name 6" for c in r)), None
    )
    if header_index is None:
        return []
    header = [c.strip() for c in rows[header_index]]
    name_cols = [header.index(f"Name {n}") for n in range(1, 7) if f"Name {n}" in header]
    names = []
    for row in rows[header_index + 1 :]:
        parts = [row[i].strip() for i in name_cols if i < len(row) and row[i].strip()]
        if parts:
            names.append(" ".join(parts))
    return names


def parse_ca_sema(text: str) -> list[str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    names = []
    for record in root.iter():
        children = {child.tag.lower(): (child.text or "").strip() for child in record}
        if not children:
            continue
        entity = children.get("entityorship") or children.get("entity")
        given = children.get("givenname")
        last = children.get("lastname")
        if entity:
            names.append(entity)
        elif given or last:
            names.append(" ".join(p for p in (given, last) if p))
    return names


PARSERS = {"ofac_sdn": parse_ofac_sdn, "uk_ofsi": parse_uk_ofsi, "ca_sema": parse_ca_sema}


async def refresh(settings: Settings, client=None) -> dict[str, int]:
    """Download every list. A failed or empty download keeps the previous file."""
    import httpx

    owns = client is None
    client = client or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
    counts: dict[str, int] = {}
    directory = list_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        for source, url in SOURCES.items():
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                names = PARSERS[source](resp.content.decode("utf-8", errors="replace"))
            except Exception:
                log.warning("sanctions_download_failed", source=source, exc_info=True)
                continue
            if not names:
                log.warning("sanctions_list_empty", source=source)
                continue
            path = directory / f"{source}.txt"
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(names) + "\n", encoding="utf-8")
            tmp.replace(path)
            _cache.pop(str(path), None)
            counts[source] = len(names)
    finally:
        if owns:
            await client.aclose()
    return counts


def _load(path: Path) -> list[frozenset[str]]:
    mtime = path.stat().st_mtime
    cached = _cache.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    entries = [
        normalize_name(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    entries = [e for e in entries if e]
    _cache[str(path)] = (mtime, entries)
    return entries


@dataclass
class ScreenResult:
    loaded_sources: list[str] = field(default_factory=list)
    exact: list[dict] = field(default_factory=list)
    partial: list[dict] = field(default_factory=list)

    @property
    def result(self) -> str:
        if not self.loaded_sources:
            return "error"
        if self.exact:
            return "fail"
        if self.partial:
            return "warn"
        return "pass"


def screen(settings: Settings, names: list[str]) -> ScreenResult:
    out = ScreenResult()
    candidates = [(n, normalize_name(n)) for n in names if n and normalize_name(n)]
    for source in SOURCES:
        path = list_dir(settings) / f"{source}.txt"
        if not path.exists():
            continue
        out.loaded_sources.append(source)
        for listed in _load(path):
            for raw, words in candidates:
                if words == listed:
                    out.exact.append(
                        {"name": raw, "source": source, "listed": " ".join(sorted(listed))}
                    )
                elif len(listed) >= 2 and listed <= words:
                    out.partial.append(
                        {"name": raw, "source": source, "listed": " ".join(sorted(listed))}
                    )
    # Cap what we store on the check row.
    out.exact = out.exact[:20]
    out.partial = out.partial[:20]
    return out
