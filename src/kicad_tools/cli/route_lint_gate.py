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
    the exact command is printed.

    **Copper waivers carry over only with the same ``--seed``.**  ``kct
    check`` keys for copper findings name the track/via UUIDs, and an
    unseeded route gives its copper fresh random UUIDs on every run, so a
    copper waiver recorded against one run never matches the next one.  Re-run
    with the **same ``--seed``** (and unchanged inputs, so the copper is
    reproduced) for the waiver to apply; any change to the routed copper
    invalidates it.  Footprint/pad-keyed findings and ``mistake.*`` keys are
    not affected.  Geometry-based copper keys are tracked in Issue #6088.
    Stale waivers only make the gate stricter -- it never passes a board it
    should have rolled back.

Verdict and rollback
    ``introduced_errors > 0`` rolls back; with ``--lint-gate-strict`` (or
    ``kct route --strict``) ``introduced_warnings > 0`` does too.  Rolling
    back restores ``--output`` to exactly the bytes it held before the run
    (or removes it when it did not exist -- for an in-place route that is
    the input board) and exits **3**, the route rollback exit code shared
    with the connectivity rollback (Issue #2839).  A routed board that
    cannot be linted fails closed: it is rolled back too, also exit 3.  A
    baseline that cannot be linted aborts before routing with exit 1 (under
    ``--format json`` with a ``lint_gate`` document of status ``error``).

    "Cannot be linted" means the linter produced no report, not that it
    exited non-zero: ``kct check`` exits 2 and ``kct detect-mistakes`` exits
    1 when they *find* errors, which is an ordinary result.  Success is
    judged by whether the report parses as the expected JSON document.

    When routing itself raises or exits (Ctrl+C, SIGTERM, a deadline, a
    strict connectivity exit) after writing a board, that board was never
    judged: it is moved aside to ``<output-stem>.lint-rejected.kicad_pcb``
    and ``--output`` is left as for a rollback.

Staging (Issue #6090)
    Routing never writes ``--output`` directly.  :meth:`LintGate.stage`
    creates a private directory next to it
    (``.<output-name>.lint-gate-<pid>-<random>/``) and the router writes the
    board -- and every artifact it derives from the output name (``_partial``,
    ``.kicad_pro``/``.kicad_dru``, ``_4layer`` siblings, ...) -- in there,
    under the same file names.  Only after the gate passes is the board moved
    onto ``--output`` with :func:`os.replace` (atomic: same directory, same
    filesystem).  An unjudged board therefore can never sit at ``--output``,
    even after SIGKILL: a killed run leaves ``--output`` at its pre-run bytes
    and its unjudged work in the hidden staging directory, which is safe to
    delete.  The project files beside ``--output`` are copied into the
    staging directory first, so the routed board is checked against the same
    rules it would have seen in place.

    While the gate is active and nothing else owns SIGTERM, a SIGTERM (what
    ``timeout`` and CI job cancellations send) becomes a
    :class:`LintGateTerminated` exit (code 143), so the gate can still move
    the unjudged board aside and say so.  The supervised ``--timeout`` worker
    and the adaptive/rule-relaxation windows install their own SIGTERM
    handlers; those are left alone and unwind through the same abort path.

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
import signal
import sys
import tempfile
import threading
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

#: Output-name siblings copied into the staging directory before routing, so
#: the staged board sees the same project rules and the escalation path's
#: stale-sibling cleanup (``_4layer``/``_6layer``) is mirrored back.
_SEEDED_SUFFIXES = (".kicad_pro", ".kicad_dru", ".kicad_prl")

#: Exit code of a run terminated by SIGTERM while the gate was active
#: (128 + SIGTERM, what the default disposition reports).
SIGTERM_EXIT = 143


class LintGateError(Exception):
    """A board could not be linted."""


class LintGateTerminated(SystemExit):
    """SIGTERM arrived while ``--lint-gate`` was active (Issue #6090).

    A ``SystemExit`` so the CLI exits with :data:`SIGTERM_EXIT` once the gate
    has cleaned up, without a traceback.
    """

    def __init__(self) -> None:
        super().__init__(SIGTERM_EXIT)


def _raise_terminated(signum, frame) -> None:
    raise LintGateTerminated()


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
        checkpoint: Path | None = None,
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
        #: Why the baseline could not be linted (set by :meth:`begin`).
        self.baseline_error: str | None = None
        self.checkpoint = Path(checkpoint) if checkpoint is not None else None
        # -- staging (Issue #6090) --
        self._staging_dir: Path | None = None
        #: The board routing writes (inside the staging directory), or None.
        self.staged: Path | None = None
        self._seeded: dict[str, bytes] = {}
        self._output_stat_before: os.stat_result | None = None
        self._prev_sigterm: Any = None
        self._sigterm_installed = False
        #: Staged path -> final path (``None``: not promoted), for receipts/JSON.
        self.moves: dict[Path, Path | None] = {}
        #: True once a judged board was promoted onto ``--output``.
        self.promoted = False
        self._staging_root: Path | None = None

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
        checkpoint = getattr(args, "checkpoint", None)
        return cls(
            source,
            output,
            check_flags=flags,
            waivers=Path(waivers) if waivers else None,
            strict=bool(getattr(args, "lint_gate_strict", False) or getattr(args, "strict", False)),
            quiet=bool(getattr(args, "quiet", False)),
            command="kct route",
            checkpoint=Path(checkpoint) if checkpoint else None,
        )

    @classmethod
    def from_route_auto_args(cls, args) -> LintGate:
        """Build the gate for ``kct route-auto`` (requires ``--output``)."""
        waivers = getattr(args, "lint_gate_waivers", None)
        checkpoint = getattr(args, "checkpoint", None)
        return cls(
            Path(args.pcb),
            Path(args.output),
            waivers=Path(waivers) if waivers else None,
            strict=bool(getattr(args, "lint_gate_strict", False)),
            quiet=bool(getattr(args, "quiet", False) or getattr(args, "global_quiet", False)),
            command="kct route-auto",
            checkpoint=Path(checkpoint) if checkpoint else None,
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
    def _run(
        self, main: Callable[[list[str]], int], argv: list[str]
    ) -> tuple[int, str, io.StringIO]:
        """Run a linter in-process; return ``(exit code, stdout, stderr buffer)``.

        The exit code is NOT a success signal: ``kct check`` exits 2 and
        ``kct detect-mistakes`` exits 1 when they find error-severity
        findings.  Callers judge success by whether the report parses.
        """
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = main(argv)
                except LintGateTerminated:
                    raise  # SIGTERM while linting: unwind, never a lint result
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        except BaseException:
            _replay(err)
            raise
        return int(code or 0), out.getvalue(), err

    @staticmethod
    def _failed(what: str, code: int, err: io.StringIO, problem: str) -> LintGateError:
        # Tool failure: the captured diagnostics are the only clue.
        _replay(err)
        return LintGateError(f"{what} failed (exit {code}): {problem}")

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
        what = f"kct check on the {label} board {board}"
        code, _text, err = self._run(check_main, argv)
        if not report_path.is_file():
            raise self._failed(what, code, err, "it wrote no report")
        try:
            report = json.loads(report_path.read_text())
        except json.JSONDecodeError as e:
            raise self._failed(what, code, err, f"unparseable report ({e})") from e
        if not isinstance(report, dict) or not isinstance(report.get("violations"), list):
            raise self._failed(what, code, err, "report has no 'violations' list")

        what = f"kct detect-mistakes on the {label} board {board}"
        code, text, err = self._run(
            mistakes_main,
            [str(board), "--format", "json", "--waivers", str(self.waivers)],
        )
        try:
            mistakes = json.loads(text)
        except json.JSONDecodeError as e:
            raise self._failed(what, code, err, f"no JSON document on stdout ({e})") from e
        if not isinstance(mistakes, dict) or not isinstance(mistakes.get("mistakes"), list):
            raise self._failed(what, code, err, "JSON document has no 'mistakes' list")

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
        if self.checkpoint is not None and _same_path(self.checkpoint, self.output):
            # A checkpoint is written unjudged, after every improving pass.
            self.baseline_error = (
                f"--checkpoint {self.checkpoint} is the --output path; checkpoints are "
                "written unjudged, so under --lint-gate they need a separate file. "
                "Nothing was routed"
            )
            return f"[lint-gate] {self.baseline_error}"
        self._scratch = tempfile.TemporaryDirectory(prefix="kct-lint-gate-")
        try:
            self._before = self.output.read_bytes() if self.output.is_file() else None
            self._output_stat_before = self.output.stat() if self.output.is_file() else None
            self._pin_sidecars(Path(self._scratch.name))
            record_stage("lint-gate-baseline")
            self._say(f"linting input board {self.source} (kct check + detect-mistakes)")
            self._baseline = self.lint(self.source, "baseline")
        except (LintGateError, OSError, ValueError) as e:
            self.close()
            self.baseline_error = f"cannot lint the input board, nothing was routed: {e}"
            return f"[lint-gate] {self.baseline_error}"
        active = sum(1 for v in self._baseline["violations"] if not v.get("waived"))
        self._say(f"baseline: {active} active finding(s); waivers: {self._waivers_label()}")
        return None

    def stage(self) -> Path | None:
        """Create the staging directory; return the board path routing must write.

        Call after a successful :meth:`begin` and point the router's output at
        the returned path.  Returns ``None`` when no staging directory can be
        created next to ``--output``; the run is then gated in place (the
        pre-#6090 behaviour, which SIGKILL can defeat) and the caller keeps
        routing to ``--output``.  Either way SIGTERM is turned into a clean
        :class:`LintGateTerminated` exit while the gate is active.
        """
        if self._baseline is None:
            return None
        self._install_sigterm()
        parent = self.output.parent.absolute()
        try:
            directory = Path(
                tempfile.mkdtemp(prefix=f".{self.output.name}.lint-gate-{os.getpid()}-", dir=parent)
            )
        except OSError as e:
            self._err(
                f"cannot create a staging directory next to {self.output} ({e}); gating it in place"
            )
            return None
        self._staging_dir = directory
        self.staged = directory / self.output.name
        try:
            for name in self._seed_names():
                src = parent / name
                if src.is_file():
                    data = src.read_bytes()
                    (directory / name).write_bytes(data)
                    self._seeded[name] = data
        except OSError as e:
            self._err(f"cannot seed the staging directory ({e}); gating {self.output} in place")
            shutil.rmtree(directory, ignore_errors=True)
            self._staging_dir = self.staged = None
            self._seeded = {}
            return None
        self._staging_root = directory
        return self.staged

    def _seed_names(self) -> list[str]:
        stem = self.output.stem
        names = [f"{stem}{suffix}" for suffix in _SEEDED_SUFFIXES]
        names += [
            f"{stem}_{layers}layer{suffix}"
            for layers in (4, 6)
            for suffix in (".kicad_pcb", ".kicad_prl")
        ]
        return names

    @property
    def board(self) -> Path:
        """The board routing writes: the staged file, or ``--output`` in place."""
        return self.staged if self.staged is not None else self.output

    def baseline_error_outcome(self) -> GateOutcome:
        """The verdict for a run :meth:`begin` refused (for ``--format json``)."""
        return GateOutcome("error", self.baseline_error or "", self.strict)

    def _waivers_label(self) -> str:
        return "none" if self._waivers_pinned_empty else str(self.waivers)

    def finish(self) -> GateOutcome:
        """Lint the routed board; promote it to ``--output`` or roll it back."""
        try:
            return self._finish()
        except BaseException:
            # A deadline, SIGTERM or interrupt while judging must not leave an
            # unjudged board at --output.
            self.abort(judging=True)
            raise
        finally:
            self.close()

    def abort(self, *, judging: bool = False) -> None:
        """Routing raised: move the unjudged board aside; ``--output`` keeps its pre-run state.

        The router may have saved a best-so-far board before exiting (Ctrl+C
        during adaptive routing saves ``best_completed_attempt`` and exits 5);
        it is kept at :attr:`rejected_path` rather than discarded.
        """
        exc = sys.exc_info()[1]
        why = " (SIGTERM)" if isinstance(exc, LintGateTerminated) else ""
        try:
            if self._baseline is not None:
                self._recover_stray()
                if self._fresh():
                    rejected, restored = self._reject()
                    stage = (
                        "while the routed board was being linted"
                        if judging
                        else "before the routed board was linted"
                    )
                    self._err(
                        f"routing aborted{why} {stage}; it is kept "
                        f"(unjudged) at {rejected}, {self._restored_text(restored)}"
                    )
                else:
                    self._unstage(None)
                    if why:
                        self._err(
                            f"routing aborted{why} before a routed board was written; "
                            f"{self.output} is untouched"
                        )
        except OSError:
            pass
        finally:
            self.close()

    def close(self) -> None:
        self._restore_sigterm()
        if self._staging_dir is not None:
            directory, self._staging_dir = self._staging_dir, None
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                self._err(
                    f"left unjudged routing artifacts in {directory} (never promoted to "
                    f"{self.output}); safe to delete"
                )
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None

    # -- SIGTERM (Issue #6090) ---------------------------------------------
    def _install_sigterm(self) -> None:
        """Turn SIGTERM into :class:`LintGateTerminated` unless something owns it.

        The supervised ``--timeout`` worker installs ``_deadline_signal`` (its
        :class:`RouteDeadlineExpired` unwind is left intact); only the default
        disposition -- which would kill the process with no cleanup at all --
        is replaced.
        """
        if self._sigterm_installed or threading.current_thread() is not threading.main_thread():
            return
        try:
            previous = signal.getsignal(signal.SIGTERM)
            if previous is not signal.SIG_DFL:
                return
            signal.signal(signal.SIGTERM, _raise_terminated)
        except (ValueError, OSError):
            return
        self._prev_sigterm = previous
        self._sigterm_installed = True

    def _restore_sigterm(self) -> None:
        if not self._sigterm_installed:
            return
        self._sigterm_installed = False
        with contextlib.suppress(ValueError, OSError):
            if signal.getsignal(signal.SIGTERM) is _raise_terminated:
                signal.signal(signal.SIGTERM, self._prev_sigterm)

    # -- staged board ------------------------------------------------------
    def _fresh(self) -> bool:
        board = self.board
        if not board.is_file():
            return False
        return board.read_bytes() != self._before

    def _recover_stray(self) -> None:
        """Undo a write that bypassed staging and landed on ``--output`` itself.

        Every routing path writes the output name it is given, so this is a
        safety net: the stray board is judged as the staged one (when routing
        staged nothing) and ``--output`` goes back to its pre-run bytes.
        """
        if self.staged is None:
            return
        current = self.output.read_bytes() if self.output.is_file() else None
        if current == self._before:
            return
        if current is not None and not self.staged.exists():
            os.replace(self.output, self.staged)
        self._restore()

    def _unstage(self, board_to: Path | None) -> None:
        """Move the staging directory's contents to their final places.

        ``board_to`` is ``--output`` (promote), :attr:`rejected_path`
        (reject) or ``None`` (nothing new was routed: drop the staged board).
        Other artifacts go next to ``--output`` under the names the router
        gave them; project files go beside a rejected board instead, so
        ``--output``'s own rules are not touched by a rejected route.  Seeded
        copies that routing did not change are dropped, and seeded siblings
        routing deleted (stale ``_4layer`` boards) are deleted for real.
        """
        directory = self._staging_dir
        if directory is None or self.staged is None:
            return
        parent = self.output.parent
        receipt: dict[Path, Path | None] = {}
        deleted = [name for name in self._seeded if not (directory / name).exists()]
        rejecting = board_to is not None and board_to != self.output
        if self.staged.is_file():
            if board_to is None:
                self.staged.unlink()
            else:
                os.replace(self.staged, board_to)
                self.moves[self.staged] = board_to
                receipt[self.staged] = None if rejecting else board_to
        if rejecting:
            assert board_to is not None
            for suffix in _PROJECT_SUFFIXES:
                staged_side = self.staged.with_suffix(suffix)
                if staged_side.is_file():
                    os.replace(staged_side, board_to.with_suffix(suffix))
                    self.moves[staged_side] = board_to.with_suffix(suffix)
                    receipt[staged_side] = None
                else:
                    src = self._project_sidecar_source(suffix)
                    if src is not None:
                        shutil.copyfile(src, board_to.with_suffix(suffix))
        for item in sorted(directory.iterdir()):
            name = item.name
            if name in self._seeded and item.is_file() and item.read_bytes() == self._seeded[name]:
                item.unlink()
                continue
            target = parent / name
            try:
                if item.is_dir() and target.exists():
                    continue  # never merge directories; left in staging (reported)
                os.replace(item, target)
            except OSError:
                continue
            self.moves[item] = target
            receipt[item] = target
        for name in deleted:
            (parent / name).unlink(missing_ok=True)
        from .route_receipt import relocate

        relocate(receipt)

    # -- verdict -----------------------------------------------------------
    def _finish(self) -> GateOutcome:
        from .route_deadline import record_stage

        waivers = None if self._waivers_pinned_empty else str(self.waivers)
        if self._baseline is None:
            return GateOutcome("skipped", "input board was not linted", self.strict)
        self._recover_stray()
        if not self._fresh():
            self._unstage(None)
            self._say("no routed board was written; nothing to gate")
            return GateOutcome("skipped", "no routed board written", self.strict, waivers=waivers)
        record_stage("lint-gate")
        where = f" (staged at {self.staged})" if self.staged is not None else ""
        self._say(f"linting routed board {self.output}{where}")
        try:
            candidate = self.lint(self.board, "candidate")
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
            self._unstage(self.output)
            self.promoted = True
            self._say(
                f"PASS: no new error findings{' or warnings' if self.strict else ''} "
                f"({summary['introduced']} introduced, {summary['resolved']} resolved); "
                f"wrote {self.output}"
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
        if self.staged is not None:
            # --output was never written: it still holds its pre-run state.
            self._unstage(rejected)
            return rejected, ("previous" if self._before is not None else "removed")
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
        if self.staged is not None:
            if restored == "removed":
                return f"{self.output} was not written (it did not exist before this run)"
            return f"{self.output} was not touched (it keeps its pre-run contents)"
        if restored == "removed":
            return f"{self.output} removed (it did not exist before this run)"
        return f"{self.output} restored to its pre-run contents"

    def _discard_stale_rejected(self) -> None:
        rejected = self.rejected_path
        if rejected.is_file():
            rejected.unlink()
            for suffix in _PROJECT_SUFFIXES:
                rejected.with_suffix(suffix).unlink(missing_ok=True)

    # -- machine output ----------------------------------------------------
    def relocate(self, value: Any) -> Any:
        """Rewrite staged paths inside a JSON-able ``value`` to where the files ended up."""
        pairs = self._path_pairs()
        if not pairs:
            return value

        def fix(v: Any) -> Any:
            if isinstance(v, str):
                for old, new in pairs:
                    if old in v:
                        v = v.replace(old, new)
                return v
            if isinstance(v, dict):
                return {k: fix(x) for k, x in v.items()}
            if isinstance(v, list):
                return [fix(x) for x in v]
            return v

        return fix(value)

    def _path_pairs(self) -> list[tuple[str, str]]:
        root = self._staging_root
        if root is None or self.staged is None:
            return []
        files: dict[Path, Path] = {self.staged: self.output}
        files.update({k: v for k, v in self.moves.items() if v is not None})
        pairs = [(str(k), str(v)) for k, v in files.items()]
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        parent = str(self.output.parent)
        pairs.append((str(root) + os.sep, "" if parent == "." else parent + os.sep))
        pairs.append((str(root), parent))
        return pairs

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
        if check_keys:
            self._err(
                "  (copper finding keys name track/via UUIDs: a copper waiver carries over "
                "only to a re-run with the SAME --seed and unchanged inputs -- an unseeded "
                "route gets new UUIDs every run; see Issue #6088)"
            )
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


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve() or (
            left.exists() and right.exists() and left.samefile(right)
        )
    except OSError:
        return False


def _quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


def merge_into_json_document(
    text: str,
    outcome: GateOutcome,
    exit_code: int,
    relocate: Callable[[Any], Any] | None = None,
) -> str:
    """Add ``lint_gate`` to the single ``--format json`` document in ``text``.

    ``text`` is what the command would have written to stdout.  When it is
    exactly one JSON object the gate verdict is added to it (and its
    ``exit_code``/``success`` fields follow a rollback); when it is empty a
    minimal ``{"exit_code", "lint_gate"}`` document is produced.  Anything
    else is returned unchanged -- adding a second document would break the
    single-document contract (#5938) worse than omitting the verdict, which
    the exit code and stderr still carry.

    ``relocate`` (:meth:`LintGate.relocate`) rewrites the staging paths the
    router reported into the names the files ended up under (Issue #6090).
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
        doc = relocate(parsed) if relocate is not None else parsed
    doc["lint_gate"] = outcome.to_dict()
    if outcome.rolled_back:
        if "exit_code" in doc:
            doc["exit_code"] = exit_code
        if "success" in doc:
            doc["success"] = False
    return json.dumps(doc, indent=2) + "\n"
