"""Regression tests for the --drc-aware gate (Issue #8052)."""

from types import SimpleNamespace

from kicad_tools.router.optimizer import pcb as pcb_mod
from kicad_tools.router.optimizer.config import OptimizationConfig
from kicad_tools.router.optimizer.pcb import optimize_pcb, parse_segments

_PCB = """\
(kicad_pcb
  (net 0 "")
  (net 1 "A")
  (net 2 "B")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 110 100) (end 120 100) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 100 200) (end 110 200) (width 0.25) (layer "F.Cu") (net 2))
  (segment (start 110 200) (end 120 200) (width 0.25) (layer "F.Cu") (net 2))
)
"""


def _merge(segs):
    """Merge a net's collinear segments into one."""
    first, last = segs[0], segs[-1]
    first = type(first)(**{**first.__dict__, "x2": last.x2, "y2": last.y2})
    return [first]


def _run(tmp_path, monkeypatch, counter):
    src = tmp_path / "in.kicad_pcb"
    out = tmp_path / "out.kicad_pcb"
    src.write_text(_PCB)
    monkeypatch.setattr(pcb_mod, "_run_drc_error_count_by_category", counter)
    cfg = OptimizationConfig(drc_aware=True, drc_manufacturer="jlcpcb")
    stats = optimize_pcb(str(src), str(out), _merge, cfg)
    return stats, parse_segments(out.read_text())


def _count_for(text, baseline):
    """Fake DRC: a short appears whenever net 1 has been merged."""
    merged = text.count("(net 1)") < 2
    counts = {"__total__": 0}
    if merged:
        counts = {"__total__": 1, "clearance_segment_zone": 1}
    return counts


def test_zone_short_rolls_back_offending_net(tmp_path, monkeypatch):
    stats, segs = _run(
        tmp_path,
        monkeypatch,
        lambda text, **kw: _count_for(text, 0),
    )
    assert stats.nets_rolled_back == 1
    assert stats.drc_errors_after == 0
    assert not stats.drc_failsafe_reverted
    assert len(segs["A"]) == 2  # restored
    assert len(segs["B"]) == 1  # clean merge kept


def test_failsafe_reverts_everything_when_baseline_unreachable(tmp_path, monkeypatch):
    def counter(text, **kw):
        # Every variant (even all-original) reports the short only for
        # non-original text; the loop's first trial cannot fix it because the
        # short needs *both* nets reverted.
        both_original = text.count("(net 1)") == 2 and text.count("(net 2)") == 2
        if both_original:
            return {"__total__": 0}
        return {"__total__": 1, "clearance_net0_bridge": 1}

    stats, segs = _run(tmp_path, monkeypatch, counter)
    assert stats.drc_failsafe_reverted
    assert stats.drc_errors_after == 0
    assert len(segs["A"]) == 2 and len(segs["B"]) == 2


def test_count_includes_zone_checks(monkeypatch):
    calls = []

    class _Res:
        errors: list = []
        error_count = 0

        def merge(self, other):
            return None

    class _Checker:
        def __init__(self, *a, **k):
            pass

        def check_clearances(self):
            return _Res()

        def check_dimensions(self):
            calls.append("dim")
            return _Res()

        def check_segment_zone_clearances(self):
            calls.append("seg_zone")
            return _Res()

        def check_via_zone_clearances(self):
            calls.append("via_zone")
            return _Res()

    import kicad_tools.schema.pcb as schema_pcb
    import kicad_tools.validate as validate

    monkeypatch.setattr(schema_pcb.PCB, "load", staticmethod(lambda p: SimpleNamespace()))
    monkeypatch.setattr(validate, "DRCChecker", _Checker)
    counts = pcb_mod._run_drc_error_count_by_category(
        _PCB, manufacturer="jlcpcb", layers=2, copper_oz=1.0
    )
    assert counts["__total__"] == 0
    assert {"seg_zone", "via_zone"} <= set(calls)


# --- Issue #8207: diagnostics -------------------------------------------


def test_rollback_records_new_violations_and_kept(tmp_path, monkeypatch):
    stats, _ = _run(tmp_path, monkeypatch, lambda text, **kw: _count_for(text, 0))
    rows = {n.net: n for n in stats.per_net}
    assert rows["A"].rolled_back
    assert rows["A"].new_violations == {"clearance_segment_zone": 1}
    assert rows["B"].new_violations == {}
    assert stats.nets_kept == 1 and stats.nets_optimized == 2


def test_failsafe_names_rules_and_writes_input_verbatim(tmp_path, monkeypatch):
    def counter(text, **kw):
        if text.count("(net 1)") == 2 and text.count("(net 2)") == 2:
            return {"__total__": 0}
        return {"__total__": 1, "clearance_net0_bridge": 1}

    stats, _ = _run(tmp_path, monkeypatch, counter)
    assert stats.drc_failsafe_reverted
    assert stats.drc_failsafe_rules == {"clearance_net0_bridge": 1}
    assert stats.drc_errors_after == stats.drc_errors_before
    assert stats.nets_kept == 0
    assert (tmp_path / "out.kicad_pcb").read_text() == _PCB


def test_replace_segments_round_trips_unchanged_geometry():
    from kicad_tools.router.optimizer.pcb import replace_segments

    segs = parse_segments(_PCB)
    again = parse_segments(replace_segments(_PCB, segs, segs))
    assert again == segs


# --- Issue #8367: partial keep when the greedy loop cannot reach baseline --


def test_failsafe_keeps_clean_nets_and_rejects_only_bad_one(tmp_path, monkeypatch):
    calls = {"n": 0}

    def counter(text, **kw):
        calls["n"] += 1
        # Calls 2-4 are the full-board check and the greedy trials: a noisy
        # referee reports +1 for all of them, so the greedy loop cannot get
        # back to baseline.  Afterwards only net A's merge is truly bad.
        if 2 <= calls["n"] <= 4:
            return {"__total__": 1, "clearance_pad_segment": 1}
        if text.count("(net 1)") < 2:
            return {"__total__": 1, "clearance_pad_segment": 1}
        return {"__total__": 0}

    stats, segs = _run(tmp_path, monkeypatch, counter)
    assert not stats.drc_failsafe_reverted
    assert len(segs["A"]) == 2  # bad net rolled back
    assert len(segs["B"]) == 1  # clean net kept
    assert stats.nets_rolled_back == 1
    assert stats.nets_kept == 1
    assert stats.drc_errors_after == 0


# --- Issue #8367: referee uses the board's own clearance -------------------


def test_referee_uses_board_clearance_not_preset(monkeypatch):
    """A 0.1 mm board must not be refereed at the manufacturer preset."""
    from kicad_tools.manufacturers import get_profile

    seen = {}

    class _Res:
        errors: list = []

        def merge(self, other):
            return None

    class _Checker:
        def __init__(self, *a, **k):
            self.design_rules = get_profile("jlcpcb").get_design_rules(2, 1.0)

        def check_clearances(self):
            seen["clearance"] = self.design_rules.min_clearance_mm
            return _Res()

        check_dimensions = check_segment_zone_clearances = check_via_zone_clearances = lambda self: (
            _Res()
        )

    import kicad_tools.schema.pcb as schema_pcb
    import kicad_tools.validate as validate

    monkeypatch.setattr(schema_pcb.PCB, "load", staticmethod(lambda p: SimpleNamespace()))
    monkeypatch.setattr(validate, "DRCChecker", _Checker)
    kw = {"manufacturer": "jlcpcb", "layers": 2, "copper_oz": 1.0}
    pcb_mod._run_drc_error_count_by_category(_PCB, **kw)
    preset = seen["clearance"]
    pcb_mod._run_drc_error_count_by_category(_PCB, min_clearance_mm=0.0508, **kw)
    assert seen["clearance"] == 0.0508 != preset


# --- Issue #8367: external arbitration when the referee is rule-blind ------

_PCB3 = _PCB.replace('(net 2 "B")', '(net 2 "B")\n  (net 3 "C")').replace(
    "\n)\n",
    '\n  (segment (start 100 300) (end 110 300) (width 0.25) (layer "F.Cu") (net 3))'
    '\n  (segment (start 110 300) (end 120 300) (width 0.25) (layer "F.Cu") (net 3))\n)\n',
)


def _run3(tmp_path, monkeypatch, arbiter):
    src = tmp_path / "in.kicad_pcb"
    out = tmp_path / "out.kicad_pcb"
    src.write_text(_PCB3)
    # Rule-blind referee: any change to any net reads as a new violation.
    monkeypatch.setattr(
        pcb_mod,
        "_run_drc_error_count_by_category",
        lambda text, **kw: (
            {"__total__": 0}
            if all(text.count(f"(net {i})") == 2 for i in (1, 2, 3))
            else {"__total__": 1, "clearance_pad_segment": 1}
        ),
    )
    cfg = OptimizationConfig(drc_aware=True, drc_manufacturer="jlcpcb", drc_arbiter=arbiter)
    stats = optimize_pcb(str(src), str(out), _merge, cfg)
    return stats, parse_segments(out.read_text())


def test_arbiter_keeps_clean_nets_when_referee_rejects_all(tmp_path, monkeypatch):
    # kicad-cli only objects to net C's merge.
    arb = lambda text: 1 if text.count("(net 3)") < 2 else 0  # noqa: E731
    stats, segs = _run3(tmp_path, monkeypatch, arb)
    assert not stats.drc_failsafe_reverted
    assert len(segs["A"]) == 1 and len(segs["B"]) == 1  # kept
    assert len(segs["C"]) == 2  # rejected
    assert stats.nets_kept == 2 and stats.nets_rolled_back == 1


def test_arbiter_all_bad_still_reverts_everything(tmp_path, monkeypatch):
    arb = lambda text: 0 if all(text.count(f"(net {i})") == 2 for i in (1, 2, 3)) else 1  # noqa: E731
    stats, segs = _run3(tmp_path, monkeypatch, arb)
    assert stats.drc_failsafe_reverted
    assert all(len(v) == 2 for v in segs.values())


def test_arbiter_unavailable_keeps_in_process_behaviour(tmp_path, monkeypatch):
    stats, segs = _run3(tmp_path, monkeypatch, lambda text: None)
    assert stats.drc_failsafe_reverted
    assert all(len(v) == 2 for v in segs.values())
