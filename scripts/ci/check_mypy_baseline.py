#!/usr/bin/env python3
"""Mypy baseline gate for CI (issue #3512).

Runs ``mypy`` over the source tree, compares the resulting errors against a
committed baseline (``.github/mypy-baseline.txt``), and fails ONLY when there
are NEW errors beyond the baseline.  This is the type-checking analogue of the
DRC tolerance allowlist (``scripts/ci/check_routed_drc.py`` /
``.github/routed-drc-tolerance.yml``): it lets the Type Check job gate against
regressions immediately, without requiring the existing 1500+ errors to be
fixed in one mega-PR.

Why a normalized baseline rather than a raw diff
------------------------------------------------
Mypy reports errors as ``path:line: error: message  [code]``.  Keying on the
*exact* ``path:line`` would treat every refactor that shifts line numbers as
hundreds of "new" errors (and hundreds of "fixed" ones), making the gate
useless.  Instead we normalize each error to a stable *signature*:

    <path>\t<error-code>\t<normalized-message>

The line number is dropped, and volatile substrings in the message (numbers,
quoted identifiers, temp module paths) are masked so a signature stays stable
across unrelated edits.  We then compare *multisets* of signatures: the
current run may contain each baseline signature at most as many times as the
baseline does.  Any signature that appears more often than the baseline (or
that the baseline does not contain at all) is a NEW error and fails the gate.

This means:
  * Fixing an error (signature count drops) -> gate passes (and nags to
    tighten the baseline, mirroring the DRC drift warning).
  * Adding a brand-new error -> gate fails.
  * Moving code around (line numbers shift) -> gate passes.

Burn-down intent
----------------
The baseline is a debt ledger, not a target.  When you fix type errors, run
``--update`` (or ``--write-baseline``) to regenerate ``.github/mypy-baseline.txt``
in the same PR so the floor ratchets down and the fixed errors can never
silently come back.  The end state is an empty baseline and the eventual
removal of this wrapper in favour of a bare ``mypy`` invocation.

Exit codes:
    0 -- No new errors (job passes).  Stale-baseline entries (errors that no
         longer occur) are surfaced as warnings but do not fail the gate.
    1 -- Tool failure (mypy could not run, baseline unreadable, etc.).
    2 -- New errors beyond the baseline (job fails).

GitHub-Actions annotations (``::error file=...::``) are emitted for each new
error so the PR Files-changed view surfaces the regression inline.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

DEFAULT_BASELINE = Path(".github/mypy-baseline.txt")
DEFAULT_TARGET = "src/"

# Mypy error lines look like:
#   src/pkg/mod.py:248: error: Function "builtins.any" is ...  [valid-type]
# We deliberately ignore ``note:`` lines (they are follow-ups to an error,
# not independently gateable) and the trailing ``Found N errors`` summary.
_ERROR_LINE_RE = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+):(?:\d+:)?\s+error:\s+(?P<message>.*?)\s*(?:\[(?P<code>[a-z0-9-]+)\])?$"
)

# Substrings inside an error message that are volatile across unrelated edits
# and must be masked so a signature stays stable.
_NUMBER_RE = re.compile(r"\b\d+\b")


def normalize_message(message: str) -> str:
    """Mask volatile substrings in a mypy error message.

    The goal is a signature that is stable across edits that do not change the
    *nature* of the error.  Bare integers (array sizes, argument counts,
    line/column references mypy sometimes embeds) are masked to ``N`` because
    they routinely shift without the error meaningfully changing.  Quoted
    identifiers are intentionally PRESERVED -- they carry the semantic content
    of the error (e.g. which attribute is missing) and dropping them would
    collapse distinct errors into one signature, letting genuinely new errors
    hide behind an existing baseline entry.
    """
    return _NUMBER_RE.sub("N", message).strip()


def signature(path: str, message: str, code: str) -> str:
    """Build a stable, line-number-independent signature for one error.

    Format: ``<path>\\t<code>\\t<normalized-message>``.  Path is included so
    the same error class in two files counts as two distinct signatures, and
    so a new error in file A can't be masked by a since-fixed error in file B.
    """
    return f"{path}\t{code}\t{normalize_message(message)}"


def parse_mypy_output(output: str) -> Counter[str]:
    """Parse raw mypy stdout into a multiset of error signatures.

    Non-error lines (``note:`` follow-ups, the ``Found N errors`` summary,
    blank lines) are ignored.  Returns a :class:`collections.Counter` mapping
    each signature to the number of times it occurred, so duplicate errors in
    the same file are counted independently.
    """
    counts: Counter[str] = Counter()
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        m = _ERROR_LINE_RE.match(line)
        if not m:
            continue
        path = m.group("path")
        message = m.group("message")
        code = m.group("code") or ""
        counts[signature(path, message, code)] += 1
    return counts


def run_mypy(target: str) -> str:
    """Run mypy over ``target`` and return its combined stdout+stderr.

    Mypy exits 1 when it finds errors and 0 when clean; both are normal here.
    A different/failed invocation (e.g. config error) is detected by the
    caller via the absence of parseable error lines AND a non-summary tail.
    """
    proc = subprocess.run(
        ["mypy", target],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout + proc.stderr


def load_baseline(baseline_path: Path) -> Counter[str]:
    """Load the committed baseline file into a multiset of signatures.

    Lines starting with ``#`` and blank lines are ignored (so the file can
    carry a header explaining the burn-down contract).  A missing baseline is
    treated as empty: every error then counts as new (strict mode).
    """
    counts: Counter[str] = Counter()
    if not baseline_path.exists():
        return counts
    for raw_line in baseline_path.read_text().splitlines():
        line = raw_line.rstrip("\n")
        if not line or line.lstrip().startswith("#"):
            continue
        counts[line] += 1
    return counts


_BASELINE_HEADER = """\
# Mypy baseline -- committed debt ledger for the CI Type Check gate (issue #3512).
#
# Each non-comment line is a NORMALIZED mypy error signature:
#     <path>\\t<error-code>\\t<message-with-numbers-masked>
# Line numbers are intentionally absent so the gate survives refactors that
# shift code around.  scripts/ci/check_mypy_baseline.py compares the current
# mypy run against this multiset and fails CI only on NEW errors.
#
# BURN-DOWN INTENT: this is debt, not a target.  When you fix type errors,
# regenerate this file in the same PR:
#     uv run python scripts/ci/check_mypy_baseline.py --update
# so the floor ratchets down and the fixed errors cannot silently return.
# The end state is an empty baseline and removal of the wrapper.
#
# DO NOT hand-edit. DO NOT add entries to silence a NEW error -- fix the type
# error or, if it is a false positive, add a targeted ``# type: ignore[code]``
# with a comment, which removes it from the report entirely.
"""


def write_baseline(baseline_path: Path, counts: Counter[str]) -> None:
    """Write the baseline file: header + sorted signatures (one per repeat)."""
    lines = [_BASELINE_HEADER]
    for sig in sorted(counts.elements()):
        lines.append(sig)
    baseline_path.write_text("\n".join(lines) + "\n")


def annotate_error(path: str, message: str) -> None:
    """Emit a GitHub-Actions ``::error file=...::`` annotation."""
    print(f"::error file={path}::{message}", flush=True)


def diff_against_baseline(
    current: Counter[str], baseline: Counter[str]
) -> tuple[Counter[str], Counter[str]]:
    """Return ``(new_errors, fixed_errors)`` multisets.

    ``new_errors`` are signatures occurring MORE often in ``current`` than in
    ``baseline`` (these fail the gate).  ``fixed_errors`` are signatures
    occurring LESS often than the baseline allows (these are surfaced as
    stale-baseline warnings so the ledger can be tightened).
    """
    new_errors = current - baseline  # Counter subtraction clamps at 0
    fixed_errors = baseline - current
    return new_errors, fixed_errors


def _render_signature(sig: str) -> tuple[str, str]:
    """Split a signature back into ``(path, human_message)`` for reporting."""
    parts = sig.split("\t")
    if len(parts) == 3:
        path, code, message = parts
        suffix = f"  [{code}]" if code else ""
        return path, f"{message}{suffix}"
    return "<unknown>", sig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_mypy_baseline",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE),
        help=f"Path to the baseline file (default: {DEFAULT_BASELINE}).",
    )
    parser.add_argument(
        "--target",
        default=DEFAULT_TARGET,
        help=f"Path mypy checks (default: {DEFAULT_TARGET!r}).",
    )
    parser.add_argument(
        "--update",
        "--write-baseline",
        dest="update",
        action="store_true",
        help="Regenerate the baseline file from the current mypy run and exit 0. "
        "Use this when burning down errors so the floor ratchets to the new count.",
    )
    parser.add_argument(
        "--mypy-output",
        default=None,
        help="Read mypy output from this file instead of invoking mypy "
        "(used by the test suite to feed synthetic runs).",
    )
    args = parser.parse_args(argv)

    baseline_path = Path(args.baseline)

    if args.mypy_output is not None:
        raw = Path(args.mypy_output).read_text()
    else:
        raw = run_mypy(args.target)

    current = parse_mypy_output(raw)

    # Guard against a broken mypy invocation masquerading as "0 errors".
    # If mypy emitted output but we parsed nothing AND the output does not look
    # like a clean run, treat it as a tool failure rather than a green gate.
    if not current and raw.strip() and "no issues found" not in raw.lower():
        if "Found 0 errors" not in raw:
            print("::error::mypy produced output but no parseable error lines:", flush=True)
            print(raw, flush=True)
            return 1

    if args.update:
        write_baseline(baseline_path, current)
        print(
            f"Wrote baseline with {sum(current.values())} error(s) "
            f"({len(current)} unique signature(s)) to {baseline_path}.",
            flush=True,
        )
        return 0

    baseline = load_baseline(baseline_path)
    new_errors, fixed_errors = diff_against_baseline(current, baseline)

    if fixed_errors:
        n_fixed = sum(fixed_errors.values())
        print(
            f"::warning::{n_fixed} baseline error(s) no longer occur. Tighten "
            f"the floor by running `uv run python {Path(__file__).name} --update` "
            f"(or `scripts/ci/{Path(__file__).name} --update`) and committing "
            f"the updated {baseline_path}.",
            flush=True,
        )

    if new_errors:
        n_new = sum(new_errors.values())
        print(
            f"\n{n_new} NEW mypy error(s) beyond the baseline ({baseline_path}):\n",
            flush=True,
        )
        for sig in sorted(new_errors.elements()):
            path, human = _render_signature(sig)
            annotate_error(path, f"new type error: {human}")
            print(f"  {path}: {human}", flush=True)
        print(
            f"\nType Check gate failed (exit 2). Fix the new error(s) above, or "
            f"-- if a finding is a genuine false positive -- add a targeted "
            f"`# type: ignore[<code>]`. Do NOT add the error to "
            f"{baseline_path} to silence it.",
            flush=True,
        )
        return 2

    total = sum(current.values())
    print(
        f"OK: mypy reports {total} error(s), all within the baseline "
        f"({sum(baseline.values())} allowed). No new type errors.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
