"""The default silk-legalisation pass for board-producing commands.

Issue #9799 (#9795 phase 3, operator ruling "silk is legal by default"):
``kct route`` / ``route-auto``, ``kct optimize-placement``, ``kct export``
and ``kct readiness --generate`` run the shared
:func:`~kicad_tools.drc.repair_silkscreen.legalize_silk` pass on the board
they publish, so a normal pipeline can no longer ship an authored silk
defect.  The pass names its result and anything it could not fix in the
command output; it never waives anything and never gates the exit code (the
independent ``kct check`` and native DRC gates stay authoritative).  Each
command takes ``--no-silk-fix`` to leave authored silk byte-identical.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

__all__ = ["run_default_silk_pass", "silk_pass_document", "print_silk_pass_result"]


def _resolve_profile(
    board: Path,
    mfr: str | None,
    *,
    layers: int | None = None,
    copper_oz: float | None = None,
):
    """Resolve the manufacturer :class:`DesignRules` the pass runs against.

    Returns ``None`` (pass skipped, never fatal) when the tier has no
    profile (e.g. the ``generic`` export pseudo-tier) or the board's layer
    count / copper weight cannot be resolved.
    """
    from kicad_tools.manufacturers import get_profile

    if not mfr:
        return None
    if layers is None:
        layers = _detect_layers(board)
        if layers is None:
            return None
    if copper_oz is None:
        # None (unprobeable) leaves the profile's own default in place.
        copper_oz = _probe_copper_oz(board) or 1.0
    try:
        profile = get_profile(mfr)
        return profile.get_design_rules(layers=layers, copper_oz=float(copper_oz))
    except (ValueError, KeyError, OSError):
        return None


def _detect_layers(board: Path) -> int | None:
    try:
        from kicad_tools.router.io import detect_layer_stack

        return int(detect_layer_stack(board.read_text()).num_layers)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _probe_copper_oz(board: Path) -> float | None:
    try:
        from kicad_tools.physics.stackup import Stackup
        from kicad_tools.schema.pcb import PCB

        derived = Stackup.from_pcb(PCB.load(str(board))).outer_inner_copper_oz()
        if derived is not None and derived[0] is not None:
            return float(derived[0])
    except Exception:  # noqa: BLE001 - the profile default applies on any quirk
        return None
    return None


def run_default_silk_pass(
    board: str | Path,
    *,
    mfr: str | None,
    layers: int | None = None,
    copper_oz: float | None = None,
    quiet: bool = False,
) -> dict[str, Any] | None:
    """Run the shared silk-legalisation pass in place on *board* (#9799).

    Never fatal: a board the pass cannot process (no profile, shapely
    missing, a placer rejection) is reported as a warning and returned as a
    document with ``"applied": False`` so the caller's own work -- routed
    copper, placement, the export bundle -- still ships.

    Args:
        board: The finished board to legalise (modified in place only when
            the pass fixes something; a zero-edit board is not re-saved).
        mfr: Manufacturer tier id.  ``None`` / an unknown tier skips.
        layers: Copper layer count; detected from *board* when ``None``.
        copper_oz: Outer copper weight; probed from the board stackup when
            ``None`` (the profile default applies when unprobeable).
        quiet: Suppress the human-readable summary lines.

    Returns:
        A JSON-safe document describing the outcome (see
        :func:`silk_pass_document`), or ``None`` when no profile resolved
        and nothing was attempted.
    """
    board_path = Path(board)
    rules = _resolve_profile(board_path, mfr, layers=layers, copper_oz=copper_oz)
    if rules is None:
        return None

    from kicad_tools.drc.repair_silkscreen import legalize_silk

    try:
        report = legalize_silk(board_path, rules)
    except ImportError as exc:
        print(f"Warning: silk legalisation skipped (shapely missing): {exc}", file=sys.stderr)
        return {"applied": False, "manufacturer": mfr, "error": f"shapely missing: {exc}"}
    except ValueError as exc:
        print(f"Warning: silk legalisation skipped: {exc}", file=sys.stderr)
        return {"applied": False, "manufacturer": mfr, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never sink the command's own work
        print(f"Warning: silk legalisation failed: {exc}", file=sys.stderr)
        return {"applied": False, "manufacturer": mfr, "error": str(exc)}

    document = silk_pass_document(report, mfr)
    if not quiet:
        print_silk_pass_result(document)
    return document


def silk_pass_document(report, mfr: str | None) -> dict[str, Any]:
    """One JSON-safe summary of a :class:`SilkLegalizeReport` (#9799)."""
    return {
        "applied": True,
        "manufacturer": mfr,
        "total_fixed": report.total_fixed,
        "total_moved": report.total_moved,
        "unresolved": [u.to_dict() for u in report.unresolved],
        "board": str(report.output_path) if report.output_path else None,
    }


def print_silk_pass_result(document: dict[str, Any]) -> None:
    """Name the pass's result and anything it could not fix (#9799)."""
    if not document.get("applied"):
        return
    fixed = document.get("total_fixed", 0)
    moved = document.get("total_moved", 0)
    unresolved = document.get("unresolved") or []
    outcome = "no silk edits needed" if fixed == 0 else f"{fixed} silk edit(s)"
    if moved:
        outcome += f", {moved} reference(s) moved"
    outcome += f"; {len(unresolved)} unresolved item(s)"
    print(f"\n--- Silk legalisation (default pass, {document.get('manufacturer')}) ---")
    print(f"  {outcome}")
    for item in unresolved:
        detail = f" -- {item['detail']}" if item.get("detail") else ""
        print(f"  unresolved [{item['kind']}] {item['item']}{detail}")
    if not unresolved:
        print("  0 unresolved from this pass; kct check / native DRC stay authoritative.")
