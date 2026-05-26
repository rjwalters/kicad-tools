"""Schema-level via round-trip tests for issue #3124.

The schema-level ``Via`` class (``kicad_tools.schema.pcb.Via``) is the
KiCad-format-aware parser/serializer used by the higher-level PCB
editor and by any code path that reads + re-emits a PCB file through
the schema layer.  Prior to issue #3124 it silently dropped the
``(via micro ...)`` / ``(via blind ...)`` / ``(via buried ...)``
leading type tokens because it had no field for them.

These tests cover:

1. ``from_sexp`` parses the leading ``micro`` token into
   ``Via.via_type = "micro"``.
2. ``to_sexp`` re-emits the token when ``via_type`` is set.
3. Standard (through-hole) vias have ``via_type = None`` and emit
   no leading token.
4. End-to-end parse + emit + re-parse preserves the type.

This is the prerequisite for #3118 (micro-via in-pad fallback): the
router's escape-routing code can produce ``Via(is_micro=True)``, and
when the finalize pipeline reads the PCB and re-writes it (e.g. for
the multi-strategy matrix), the schema layer must not drop the token.
"""

from __future__ import annotations

import pytest

from kicad_tools.schema.pcb import Via
from kicad_tools.sexp.parser import SExpParser, SExpSerializer


def _parse_one(text: str):
    """Parse a single S-expression node."""
    return SExpParser(text).parse()


def _serialize(node) -> str:
    """Serialize an SExp node to string."""
    return SExpSerializer().serialize(node)


class TestSchemaViaMicroParse:
    """from_sexp recognizes the leading micro token."""

    def test_parses_micro_token(self):
        text = """(via micro
\t(at 10.0 20.0)
\t(size 0.3)
\t(drill 0.15)
\t(layers "F.Cu" "In1.Cu")
\t(net 5)
\t(uuid "abc-123")
)"""
        sexp = _parse_one(text)
        via = Via.from_sexp(sexp)
        assert via.via_type == "micro"
        assert via.position == (10.0, 20.0)
        assert via.size == 0.3
        assert via.drill == 0.15
        assert via.layers == ["F.Cu", "In1.Cu"]
        assert via.net_number == 5
        assert via.uuid == "abc-123"

    def test_parses_blind_token(self):
        """Schema preserves blind token too (parser handles all three)."""
        text = """(via blind
\t(at 5.0 5.0)
\t(size 0.6)
\t(drill 0.3)
\t(layers "F.Cu" "In2.Cu")
\t(net 1)
\t(uuid "xx")
)"""
        sexp = _parse_one(text)
        via = Via.from_sexp(sexp)
        assert via.via_type == "blind"

    def test_parses_buried_token(self):
        """Schema preserves buried token too."""
        text = """(via buried
\t(at 5.0 5.0)
\t(size 0.6)
\t(drill 0.3)
\t(layers "In1.Cu" "In2.Cu")
\t(net 1)
\t(uuid "xx")
)"""
        sexp = _parse_one(text)
        via = Via.from_sexp(sexp)
        assert via.via_type == "buried"

    def test_standard_via_has_no_type(self):
        """Through-hole vias parse with via_type = None."""
        text = """(via
\t(at 10.0 20.0)
\t(size 0.6)
\t(drill 0.3)
\t(layers "F.Cu" "B.Cu")
\t(net 2)
\t(uuid "yy")
)"""
        sexp = _parse_one(text)
        via = Via.from_sexp(sexp)
        assert via.via_type is None


class TestSchemaViaMicroEmit:
    """to_sexp emits the leading micro token when via_type is set."""

    def test_emits_micro_token(self):
        via = Via(
            position=(10.0, 20.0),
            size=0.3,
            drill=0.15,
            layers=["F.Cu", "In1.Cu"],
            net_number=5,
            uuid="abc-123",
            via_type="micro",
        )
        sexp = via.to_sexp()
        # First child after via name must be the "micro" atom.
        assert sexp.values, "Emitted sexp must have children"
        first = sexp.values[0]
        assert first == "micro", f"Micro via must emit 'micro' as first child atom, got: {first!r}"
        # Serialized form contains the token.
        serialized = _serialize(sexp)
        assert "micro" in serialized

    def test_emits_no_token_for_standard(self):
        via = Via(
            position=(10.0, 20.0),
            size=0.6,
            drill=0.3,
            layers=["F.Cu", "B.Cu"],
            net_number=2,
            uuid="yy",
            via_type=None,
        )
        sexp = via.to_sexp()
        # First child is the (at ...) list, not an atom.
        assert sexp.values
        first = sexp.values[0]
        from kicad_tools.sexp.parser import SExp

        assert isinstance(first, SExp), f"Standard via must NOT have leading atom, got: {first!r}"
        assert first.name == "at"


class TestSchemaViaRoundTrip:
    """parse -> emit -> reparse preserves the micro token."""

    @pytest.mark.parametrize("via_type", ["micro", "blind", "buried"])
    def test_via_type_roundtrips(self, via_type: str):
        text = f"""(via {via_type}
\t(at 12.3 45.6)
\t(size 0.4)
\t(drill 0.2)
\t(layers "F.Cu" "In1.Cu")
\t(net 7)
\t(uuid "rt-{via_type}")
)"""
        # First parse
        via1 = Via.from_sexp(_parse_one(text))
        assert via1.via_type == via_type

        # Emit + reparse
        emitted = _serialize(via1.to_sexp())
        via2 = Via.from_sexp(_parse_one(emitted))

        # All fields preserved.
        assert via2.via_type == via_type
        assert via2.position == via1.position
        assert via2.size == via1.size
        assert via2.drill == via1.drill
        assert via2.layers == via1.layers
        assert via2.net_number == via1.net_number
        assert via2.uuid == via1.uuid

    def test_standard_via_roundtrip(self):
        text = """(via
\t(at 1.0 2.0)
\t(size 0.6)
\t(drill 0.3)
\t(layers "F.Cu" "B.Cu")
\t(net 1)
\t(uuid "std-1")
)"""
        via1 = Via.from_sexp(_parse_one(text))
        assert via1.via_type is None
        emitted = _serialize(via1.to_sexp())
        via2 = Via.from_sexp(_parse_one(emitted))
        assert via2.via_type is None
        assert via2.position == (1.0, 2.0)
        assert via2.net_number == 1
