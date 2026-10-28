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
