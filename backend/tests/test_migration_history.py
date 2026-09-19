"""The migration chain is a shared resource with no lock, and until now no test.

WHY THIS FILE EXISTS. On 19 Sept 2026 two sessions independently wrote a `0053`, each
chaining off `0052_agent_calls_place`. That is a branched history: `alembic upgrade head`
refuses with "Multiple head revisions are present", and the first place it would have
surfaced is a deploy. It was caught by one session noticing that two strings matched in
`git status` - not by anything going red, and nothing could have gone red, because the test
suite builds its tables from SQLAlchemy metadata rather than by running migrations. So the
entire suite is green by construction whatever the migration graph looks like.

That is the same shape as the rest of this audit: a body of checks that cannot return the
failing answer for this class of fault. These tests are cheap, need no database, and fail in
the second a branch is created rather than at deploy.
"""

from __future__ import annotations

import pathlib

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def script() -> ScriptDirectory:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    return ScriptDirectory.from_config(cfg)


def test_the_history_has_exactly_one_head(script: ScriptDirectory) -> None:
    """Two heads means two people added a migration on top of the same parent. Deploy runs
    `alembic upgrade head`, which refuses to guess which one you meant."""
    heads = script.get_heads()
    assert len(heads) == 1, (
        "the migration history has branched - "
        f"{len(heads)} heads: {sorted(heads)}. One of them needs its `down_revision` "
        "re-pointed at the other rather than at their shared parent."
    )


def test_every_revision_resolves_and_the_chain_is_unbroken(script: ScriptDirectory) -> None:
    """Walking the graph raises if a `down_revision` names something that is not there - the
    failure mode of renaming a revision after somebody has already chained onto it, which is
    silent in every other way."""
    revisions = list(script.walk_revisions())
    assert revisions, "no migrations were found at all - is script_location right?"
    known = {rev.revision for rev in revisions}
    for rev in revisions:
        for parent in rev._all_down_revisions:
            assert parent in known, (
                f"{rev.revision} chains to {parent!r}, which does not exist. A revision was "
                "renamed or removed after something was chained onto it."
            )


def test_a_revision_id_is_never_reused(script: ScriptDirectory) -> None:
    """Alembic itself raises on a duplicate id while loading, so this asserts the property
    rather than catching it - it is here so the invariant is written down where someone
    adding a migration will read it, next to the branch check they actually need."""
    ids = [rev.revision for rev in script.walk_revisions()]
    assert len(ids) == len(set(ids)), "duplicate revision ids"


def test_every_migration_file_is_numbered_in_chain_order(script: ScriptDirectory) -> None:
    """The filename prefix is a human convenience with no meaning to alembic, which makes it
    exactly the kind of thing that drifts. A file called 0054 that actually chains beneath
    0052 is not wrong to alembic and is very misleading to a person reading `ls`.

    Only checks files that follow the NNNN_ convention, so a differently-named migration is
    not retroactively made illegal by this test.

    ONE KNOWN EXCEPTION, listed rather than skipped. `0025_ai_assistant_product` chains from
    `0039_smart_routing`, and its own docstring says why: "Filename keeps the roadmap number
    (0025) but the chain continues from the true head 0039_smart_routing." That was a
    deliberate choice, so it is named here with its reason instead of being hidden behind a
    looser assertion - the difference between a documented exception and a weakened test is
    that the exception can be read.
    """
    known_inversions = {("0025_ai_assistant_product", "0039_smart_routing")}
    numbered: dict[str, int] = {}
    for rev in script.walk_revisions():
        prefix = rev.revision.split("_", 1)[0]
        if prefix.isdigit():
            numbered[rev.revision] = int(prefix)
    for rev in script.walk_revisions():
        if rev.revision not in numbered:
            continue
        for parent in rev._all_down_revisions:
            if parent not in numbered or (rev.revision, parent) in known_inversions:
                continue
            assert numbered[parent] < numbered[rev.revision], (
                f"{rev.revision} chains beneath {parent}, but its number is not higher. "
                "The prefix means nothing to alembic and everything to the next person."
            )
