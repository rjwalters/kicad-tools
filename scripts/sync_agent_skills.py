#!/usr/bin/env python3
"""Keep ``.claude/commands/kct/`` a byte-identical copy of the packaged skills.

The source of truth for the ``kct`` agent skills is the package data in
``src/kicad_tools/agent_skills/kct/*.md`` (issue #5950), so ``pip install
kicad-tools`` ships them. ``.claude/commands/kct/`` is this repo's own copy, with
the same harness-neutral placeholders (it is not rendered), so the skill lint in
``tests/test_agent_surface_neutrality.py`` and the in-repo installer see the same
text the wheel carries.

Edit the package data, then::

    uv run python scripts/sync_agent_skills.py --write

Without ``--write`` the script only checks and exits 1 on drift.
``tests/test_agent_skills_package.py`` runs the same check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "kicad_tools" / "agent_skills" / "kct"
REPO_COPY_DIR = REPO_ROOT / ".claude" / "commands" / "kct"


def find_drift(package_dir: Path = PACKAGE_DIR, copy_dir: Path = REPO_COPY_DIR) -> list[str]:
    """Return human-readable drift lines; empty when the copy matches."""
    source = {p.name: p.read_bytes() for p in package_dir.glob("*.md")}
    copy = {p.name: p.read_bytes() for p in copy_dir.glob("*.md")} if copy_dir.is_dir() else {}
    drift = [f"missing from copy: {name}" for name in sorted(source.keys() - copy.keys())]
    drift += [f"not in package data: {name}" for name in sorted(copy.keys() - source.keys())]
    drift += [
        f"differs: {name}"
        for name in sorted(source.keys() & copy.keys())
        if source[name] != copy[name]
    ]
    return drift


def write_copy(package_dir: Path = PACKAGE_DIR, copy_dir: Path = REPO_COPY_DIR) -> None:
    copy_dir.mkdir(parents=True, exist_ok=True)
    names = {p.name for p in package_dir.glob("*.md")}
    for stale in copy_dir.glob("*.md"):
        if stale.name not in names:
            stale.unlink()
    for name in sorted(names):
        (copy_dir / name).write_bytes((package_dir / name).read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--write", action="store_true", help="rewrite .claude/commands/kct/ from package data"
    )
    args = parser.parse_args(argv)

    if args.write:
        write_copy()
        print(f"synced {REPO_COPY_DIR.relative_to(REPO_ROOT)} from package data")
        return 0

    drift = find_drift()
    if not drift:
        print("OK: .claude/commands/kct/ matches src/kicad_tools/agent_skills/kct/")
        return 0
    for line in drift:
        print(f"DRIFT: {line}", file=sys.stderr)
    print(
        "Edit src/kicad_tools/agent_skills/kct/ and run "
        "`uv run python scripts/sync_agent_skills.py --write`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
