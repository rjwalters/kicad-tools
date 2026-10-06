"""``kct check --diff OLD NEW`` -- findings introduced / resolved between revisions (Issue #5946).

Both revisions are checked with the *same* flags (everything on the command
line except ``--diff`` / ``--format`` / ``--output``), each producing the
normal ``kct check`` JSON report, and the two reports are compared by stable
finding key (:mod:`kicad_tools.validate.evidence`):

* ``introduced`` -- present in NEW, absent from OLD;
* ``resolved``   -- present in OLD, absent from NEW;
* ``changed``    -- same key on both sides, but the local evidence moved (the
  evidence hash differs), e.g. a clearance shortfall whose gap changed;
* ``unchanged``  -- same key and same evidence.

Keys are compared as multisets, so two findings that share a key are paired
one-to-one.  Waived findings are excluded on both sides: a waiver is a
reviewed decision, not a regression.

Each side is either a path (``.kicad_pcb`` or a directory containing one) or
a git revision spec ``REV:path`` (``HEAD~3:boards/x/x.kicad_pcb``; a
``./``-prefixed path is relative to the current directory, otherwise to the
repository root, mirroring ``git show``).  A revision is materialised by
exporting the board's whole directory at that revision, so sidecars that live
next to the board (net-class map, waivers, ``.kicad_pro``) come along.

Exit codes: ``0`` no new error-severity findings (and no new warnings under
``--strict``); ``2`` regressions introduced; ``1`` a side could not be
checked.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from kicad_tools.cli.format_options import stdout_to_stderr_when

# Options whose values are owned by the diff driver, not forwarded per side.
_DRIVER_OPTIONS = {"--diff": 2, "--format": 1, "--output": 1, "-o": 1}


def _match_driver_option(token: str) -> tuple[str, bool] | None:
    """Resolve ``token`` to a driver option (honouring argparse prefixes)."""
    if token == "-o":
        return "-o", False
    if token.startswith("-o") and not token.startswith("--"):
        return "-o", True  # ``-oFILE``
    if not token.startswith("--"):
        return None
    name, has_eq, _ = token.partition("=")
    for option in ("--diff", "--format", "--output"):
        # argparse accepts unique prefixes; the argv already parsed, so a
        # prefix of one of these is that option.
        if len(name) > 3 and option.startswith(name):
            return option, bool(has_eq)
    return None


def strip_driver_args(argv: Sequence[str]) -> list[str]:
    """Drop ``--diff A B``, ``--format X`` and ``--output X`` from ``argv``."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            out.extend(argv[i:])
            break
        match = _match_driver_option(token)
        if match is None:
            out.append(token)
            i += 1
            continue
        option, inline = match
        i += 1 if inline else 1 + _DRIVER_OPTIONS[option]
    return out


class DiffSideError(Exception):
    """A diff side could not be resolved or checked."""


def _git(args: Sequence[str], cwd: Path) -> bytes:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise DiffSideError(proc.stderr.decode(errors="replace").strip() or "git failed")
    return proc.stdout


def _repo_relative(rel: str, cwd: Path, root: Path, label: str, spec: str) -> Path:
    """Return ``rel`` as a normalised repo-relative path, refusing escapes."""
    root_resolved = root.resolve()
    if rel.startswith("./") or rel.startswith("../"):
        candidate = (cwd / rel).resolve()
    else:
        if Path(rel).is_absolute():
            raise DiffSideError(f"{label}: {spec!r}: path must be relative to the repository")
        # Lexical normalisation only: the path names a file *in the
        # revision*, so working-tree symlinks must not be followed.
        candidate = Path(os.path.normpath(root_resolved / rel))
    try:
        rel_path = candidate.relative_to(root_resolved)
    except ValueError:
        raise DiffSideError(f"{label}: {spec!r}: path escapes the repository root") from None
    if rel_path == Path("."):
        raise DiffSideError(f"{label}: {spec!r}: path names the repository root, not a board")
    return rel_path


def resolve_side(spec: str, scratch: Path, label: str) -> Path:
    """Return a local path for ``spec`` (a path, or ``REV:path`` git spec)."""
    as_path = Path(spec)
    if as_path.exists():
        return as_path.resolve()
    rev, sep, rel = spec.partition(":")
    if not sep or not rev or not rel:
        raise DiffSideError(f"{label}: path not found: {spec}")
    cwd = Path.cwd()
    try:
        root = Path(_git(["rev-parse", "--show-toplevel"], cwd).decode().strip())
    except (DiffSideError, OSError) as e:
        raise DiffSideError(f"{label}: {spec!r} is not a path, and not in a git repo: {e}") from e
    if rev.startswith("-"):
        # A leading dash would be parsed by git as an option (e.g.
        # ``--output=FILE`` makes ``git archive`` write/truncate FILE).
        raise DiffSideError(f"{label}: invalid git revision {rev!r} (must not start with '-')")
    rel_path = _repo_relative(rel, cwd, root, label, spec)
    parent = rel_path.parent.as_posix() or "."
    try:
        tree = (
            _git(["rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{tree}}"], root)
            .decode()
            .strip()
        )
    except DiffSideError as e:
        raise DiffSideError(f"{label}: unknown git revision {rev!r}") from e
    if not tree or tree.startswith("-"):  # pragma: no cover - defensive
        raise DiffSideError(f"{label}: unknown git revision {rev!r}")
    try:
        blob = _git(["archive", "--format=tar", tree, "--", parent], root)
    except DiffSideError as e:
        raise DiffSideError(f"{label}: cannot export {parent} at {rev}: {e}") from e
    dest = scratch / label
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # pragma: no cover - Python without extraction filters
            tar.extractall(dest)  # noqa: S202 - our own repository content
    target = dest / rel_path
    if not target.exists():
        raise DiffSideError(f"{label}: {rel_path} does not exist at {rev}")
    return target


def _active(report: dict) -> list[dict]:
    return [v for v in report.get("violations", []) if isinstance(v, dict) and not v.get("waived")]


def _brief(v: dict) -> dict:
    return {
        "key": v.get("key"),
        "rule_id": v.get("rule_id"),
        "severity": v.get("severity"),
        "message": v.get("message"),
        "items": v.get("items", []),
        "nets": v.get("nets", []),
        "layer": v.get("layer"),
        "location": v.get("location"),
        "evidence_hash": v.get("evidence_hash"),
    }


def diff_reports(old: dict, new: dict) -> dict[str, Any]:
    """Compare two ``kct check`` JSON reports by finding key."""
    by_key_old: dict[str, list[dict]] = {}
    by_key_new: dict[str, list[dict]] = {}
    for v in _active(old):
        by_key_old.setdefault(str(v.get("key")), []).append(v)
    for v in _active(new):
        by_key_new.setdefault(str(v.get("key")), []).append(v)

    introduced: list[dict] = []
    resolved: list[dict] = []
    changed: list[dict] = []
    unchanged = 0
    for key in sorted(set(by_key_old) | set(by_key_new)):
        olds = list(by_key_old.get(key, []))
        news = list(by_key_new.get(key, []))
        # Pair identical evidence first.
        remaining_new: list[dict] = []
        for v in news:
            twin = next((o for o in olds if o.get("evidence_hash") == v.get("evidence_hash")), None)
            if twin is not None:
                olds.remove(twin)
                unchanged += 1
            else:
                remaining_new.append(v)
        # Same key, different evidence: the finding persisted but moved.
        while olds and remaining_new:
            o = olds.pop(0)
            n = remaining_new.pop(0)
            changed.append({"key": key, "old": _brief(o), "new": _brief(n)})
        introduced.extend(_brief(v) for v in remaining_new)
        resolved.extend(_brief(v) for v in olds)

    old_raw, new_raw = old.get("coverage"), new.get("coverage")
    old_cov: dict = old_raw if isinstance(old_raw, dict) else {}
    new_cov: dict = new_raw if isinstance(new_raw, dict) else {}
    coverage_changes = {}
    for name in sorted(set(old_cov) | set(new_cov)):
        o_status = (old_cov.get(name) or {}).get("status")
        n_status = (new_cov.get(name) or {}).get("status")
        if o_status != n_status:
            coverage_changes[name] = {"old": o_status, "new": n_status}

    def _count(rows: list[dict], severity: str) -> int:
        return sum(1 for r in rows if r.get("severity") == severity)

    return {
        "summary": {
            "introduced": len(introduced),
            "resolved": len(resolved),
            "changed": len(changed),
            "unchanged": unchanged,
            "introduced_errors": _count(introduced, "error"),
            "introduced_warnings": _count(introduced, "warning"),
            "resolved_errors": _count(resolved, "error"),
            "resolved_warnings": _count(resolved, "warning"),
            "coverage_changes": len(coverage_changes),
        },
        "introduced": introduced,
        "resolved": resolved,
        "changed": changed,
        "coverage_changes": coverage_changes,
    }


def _print_rows(title: str, rows: list[dict]) -> None:
    print(f"{title} ({len(rows)}):")
    for row in rows:
        print(f"  [{row.get('severity')}] {row.get('rule_id')}: {row.get('message')}")
        print(f"      key={row.get('key')}")


def _render_text(result: dict, fmt: str) -> None:
    s = result["summary"]
    print(f"kct check --diff {result['old']['spec']} -> {result['new']['spec']}")
    print(
        f"  introduced: {s['introduced']} ({s['introduced_errors']} error(s), "
        f"{s['introduced_warnings']} warning(s))"
    )
    print(
        f"  resolved:   {s['resolved']} ({s['resolved_errors']} error(s), "
        f"{s['resolved_warnings']} warning(s))"
    )
    print(f"  changed:    {s['changed']} (same finding, evidence moved)")
    print(f"  unchanged:  {s['unchanged']}")
    if fmt == "summary":
        return
    if result["introduced"]:
        print()
        _print_rows("INTRODUCED", result["introduced"])
    if result["resolved"]:
        print()
        _print_rows("RESOLVED", result["resolved"])
    if result["coverage_changes"]:
        print()
        print("COVERAGE CHANGES:")
        for name, change in result["coverage_changes"].items():
            print(f"  {name}: {change['old']} -> {change['new']}")


def run_diff(
    old_spec: str,
    new_spec: str,
    passthrough: Sequence[str],
    *,
    fmt: str,
    output: str | None,
    strict: bool,
    check_main: Callable[[list[str]], int],
) -> int:
    """Check both sides with ``passthrough`` flags and report the difference."""
    with tempfile.TemporaryDirectory(prefix="kct-check-diff-") as tmp:
        scratch = Path(tmp)
        reports: dict[str, dict] = {}
        sides: dict[str, dict] = {}
        for label, spec in (("old", old_spec), ("new", new_spec)):
            try:
                path = resolve_side(spec, scratch, label)
            except DiffSideError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            report_path = scratch / f"{label}-report.json"
            # The machine-readable report goes to ``--output``; the side's own
            # human summary is diverted to stderr so stdout carries the diff
            # alone (``--format json`` purity, #5938).
            argv = [str(path), *passthrough, "--format", "summary", "--output", str(report_path)]
            print(f"[INFO] kct check --diff: checking {label} side {spec}", file=sys.stderr)
            with stdout_to_stderr_when(True):
                try:
                    code = check_main(argv)
                except SystemExit as exc:
                    code = int(exc.code or 0) if isinstance(exc.code, int) else 1
            if code == 1 or not report_path.is_file():
                print(f"Error: kct check failed on the {label} side ({spec})", file=sys.stderr)
                return 1
            reports[label] = json.loads(report_path.read_text())
            sides[label] = {
                "spec": spec,
                "file": reports[label].get("file"),
                "exit_code": code,
                "summary": reports[label].get("summary", {}),
            }

    result = {
        "old": sides["old"],
        "new": sides["new"],
        **diff_reports(reports["old"], reports["new"]),
    }
    if fmt == "json":
        print(json.dumps(result, indent=2))
    else:
        _render_text(result, fmt)
    if output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n")

    s = result["summary"]
    if s["introduced_errors"] or (strict and s["introduced_warnings"]):
        return 2
    return 0
