"""Round-trip serialization tests for ``NetClassRouting`` JSON sidecar.

Issue #2684 / Epic #2556 Phase 2.5c-cli.  Asserts that
:meth:`NetClassRouting.to_dict` and :meth:`NetClassRouting.from_dict` form
a byte-equivalent round trip for every field the diff-pair validate rules
consume:

- ``coupled_routing`` (Phase 2E / #2638)
- ``coupled_continuity_threshold`` (Phase 2G / #2640)
- ``skew_tolerance_mm`` (Phase 3H / #2647)
- ``diffpair_partner`` (Phase 1B / #2558)
- ``target_diff_impedance`` (Phase 3K / #2650)
- ``intra_pair_clearance`` (Phase 1A / #2557)

Also covers the map-level helpers ``net_class_map_to_dict`` and
``net_class_map_from_dict`` (the wire format for the
``kct check --net-class-map`` sidecar).
"""

from __future__ import annotations

import json
from dataclasses import fields

import pytest

from kicad_tools.router.rules import (
    NET_CLASS_HIGH_SPEED,
    LengthConstraint,
    NetClassRouting,
    create_net_class_map,
    net_class_map_from_dict,
    net_class_map_to_dict,
)


class TestNetClassRoutingRoundTrip:
    """Byte-equivalent round-trip for individual ``NetClassRouting`` instances."""

    def test_minimal_roundtrip(self):
        """Default-valued instance (only ``name`` set) round-trips exactly."""
        nc = NetClassRouting(name="Default")
        assert NetClassRouting.from_dict(nc.to_dict()) == nc

    def test_diffpair_fields_roundtrip(self):
        """All diff-pair-relevant fields survive a round trip."""
        nc = NetClassRouting(
            name="HighSpeed",
            coupled_routing=True,
            coupled_continuity_threshold=0.85,
            skew_tolerance_mm=3.0,
            diffpair_partner="USB_D-",
            target_diff_impedance=90.0,
            target_single_impedance=50.0,
            intra_pair_clearance=0.075,
            impedance_tolerance_percent=8.0,
        )
        rt = NetClassRouting.from_dict(nc.to_dict())
        assert rt == nc
        # Explicit field-by-field checks (defensive against equality regression).
        assert rt.coupled_routing is True
        assert rt.coupled_continuity_threshold == 0.85
        assert rt.skew_tolerance_mm == 3.0
        assert rt.diffpair_partner == "USB_D-"
        assert rt.target_diff_impedance == 90.0
        assert rt.target_single_impedance == 50.0
        assert rt.intra_pair_clearance == 0.075
        assert rt.impedance_tolerance_percent == 8.0

    def test_layer_preferences_roundtrip(self):
        """``preferred_layers`` / ``avoid_layers`` lists survive round trip."""
        nc = NetClassRouting(
            name="LayerPref",
            preferred_layers=[0, 1],
            avoid_layers=[2, 3],
            layer_cost_multiplier=3.5,
        )
        rt = NetClassRouting.from_dict(nc.to_dict())
        assert rt == nc
        assert rt.preferred_layers == [0, 1]
        assert rt.avoid_layers == [2, 3]

    def test_length_constraint_nested_roundtrip(self):
        """Nested ``LengthConstraint`` is serialized + deserialized exactly."""
        nc = NetClassRouting(
            name="LengthMatched",
            length_constraint=LengthConstraint(
                net_id=5,
                min_length=10.0,
                max_length=20.0,
                match_group="ddr_data",
                match_tolerance=0.3,
            ),
        )
        rt = NetClassRouting.from_dict(nc.to_dict())
        assert rt == nc
        assert rt.length_constraint is not None
        assert rt.length_constraint.net_id == 5
        assert rt.length_constraint.match_group == "ddr_data"

    def test_length_constraint_none_roundtrip(self):
        """A ``None`` length_constraint stays ``None`` through round trip."""
        nc = NetClassRouting(name="NoConstraint", length_constraint=None)
        rt = NetClassRouting.from_dict(nc.to_dict())
        assert rt == nc
        assert rt.length_constraint is None

    def test_predefined_high_speed_roundtrip(self):
        """The shipped ``NET_CLASS_HIGH_SPEED`` singleton round-trips exactly.

        This is the canonical instance flipped to ``coupled_routing=True``
        in #2651.  A regression here would silently break the producer-
        consumer drift-prevention contract.
        """
        rt = NetClassRouting.from_dict(NET_CLASS_HIGH_SPEED.to_dict())
        assert rt == NET_CLASS_HIGH_SPEED
        assert rt.coupled_routing is True
        assert rt.intra_pair_clearance == 0.075

    def test_to_dict_is_json_serializable(self):
        """``to_dict`` output survives ``json.dumps`` / ``json.loads``.

        Catches anything that creeps in like ``set``/``tuple`` -- the
        sidecar must be JSON-portable.
        """
        nc = NetClassRouting(
            name="HighSpeed",
            coupled_routing=True,
            skew_tolerance_mm=3.0,
            diffpair_partner="USB_D-",
            preferred_layers=[0, 1],
            length_constraint=LengthConstraint(net_id=1),
        )
        wire = json.dumps(nc.to_dict())
        rt = NetClassRouting.from_dict(json.loads(wire))
        assert rt == nc

    def test_from_dict_requires_name(self):
        """``from_dict`` rejects entries without a ``name`` field."""
        with pytest.raises(ValueError, match="requires a 'name' field"):
            NetClassRouting.from_dict({"priority": 1})

    def test_from_dict_tolerates_unknown_keys(self):
        """Forward-compatibility: unknown keys in the dict are ignored."""
        nc = NetClassRouting.from_dict({"name": "X", "future_field_not_yet_added": 42})
        assert nc.name == "X"

    def test_from_dict_fills_defaults_for_missing_optional(self):
        """Missing optional keys fall back to dataclass defaults."""
        nc = NetClassRouting.from_dict({"name": "Sparse"})
        assert nc.name == "Sparse"
        assert nc.priority == 5
        assert nc.coupled_routing is False
        assert nc.skew_tolerance_mm is None

    def test_match_group_fields_roundtrip(self):
        """Phase 1A match-group declaration fields (#2687) round-trip exactly.

        Regression test for the coordination gap between PR #2691 (which
        added :attr:`length_match_group` / :attr:`length_match_reference` /
        :attr:`length_match_tolerance_mm`) and this PR's
        :meth:`to_dict` / :meth:`from_dict`.  Without explicit keys in
        both serializers, these fields would silently round-trip as
        ``None`` and any Phase 1B/1C/1D producer would lose its
        match-group annotation through the sidecar.
        """
        nc = NetClassRouting(
            name="DDR_DATA_BYTE0",
            length_match_group="DDR_DATA",
            length_match_reference="DQS_P",
            length_match_tolerance_mm=0.1,
        )
        rt = NetClassRouting.from_dict(nc.to_dict())
        assert rt == nc
        # Explicit field-by-field checks (defensive against equality regression).
        assert rt.length_match_group == "DDR_DATA"
        assert rt.length_match_reference == "DQS_P"
        assert rt.length_match_tolerance_mm == 0.1

    def test_match_group_clock_sentinel_roundtrip(self):
        """The Phase 2/3 ``"clock"`` reference-policy sentinel survives the wire.

        Forward-compat: Phase 1A accepts the sentinel but does not yet
        implement protocol resolution.  The sidecar must preserve the
        string verbatim so Phase 2/3 consumers see it unchanged.
        """
        nc = NetClassRouting(
            name="MIPI_CSI_LANE0",
            length_match_group="MIPI_CSI",
            length_match_reference="clock",
            length_match_tolerance_mm=0.5,
        )
        rt = NetClassRouting.from_dict(nc.to_dict())
        assert rt == nc
        assert rt.length_match_reference == "clock"


class TestDriftPrevention:
    """Lock the :meth:`to_dict` / :meth:`from_dict` contract against drift.

    The coordination failure that produced the PR #2691 conflict was a
    new field added to the :class:`NetClassRouting` dataclass body
    without corresponding entries in this PR's serialization methods.
    Dataclass equality only compares declared fields, so the existing
    round-trip tests would not catch a silent ``None``-default drop --
    the new field would simply default to ``None`` on both sides of the
    round trip and the assertion would pass.

    These tests introspect :func:`dataclasses.fields` and assert each
    declared field has a corresponding key in :meth:`to_dict` output.
    They will fail loudly on any future field addition that is not
    accompanied by a serialization-method update, preventing exactly
    the failure mode that occurred today.
    """

    def test_to_dict_covers_all_netclassrouting_fields(self):
        """Every declared field on :class:`NetClassRouting` is in ``to_dict`` output."""
        all_field_names = {f.name for f in fields(NetClassRouting)}
        to_dict_keys = set(NetClassRouting(name="x").to_dict().keys())
        missing = all_field_names - to_dict_keys
        extra = to_dict_keys - all_field_names
        assert not missing, (
            f"NetClassRouting fields missing from to_dict(): {sorted(missing)}.  "
            f"Add a key for each in NetClassRouting.to_dict() and a "
            f"corresponding data.get(...) in NetClassRouting.from_dict()."
        )
        assert not extra, (
            f"NetClassRouting.to_dict() has keys not backed by dataclass fields: {sorted(extra)}."
        )

    def test_to_dict_covers_all_lengthconstraint_fields(self):
        """Every declared field on :class:`LengthConstraint` is in ``to_dict`` output."""
        all_field_names = {f.name for f in fields(LengthConstraint)}
        to_dict_keys = set(LengthConstraint(net_id=0).to_dict().keys())
        missing = all_field_names - to_dict_keys
        extra = to_dict_keys - all_field_names
        assert not missing, f"LengthConstraint fields missing from to_dict(): {sorted(missing)}."
        assert not extra, (
            f"LengthConstraint.to_dict() has keys not backed by dataclass fields: {sorted(extra)}."
        )

    def test_roundtrip_with_non_default_for_every_field(self):
        """Build an instance with a non-default value for every settable field, round-trip.

        Stronger than the per-field tests above: this walks
        :func:`dataclasses.fields` and constructs an instance with a
        non-default sentinel for each, then asserts byte-equivalent
        round-trip.  Any field that ``to_dict`` forgets will round-trip
        as its default (``None`` for optionals, the declared default for
        scalars), making the resulting instance unequal to the original
        and tripping the assertion.

        This is the drift-prevention shape recommended in the PR #2692
        judge review: it locks the contract for ALL future field
        additions, not just the three from PR #2691.
        """
        # Non-default sentinel per type.  None of these collide with the
        # declared dataclass defaults, so any silent drop will surface as
        # an inequality.
        kwargs: dict = {
            "name": "DriftSentinel",
            "priority": 99,
            "trace_width": 0.123,
            "clearance": 0.456,
            "via_size": 0.789,
            "cost_multiplier": 1.23,
            "length_critical": True,
            "noise_sensitive": True,
            "zone_priority": 42,
            "zone_connection": "solid",
            "is_pour_net": True,
            "preferred_layers": [3, 4, 5],
            "avoid_layers": [6, 7],
            "layer_cost_multiplier": 4.5,
            "length_constraint": LengthConstraint(
                net_id=11,
                min_length=1.0,
                max_length=2.0,
                match_group="drift_test",
                match_tolerance=0.25,
            ),
            "intra_pair_clearance": 0.0625,
            "diffpair_partner": "DRIFT_N",
            "coupled_routing": True,
            "target_diff_impedance": 87.5,
            "target_single_impedance": 47.5,
            "impedance_tolerance_percent": 7.5,
            "coupled_continuity_threshold": 0.65,
            "skew_tolerance_mm": 0.35,
            "length_match_group": "DRIFT_GROUP",
            "length_match_reference": "DRIFT_REF",
            "length_match_tolerance_mm": 0.15,
        }
        # If the dataclass adds a field that the kwargs map doesn't
        # cover, fail loudly here -- this forces the test author to
        # extend the sentinel map alongside the field addition.
        declared = {f.name for f in fields(NetClassRouting)}
        assert declared == set(kwargs.keys()), (
            f"Drift sentinel map out of sync with NetClassRouting fields.  "
            f"Missing from kwargs: {sorted(declared - set(kwargs.keys()))}.  "
            f"Extra in kwargs: {sorted(set(kwargs.keys()) - declared)}."
        )
        nc = NetClassRouting(**kwargs)
        rt = NetClassRouting.from_dict(nc.to_dict())
        assert rt == nc


class TestNetClassMapHelpers:
    """Round-trip + error-path tests for the map-level helpers."""

    def test_empty_map_roundtrips(self):
        d = net_class_map_to_dict({})
        assert d == {}
        assert net_class_map_from_dict(d) == {}

    def test_typical_map_roundtrips(self):
        """Realistic board-03-style map round-trips byte-equivalently."""
        m = create_net_class_map(
            power_nets=["VCC", "GND"],
            high_speed_nets=["USB_D+", "USB_D-"],
        )
        rt = net_class_map_from_dict(net_class_map_to_dict(m))
        assert rt == m

    def test_json_text_roundtrip(self):
        """Full JSON text round trip (the on-disk sidecar form)."""
        m = create_net_class_map(high_speed_nets=["D+", "D-"])
        wire = json.dumps(net_class_map_to_dict(m))
        rt = net_class_map_from_dict(json.loads(wire))
        assert rt == m

    def test_from_dict_rejects_non_dict_input(self):
        with pytest.raises(TypeError, match="expects a dict"):
            net_class_map_from_dict("not a dict")  # type: ignore[arg-type]

    def test_from_dict_rejects_non_dict_entry(self):
        with pytest.raises(TypeError, match="must be a dict"):
            net_class_map_from_dict({"USB_D+": "not a dict"})  # type: ignore[dict-item]

    def test_from_dict_rejects_missing_name(self):
        with pytest.raises(ValueError, match="requires a 'name' field"):
            net_class_map_from_dict({"USB_D+": {"priority": 1}})
