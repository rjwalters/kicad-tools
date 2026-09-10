#!/usr/bin/env python3
"""Collect a bounded diagnostic bundle for a failed board-routing CI job.

Issue #5067: four long-running CI jobs (``diffpair-routing-regression``,
``matchgroup-routing-regression``, ``board-06-end-to-end``,
``board-07-end-to-end``) re-route a board from scratch and discard the
generated PCB/schematic/DRC output on failure.  When one of those jobs
fails on CI, reproducing the failure locally previously required a full
re-route (25-90 minutes) because nothing from the failed run was
downloadable -- the motivating incident (#5044 / PR #5045) needed exactly
that: run 34524705221's Actions artifacts API reported ``total_count: 0``.

This script copies whatever generated files exist for a failed job into a
staging directory, plus a ``manifest.json`` of reproduction metadata (git
commit, seed, the exact command that was run, tool versions, and a SHA-256
hash per collected file).  It is designed to run as a CI step gated on
``if: failure()``, immediately before an ``actions/upload-artifact`` step
uploads the staging directory it populates.

Design constraints (see the issue's Refined Acceptance Criteria):

* Never fails collection because a source file/directory is missing --
  partial output is the expected case for a routing job that failed
  mid-route or mid-validation.  Missing sources are recorded in the
  manifest (``"present": false``) rather than raising.
* Never uploads more than the caller explicitly lists via ``--source`` --
  this script does not glob the repo root or copy ``.venv``/``.git``.
* Deterministic, side-effect-free with respect to the sources: files are
  copied (not moved), and the destination is always created fresh.

Usage::

    python scripts/ci/collect_failed_routing_bundle.py DEST \\
        --source boards/06-diffpair-test/regression-output \\
        --commit "$GITHUB_SHA" \\
        --seed 42 \\
        --command "python boards/06-diffpair-test/generate_design.py ... --seed 42" \\
        --python-version "$(python --version)" \\
        --kicad-version "$(kicad-cli --version)"

Each ``--source`` may be a file or a directory (copied recursively). The
destination layout mirrors each source's basename under ``DEST/`` so
sources from different roots don't collide; pass ``--source
LABEL=PATH`` to control the destination subdirectory name explicitly
(useful when two sources share a basename, e.g. two ``/tmp/board06-ci``-
shaped trees).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CollectedSource:
    """One ``--source`` entry's outcome."""

    label: str
    source: str
    present: bool
    dest_relpath: str | None
    files: list[dict[str, object]] = field(default_factory=list)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(src: Path, dest: Path) -> dict[str, object]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return {
        "path": str(dest),
        "size_bytes": dest.stat().st_size,
        "sha256": _sha256(dest),
    }


def parse_source_arg(raw: str) -> tuple[str, Path]:
    """Split a ``--source`` value into ``(label, path)``.

    Accepts either ``PATH`` (label derived from ``Path(PATH).name``) or
    ``LABEL=PATH`` (explicit label, for disambiguating same-basename
    sources such as multiple ``/tmp/board0X-*`` trees).
    """
    if "=" in raw:
        label, _, path_str = raw.partition("=")
        label = label.strip()
        if not label:
            raise ValueError(f"--source {raw!r}: label before '=' must not be empty")
        return label, Path(path_str)
    path = Path(raw)
    label = path.name or str(path)
    return label, path


def collect_source(label: str, source: Path, dest_root: Path) -> CollectedSource:
    """Copy one source (file or directory) into ``dest_root/label``.

    Never raises on a missing source -- returns ``present=False`` instead,
    per the "partial output is expected" design constraint.
    """
    if not source.exists():
        return CollectedSource(label=label, source=str(source), present=False, dest_relpath=None)

    dest_subdir = dest_root / label
    files: list[dict[str, object]] = []

    if source.is_file():
        # Always nest a single-file source under label/<filename>, so the
        # destination shape is predictable regardless of whether the label
        # was derived from the source name or passed explicitly.
        dest_path = dest_subdir / source.name
        files.append(_copy_file(source, dest_path))
    else:
        for child in sorted(source.rglob("*")):
            if child.is_file():
                rel = child.relative_to(source)
                dest_path = dest_subdir / rel
                files.append(_copy_file(child, dest_path))

    return CollectedSource(
        label=label,
        source=str(source),
        present=True,
        dest_relpath=str(dest_subdir.relative_to(dest_root)),
        files=files,
    )


def build_manifest(
    *,
    job: str,
    board: str,
    commit: str,
    seed: str | None,
    command: str,
    python_version: str | None,
    kicad_version: str | None,
    collected: list[CollectedSource],
) -> dict[str, object]:
    total_files = sum(len(c.files) for c in collected)
    return {
        "job": job,
        "board": board,
        "commit": commit,
        "seed": seed,
        "command": command,
        "python_version": python_version,
        "kicad_version": kicad_version,
        "total_files_collected": total_files,
        "sources": [
            {
                "label": c.label,
                "source": c.source,
                "present": c.present,
                "dest_relpath": c.dest_relpath,
                "files": c.files,
            }
            for c in collected
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dest", type=Path, help="Staging directory to populate (created fresh).")
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        dest="sources",
        metavar="[LABEL=]PATH",
        help=(
            "A file or directory to collect (repeatable). Missing sources are "
            "recorded, not fatal. Use LABEL=PATH to disambiguate same-basename "
            "sources."
        ),
    )
    parser.add_argument("--job", required=True, help="CI job id, e.g. board-06-end-to-end.")
    parser.add_argument(
        "--board", required=True, help="Board directory, e.g. boards/06-diffpair-test."
    )
    parser.add_argument("--commit", required=True, help="Source commit SHA (e.g. $GITHUB_SHA).")
    parser.add_argument("--seed", default=None, help="Routing seed used for the run, if any.")
    parser.add_argument(
        "--command",
        required=True,
        help="The exact command that was invoked to (re)generate the bundle.",
    )
    parser.add_argument("--python-version", default=None, help="`python --version` output.")
    parser.add_argument("--kicad-version", default=None, help="`kicad-cli --version` output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    dest: Path = args.dest
    dest.mkdir(parents=True, exist_ok=True)

    collected: list[CollectedSource] = []
    for raw_source in args.sources:
        label, path = parse_source_arg(raw_source)
        result = collect_source(label, path, dest)
        collected.append(result)
        status = "collected" if result.present else "MISSING (skipped)"
        print(f"[collect-failed-routing-bundle] {label} ({path}): {status}", flush=True)

    manifest = build_manifest(
        job=args.job,
        board=args.board,
        commit=args.commit,
        seed=args.seed,
        command=args.command,
        python_version=args.python_version,
        kicad_version=args.kicad_version,
        collected=collected,
    )
    manifest_path = dest / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(
        f"[collect-failed-routing-bundle] wrote manifest with "
        f"{manifest['total_files_collected']} file(s) to {manifest_path}",
        flush=True,
    )
    # Collection itself never fails the job -- a missing source is
    # expected (partial routing/validation output), and this step must
    # not mask or replace the job's own pass/fail steps.
    return 0


if __name__ == "__main__":
    sys.exit(main())
