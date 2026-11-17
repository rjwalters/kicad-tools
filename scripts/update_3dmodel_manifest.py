#!/usr/bin/env python3
"""Regenerate ``src/kicad_tools/data/kicad-3dmodels/manifest.json`` (#7683).

Walks the ``${KICAD10_3DMODEL_DIR}/...`` references in ``boards/**`` and
``src/kicad_tools/data/**`` footprints/boards, fetches each file from
``kicad-packages3D`` at ``PINNED_3DMODEL_TAG`` and records its sha256 plus the
upstream commit.  Needs network; run it deliberately when bumping the tag or
when a board starts referencing a new stock model.

    uv run python scripts/update_3dmodel_manifest.py [--commit <sha>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

from kicad_tools.pcb.models3d_pinned import (
    MANIFEST_PATH,
    PINNED_3DMODEL_TAG,
    _url,
    default_fetcher,
)

ROOT = Path(__file__).resolve().parents[1]
REF = re.compile(r"\$\{KICAD\d*_3DMODEL_DIR\}/([^\"\s)]+)")
UPSTREAM_GIT = "https://gitlab.com/kicad/libraries/kicad-packages3D.git"


def collect_refs() -> list[str]:
    refs: set[str] = set()
    for base in (ROOT / "boards", ROOT / "src" / "kicad_tools" / "data"):
        for pattern in ("*.kicad_pcb", "*.kicad_mod"):
            for path in base.rglob(pattern):
                refs.update(REF.findall(path.read_text(encoding="utf-8", errors="replace")))
    return sorted(refs)


def upstream_commit(tag: str) -> str:
    """Commit the *tag* points at (the peeled ``^{}`` entry for an annotated tag)."""
    lines = subprocess.run(
        ["git", "ls-remote", UPSTREAM_GIT, f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    refs: dict[str, str] = {}
    for line in lines:
        sha, _, ref = line.partition("\t")
        refs[ref] = sha
    # ``refs/tags/<tag>`` alone is the tag *object* for an annotated tag.
    return refs.get(f"refs/tags/{tag}^{{}}") or refs[f"refs/tags/{tag}"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", help="upstream commit (default: resolved from the tag)")
    args = parser.parse_args()

    files: dict[str, str] = {}
    missing: list[str] = []
    for rel in collect_refs():
        try:
            payload = default_fetcher(_url(PINNED_3DMODEL_TAG, rel))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            # A ref the pinned library does not have is a board defect the
            # audit reports (dangling); it never enters the manifest.
            missing.append(rel)
            continue
        files[rel] = hashlib.sha256(payload).hexdigest()
        print(f"{files[rel][:12]}  {rel}", file=sys.stderr)
    for rel in missing:
        print(f"absent at {PINNED_3DMODEL_TAG}: {rel}", file=sys.stderr)
    manifest = {
        "tag": PINNED_3DMODEL_TAG,
        "commit": args.commit or upstream_commit(PINNED_3DMODEL_TAG),
        "source": UPSTREAM_GIT,
        "files": files,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {MANIFEST_PATH} ({len(files)} files)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
