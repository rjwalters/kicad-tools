"""Shared board-recipe LVS step (issue #3762).

Board 00's ``run_lvs()`` (``boards/00-simple-led/generate_design.py``) was
the original template: it runs LVS, writes ``output/lvs.json`` (v1 schema),
and raises :class:`BoardNetlistMismatch` on a dirty board so the recipe's
exit gate trips.  This module extracts that logic into a single reusable
entrypoint, :func:`write_lvs_report`, so the copper-LVS manufacturability
leg can be wired into every demo board without 7x copy-paste drift.

It runs **both** comparators:

* the label-based :func:`compare_netlists` (trusts each pad's ``(net ...)``
  label), and
* the copper-extracted :func:`compare_copper_netlist` (#3742; diffs the
  *physical* copper partition against the schematic, catching shorts/opens
  a mislabeled router would hide).

The emitted ``lvs.json`` records both comparators' results.  ``clean`` is
the AND of the *gated* comparators (the ones selected via ``run_copper`` /
``run_label``); a comparator that is run-but-not-gated is reflected in the
payload but does not flip ``clean`` or trigger a raise.

Gate policy is per-board (see the curator matrix on #3762):

* Boards verified clean (00, 01, 02) gate on both comparators.
* Boards 06/07 are label-dirty (PCB-first test fixtures whose floating
  schematic pins read ``schematic_net=None``); they gate on copper only
  (``run_label=False``).  **Vacuity caveat (#4005 review):** on a fully
  *wireless* fixture schematic the copper comparator binds zero pins, so
  its historical ``clean=True`` was zero-evidence.  The comparator now
  returns a dirty ``vacuous`` result in that case, which means a
  copper-only gate (``run_label=False``) on a wireless schematic FAILS
  instead of passing vacuously — such boards must either wire their
  schematic or skip the LVS step explicitly (emit no ``lvs.json``; the
  gallery then honestly shows "LVS not run").  Note the label comparator
  has no symmetric hole: on a wireless schematic every netted PCB pad
  mismatches ``schematic_net=None``, so label-LVS reads *dirty*, never
  vacuously clean.
* Boards in :data:`ADVISORY_LVS_BOARDS` (04/05) are genuinely dirty on a
  fresh clean-room regen; they still run LVS and emit ``lvs.json`` so the
  gallery chip and ``board-metrics`` surface the true state, but pass
  ``require_clean=False`` so the recipe logs the mismatch summary without
  raising.  Board 03 graduated to a hard copper-LVS gate in #3795 (its
  recipe regenerates copper-clean) and is no longer in the allowlist.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from kicad_tools.lvs.board_lvs import (
    BoardNetlistMismatch,
    LVSResult,
    compare_netlists,
)
from kicad_tools.lvs.copper_lvs import (
    CopperLVSMismatch,
    CopperLVSResult,
    compare_copper_netlist,
    result_from_json,
)

# Boards that run LVS + emit ``lvs.json`` but are NOT yet copper/label clean.
# Recipes for these boards must pass ``require_clean=False`` so a dirty
# comparator logs a summary instead of raising.  CI does NOT assert
# ``clean=true`` for these boards.  This allowlist is the single auditable
# place for the exemption -- shrink it as per-board fix follow-ups land.
#
# Graduated (removed): board 03 (#3795) -- its recipe regenerates
# copper-LVS clean (0/0) on a fresh clean-room route, now hard-gated.
#
# Still advisory:
# * 04-stm32-devboard -- the COMMITTED PCB was hand-fixed (#3785/#3796:
#   OSC short + power-pad bonding), but a fresh ``generate_design.py``
#   regen re-introduces the OSC_IN<->OSC_OUT short + 20 same-net power
#   opens (21 mismatches).  The RECIPE cannot yet produce a clean board
#   from scratch, so a hard gate would be RED.  Graduation blocked on a
#   recipe-reproducibility fix (board-04 follow-up).
# * 05-bldc-motor-controller -- incomplete routing, blocked on #3775/#3766.
ADVISORY_LVS_BOARDS: frozenset[str] = frozenset(
    {
        "04-stm32-devboard",
        "05-bldc-motor-controller",
    }
)

# JSON Schema URL stamped into every emitted ``lvs.json``.  Kept identical
# to board-00's historical value so downstream readers (board-metrics
# ``_parse_lvs``, ``scripts/ci/check_board_00_e2e.py``) are unaffected.
_LVS_SCHEMA_URL = "https://kicad-tools.org/schemas/lvs/v1.json"


class FreshCopperCheckError(RuntimeError):
    """The fresh out-of-process copper-LVS check could not be obtained.

    Raised when the ``python -m kicad_tools.lvs.copper_lvs`` subprocess
    fails to run or emits output we cannot parse.  The gate treats this as
    fatal (fail closed) rather than silently trusting the in-process result.
    """


def _copper_mismatch_key(
    result: CopperLVSResult,
) -> tuple[bool, frozenset[tuple[str, str, str, str, str]]]:
    """Canonical, order-independent identity of a copper-LVS result.

    Two results are equal iff they agree on ``clean`` AND carry the same
    set of mismatches.  Used to detect in-process-vs-fresh divergence on
    identical bytes (#3838).
    """
    mismatches = frozenset((m.kind, m.net_a, m.net_b, m.pad_a, m.pad_b) for m in result.mismatches)
    return (result.clean, mismatches)


def _fresh_copper_compare(sch_path: Path, routed_pcb_path: Path) -> CopperLVSResult:
    """Run :func:`compare_copper_netlist` in a fresh subprocess.

    Spawns ``python -m kicad_tools.lvs.copper_lvs <sch> <pcb>`` so the
    comparison loads the persisted ``.kicad_pcb`` bytes from a *clean*
    interpreter — no in-process recipe fill/zone state, no cached netlist,
    a fresh shapely-availability resolution.  This is byte-for-byte what
    CI's fresh re-check (and any downstream consumer) will see.

    Raises:
        FreshCopperCheckError: when the subprocess exits non-zero or its
            stdout cannot be parsed as a copper-LVS JSON result.
    """
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.lvs.copper_lvs",
        str(sch_path),
        str(routed_pcb_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - environment-dependent
        raise FreshCopperCheckError(f"failed to spawn fresh copper-LVS check: {exc}") from exc

    if proc.returncode != 0:
        raise FreshCopperCheckError(
            "fresh copper-LVS subprocess exited "
            f"{proc.returncode}:\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    try:
        payload = json.loads(proc.stdout)
        return result_from_json(payload)
    except (ValueError, KeyError) as exc:
        raise FreshCopperCheckError(
            f"could not parse fresh copper-LVS output: {exc}\nstdout was:\n{proc.stdout}"
        ) from exc


def _authoritative_copper_result(
    sch_path: Path,
    routed_pcb_path: Path,
    *,
    in_process: CopperLVSResult,
) -> CopperLVSResult:
    """Return the gate-authoritative copper-LVS result, failing closed.

    Re-runs the copper comparison in a fresh subprocess (the byte-for-byte
    on-disk view a CI re-check uses).  Returns the fresh result, but if the
    fresh and in-process results disagree on ``clean`` or on their mismatch
    set, the board is treated as DIRTY: a synthetic ``open`` mismatch is
    appended recording the divergence so the gate trips and the divergence
    is visible in ``lvs.json`` / the summary (#3838).
    """
    fresh = _fresh_copper_compare(sch_path, routed_pcb_path)

    if _copper_mismatch_key(fresh) == _copper_mismatch_key(in_process):
        return fresh

    # Disagreement on identical bytes -> fail closed.  Surface BOTH legs so
    # the divergence itself is debuggable, and union their mismatches so no
    # real defect is hidden by whichever leg happened to look cleaner.
    print(
        "   copper-LVS GATE DIVERGENCE (#3838): in-process result disagrees "
        "with a fresh out-of-process re-check on the same persisted bytes; "
        "failing closed (treating board as DIRTY)."
    )
    print(f"      in-process: clean={in_process.clean} ({len(in_process.mismatches)} mismatch(es))")
    print(f"      fresh:      clean={fresh.clean} ({len(fresh.mismatches)} mismatch(es))")

    combined = dict.fromkeys(_copper_mismatch_key(in_process)[1] | _copper_mismatch_key(fresh)[1])
    union = tuple(
        CopperLVSMismatch(kind=k, net_a=a, net_b=b, pad_a=pa, pad_b=pb)
        for (k, a, b, pa, pb) in combined
    )
    divergence = CopperLVSMismatch(
        kind="open",
        net_a="<gate-divergence>",
        net_b="<gate-divergence>",
        pad_a=f"in_process.clean={in_process.clean}",
        pad_b=f"fresh.clean={fresh.clean}",
    )
    return CopperLVSResult(clean=False, mismatches=(divergence, *union))


def write_lvs_report(
    sch_path: Path,
    routed_pcb_path: Path,
    output_dir: Path,
    *,
    require_clean: bool = True,
    run_copper: bool = True,
    run_label: bool = True,
    fresh_copper_check: bool = True,
) -> tuple[bool, bool]:
    """Run copper + label LVS, write ``output/lvs.json``, optionally raise.

    Args:
        sch_path: Path to the schematic (design intent).
        routed_pcb_path: Path to the routed PCB (what manufacturing sees).
        output_dir: Directory to write ``lvs.json`` into (created if absent).
        require_clean: When ``True`` (hard gate), raise
            :class:`BoardNetlistMismatch` if any *gated* comparator is dirty.
            When ``False`` (advisory), log the mismatch summary and return
            the dirty flags without raising.  A *vacuous* copper result
            (schematic binds zero pins, #4005 review) counts as dirty:
            gating copper-only on a wireless schematic raises rather than
            passing on zero evidence.
        run_copper: When ``True``, run the copper-extracted comparator and
            include it in the gated ``clean`` decision.
        run_label: When ``True``, run the label-based comparator and include
            it in the gated ``clean`` decision.
        fresh_copper_check: When ``True`` (default) and ``run_copper`` is
            set, the copper-LVS result used for the gate is re-derived in a
            *fresh subprocess* against the persisted ``routed_pcb_path``
            bytes (issue #3838), so the gated decision equals what a clean
            out-of-process re-check / CI sees.  If the in-process and fresh
            results disagree the board is treated as DIRTY (fail closed).
            Set ``False`` only in unit tests that monkeypatch the
            in-process comparator and have no real files on disk.

    Returns:
        ``(copper_clean, label_clean)``.  A comparator that was not run is
        reported as ``True`` (vacuously clean) so callers can treat the
        return as "nothing gated is dirty".

    Raises:
        BoardNetlistMismatch: when ``require_clean`` and a gated comparator
            is dirty.  The report is still written before raising.
        FreshCopperCheckError: when the fresh out-of-process copper check
            cannot be obtained (subprocess failure / unparseable output).
        ValueError: when neither comparator is selected to run.
    """
    if not run_copper and not run_label:
        raise ValueError("write_lvs_report: at least one of run_copper/run_label must be True")

    print("\n" + "=" * 60)
    print("Running LVS (schematic <-> PCB netlist match)...")
    print("=" * 60)

    copper_result = compare_copper_netlist(sch_path, routed_pcb_path) if run_copper else None
    label_result = compare_netlists(sch_path, routed_pcb_path) if run_label else None

    # Make the copper leg authoritative against the ON-DISK artifact: the
    # in-process ``compare_copper_netlist`` above can see a cleaner board
    # than a fresh process does (Python-post-processed fill that a fresh
    # kicad-cli refill reverts; shapely-availability skew).  Re-derive the
    # gated copper result in a fresh subprocess and fail closed if the two
    # disagree on identical bytes (#3838).
    if run_copper and fresh_copper_check and copper_result is not None:
        copper_result = _authoritative_copper_result(
            sch_path, routed_pcb_path, in_process=copper_result
        )

    copper_clean = copper_result.clean if copper_result is not None else True
    label_clean = label_result.clean if label_result is not None else True

    # ``clean`` is the AND of only the *gated* comparators.  A comparator
    # that was run but not selected does not flip ``clean``.
    gated_clean = copper_clean and label_clean

    output_dir.mkdir(parents=True, exist_ok=True)
    lvs_path = output_dir / "lvs.json"
    lvs_path.write_text(
        json.dumps(
            _build_payload(copper_result, label_result, clean=gated_clean),
            indent=2,
        )
        + "\n"
    )

    _print_summary(copper_result, label_result, lvs_path, run_copper, run_label)

    if not gated_clean and require_clean:
        # Reuse BoardNetlistMismatch (board-00's exit-gate exception).  When
        # only copper is dirty we synthesize an LVSResult so the exception's
        # carried ``.result`` still reflects "dirty"; the human-readable
        # copper detail was already printed by ``_print_summary``.
        raise BoardNetlistMismatch(_mismatch_result(copper_result, label_result))

    return copper_clean, label_clean


def _build_payload(
    copper_result: CopperLVSResult | None,
    label_result: LVSResult | None,
    *,
    clean: bool,
) -> dict:
    """Assemble the v1 ``lvs.json`` payload.

    ``mismatches`` carries the label-based mismatches (unchanged from the
    historical board-00 schema so ``_parse_lvs`` and the e2e asserter keep
    working).  ``copper_mismatches`` is an additive field recording the
    copper-extracted shorts/opens so a copper-dirty board is reflected too.
    ``copper_vacuous`` / ``copper_bound_pad_count`` (additive, #4005
    review) record whether the copper verdict carried any schematic
    evidence; a vacuous copper leg forces ``clean=false``.
    """
    payload: dict = {
        "$schema": _LVS_SCHEMA_URL,
        "clean": clean,
        "mismatches": [
            {
                "ref": lm.ref,
                "pad": lm.pad,
                "schematic_net": lm.schematic_net,
                "pcb_net": lm.pcb_net,
            }
            for lm in (label_result.mismatches if label_result is not None else ())
        ],
        "copper_mismatches": [
            {
                "kind": cm.kind,
                "net_a": cm.net_a,
                "net_b": cm.net_b,
                "pad_a": cm.pad_a,
                "pad_b": cm.pad_b,
            }
            for cm in (copper_result.mismatches if copper_result is not None else ())
        ],
    }
    if copper_result is not None:
        payload["copper_vacuous"] = copper_result.vacuous
        payload["copper_bound_pad_count"] = copper_result.bound_pad_count
    return payload


def _mismatch_result(
    copper_result: CopperLVSResult | None,
    label_result: LVSResult | None,
) -> LVSResult:
    """Pick the LVSResult to carry on the raised exception.

    Prefer the label result when it is the dirty one (it has the
    pin-level ``ref/pad`` detail the exception message renders).  When only
    copper is dirty, fall back to a synthetic dirty ``LVSResult`` (the
    copper detail was already printed in the summary).
    """
    if label_result is not None and not label_result.clean:
        return label_result
    return LVSResult(clean=False, mismatches=())


def _print_summary(
    copper_result: CopperLVSResult | None,
    label_result: LVSResult | None,
    lvs_path: Path,
    run_copper: bool,
    run_label: bool,
) -> None:
    """Print a human-readable LVS summary to the recipe log."""
    if run_label and label_result is not None:
        if label_result.clean:
            print(f"\n   label-LVS PASS: 0 mismatches ({lvs_path.name})")
        else:
            print(f"\n   label-LVS FAIL: {len(label_result.mismatches)} mismatch(es):")
            for lm in label_result.mismatches[:5]:
                print(
                    f"      - {lm.ref}.{lm.pad}: schematic={lm.schematic_net!r} pcb={lm.pcb_net!r}"
                )

    if run_copper and copper_result is not None:
        if copper_result.vacuous:
            print(
                "   copper-LVS VACUOUS (treated as FAIL): schematic binds 0 pins "
                "-- no shorts/opens are detectable, so 'clean' would be "
                "zero-evidence (#4005 review).  Wire the schematic or skip the "
                "LVS step explicitly."
            )
        elif copper_result.clean:
            bound = copper_result.bound_pad_count
            evidence = f" ({bound} bound pad(s))" if bound is not None else ""
            print(f"   copper-LVS PASS: 0 shorts / 0 opens{evidence}")
        else:
            print(
                f"   copper-LVS FAIL: {len(copper_result.shorts)} short(s) / "
                f"{len(copper_result.opens)} open(s):"
            )
            for cm in copper_result.mismatches[:5]:
                print(f"      - {cm.kind}: {cm.net_a} <-> {cm.net_b} ({cm.pad_a}, {cm.pad_b})")
