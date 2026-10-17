"""Pin the endpoint-bucketing semantics of the shared wire helpers (issue #7008).

``remove-wire`` / ``remove-component`` / ``disconnect`` bucket by 0.1 mm
truncation while ``cleanup-wires`` buckets at micron resolution by rounding.
They are intentionally different; unifying them is a user-visible change that
needs its own changelog fragment.  These tests fail if a tidy-up merges them.
"""

from __future__ import annotations

from kicad_tools.cli.sch_cleanup_wires import _quantize
from kicad_tools.cli.sch_wire_common import (
    _find_wires_at_point,
    _find_wires_at_point_indexed,
    _wire_endpoint_counts,
    truncating_bucket,
)
from kicad_tools.operations.symbol_ops import _find_wires_at_point as symbol_ops_find
from kicad_tools.schema import Schematic
from kicad_tools.sexp import SExp, parse_string

_SCH = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (wire (pts (xy 100 50) (xy 150 50)) (uuid "w1"))
  (wire (pts (xy 100 50) (xy 100 100)) (uuid "w2"))
  (wire (pts (xy 200 200) (xy 210 200)) (uuid "w3"))
)
"""


def _wire(x1: float, y1: float, x2: float, y2: float) -> SExp:
    return SExp.list("wire", SExp.list("pts", SExp.list("xy", x1, y1), SExp.list("xy", x2, y2)))


def _micron_key(x: float, y: float) -> tuple[int, int]:
    return (_quantize(x), _quantize(y))


class TestEndpointBucketSplit:
    def test_truncating_key_separates_12_69999_from_12_7(self):
        wires = [_wire(12.69999, 0.0, 20.0, 0.0), _wire(12.7, 0.0, 30.0, 0.0)]
        counts = _wire_endpoint_counts(wires, key=truncating_bucket)
        assert counts[(126, 0)] == 1
        assert counts[(127, 0)] == 1

    def test_micron_key_merges_12_69999_with_12_7(self):
        wires = [_wire(12.69999, 0.0, 20.0, 0.0), _wire(12.7, 0.0, 30.0, 0.0)]
        counts = _wire_endpoint_counts(wires, key=_micron_key)
        assert counts[(_quantize(12.7), 0)] == 2

    def test_key_is_required(self):
        try:
            _wire_endpoint_counts([_wire(0, 0, 1, 1)])  # type: ignore[call-arg]
        except TypeError:
            return
        raise AssertionError("_wire_endpoint_counts must not default its bucket key")


class TestFindWiresAtPointShapes:
    def test_shape_a_returns_bare_sexps(self, tmp_path):
        path = tmp_path / "t.kicad_sch"
        path.write_text(_SCH)
        sch = Schematic.load(path)
        hits = _find_wires_at_point(sch, (100.0, 50.0))
        assert len(hits) == 2
        assert all(isinstance(h, SExp) for h in hits)

    def test_shape_b_returns_sexp_and_endpoint_index(self, tmp_path):
        path = tmp_path / "t.kicad_sch"
        path.write_text(_SCH)
        sch = Schematic.load(path)
        hits = _find_wires_at_point_indexed(sch, (150.0, 50.0))
        assert len(hits) == 1
        wire, idx = hits[0]
        assert isinstance(wire, SExp)
        assert idx == 1

    def test_symbol_ops_variant_takes_raw_sexp(self):
        sexp = parse_string(_SCH)
        hits = symbol_ops_find(sexp, (100.0, 50.0))
        assert sorted(idx for _w, idx in hits) == [0, 0]
