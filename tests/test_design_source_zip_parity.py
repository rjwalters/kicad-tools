"""Archived ``design-source.zip`` recipe sources must match the live recipe (Issue #9231).

Boards 02, 05, 06 and 07 ship their archived recipe sources as
``boards/<board>/output/manufacturing/design-source.zip`` (built by
``scripts/boards/_bundle_links.py`` / ``scripts/boards/refresh_readiness.py``).
The archive is what a fab or an outside reviewer reads to see how the board was
produced, so a stale member misrepresents the recipe that made the committed
artifacts -- and nothing compared the members to the live tree, so drift was
invisible until someone unzipped.

Only ``.py`` members are compared: ``.md`` members are link-rewritten and
generated outputs (BOM, netlists) are not recipe sources.  Members are read
with :mod:`zipfile`; nothing is extracted to disk.

The loose ``design-source/*.py`` copies of boards 03/04 are a separate
archive form with their own escape hatch; keep the two independent.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARDS_DIR = REPO_ROOT / "boards"
REFRESH_SCRIPT = REPO_ROOT / "scripts" / "boards" / "refresh_readiness.py"

# Archived members deliberately left behind the live recipe, keyed by
# "<board-dir>/<member path inside the zip>".  Each entry cites the issue that
# owns the refresh; remove the entry when that lands (strict xfail turns an
# unexpected pass into a failure).
EXPECTED_STALE: dict[str, str] = {
    # Zip last refreshed in 99122e5a1; the live recipe has moved since, and
    # ``refresh_readiness.py`` cannot re-emit the zip while the strict gate
    # refuses on the unaccepted warning flood (#8399 stalemate).
    "05-bldc-motor-controller/redesign/build.py": (
        "Issue #9068: #9057/#8532 added the wedge fix and the global "
        "fiducials + board-ID silk to redesign/build.py after the archive was "
        "last refreshed; the gate-pass rebuild + re-pin (blocked by the #8399 "
        "warning-flood stalemate) re-emits the zip."
    ),
    "05-bldc-motor-controller/redesign/hardware.py": (
        "Issue #9068: #9071's silk_hole_overlap fixes (reference-field offsets, "
        "SILK_STROKE_EDITS) landed in redesign/hardware.py after the archive "
        "was last refreshed; the gate-pass rebuild + re-pin (blocked by the "
        "#8399 warning-flood stalemate) re-emits the zip."
    ),
    "07-matchgroup-test/real_design/validate.py": (
        "Issue #8399: #9117/#9175/#9198 moved board 07's matched-length and "
        "off-path gate in real_design/validate.py after the archive was last "
        "refreshed; refresh_readiness.py refuses to re-emit the zip while "
        "redundant_copper_* warnings remain, so the last child of #8399 owns "
        "the re-pin."
    ),
}


def _zip_members() -> list[tuple[Path, str]]:
    """Every ``.py`` member of every board ``design-source.zip``, sorted."""
    members: list[tuple[Path, str]] = []
    for archive in sorted(BOARDS_DIR.glob("*/output/manufacturing/design-source.zip")):
        with zipfile.ZipFile(archive) as zf:
            members.extend(
                (archive, info.filename)
                for info in zf.infolist()
                if not info.is_dir() and info.filename.endswith(".py")
            )
    return members


ZIP_MEMBERS = _zip_members()


def board_dir(archive: Path) -> Path:
    """Return the board directory owning ``<board>/output/manufacturing/design-source.zip``."""
    return archive.parents[2]


def member_key(param: tuple[Path, str]) -> str:
    """Return the ``<board-dir>/<member path>`` identity of an archived member."""
    archive, member = param
    return f"{board_dir(archive).name}/{member}"


def refresh_guidance(board: str) -> str:
    """Say which refresh path exists for ``board``, rather than blanket advice."""
    if f'"{board}": BoardConfig(' in REFRESH_SCRIPT.read_text(encoding="utf-8"):
        return (
            f"refresh it through scripts/boards/refresh_readiness.py "
            f"(it has a BOARDS entry for {board}) so the manifest hashes stay "
            "consistent"
        )
    return (
        f"scripts/boards/refresh_readiness.py has no BOARDS entry for {board}, "
        "so regenerate the archive with the board's own recipe packaging step"
    )


@pytest.mark.parametrize("zip_member", ZIP_MEMBERS, ids=member_key)
def test_design_source_zip_member_matches_live_recipe(
    zip_member: tuple[Path, str], request
) -> None:
    archive, member = zip_member
    key = member_key(zip_member)
    board = board_dir(archive)
    live = board / member
    assert live.is_file(), (
        f"{key}: archived design-source.zip member has no live counterpart "
        f"({live}); the archive is the only remaining copy, so parity cannot "
        "hold. Restore the live recipe or regenerate the archive without it."
    )
    if key in EXPECTED_STALE:
        request.applymarker(pytest.mark.xfail(reason=EXPECTED_STALE[key], strict=True))
    with zipfile.ZipFile(archive) as zf:
        archived = zf.read(member)
    assert archived == live.read_bytes(), (
        f"{key}: archived design-source.zip member drifted from the live "
        f"recipe ({live}); {refresh_guidance(board.name)}, or record it in "
        "EXPECTED_STALE citing the owning issue."
    )


def test_every_zip_board_is_collected() -> None:
    """No board with a ``design-source.zip`` may be silently skipped by the glob."""
    boards_with_zip = {
        p.parent.parent.parent.name
        for p in BOARDS_DIR.glob("*/output/manufacturing/design-source.zip")
    }
    assert boards_with_zip, "no boards/*/output/manufacturing/design-source.zip found"
    collected = {board_dir(archive).name for archive, _ in ZIP_MEMBERS}
    missing = boards_with_zip - collected
    assert not missing, (
        f"design-source.zip present but no .py members collected for {sorted(missing)}; "
        "the parity check is comparing nothing for them."
    )


def test_expected_stale_entries_name_real_members() -> None:
    """A renamed or removed member must not leave a dead escape-hatch entry."""
    keys = {member_key(m) for m in ZIP_MEMBERS}
    dead = sorted(set(EXPECTED_STALE) - keys)
    assert not dead, f"EXPECTED_STALE entries match no archived .py member: {dead}"
