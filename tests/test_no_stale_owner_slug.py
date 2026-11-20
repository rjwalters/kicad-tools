"""No tracked file hard-codes the pre-transfer owner slug (issue #9298).

The repository moved from the personal account ``rjwalters`` to the
``2AMLogic`` organization. GitHub redirects old URLs, but a redirect breaks
the moment a repository of the old name is created under the old account,
and PyPI/consumer metadata must point at the real home. This guard fails
when the old slug is reintroduced outside the excluded paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

OLD_SLUG = "rjwalters/kicad-tools"
NEW_SLUG = "2AMLogic/kicad-tools"

#: The slug with the separator written other ways that appear in source:
#: ``\/`` (backslash + slash) inside regex/JSON string literals, ``%2F`` in
#: URL-encoded paths. One or two separator characters, so both the bare
#: ``/`` and the escaped ``\/`` match.
OLD_SLUG_PATTERN = r"rjwalters([/\\]{1,2}|%2[Ff])kicad-tools"

# Paths the guard deliberately ignores:
# - ``.loom/``, ``.claude/``, ``.agents/``: installer-managed mirrors of
#   upstream Loom / Repo Skills / Squad, refreshed by their own installers.
# - ``CHANGELOG.md`` and existing ``changelog.d/`` fragments: release
#   history, never rewritten.
# - this file: its constant must spell the old slug to detect it.
# - board evidence archives (``boards/<board>/output/``, board 05's
#   ``redesign/``, board 07's ``real_design/``, board 05's pinned
#   ``README.md``): hash-bound provenance bundles. ``manifest.json`` records
#   a sha256 *and* size for every file in the bundle and ``verify_manifest()``
#   (``src/kicad_tools/export/manufacturing.py``) recomputes both, while
#   ``readiness.json`` pins input digests. Rewriting files inside those
#   bundles would falsify the record of what was actually shipped, so the
#   archived copies keep the pre-transfer slug by design — the same reasoning
#   as release history. Live sources outside the archives (e.g.
#   ``boards/04-stm32-devboard/check_manufacturing.py``, the board root
#   script, versus its archived copy under ``output/manufacturing/``) are
#   still guarded.
EXCLUDED = (
    ".loom/",
    ".claude/",
    ".agents/",
    "CHANGELOG.md",
    "changelog.d/",
    "tests/test_no_stale_owner_slug.py",
    "boards/00-simple-led/output/",
    "boards/01-voltage-divider/output/",
    "boards/02-charlieplex-led/output/",
    "boards/03-usb-joystick/output/",
    "boards/04-stm32-devboard/output/",
    "boards/05-bldc-motor-controller/output/",
    "boards/05-bldc-motor-controller/redesign/",
    "boards/05-bldc-motor-controller/README.md",
    "boards/06-diffpair-test/output/",
    "boards/07-matchgroup-test/output/",
    "boards/07-matchgroup-test/real_design/",
    "boards/09-usbc-pd-power/output/",
)


def _tracked_files_with_old_slug() -> list[str]:
    proc = subprocess.run(
        ["git", "grep", "-I", "-l", "-E", OLD_SLUG_PATTERN],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise AssertionError(f"git grep failed ({proc.returncode}): {proc.stderr}")
    return proc.stdout.splitlines()


def _is_excluded(path: str) -> bool:
    return any(path == excl or path.startswith(excl) for excl in EXCLUDED)


def test_no_tracked_file_contains_old_owner_slug() -> None:
    offenders = [p for p in _tracked_files_with_old_slug() if not _is_excluded(p)]
    assert not offenders, (
        f"{OLD_SLUG} must not appear in tracked files; the repository lives at "
        f"{NEW_SLUG}. Offenders (update them to {NEW_SLUG}; extend EXCLUDED only "
        "for installer-managed or release-history paths):\n" + "\n".join(offenders)
    )


def test_exclusions_still_exist() -> None:
    """Prune the exclusion list when one of its paths disappears."""
    for excl in EXCLUDED:
        assert (ROOT / excl).exists(), (
            f"exclusion {excl!r} no longer exists in the tree; prune it from EXCLUDED"
        )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("README.md", False),
        ("src/kicad_tools/validate/sarif.py", False),
        (".loom/scripts/worktree.sh", True),
        (".claude/commands/repo/update-tools.md", True),
        (".agents/skills/repo-update-tools/SKILL.md", True),
        ("CHANGELOG.md", True),
        ("changelog.d/9298.changed.md", True),
        ("tests/test_no_stale_owner_slug.py", True),
        ("boards/07-matchgroup-test/output/manufacturing/check-report.json", True),
        ("boards/05-bldc-motor-controller/README.md", True),
        ("boards/04-stm32-devboard/check_manufacturing.py", False),
    ],
)
def test_exclusion_classification(path: str, expected: bool) -> None:
    """A reintroduced slug in a non-excluded path is flagged, excluded ones are not."""
    assert _is_excluded(path) is expected
