"""Guard: LCSC/EasyEDA STEP bodies must never be committed or shipped.

Operator ruling Q44 (issue #7686): EasyEDA/LCSC 3D models are per-user,
opt-in and fetched at use time only.  A fetched body must never end up in a
committed board, a manufacturing bundle, a wheel or a site render.  This module
is the enforcement point used by ``tests/test_lcsc_no_commit_guard.py`` and by
export/embedding code that needs to exclude LCSC-sourced bodies.

A STEP file is an LCSC body when *either*

* its sha256 equals that of a body in an LCSC cache directory (the cache's
  ``C<n>.meta.json`` records, plus a digest of every ``*.step`` there), or
* it has a sibling ``<name>.meta.json`` (or ``<name>.step.meta.json``) whose
  ``source`` is ``lcsc``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path

from kicad_tools.pcb.lcsc_models import CACHE_SOURCE, lcsc_cache_dir

__all__ = [
    "GUARDED_ROOTS",
    "STEP_SUFFIXES",
    "find_lcsc_step_violations",
    "is_lcsc_sourced",
    "lcsc_cache_digests",
    "tracked_step_files",
]

#: Repo-relative directories in which an LCSC body must never appear.
GUARDED_ROOTS: tuple[str, ...] = ("boards", "src")

STEP_SUFFIXES: frozenset[str] = frozenset({".step", ".stp"})


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def lcsc_cache_digests(cache_dir: Path | None = None) -> dict[str, str]:
    """Map sha256 -> description for every body in the LCSC cache at *cache_dir*.

    Both the digest recorded in each ``C<n>.meta.json`` (the fetch-time pin)
    and the digest of the file now on disk are included, so a replaced body is
    still caught.  A missing cache directory yields an empty map.
    """
    cache = cache_dir if cache_dir is not None else lcsc_cache_dir()
    digests: dict[str, str] = {}
    if not cache.is_dir():
        return digests
    for meta in sorted(cache.glob("C*.meta.json")):
        try:
            raw = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and isinstance(raw.get("sha256"), str):
            digests[raw["sha256"]] = f"{meta.name.removesuffix('.meta.json')} (cache metadata)"
    for step in sorted(cache.iterdir()):
        if step.suffix.lower() in STEP_SUFFIXES:
            digest = _sha256_file(step)
            if digest:
                digests[digest] = f"{step.name} (cache file)"
    return digests


def _sibling_meta_sources(step: Path) -> list[Path]:
    return [
        step.with_name(step.name + ".meta.json"),
        step.with_suffix(".meta.json"),
    ]


def is_lcsc_sourced(step: Path, digests: dict[str, str] | None = None) -> str | None:
    """Return why *step* is an LCSC body, or ``None`` if it is not one."""
    for meta in _sibling_meta_sources(step):
        try:
            raw = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and raw.get("source") == CACHE_SOURCE:
            return f"sibling {meta.name} has source={CACHE_SOURCE}"
    if digests:
        digest = _sha256_file(step)
        if digest is not None and digest in digests:
            return f"sha256 matches LCSC cache entry {digests[digest]}"
    return None


def find_lcsc_step_violations(
    paths: Iterable[Path],
    digests: dict[str, str] | None = None,
) -> list[str]:
    """Return one message per STEP file in *paths* that is an LCSC body."""
    violations: list[str] = []
    for path in paths:
        if path.suffix.lower() not in STEP_SUFFIXES:
            continue
        reason = is_lcsc_sourced(path, digests)
        if reason:
            violations.append(f"{path}: {reason}")
    return violations


def tracked_step_files(root: Path, roots: Iterable[str] = GUARDED_ROOTS) -> list[Path]:
    """STEP files tracked or staged by git under the guarded *roots* of *root*.

    Uses ``git ls-files --cached`` (tracked, including staged additions).
    Returns an empty list when *root* is not a git work tree.
    """
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "ls-files", "--cached", "-z", "--", *roots],  # noqa: S607
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    files = [root / name for name in out.decode().split("\0") if name]
    return [f for f in files if f.suffix.lower() in STEP_SUFFIXES]
