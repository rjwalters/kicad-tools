"""Archived ``design-source/*.py`` must stay byte-identical to the live recipe (Issue #9228).

``boards/<board>/output/manufacturing/design-source/`` holds verbatim copies of
board-level recipe sources (``scripts/boards/_bundle_links.py``), and it can
drift from the live sources with nothing failing: board 03's archived
``route_demo.py`` sat 11 lines behind its live counterpart on ``main`` until
PR #9177 repaired it incidentally during a full-recipe regeneration. The
archive is what a fab or an outside reviewer reads to see how the board was
produced, so a stale snapshot misrepresents the recipe that made the committed
artifacts.

Only ``.py`` copies are compared: ``.md`` copies are deliberately
link-rewritten by ``_bundle_links.py`` and are not byte-identical by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

BOARDS_DIR = Path(__file__).resolve().parent.parent / "boards"

# Archived copies deliberately left behind the live recipe, keyed by
# "<board-dir>/<archive filename>" relative to the boards directory.  Each
# entry cites the issue that owns the refresh; remove the entry when that
# lands (strict xfail turns an unexpected pass into a failure).
EXPECTED_STALE: dict[str, str] = {}

ARCHIVE_COPIES = sorted(BOARDS_DIR.glob("*/output/manufacturing/design-source/*.py"))


def archive_key(archive_copy: Path) -> str:
    """Return the ``<board-dir>/<filename>`` identity of an archived copy."""
    board_dir = archive_copy.parents[3]  # design-source/manufacturing/output/<board>
    return f"{board_dir.name}/{archive_copy.name}"


def live_counterpart(archive_copy: Path) -> Path:
    """Return the live board-level recipe path an archived copy must match."""
    board_dir = archive_copy.parents[3]  # design-source/manufacturing/output/<board>
    return board_dir / archive_copy.name


@pytest.mark.parametrize("archive_copy", ARCHIVE_COPIES, ids=archive_key)
def test_design_source_copy_matches_live_recipe(archive_copy: Path, request) -> None:
    key = archive_key(archive_copy)
    live = live_counterpart(archive_copy)
    assert live.is_file(), (
        f"{key}: archived design-source copy has no live counterpart ({live}); "
        "the archive is the only remaining copy, so parity cannot hold. "
        "Restore the live recipe or remove the stale copy."
    )
    if key in EXPECTED_STALE:
        request.applymarker(pytest.mark.xfail(reason=EXPECTED_STALE[key], strict=True))
    assert archive_copy.read_bytes() == live.read_bytes(), (
        f"{key}: archived design-source copy drifted from the live recipe "
        f"({live}); refresh it through scripts/boards/refresh_readiness.py so "
        "the manifest hashes stay consistent, or record it in EXPECTED_STALE "
        "citing the owning issue."
    )


def test_archive_inventory_is_non_empty() -> None:
    """Guard against a vacuous pass: at least one archived copy must exist."""
    assert ARCHIVE_COPIES, (
        "no boards/*/output/manufacturing/design-source/*.py copies found; "
        "the parity check is comparing nothing."
    )
