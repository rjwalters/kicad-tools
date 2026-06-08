"""Tests for the MountingHoleGroup placeable primitive (Issue #3352, P_AS1)."""

import pytest

from kicad_tools.pcb.mounting_holes import MountingHoleGroup


class TestConstruction:
    """Tests for MountingHoleGroup construction and validation."""

    def test_basic_construction(self):
        """Constructor accepts holes + anchor."""
        group = MountingHoleGroup(
            holes=[(0, 0), (10, 0), (0, 10), (10, 10)],
            anchor=(5.0, 5.0),
        )
        assert len(group.holes) == 4
        assert group.anchor == (5.0, 5.0)
        assert group.hole_diameter_mm == 3.2  # default
        assert group.keepout_radius_mm == 5.0  # default

    def test_construction_with_overrides(self):
        """Constructor accepts hole_diameter and keepout_radius overrides."""
        group = MountingHoleGroup(
            holes=[(0, 0)],
            anchor=(0.0, 0.0),
            hole_diameter_mm=2.5,
            keepout_radius_mm=3.0,
        )
        assert group.hole_diameter_mm == 2.5
        assert group.keepout_radius_mm == 3.0

    def test_empty_holes_rejected(self):
        """An empty hole list raises ValueError."""
        with pytest.raises(ValueError, match="non-empty"):
            MountingHoleGroup(holes=[], anchor=(0.0, 0.0))

    def test_zero_hole_diameter_rejected(self):
        """A non-positive hole diameter raises ValueError."""
        with pytest.raises(ValueError, match="hole_diameter_mm must be positive"):
            MountingHoleGroup(
                holes=[(0, 0)],
                anchor=(0.0, 0.0),
                hole_diameter_mm=0.0,
            )

    def test_negative_keepout_radius_rejected(self):
        """A non-positive keepout radius raises ValueError."""
        with pytest.raises(ValueError, match="keepout_radius_mm must be positive"):
            MountingHoleGroup(
                holes=[(0, 0)],
                anchor=(0.0, 0.0),
                keepout_radius_mm=-1.0,
            )

    def test_from_spec_factory(self):
        """from_spec() builds a group from a duck-typed spec object."""

        class FakeSpec:
            holes = [(0.0, 0.0), (10.0, 10.0)]
            anchor = (5.0, 5.0)
            hole_diameter_mm = 3.2
            keepout_radius_mm = 5.0

        group = MountingHoleGroup.from_spec(FakeSpec())
        assert group.holes == [(0.0, 0.0), (10.0, 10.0)]
        assert group.anchor == (5.0, 5.0)
        assert group.hole_diameter_mm == 3.2
        assert group.keepout_radius_mm == 5.0


class TestMoveTo:
    """Tests for MountingHoleGroup.move_to()."""

    def test_move_to_updates_anchor(self):
        """move_to() updates the anchor position."""
        group = MountingHoleGroup(
            holes=[(0, 0), (10, 0)],
            anchor=(5.0, 5.0),
        )
        group.move_to((20.0, 30.0))
        assert group.anchor == (20.0, 30.0)

    def test_move_to_preserves_local_holes(self):
        """move_to() does not modify the local hole positions."""
        original_holes = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (10.0, 10.0)]
        group = MountingHoleGroup(
            holes=list(original_holes),
            anchor=(5.0, 5.0),
        )
        group.move_to((100.0, 100.0))
        assert group.holes == original_holes

    def test_move_to_shifts_board_positions(self):
        """move_to() shifts the on-board hole positions by the anchor delta."""
        group = MountingHoleGroup(
            holes=[(0, 0), (10, 0)],
            anchor=(0.0, 0.0),
        )
        # Initially at anchor (0, 0): board positions are (0, 0) and (10, 0)
        assert group.board_positions() == [(0.0, 0.0), (10.0, 0.0)]
        # Move anchor to (5, 5): board positions become (5, 5) and (15, 5)
        group.move_to((5.0, 5.0))
        assert group.board_positions() == [(5.0, 5.0), (15.0, 5.0)]

    def test_move_to_accepts_int_coords(self):
        """move_to() accepts int coordinates and stores them as floats."""
        group = MountingHoleGroup(
            holes=[(0, 0)],
            anchor=(0.0, 0.0),
        )
        group.move_to((10, 20))  # ints
        assert group.anchor == (10.0, 20.0)
        assert isinstance(group.anchor[0], float)


class TestBoardPositions:
    """Tests for MountingHoleGroup.board_positions()."""

    def test_returns_list_of_tuples(self):
        """board_positions() returns absolute (x, y) tuples in declaration order."""
        group = MountingHoleGroup(
            holes=[(0, 0), (10, 0), (0, 10), (10, 10)],
            anchor=(5.0, 7.0),
        )
        positions = group.board_positions()
        assert positions == [(5.0, 7.0), (15.0, 7.0), (5.0, 17.0), (15.0, 17.0)]

    def test_single_hole(self):
        """A single-hole group returns one position."""
        group = MountingHoleGroup(
            holes=[(0, 0)],
            anchor=(10.0, 10.0),
        )
        assert group.board_positions() == [(10.0, 10.0)]


class TestBoundingBox:
    """Tests for bbox_local() and bbox_board()."""

    def test_bbox_local_includes_keepout(self):
        """bbox_local() includes the keepout radius around holes."""
        group = MountingHoleGroup(
            holes=[(0, 0), (10, 0)],
            anchor=(0.0, 0.0),
            keepout_radius_mm=2.0,
        )
        # Holes at x=0 and x=10, keepout=2 -> bbox x: [-2, 12]
        min_x, min_y, max_x, max_y = group.bbox_local()
        assert min_x == -2.0
        assert max_x == 12.0
        assert min_y == -2.0
        assert max_y == 2.0

    def test_bbox_board_shifts_by_anchor(self):
        """bbox_board() shifts the local bbox by the anchor."""
        group = MountingHoleGroup(
            holes=[(0, 0)],
            anchor=(5.0, 5.0),
            keepout_radius_mm=1.0,
        )
        min_x, min_y, max_x, max_y = group.bbox_board()
        assert min_x == 4.0  # 5 - 1
        assert max_x == 6.0  # 5 + 1
        assert min_y == 4.0
        assert max_y == 6.0


class TestFitsInEnvelope:
    """Tests for MountingHoleGroup.fits_in_envelope()."""

    def test_fits_in_envelope_basic(self):
        """A group well inside an envelope returns True."""
        group = MountingHoleGroup(
            holes=[(0, 0), (90, 0), (0, 90), (90, 90)],
            anchor=(5.0, 5.0),
            keepout_radius_mm=3.0,
        )
        # 4 holes at corners of a 90x90 pattern, 5 mm inset from board origin,
        # 3 mm keepout -> max corner is (5+90+3, 5+90+3) = (98, 98) <= 100
        assert group.fits_in_envelope(100, 100)

    def test_does_not_fit_when_too_tight(self):
        """A group whose keepout crosses the envelope edge returns False."""
        group = MountingHoleGroup(
            holes=[(0, 0), (95, 0)],
            anchor=(5.0, 5.0),
            keepout_radius_mm=5.0,
        )
        # Hole at x=100 (anchor 5 + hole 95) with 5 mm keepout -> reaches x=105
        # Envelope is only 100 wide -> doesn't fit
        assert not group.fits_in_envelope(100, 100)

    def test_does_not_fit_below_origin(self):
        """A group with negative anchor (crosses board origin) returns False."""
        group = MountingHoleGroup(
            holes=[(0, 0)],
            anchor=(2.0, 2.0),
            keepout_radius_mm=5.0,
        )
        # Hole at (2, 2) with 5 mm keepout -> reaches (-3, -3) which is < 0
        assert not group.fits_in_envelope(100, 100)

    def test_softstart_revB_corner_holes(self):
        """Softstart rev B's 4 corner M3 holes fit in the 150x100 envelope."""
        # M3 = 3.2 mm clearance, 5 mm keepout; corners 5 mm inset
        group = MountingHoleGroup(
            holes=[(0, 0), (140, 0), (0, 90), (140, 90)],
            anchor=(5.0, 5.0),
        )
        # Board positions: (5,5), (145,5), (5,95), (145,95) + 5 mm keepout
        # max corner: (150, 100) -- exactly at the edge
        assert group.fits_in_envelope(150, 100)
        # Slightly too small envelope -- keepout crosses edge
        assert not group.fits_in_envelope(149, 99)

    def test_grow_envelope_then_check(self):
        """After moving to a new envelope, fits_in_envelope() uses new anchor."""
        group = MountingHoleGroup(
            holes=[(0, 0), (90, 0), (0, 90), (90, 90)],
            anchor=(5.0, 5.0),
        )
        # Fits in 100x100
        assert group.fits_in_envelope(100, 100)
        # Move to 150x150 envelope center
        group.move_to((30.0, 30.0))
        # Holes now extend to (120, 120) + keepout 5 -> (125, 125) <= 150
        assert group.fits_in_envelope(150, 150)


class TestIntersects:
    """Tests for MountingHoleGroup.intersects() (collision detection)."""

    def test_disjoint_groups_do_not_intersect(self):
        """Groups whose keepout bboxes don't overlap return False."""
        a = MountingHoleGroup(holes=[(0, 0)], anchor=(0.0, 0.0), keepout_radius_mm=2.0)
        b = MountingHoleGroup(holes=[(0, 0)], anchor=(100.0, 100.0), keepout_radius_mm=2.0)
        assert not a.intersects(b)
        assert not b.intersects(a)

    def test_overlapping_keepouts_intersect(self):
        """Groups whose keepout bboxes overlap return True."""
        a = MountingHoleGroup(holes=[(0, 0)], anchor=(0.0, 0.0), keepout_radius_mm=5.0)
        b = MountingHoleGroup(holes=[(0, 0)], anchor=(3.0, 0.0), keepout_radius_mm=5.0)
        # a: [-5, 5] x [-5, 5];  b: [-2, 8] x [-5, 5] -- overlap
        assert a.intersects(b)
        assert b.intersects(a)

    def test_touching_groups_intersect(self):
        """Groups whose bboxes share an edge are considered to intersect."""
        a = MountingHoleGroup(holes=[(0, 0)], anchor=(0.0, 0.0), keepout_radius_mm=5.0)
        b = MountingHoleGroup(holes=[(0, 0)], anchor=(10.0, 0.0), keepout_radius_mm=5.0)
        # a: x in [-5, 5];  b: x in [5, 15] -- they share x=5
        assert a.intersects(b)


class TestToFootprintDict:
    """Tests for MountingHoleGroup.to_footprint_dict()."""

    def test_top_level_keys(self):
        """to_footprint_dict() emits the documented top-level keys."""
        group = MountingHoleGroup(holes=[(0, 0)], anchor=(5.0, 5.0))
        data = group.to_footprint_dict()
        assert "anchor" in data
        assert "hole_diameter_mm" in data
        assert "keepout_radius_mm" in data
        assert "holes" in data

    def test_per_hole_position_is_board_coords(self):
        """Each hole's 'position' is in board coordinates (anchor + local)."""
        group = MountingHoleGroup(
            holes=[(0, 0), (10, 0)],
            anchor=(5.0, 7.0),
        )
        data = group.to_footprint_dict()
        positions = [h["position"] for h in data["holes"]]
        assert positions == [(5.0, 7.0), (15.0, 7.0)]

    def test_per_hole_local_position_preserved(self):
        """Each hole's 'local_position' preserves the group-frame value."""
        group = MountingHoleGroup(
            holes=[(0, 0), (10, 0)],
            anchor=(5.0, 7.0),
        )
        data = group.to_footprint_dict()
        local_positions = [h["local_position"] for h in data["holes"]]
        assert local_positions == [(0, 0), (10, 0)]

    def test_per_hole_geometry_fields(self):
        """Each hole exposes drill and keepout fields."""
        group = MountingHoleGroup(
            holes=[(0, 0)],
            anchor=(0.0, 0.0),
            hole_diameter_mm=2.5,
            keepout_radius_mm=3.0,
        )
        data = group.to_footprint_dict()
        hole = data["holes"][0]
        assert hole["drill_mm"] == 2.5
        assert hole["keepout_radius_mm"] == 3.0

    def test_serialization_after_move(self):
        """Moving the group updates the to_footprint_dict() output."""
        group = MountingHoleGroup(
            holes=[(0, 0)],
            anchor=(0.0, 0.0),
        )
        data_before = group.to_footprint_dict()
        assert data_before["holes"][0]["position"] == (0.0, 0.0)

        group.move_to((42.0, 42.0))
        data_after = group.to_footprint_dict()
        assert data_after["holes"][0]["position"] == (42.0, 42.0)


class TestSchemaIntegration:
    """Tests verifying MountingHoleGroup can round-trip through the spec schema."""

    def test_from_spec_with_pydantic_model(self):
        """from_spec() works with the actual MountingHoleGroupSpec pydantic model."""
        from kicad_tools.spec.schema import MountingHoleGroupSpec

        spec = MountingHoleGroupSpec(
            holes=[(0.0, 0.0), (140.0, 0.0), (0.0, 90.0), (140.0, 90.0)],
            anchor=(5.0, 5.0),
            hole_diameter_mm=3.2,
            keepout_radius_mm=5.0,
        )
        group = MountingHoleGroup.from_spec(spec)
        assert group.holes == [(0.0, 0.0), (140.0, 0.0), (0.0, 90.0), (140.0, 90.0)]
        assert group.anchor == (5.0, 5.0)
        assert group.hole_diameter_mm == 3.2
        assert group.keepout_radius_mm == 5.0
        # And the round-tripped group should fit the softstart envelope
        assert group.fits_in_envelope(150, 100)
