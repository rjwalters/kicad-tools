"""One completion verdict for pour nets (Issue #5785, Epic #5784 Phase 1).

Before this module three surfaces disagreed about the same copper: ``kct route``
exited 0, :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` labelled
the residual "advisory-plane-residual" and ``kct net-status`` exited 2, while
``kicad-cli pcb drc`` listed the stranded pad as an ``unconnected_items``
entry.  The rule is now single and mirrors kicad-cli: **a pad stranded on a
pour is a completion failure**, unless the user explicitly opts in to treating
it as advisory (``--allow-stranded-pour-pads`` on ``kct route`` /
``kct net-status``, or ``KCT_ALLOW_STRANDED_POUR_PADS=1``).

The mapping lives here, as pure functions, so every surface (and the tests)
share it.
"""

from __future__ import annotations

import os

__all__ = [
    "ALLOW_STRANDED_ENV",
    "ORACLE_DISABLE_ENV",
    "allow_stranded_pour_pads",
    "oracle_rounds",
    "stranded_pour_blocks",
    "stranded_pour_exit",
]

#: Environment opt-in equivalent to ``--allow-stranded-pour-pads``.
ALLOW_STRANDED_ENV = "KCT_ALLOW_STRANDED_POUR_PADS"
#: Set to ``0`` to disable the oracle loop and the stranded-pad verdict
#: (restores the pre-#5785 behaviour; used to measure before/after).
ORACLE_DISABLE_ENV = "KCT_ORACLE_COMPLETION"

_TRUTHY = {"1", "true", "yes", "on"}


def allow_stranded_pour_pads(flag: bool = False) -> bool:
    """True when stranded pour pads are explicitly opted in as advisory."""
    return bool(flag) or os.environ.get(ALLOW_STRANDED_ENV, "").strip().lower() in _TRUTHY


def oracle_rounds(requested: int | None, default: int) -> int:
    """Effective oracle round budget (``0`` disables the loop and verdict)."""
    if os.environ.get(ORACLE_DISABLE_ENV, "").strip() == "0":
        return 0
    if requested is None:
        return default
    return max(int(requested), 0)


def stranded_pour_blocks(stranded: int, allow: bool) -> bool:
    """Whether ``stranded`` unconnected pour links fail the completion verdict."""
    return stranded > 0 and not allow


def stranded_pour_exit(rc: int, stranded: int, allow: bool) -> int:
    """Map an exit code through the stranded-pour gate.

    Same contract as the #5862 short gate: ``0`` (met the completion
    threshold) becomes ``3`` and ``2`` (below threshold) becomes ``4`` --
    "routing succeeded but the board is not electrically complete".  Any other
    code already carries a more specific diagnosis and passes through.
    """
    if not stranded_pour_blocks(stranded, allow):
        return rc
    if rc == 0:
        return 3
    if rc == 2:
        return 4
    return rc
