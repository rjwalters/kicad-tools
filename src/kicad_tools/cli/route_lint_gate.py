"""``--lint-gate``: roll back a route that introduces new lint findings (Issue #6054).

``kct route --lint-gate`` and ``kct route-auto --lint-gate`` lint the input
board before routing and the routed board after it, and compare the two by
stable finding key with :func:`kicad_tools.cli.check_diff.diff_reports` --
the same pairing ``kct check --diff`` uses (Issue #5946).

What is linted
    ``kct check`` (the JSON report it writes with ``--output``) **and**
    ``kct detect-mistakes`` (its ``mistake.*`` findings carry the same
    ``key`` / ``evidence_hash`` identity since Issue #6006).  Both are merged
    into one finding list per side, so a single diff covers both linters.

Sidecars are pinned to the input board
    The routed board usually has another name (``<board>_routed``), so the
    stem-keyed sidecars ``kct check`` auto-discovers (``<board>.kct-waivers.json``,
    ``<board>.net_class_map.json``, ``<board>.current_paths.json``) would not be
    found for it.  The gate resolves them **once, from the input board, before
    routing**, and passes the same explicit paths to every lint run on both
    sides.  ``--lint-gate-waivers PATH`` overrides the waivers file.  With no
    waivers sidecar at all, an empty one is pinned so neither side picks up a
    stray file.

Waivers
    Waived findings are excluded on both sides by ``diff_reports``.  An
    evidence-bound waiver only matches the finding it was reviewed against,
    so the usual workflow is: the gate rolls back and keeps the rejected
    board at ``<output-stem>.lint-rejected.kicad_pcb``; review it, then
    ``kct check <rejected> --waivers <W> --waive KEY ...`` (or
    ``kct detect-mistakes ... --waive KEY ...`` for ``mistake.*`` keys) --
    the exact command is printed.  The next run with the same copper passes.
    ``kct check`` keys for copper findings name the track/via UUIDs, so a
    waiver recorded against one routing run matches a later run only when
    that run reproduces the same copper.

Verdict and rollback
    ``introduced_errors > 0`` rolls back; with ``--lint-gate-strict`` (or
    ``kct route --strict``) ``introduced_warnings > 0`` does too.  Rolling
    back restores ``--output`` to exactly the bytes it held before the run
    (or removes it when it did not exist -- for an in-place route that is
    the input board) and exits **3**, the route rollback exit code shared
    with the connectivity rollback (Issue #2839).  A routed board that
    cannot be linted fails closed: it is rolled back too, also exit 3.  A
    baseline that cannot be linted aborts before routing with exit 1.

Not gated
    ``--checkpoint`` files (best-so-far intermediates; a later ``--resume``
    is gated when *its* run finishes), the ``_partial`` snapshot of an
    interrupted run, and a supervised ``--timeout`` (the supervisor already
    quarantines the output as unverified).  ``--dry-run`` writes nothing and
    skips the gate.  The baseline is always the board named on the command
    line, never the ``--resume`` checkpoint.

Machine output
    The lint runs never write to stdout.  Under ``--format json`` the
    command's single document gains a ``lint_gate`` object (see
    :meth:`GateOutcome.to_dict`).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.cli.check_diff import diff_reports

#: Exit code of a run whose routed board the gate rolled back.  Shared with
#: the connectivity rollback (Issue #2839) and the other post-route gates.
LINT_GATE_EXIT = 3

#: Suffix inserted before ``.kicad_pcb`` for the rejected board kept on rollback.
REJECTED_INFIX = ".lint-rejected"

_PROJECT_SUFFIXES = (".kicad_pro", ".kicad_dru")


class LintGateError(Exception):
    """A board could not be linted."""


@dataclass
class GateOutcome:
    """The verdict of one gated run."""

    status: str  # "pass" | "rolled_back" | "skipped" | "error"
    reason: str = ""
    strict: bool = False
    summary: dict[str, Any] = field(default_factory=dict)
    introduced: list[dict[str, Any]] = field(default_factory=list)
    rejected_board: str | None = None
    output_restored: str | None = None  # "previous" | "removed" | None
    waivers: str | None = None

    @property
    def rolled_back(self) -> bool:
        return self.status in ("rolled_back", "error")

    def exit_code(self, rc: int) -> int:
        """Map the routing exit code through the gate verdict."""
        return LINT_GATE_EXIT if self.rolled_back else rc

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "strict": self.strict,
            "summary": self.summary,
            "introduced": self.introduced,
            "rejected_board": self.rejected_board,
            "output_restored": self.output_restored,
            "waivers": self.waivers,
        }


def _replay(buffer: io.StringIO) -> None:
    text = buffer.getvalue()
    if text:
        sys.stderr.write(text)


def _mistake_violation(m: dict) -> dict:
    """A ``detect-mistakes`` JSON finding in ``kct check`` report shape."""
    key = m.get("key") or ""
    parts = key.split("|")
    items = [p for p in parts[1].split(",") if p] if len(parts) == 4 else []
    nets = [p for p in parts[2].split(",") if p] if len(parts) == 4 else []
    layer = (parts[3] or None) if len(parts) == 4 else None
    return {
        "key": key,
        "rule_id": m.get("rule_id") or f"mistake.{m.get('category')}",
        "severity": m.get("severity"),
        "message": m.get("title"),
        "items": items,
        "nets": nets,
        "layer": layer,
        "location": m.get("location"),
        "evidence_hash": m.get("evidence_hash"),
        "waived": bool(m.get("waived")),
    }


class LintGate:
    """Baseline-then-candidate lint of a routing run, with rollback."""

    def __init__(
        self,
        source: Path,
        output: Path,
        *,
        check_flags: Sequence[str] = (),
        waivers: Path | None = None,
        strict: bool = False,
        quiet: bool = False,
        command: str = "kct route",
        check_main: Callable[[list[str]], int] | None = None,
        mistakes_main: Callable[[list[str]], int] | None = None,
    ) -> None:
        self.source = Path(source)
        self.output = Path(output)
        self.check_flags = list(check_flags)
        self.explicit_waivers = Path(waivers) if waivers is not None else None
        self.strict = strict
        self.quiet = quiet
        self.command = command
        self._check_main = check_main
        self._mistakes_main = mistakes_main
        self._scratch: tempfile.TemporaryDirectory[str] | None = None
        self._before: bytes | None = None
        self._baseline: dict | None = None
        self.waivers: Path | None = None
        self._waivers_pinned_empty = False
        self._flags: list[str] = []

    # -- construction ------------------------------------------------------
    @classmethod
    def from_route_args(cls, args, argv: Sequence[str] | None = None) -> LintGate:
        """Build the gate for ``kct route`` (``route_cmd`` inner args)."""
        from .route_cmd import _flag_passed_explicitly

        source = Path(args.pcb)
        output = Path(args.output) if args.output else source.with_stem(source.stem + "_routed")
        flags: list[str] = []
        argv_list = list(argv) if argv is not None else None
        if _flag_passed_explicitly(argv_list, ("--manufacturer", "--mfr")):
            flags += ["--mfr", str(args.manufacturer)]
        if getattr(args, "copper", None):
            flags += ["--copper", str(args.copper)]
        if getattr(args, "net_class_map", None):
            flags += ["--net-class-map", str(Path(args.net_class_map).resolve())]
        if getattr(args, "current_paths", None):
            flags += ["--current-paths", str(Path(args.current_paths).resolve())]
        elif getattr(args, "no_current_paths", False):
            flags.append("--no-current-paths")
        waivers = getattr(args, "lint_gate_waivers", None)
        return cls(
            source,
            output,
            check_flags=flags,
            waivers=Path(waivers) if waivers else None,
            strict=bool(getattr(args, "lint_gate_strict", False) or getattr(args, "strict", False)),
            quiet=bool(getattr(args, "quiet", False)),
            command="kct route",
        )

    @classmethod
    def from_route_auto_args(cls, args) -> LintGate:
        """Build the gate for ``kct route-auto`` (requires ``--output``)."""
        waivers = getattr(args, "lint_gate_waivers", None)
        return cls(
            Path(args.pcb),
            Path(args.output),
            waivers=Path(waivers) if waivers else None,
            strict=bool(getattr(args, "lint_gate_strict", False)),
            quiet=bool(getattr(args, "quiet", False) or getattr(args, "global_quiet", False)),
            command="kct route-auto",
        )

    # -- output ------------------------------------------------------------
    def _say(self, msg: str) -> None:
        if not self.quiet:
            print(f"[lint-gate] {msg}", file=sys.stderr)

    def _err(self, msg: str) -> None:
        print(f"[lint-gate] {msg}", file=sys.stderr)

    # -- sidecar pinning ---------------------------------------------------
    def _pin_sidecars(self, scratch: Path) -> None:
        from kicad_tools.router.current_paths import discover_current_paths_sidecar
        from kicad_tools.validate.rules.courtyard_waivers import (
            discover_courtyard_waivers_sidecar,
        )
        from kicad_tools.validate.rules.waivers import discover_waivers_sidecar

        from .check_cmd import _discover_net_class_map_sidecar

        flags = list(self.check_flags)
        if "--net-class-map" not in flags:
            ncm = _discover_net_class_map_sidecar(self.source)
            flags += ["--net-class-map", str(ncm.resolve())] if ncm else ["--no-net-class-map"]
        if "--current-paths" not in flags and "--no-current-paths" not in flags:
            cp = discover_current_paths_sidecar(self.source)
            flags += ["--current-paths", str(cp.resolve())] if cp else ["--no-current-paths"]
        cw = discover_courtyard_waivers_sidecar(self.source)
        if cw is not None:
            flags += ["--courtyard-waivers", str(cw.resolve())]

        waivers = self.explicit_waivers
        if waivers is not None:
            if not waivers.is_file():
                raise LintGateError(f"--lint-gate-waivers file not found: {waivers}")
        else:
            waivers = discover_waivers_sidecar(self.source)
        if waivers is None:
            waivers = scratch / "no-waivers.kct-waivers.json"
            waivers.write_text(json.dumps({"version": 3, "waivers": []}) + "\n")
            self._waivers_pinned_empty = True
        self.waivers = waivers.resolve()
        self._flags = flags

    # -- linting -----------------------------------------------------------
    def _run(self, main: Callable[[list[str]], int], argv: list[str], what: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = main(argv)
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        except BaseException:
            _replay(err)
            raise
        if code == 1:
            # Tool failure: the captured diagnostics are the only clue.
            _replay(err)
            raise LintGateError(f"{what} failed (exit 1)")
        return int(code or 0), out.getvalue()

    def lint(self, board: Path, label: str) -> dict:
        """Merged ``kct check`` + ``detect-mistakes`` findings for ``board``."""
        assert self._scratch is not None and self.waivers is not None
        from .check_cmd import main as default_check_main
        from .mistakes_cmd import main as default_mistakes_main

        check_main = self._check_main or default_check_main
        mistakes_main = self._mistakes_main or default_mistakes_main

        scratch = Path(self._scratch.name)
        report_path = scratch / f"{label}-check.json"
        report_path.unlink(missing_ok=True)
        argv = [
            str(board),
            *self._flags,
            "--waivers",
            str(self.waivers),
            "--format",
            "summary",
            "--output",
            str(report_path),
        ]
        self._run(check_main, argv, f"kct check on the {label} board {board}")
        if not report_path.is_file():
            raise LintGateError(f"kct check wrote no report for the {label} board {board}")
        report = json.loads(report_path.read_text())

        _code, text = self._run(
            mistakes_main,
            [str(board), "--format", "json", "--waivers", str(self.waivers)],
            f"kct detect-mistakes on the {label} board {board}",
        )
        try:
            mistakes = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            raise LintGateError(f"kct detect-mistakes JSON for the {label} board: {e}") from e

        violations = list(report.get("violations", []))
        violations += [_mistake_violation(m) for m in mistakes.get("mistakes", [])]
        coverage = dict(report.get("coverage") or {})
        for c in mistakes.get("coverage", []):
            coverage[f"mistake:{c.get('check_name')}"] = {"status": c.get("status")}
        return {"violations": violations, "coverage": coverage}

    # -- the two phases ----------------------------------------------------
    def begin(self) -> str | None:
        """Snapshot ``--output`` and lint the input board.

        Returns an error message (the caller aborts with exit 1, before any
        routing) or ``None``.
        """
        from .route_deadline import record_stage

        if not self.source.is_file():
            return None  # the router reports the missing input itself
        self._scratch = tempfile.TemporaryDirectory(prefix="kct-lint-gate-")
        try:
            self._before = self.output.read_bytes() if self.output.is_file() else None
            self._pin_sidecars(Path(self._scratch.name))
            record_stage("lint-gate-baseline")
            self._say(f"linting input board {self.source} (kct check + detect-mistakes)")
            self._baseline = self.lint(self.source, "baseline")
        except (LintGateError, OSError, ValueError) as e:
            self.close()
            return f"[lint-gate] cannot lint the input board, nothing was routed: {e}"
        active = sum(1 for v in self._baseline["violations"] if not v.get("waived"))
        self._say(f"baseline: {active} active finding(s); waivers: {self._waivers_label()}")
        return None

    def _waivers_label(self) -> str:
        return "none" if self._waivers_pinned_empty else str(self.waivers)

    def finish(self) -> GateOutcome:
        """Lint the routed board and roll it back when it regressed."""
        try:
            return self._finish()
        except BaseException:
            # A deadline or interrupt while judging must not leave an
            # unjudged board at --output.
            with contextlib.suppress(Exception):
                if self._fresh():
                    self._restore()
            raise
        finally:
            self.close()

    def abort(self) -> None:
        """Routing raised: restore ``--output`` (it was never judged) and clean up."""
        try:
            if self._baseline is not None and self._fresh():
                restored = self._restore()
                self._err(f"routing aborted; {self._restored_text(restored)}")
        except OSError:
            pass
        finally:
            self.close()

    def close(self) -> None:
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None

    def _fresh(self) -> bool:
        if not self.output.is_file():
            return False
        return self.output.read_bytes() != self._before

    def _finish(self) -> GateOutcome:
        from .route_deadline import record_stage

        waivers = None if self._waivers_pinned_empty else str(self.waivers)
        if self._baseline is None:
            return GateOutcome("skipped", "input board was not linted", self.strict)
        if not self._fresh():
            self._say("no routed board was written; nothing to gate")
            return GateOutcome("skipped", "no routed board written", self.strict, waivers=waivers)
        record_stage("lint-gate")
        self._say(f"linting routed board {self.output}")
        try:
            candidate = self.lint(self.output, "candidate")
        except (LintGateError, OSError, ValueError) as e:
            rejected, restored = self._reject()
            self._err(
                f"ROLLED BACK: the routed board could not be linted ({e}); "
                f"kept at {rejected}, {self._restored_text(restored)}"
            )
            return GateOutcome(
                "error",
                f"routed board could not be linted: {e}",
                self.strict,
                rejected_board=str(rejected),
                output_restored=restored,
                waivers=waivers,
            )
        diff = diff_reports(self._baseline, candidate)
        summary = diff["summary"]
        blocking = [
            row
            for row in diff["introduced"]
            if row.get("severity") == "error" or (self.strict and row.get("severity") == "warning")
        ]
        if not blocking:
            self._discard_stale_rejected()
            self._say(
                f"PASS: no new error findings{' or warnings' if self.strict else ''} "
                f"({summary['introduced']} introduced, {summary['resolved']} resolved)"
            )
            return GateOutcome(
                "pass",
                "",
                self.strict,
                summary=summary,
                introduced=diff["introduced"],
                waivers=waivers,
            )
        rejected, restored = self._reject()
        self._report_rollback(blocking, summary, rejected, restored)
        return GateOutcome(
            "rolled_back",
            f"routing introduced {len(blocking)} blocking finding(s)",
            self.strict,
            summary=summary,
            introduced=diff["introduced"],
            rejected_board=str(rejected),
            output_restored=restored,
            waivers=waivers,
        )

    # -- rollback ----------------------------------------------------------
    @property
    def rejected_path(self) -> Path:
        return self.output.with_name(f"{self.output.stem}{REJECTED_INFIX}{self.output.suffix}")

    def _project_sidecar_source(self, suffix: str) -> Path | None:
        for board in (self.output, self.source):
            candidate = board.with_suffix(suffix)
            if candidate.is_file():
                return candidate
        return None

    def _reject(self) -> tuple[Path, str]:
        rejected = self.rejected_path
        shutil.copyfile(self.output, rejected)
        # Keep the project rules beside it so `kct check <rejected>` judges it
        # under the same rules (and records waivers against the same findings).
        for suffix in _PROJECT_SUFFIXES:
            src = self._project_sidecar_source(suffix)
            if src is not None:
                shutil.copyfile(src, rejected.with_suffix(suffix))
        return rejected, self._restore()

    def _restore(self) -> str:
        if self._before is None:
            self.output.unlink(missing_ok=True)
            return "removed"
        tmp = self.output.with_suffix(self.output.suffix + ".lint-gate.tmp")
        tmp.write_bytes(self._before)
        os.replace(tmp, self.output)
        return "previous"

    def _restored_text(self, restored: str) -> str:
        if restored == "removed":
            return f"{self.output} removed (it did not exist before this run)"
        return f"{self.output} restored to its pre-run contents"

    def _discard_stale_rejected(self) -> None:
        rejected = self.rejected_path
        if rejected.is_file():
            rejected.unlink()
            for suffix in _PROJECT_SUFFIXES:
                rejected.with_suffix(suffix).unlink(missing_ok=True)

    def _report_rollback(
        self, blocking: list[dict], summary: dict, rejected: Path, restored: str
    ) -> None:
        kinds = "error(s)" + (" / warning(s)" if self.strict else "")
        self._err(
            f"ROLLED BACK: routing introduced {len(blocking)} new {kinds} "
            f"({summary['introduced_errors']} error(s), "
            f"{summary['introduced_warnings']} warning(s) introduced in total):"
        )
        for row in blocking[:20]:
            self._err(f"  [{row.get('severity')}] {row.get('rule_id')}: {row.get('message')}")
            self._err(f"      key={row.get('key')}")
        if len(blocking) > 20:
            self._err(f"  ... and {len(blocking) - 20} more")
        self._err(f"  rejected board kept at {rejected}")
        self._err(f"  {self._restored_text(restored)}")
        waivers = (
            self.waivers
            if not self._waivers_pinned_empty
            else self.source.with_name(f"{self.source.stem}.kct-waivers.json")
        )
        check_keys = [r["key"] for r in blocking if not str(r.get("key")).startswith("mistake.")]
        mistake_keys = [r["key"] for r in blocking if str(r.get("key")).startswith("mistake.")]
        self._err("  to accept a reviewed finding, waive it against the rejected board and re-run:")
        flags = " ".join(_quote(f) for f in self._flags)
        for tool, keys in (("kct check", check_keys), ("kct detect-mistakes", mistake_keys)):
            if not keys:
                continue
            extra = f" {flags}" if tool == "kct check" and flags else ""
            waive = " ".join(f"--waive {_quote(k)}" for k in dict.fromkeys(keys))
            self._err(
                f"    {tool} {_quote(str(rejected))}{extra} --waivers {_quote(str(waivers))} "
                f"{waive} --waive-reason '...' --waive-reviewer '...'"
            )


def _quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


def merge_into_json_document(text: str, outcome: GateOutcome, exit_code: int) -> str:
    """Add ``lint_gate`` to the single ``--format json`` document in ``text``.

    ``text`` is what the command would have written to stdout.  When it is
    exactly one JSON object the gate verdict is added to it (and its
    ``exit_code``/``success`` fields follow a rollback); when it is empty a
    minimal ``{"exit_code", "lint_gate"}`` document is produced.  Anything
    else is returned unchanged -- adding a second document would break the
    single-document contract (#5938) worse than omitting the verdict, which
    the exit code and stderr still carry.
    """
    if not text.strip():
        doc: dict[str, Any] = {"exit_code": exit_code}
    else:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if not isinstance(parsed, dict):
            return text
        doc = parsed
    doc["lint_gate"] = outcome.to_dict()
    if outcome.rolled_back:
        if "exit_code" in doc:
            doc["exit_code"] = exit_code
        if "success" in doc:
            doc["success"] = False
    return json.dumps(doc, indent=2) + "\n"
