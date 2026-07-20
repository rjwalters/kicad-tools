"""Tests for ``kct check --emit-dru`` / ``--emit-drc-constraints`` (issue #4375).

``kicad-cli pcb drc`` reasons over a board's *embedded* Board Setup while
``kct check --mfr <tier>`` reasons over the *manufacturer fab floors*.  On a
board whose embedded setup is looser than the tier the two engines disagree
silently (0-vs-63 on the repro board).  These flags make the two engines
agree **by construction**: after the pure-Python check resolves its
``DesignRules`` (layer count + copper weights + net-class map), it emits the
sidecars ``kicad-cli`` auto-loads (``<board>.kicad_dru`` and, for the full
flag, ``<board>.kicad_pro``) from that SAME resolved rules object -- so there
is no separately-resolved profile that could drift.

The load-bearing contracts pinned here:

1. **Parity by construction** -- the emitted ``.kicad_dru`` is byte-identical
   to ``generate_dru(checker.design_rules, ...)`` for the resolved tier /
   layer / copper, so the two engines cannot silently diverge.
2. **DRU-only vs both** -- ``--emit-dru`` writes only the ``.kicad_dru``;
   ``--emit-drc-constraints`` also writes the ``.kicad_pro`` whose applied
   Default netclass clearance is required for kicad-cli clearance parity
   (#4097).
3. **Pure side effect** -- the ``.kicad_pcb`` is byte-identical afterward and
   the exit code is unchanged versus the same invocation without the flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.check_cmd import _emit_drc_sidecars, main
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.dru_generator import generate_dru
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_03 = REPO_ROOT / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"


@pytest.fixture
def board_copy(tmp_path: Path) -> Path:
    """A writable copy of board 03 whose sidecars land in ``tmp_path``."""
    dst = tmp_path / "board.kicad_pcb"
    dst.write_text(BOARD_03.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


def _resolved_layers(pcb_path: Path) -> int:
    """Layer count as ``kct check`` auto-detects it (no explicit --layers)."""
    detected = len(PCB.load(pcb_path).copper_layers)
    return detected if detected > 0 else 2


# ---------------------------------------------------------------------------
# Parity by construction: emitted .kicad_dru == generate_dru(design_rules)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mfr", "layers", "copper"),
    [
        ("jlcpcb", 2, 1.0),
        ("jlcpcb", 4, 2.0),
    ],
)
def test_emit_dru_matches_checker_resolved_rules(
    board_copy: Path, mfr: str, layers: int, copper: float
) -> None:
    """The emitted ``.kicad_dru`` equals the rules the check actually used.

    This is the whole point of wiring emission into ``kct check`` rather than
    re-resolving a fresh profile: the sidecar is emitted from
    ``checker.design_rules``, so a mismatched ``--layers`` / ``--copper`` can
    never make the two engines disagree.
    """
    rc = main(
        [
            str(board_copy),
            "--mfr",
            mfr,
            "--layers",
            str(layers),
            "--copper",
            str(copper),
            "--emit-dru",
            "--allow-incomplete",
        ]
    )
    assert rc in (0, 2)  # verdict, not a tool error

    dru_path = board_copy.with_suffix(".kicad_dru")
    assert dru_path.exists()

    # Reconstruct the checker with the SAME explicit inputs the CLI resolved
    # and confirm the emitted text is byte-identical to what its resolved
    # design_rules produce -- parity by construction.
    pcb = PCB.load(board_copy)
    checker = DRCChecker(
        pcb,
        manufacturer=mfr,
        layers=layers,
        copper_oz=copper,
        copper_oz_outer=copper,
    )
    expected = generate_dru(checker.design_rules, manufacturer_name=mfr)
    assert dru_path.read_text(encoding="utf-8") == expected

    # And the emitted floors track the profile minimums for that tier.
    rules = checker.design_rules
    assert f"(min {rules.min_trace_width_mm}mm)" in dru_path.read_text(encoding="utf-8")
    assert f"(min {rules.min_clearance_mm}mm)" in dru_path.read_text(encoding="utf-8")


def test_emit_dru_micro_via_exemption_present(board_copy: Path) -> None:
    """The emitted DRU keeps the micro-via exemption (#3118/#3734).

    ``validate/rules/dimensions.py`` exempts micro vias from the standard
    via floors; the DRU must mirror that or parity silently regresses.
    """
    main([str(board_copy), "--mfr", "jlcpcb", "--emit-dru", "--allow-incomplete"])
    dru = board_copy.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert "A.Via_Type != 'Micro'" in dru


# ---------------------------------------------------------------------------
# Ampacity dedup: one rule pair per CLASS, not per NET (#4375 Judge feedback)
# ---------------------------------------------------------------------------


def test_emit_dedups_shared_ampacity_class_to_one_rule_pair(board_copy: Path) -> None:
    """A class spanning several nets emits ONE ampacity rule pair, not N.

    ``_emit_drc_sidecars`` receives the resolved ``net_class_map`` -- keyed by
    board-net name, with the SAME ampacity class attached to every net in that
    class.  A ``--net-class-map`` sidecar deserializes a *distinct-but-same-*
    ``name`` object per net, so identity-based dedup (``dict.fromkeys``) cannot
    collapse them.  Feeding ``list(net_class_map.values())`` straight to
    ``generate_dru`` therefore emitted one ``Ampacity Min Width`` rule pair per
    NET (3 nets -> 6 rules) instead of per CLASS, bloating the DRU and risking
    KiCad duplicate-rule-name warnings -- contradicting "clean by construction".

    This drives the REAL emission shape (multiple net keys -> distinct objects
    sharing ``name`` + ``target_ampacity``) and asserts each ampacity rule name
    appears exactly once.
    """
    pcb = PCB.load(board_copy)
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4, copper_oz=2.0, copper_oz_outer=2.0)

    # Distinct objects, same name -- exactly what a sidecar deserialization
    # produces for three nets sharing one POWER class.
    net_class_map = {
        "VBUS": NetClassRouting(name="POWER", target_ampacity=15.0),
        "+5V": NetClassRouting(name="POWER", target_ampacity=15.0),
        "GND": NetClassRouting(name="POWER", target_ampacity=15.0),
    }

    _emit_drc_sidecars(
        board_copy,
        checker,
        manufacturer_id="jlcpcb",
        layers=4,
        copper_oz=2.0,
        net_class_map=net_class_map,
        emit_both=False,
    )

    dru = board_copy.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert dru.count("Ampacity Min Width (POWER, external)") == 1
    assert dru.count("Ampacity Min Width (POWER, internal)") == 1
    # Byte-identical to a SINGLE deduplicated class -- the DRU carries no
    # per-net redundancy.
    expected = generate_dru(
        checker.design_rules,
        manufacturer_name="jlcpcb",
        net_classes=[NetClassRouting(name="POWER", target_ampacity=15.0)],
    )
    assert dru == expected


def test_emit_preserves_distinct_ampacity_classes(board_copy: Path) -> None:
    """Dedup collapses only same-name classes -- distinct classes both survive.

    Guards against an over-eager dedup that would drop a second, genuinely
    different ampacity class (e.g. POWER + MOTOR spanning different nets).
    """
    pcb = PCB.load(board_copy)
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4, copper_oz=2.0, copper_oz_outer=2.0)

    net_class_map = {
        "VBUS": NetClassRouting(name="POWER", target_ampacity=15.0),
        "+5V": NetClassRouting(name="POWER", target_ampacity=15.0),
        "PHASE_A": NetClassRouting(name="MOTOR", target_ampacity=8.0),
        "PHASE_B": NetClassRouting(name="MOTOR", target_ampacity=8.0),
    }

    _emit_drc_sidecars(
        board_copy,
        checker,
        manufacturer_id="jlcpcb",
        layers=4,
        copper_oz=2.0,
        net_class_map=net_class_map,
        emit_both=False,
    )

    dru = board_copy.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert dru.count("Ampacity Min Width (POWER, external)") == 1
    assert dru.count("Ampacity Min Width (POWER, internal)") == 1
    assert dru.count("Ampacity Min Width (MOTOR, external)") == 1
    assert dru.count("Ampacity Min Width (MOTOR, internal)") == 1


# ---------------------------------------------------------------------------
# DRU-only vs both sidecars
# ---------------------------------------------------------------------------


def test_emit_dru_writes_only_dru(board_copy: Path) -> None:
    """``--emit-dru`` writes the ``.kicad_dru`` and NOT the ``.kicad_pro``."""
    main([str(board_copy), "--mfr", "jlcpcb", "--emit-dru", "--allow-incomplete"])
    assert board_copy.with_suffix(".kicad_dru").exists()
    assert not board_copy.with_suffix(".kicad_pro").exists()


def test_emit_drc_constraints_writes_both(board_copy: Path) -> None:
    """``--emit-drc-constraints`` writes both sidecars, and the ``.kicad_pro``
    Default netclass clearance is relaxed to the tier floor (#4097 parity)."""
    import json

    main([str(board_copy), "--mfr", "jlcpcb", "--emit-drc-constraints", "--allow-incomplete"])
    pro_path = board_copy.with_suffix(".kicad_pro")
    assert board_copy.with_suffix(".kicad_dru").exists()
    assert pro_path.exists()

    layers = _resolved_layers(board_copy)
    rules = get_profile("jlcpcb").get_design_rules(layers=layers, copper_oz=1.0)
    project = json.loads(pro_path.read_text(encoding="utf-8"))
    classes = project["net_settings"]["classes"]
    default_cls = next(c for c in classes if c.get("name") == "Default")
    # kicad-cli's clearance test reads the APPLIED Default netclass clearance,
    # not min_clearance -- this write is what makes clearance agree (#4097).
    assert default_cls["clearance"] == rules.min_clearance_mm


# ---------------------------------------------------------------------------
# Pure side effect: pcb untouched, exit code unchanged
# ---------------------------------------------------------------------------


def test_emit_leaves_pcb_byte_identical(board_copy: Path) -> None:
    """Emission never mutates the ``.kicad_pcb`` (sidecars only)."""
    before = board_copy.read_bytes()
    main([str(board_copy), "--mfr", "jlcpcb", "--emit-drc-constraints", "--allow-incomplete"])
    assert board_copy.read_bytes() == before


def test_emit_does_not_change_exit_code(board_copy: Path, tmp_path: Path) -> None:
    """The verdict/exit code is identical with and without the flag."""
    plain = tmp_path / "plain.kicad_pcb"
    plain.write_text(board_copy.read_text(encoding="utf-8"), encoding="utf-8")

    rc_plain = main([str(plain), "--mfr", "jlcpcb", "--allow-incomplete"])
    rc_emit = main([str(board_copy), "--mfr", "jlcpcb", "--emit-dru", "--allow-incomplete"])
    assert rc_plain == rc_emit


def test_emit_degrades_on_unwritable_dir(board_copy: Path, capsys, monkeypatch) -> None:
    """A sidecar write failure warns without failing the check (#4375).

    The failure is injected by monkeypatching the ``.kicad_dru`` write to
    raise ``OSError`` directly, which exercises the production
    ``except OSError`` degrade path independent of the process euid.  An
    earlier version relied on ``os.chmod(parent, read-only)``, but CI runs
    pytest as root and root bypasses directory permission bits, so the write
    succeeded and the warning never fired.
    """
    plain_rc = main([str(board_copy), "--mfr", "jlcpcb", "--allow-incomplete"])
    capsys.readouterr()

    real_write_text = Path.write_text

    def failing_write_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.suffix == ".kicad_dru":
            raise OSError("injected write failure")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    rc = main([str(board_copy), "--mfr", "jlcpcb", "--emit-dru", "--allow-incomplete"])

    captured = capsys.readouterr()
    # Verdict unchanged despite the failed sidecar write, and a warning fired.
    assert rc == plain_rc
    assert "could not emit" in captured.err.lower()
