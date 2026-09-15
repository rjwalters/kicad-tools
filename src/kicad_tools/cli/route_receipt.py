"""Bind final route artifacts, independently of routing/DFM qualification."""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kicad_tools.core.atomic_write import atomic_write_text

SUFFIXES = {"board": ".kicad_pcb", "project": ".kicad_pro", "rules": ".kicad_dru"}
SCHEMA = "kicad-tools.route-artifacts.v1"


def _signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return None


def _record(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": path.name, "state": "absent", "sha256": None, "size": None}
    data = path.read_bytes()
    return {
        "path": path.name,
        "state": "present",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


@dataclass
class _Invocation:
    source: Path
    outputs: dict[Path, tuple[int, int, int, int] | None]
    authored: dict[str, bytes]
    published: set[Path] = field(default_factory=set)
    propagated: set[Path] = field(default_factory=set)


_current: ContextVar[_Invocation | None] = ContextVar("route_artifact_receipt", default=None)


def record_publication(board: Path) -> None:
    """Called by routing's atomic board writers, never by input staging."""
    invocation = _current.get()
    if invocation is not None:
        invocation.published.add(Path(board).absolute())


def record_constraint_publication(board: Path, source: Path | None) -> None:
    """Only validated source propagation establishes fresh effective rules."""
    invocation = _current.get()
    if invocation is not None and source is not None:
        if Path(source).resolve() == invocation.source.resolve():
            invocation.propagated.add(Path(board).absolute())


def configure(args: Any) -> None:
    """Capture output identity before routing and invalidate prior receipts."""
    if getattr(args, "dry_run", False):
        return
    source = Path(args.pcb).absolute()
    output = (
        Path(args.output).absolute() if args.output else source.with_stem(source.stem + "_routed")
    )
    candidates = [output, output.with_stem(output.stem + "_partial")]
    candidates.extend(output.with_stem(f"{output.stem}_{n}layer") for n in (4, 6))
    protected = [source, *candidates]
    protected.extend(source.with_suffix(suffix) for suffix in (".kicad_pro", ".kicad_dru"))
    # A receipt is derived output too: never follow an alias onto authored input.
    for candidate in candidates:
        receipt = candidate.with_suffix(".route.json")
        for target in (receipt, receipt.with_suffix(receipt.suffix + ".tmp")):
            if target.is_symlink() or any(
                target == p or (target.exists() and p.exists() and target.samefile(p))
                for p in protected
            ):
                raise ValueError(f"Route receipt aliases protected input: {target}")
    authored = {
        suffix: source.with_suffix(suffix).read_bytes()
        for suffix in (".kicad_pro", ".kicad_dru")
        if source.with_suffix(suffix).exists()
    }
    for candidate in candidates:
        candidate.with_suffix(".route.json").unlink(missing_ok=True)
    _current.set(_Invocation(source, {p: _signature(p) for p in candidates}, authored))


def _finish(invocation: _Invocation, exit_code: int) -> None:
    # Fatal setup / constraint-propagation failures must not publish an apparent
    # current result, even if a PCB was written before the failure was detected.
    if exit_code not in {0, 2, 3, 4, 5, 8, 124, 130}:
        return
    for board in sorted(invocation.published):
        before = invocation.outputs.get(board)
        if not board.is_file() or _signature(board) == before:
            continue
        # Complete no-op and explicitly skipped DRC do not generate sidecars.
        # Retain authored bytes in those paths without inventing new fab floors.
        copies = []
        if board in invocation.propagated:
            if not all(board.with_suffix(s).is_file() for s in (".kicad_pro", ".kicad_dru")):
                raise ValueError("Effective routing constraints disappeared after propagation")
        else:
            # Merely changing an output's mtime (e.g. native load/save) does not
            # establish source propagation. Reject unrelated pre-existing rules
            # rather than bind stale sidecars or overwrite another author's work.
            for suffix, data in invocation.authored.items():
                destination = board.with_suffix(suffix)
                if destination.exists():
                    if destination.read_bytes() != data:
                        raise ValueError(f"Source constraint conflict at {destination}")
                else:
                    copies.append((destination, data))
            for destination, data in copies:
                destination.write_bytes(data)
        files = {
            role: _record(board if role == "board" else board.with_suffix(suffix))
            for role, suffix in SUFFIXES.items()
        }
        if files["board"]["state"] != "present":
            raise ValueError("Published routing board disappeared before receipt creation")
        receipt = board.with_suffix(".route.json")
        payload = {
            "schema": SCHEMA,
            "route_exit_code": int(exit_code),
            "scope": "Final artifact bytes only; not DRC or factory DFM qualification",
            "files": files,
        }
        atomic_write_text(receipt, json.dumps(payload, indent=2) + "\n")
        problems = verify_route_receipt(receipt)
        if problems:
            receipt.unlink(missing_ok=True)
            raise ValueError("Route artifacts changed during publication: " + "; ".join(problems))


def run(action: Callable[[], int]) -> int:
    """Finalize once, after every in-process dispatch and postprocessing step."""
    token = _current.set(None)
    try:
        result = action()
        invocation = _current.get()
        if invocation is not None:
            try:
                _finish(invocation, result)
            except (OSError, ValueError) as exc:
                for board in invocation.published:
                    with suppress(OSError):
                        board.with_suffix(".route.json").unlink(missing_ok=True)
                print(f"Error: cannot publish route artifact receipt: {exc}", file=sys.stderr)
                return 1
        return result
    finally:
        _current.reset(token)


def verify_route_receipt(path: str | Path) -> list[str]:
    """Verify a relocated artifact set; absence is bound as well as file bytes.

    Like manufacturing ``verify_manifest``, return problems (empty means the
    binding matches). This does not re-run DRC or authenticate a receipt author.
    """
    path = Path(path)
    try:
        payload = json.loads(path.read_text())
        if payload.get("schema") != SCHEMA or set(payload["files"]) != set(SUFFIXES):
            return ["Invalid route artifact receipt schema"]
        if type(payload.get("route_exit_code")) is not int:
            return ["Invalid route exit status"]
        board_name = payload["files"]["board"]["path"]
        problems = []
        for role, suffix in SUFFIXES.items():
            record = payload["files"][role]
            name = record["path"]
            if not isinstance(name, str) or not name or Path(name).name != name:
                return [f"Invalid {role} relative path"]
            if role != "board" and name != str(Path(board_name).with_suffix(suffix)):
                return [f"{role} is not the board's effective sibling"]
            target = path.parent / name
            if record["state"] == "absent":
                if role == "board" or record["sha256"] is not None or record["size"] is not None:
                    return [f"Invalid absent {role} record"]
                if target.exists():
                    problems.append(f"{name}: expected absent")
            elif record["state"] == "present":
                if not target.is_file():
                    problems.append(f"{name}: missing")
                elif _record(target) != record:
                    problems.append(f"{name}: content hash or size mismatch")
            else:
                return [f"Invalid {role} state"]
        return problems
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return [f"Invalid route artifact receipt: {exc}"]
