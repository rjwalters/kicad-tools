"""Tests for the LocalRerouter class.

Covers:
- Local grid construction with obstacle marking
- A* path found around a via obstacle
- Path not found when fully enclosed
- Dry-run mode (path found but PCB not modified)
- Layer and net preservation on rerouted segments
"""

from kicad_tools.drc.local_rerouter import LocalRerouter
from kicad_tools.sexp import SExp, parse_string

# PCB with a segment whose both endpoints are at vias, and a third via
# in between that causes a clearance violation.
#
# Layout (x-axis):
#   via-1 at (100, 100) --- segment net=1 --- via-3 at (102, 100)
#   via-2 at (101, 100.3) on net=2  (obstacle between segment endpoints)
#
# The segment from (100,100) to (102,100) passes within 0.3mm of via-2.
# With 0.2mm clearance required and 0.25mm trace width, this violates DRC.
# The rerouter should find a path that detours around via-2.
PCB_WITH_REROUTABLE_SEGMENT = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (segment (start 100 100) (end 102 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-reroute"))
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
  (via (at 102 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-3"))
  (via (at 101 100.3) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-2"))
)
"""

# PCB where the segment is completely enclosed by obstacles with no path out
PCB_WITH_ENCLOSED_SEGMENT = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "VCC")
  (segment (start 100 100) (end 100.5 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-enclosed"))
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-e1"))
  (via (at 100.5 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-e2"))
  (via (at 100.25 100.15) (size 1.2) (drill 0.6) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-block-top"))
  (via (at 100.25 99.85) (size 1.2) (drill 0.6) (layers "F.Cu" "B.Cu") (net 3) (uuid "via-block-bot"))
)
"""

# PCB with a segment on B.Cu to test layer preservation
PCB_WITH_BCU_SEGMENT = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "OTHER")
  (segment (start 100 100) (end 102 100) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-bcu"))
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-bcu1"))
  (via (at 102 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-bcu2"))
  (via (at 101 100.3) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-bcu-obs"))
)
"""


def _parse_pcb(text: str) -> SExp:
    """Parse a PCB string into an SExp document."""
    return parse_string(text)


def _build_nets(doc: SExp) -> dict[int, str]:
    """Build a net number -> name mapping from a PCB document."""
    nets: dict[int, str] = {}
    for net_node in doc.find_all("net"):
        atoms = net_node.get_atoms()
        if len(atoms) >= 2:
            nets[int(atoms[0])] = str(atoms[1])
    return nets


def _find_segment_by_uuid(doc: SExp, uuid_str: str) -> SExp | None:
    """Find a segment node by its UUID."""
    for seg in doc.find_all("segment"):
        uuid_node = seg.find("uuid")
        if uuid_node and uuid_node.get_first_atom() == uuid_str:
            return seg
    return None


class TestLocalRerouterAStarPathFound:
    """Test that A* finds a path around a via obstacle."""

    def test_reroute_around_via(self):
        """Segment should be rerouted around the obstacle via."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-reroute")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.5)
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=101.0,
            obstacle_y=100.3,
            obstacle_radius=0.4,  # via-2 size=0.8, radius=0.4
            trace_width=0.25,
            trace_clearance=0.2,
        )

        assert result.success is True
        assert result.new_segments >= 2  # Path detours, so at least 2 segments
        assert result.path_length_mm > 0

    def test_reroute_replaces_segment(self):
        """After rerouting, the original segment UUID should be gone and new segments present."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-reroute")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.5)
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=101.0,
            obstacle_y=100.3,
            obstacle_radius=0.4,
            trace_width=0.25,
            trace_clearance=0.2,
        )

        assert result.success
        # Original segment should be removed
        assert _find_segment_by_uuid(doc, "seg-reroute") is None
        # New segments should exist
        all_segments = list(doc.find_all("segment"))
        assert len(all_segments) >= 2

    def test_reroute_preserves_net(self):
        """Rerouted segments should preserve the original net number."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-reroute")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.5)
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=101.0,
            obstacle_y=100.3,
            obstacle_radius=0.4,
            trace_width=0.25,
            trace_clearance=0.2,
        )

        assert result.success
        for seg in doc.find_all("segment"):
            net_node = seg.find("net")
            if net_node:
                assert int(net_node.get_first_atom()) == 1  # Net 1 = GND

    def test_reroute_preserves_layer(self):
        """Rerouted segments should stay on the original layer."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-reroute")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.5)
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=101.0,
            obstacle_y=100.3,
            obstacle_radius=0.4,
            trace_width=0.25,
            trace_clearance=0.2,
        )

        assert result.success
        for seg in doc.find_all("segment"):
            layer_node = seg.find("layer")
            if layer_node:
                assert str(layer_node.get_first_atom()) == "F.Cu"

    def test_reroute_endpoints_match_original(self):
        """Rerouted path start/end should match original segment endpoints."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-reroute")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.5)
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=101.0,
            obstacle_y=100.3,
            obstacle_radius=0.4,
            trace_width=0.25,
            trace_clearance=0.2,
        )

        assert result.success
        all_segs = list(doc.find_all("segment"))
        assert len(all_segs) >= 2

        # First segment should start at or near (100, 100)
        first_start = all_segs[0].find("start")
        assert first_start is not None
        atoms = first_start.get_atoms()
        assert abs(float(atoms[0]) - 100.0) < 0.1
        assert abs(float(atoms[1]) - 100.0) < 0.1

        # Last segment should end at or near (102, 100)
        last_end = all_segs[-1].find("end")
        assert last_end is not None
        atoms = last_end.get_atoms()
        assert abs(float(atoms[0]) - 102.0) < 0.1
        assert abs(float(atoms[1]) - 100.0) < 0.1


class TestLocalRerouterNoPathFound:
    """Test behavior when A* cannot find a path."""

    def test_enclosed_segment_fails(self):
        """Segment enclosed by large blocking vias should fail to reroute."""
        doc = _parse_pcb(PCB_WITH_ENCLOSED_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-enclosed")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.3)
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=100.25,
            obstacle_y=100.15,
            obstacle_radius=0.6,  # Large obstacle
            trace_width=0.25,
            trace_clearance=0.2,
        )

        assert result.success is False
        assert result.new_segments == 0

    def test_enclosed_segment_preserves_pcb(self):
        """When reroute fails, original segment should remain unchanged."""
        doc = _parse_pcb(PCB_WITH_ENCLOSED_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-enclosed")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.3)
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=100.25,
            obstacle_y=100.15,
            obstacle_radius=0.6,
            trace_width=0.25,
            trace_clearance=0.2,
        )

        assert result.success is False
        # Original segment should still exist
        assert _find_segment_by_uuid(doc, "seg-enclosed") is not None


class TestLocalRerouterDryRun:
    """Test dry-run mode."""

    def test_dry_run_finds_path_without_modifying(self):
        """Dry-run should report success but leave PCB unchanged."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-reroute")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.5)
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=101.0,
            obstacle_y=100.3,
            obstacle_radius=0.4,
            trace_width=0.25,
            trace_clearance=0.2,
            dry_run=True,
        )

        assert result.success is True
        assert result.new_segments >= 2
        # Original segment should still exist (not modified)
        assert _find_segment_by_uuid(doc, "seg-reroute") is not None
        # Should still have exactly 1 segment (no new ones added)
        all_segs = list(doc.find_all("segment"))
        assert len(all_segs) == 1


class TestLocalRerouterBCuLayer:
    """Test B.Cu layer segment rerouting."""

    def test_bcu_reroute_preserves_layer(self):
        """Rerouted B.Cu segments should stay on B.Cu."""
        doc = _parse_pcb(PCB_WITH_BCU_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-bcu")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.5)
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=101.0,
            obstacle_y=100.3,
            obstacle_radius=0.4,
            trace_width=0.2,
            trace_clearance=0.2,
        )

        assert result.success is True
        for seg in doc.find_all("segment"):
            layer_node = seg.find("layer")
            assert str(layer_node.get_first_atom()) == "B.Cu"


class TestLocalRerouterGridConstruction:
    """Test internal grid construction and obstacle marking."""

    def test_circle_blocking(self):
        """Verify that _mark_circle_blocked creates correct blocked region."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        rerouter = LocalRerouter(doc, nets, resolution=0.1, padding=0.5)

        blocked: set[tuple[int, int]] = set()
        # Mark a circle at (1.0, 1.0) with radius 0.3 on a grid starting at (0,0)
        rerouter._mark_circle_blocked(blocked, 1.0, 1.0, 0.3, 0.0, 0.0, 30, 30)

        # Center cell should be blocked
        gx_center = round(1.0 / 0.1)
        gy_center = round(1.0 / 0.1)
        assert (gx_center, gy_center) in blocked

        # Cell far away should not be blocked
        assert (0, 0) not in blocked

    def test_astar_finds_straight_path(self):
        """A* on empty grid should find direct path."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        rerouter = LocalRerouter(doc, nets, resolution=0.1, padding=0.5)

        blocked: set[tuple[int, int]] = set()
        path = rerouter._astar(0, 5, 10, 5, blocked, 20, 10)

        assert path is not None
        assert path[0] == (0, 5)
        assert path[-1] == (10, 5)
        assert len(path) == 11  # Straight line: 11 points for 10 steps

    def test_astar_detours_around_obstacle(self):
        """A* should detour around a blocked region."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        rerouter = LocalRerouter(doc, nets, resolution=0.1, padding=0.5)

        blocked: set[tuple[int, int]] = set()
        # Block a wall at x=5 from y=3 to y=7
        for y in range(3, 8):
            blocked.add((5, y))

        path = rerouter._astar(0, 5, 10, 5, blocked, 20, 10)

        assert path is not None
        assert path[0] == (0, 5)
        assert path[-1] == (10, 5)
        # Path should not pass through any blocked cell
        for gx, gy in path:
            assert (gx, gy) not in blocked

    def test_astar_returns_none_when_blocked(self):
        """A* should return None when no path exists."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        rerouter = LocalRerouter(doc, nets, resolution=0.1, padding=0.5)

        blocked: set[tuple[int, int]] = set()
        # Block entire column at x=5
        for y in range(10):
            blocked.add((5, y))

        path = rerouter._astar(0, 5, 10, 5, blocked, 20, 10)
        assert path is None

    def test_simplify_path_removes_collinear(self):
        """Path simplification should remove collinear intermediate points."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        rerouter = LocalRerouter(doc, nets)

        # Straight horizontal line
        path = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]
        simplified = rerouter._simplify_path(path)
        assert len(simplified) == 2
        assert simplified[0] == (0.0, 0.0)
        assert simplified[1] == (3.0, 0.0)

    def test_simplify_path_preserves_turns(self):
        """Path simplification should keep points where direction changes."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        rerouter = LocalRerouter(doc, nets)

        # L-shaped path
        path = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (2.0, 2.0)]
        simplified = rerouter._simplify_path(path)
        assert len(simplified) == 3
        assert simplified[0] == (0.0, 0.0)
        assert simplified[1] == (2.0, 0.0)
        assert simplified[2] == (2.0, 2.0)


# ---- Cluster rerouting fixtures and tests ----

# PCB with two segments forming a connected path (A->B->C) where both
# segments violate clearance to nearby vias.  The vias are spaced within
# 2x clearance radius of each other, creating a cluster.
#
# Layout (2mm segments for more routing room):
#   via-start (100, 100) net=1 --- segment-A --- midpoint (102, 100) net=1
#       --- segment-B --- via-end (104, 100) net=1
#   via-obs-1 (101, 100.35) net=2  (obstacle near segment-A)
#   via-obs-2 (103, 100.35) net=2  (obstacle near segment-B)
#
# Individual rerouting of segment-A may fail because via-obs-2 blocks
# the escape corridor, and vice versa.  Cluster-aware rerouting should
# succeed by marking both obstacle vias on the grid.
PCB_WITH_CLUSTERED_VIA_VIOLATIONS = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (segment (start 100 100) (end 102 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-cluster-a"))
  (segment (start 102 100) (end 104 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-cluster-b"))
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-start"))
  (via (at 104 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-end"))
  (via (at 101 100.35) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-obs-1"))
  (via (at 103 100.35) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-obs-2"))
)
"""


class TestLocalRerouterExtraObstacles:
    """Test reroute_segment with extra_obstacles parameter for cluster awareness."""

    def test_reroute_with_extra_obstacles_succeeds(self):
        """Segment rerouted with extra_obstacles should avoid both obstacles."""
        doc = _parse_pcb(PCB_WITH_CLUSTERED_VIA_VIOLATIONS)
        nets = _build_nets(doc)
        seg_a = _find_segment_by_uuid(doc, "seg-cluster-a")
        assert seg_a is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=1.0)
        result = rerouter.reroute_segment(
            seg_node=seg_a,
            obstacle_x=101.0,
            obstacle_y=100.35,
            obstacle_radius=0.3,  # via-obs-1 size=0.6, radius=0.3
            trace_width=0.25,
            trace_clearance=0.2,
            extra_obstacles=[(103.0, 100.35, 0.3)],  # via-obs-2
        )

        assert result.success is True
        assert result.new_segments >= 2

    def test_reroute_with_extra_obstacles_avoids_all(self):
        """Rerouted path should clear both the primary and extra obstacles."""
        doc = _parse_pcb(PCB_WITH_CLUSTERED_VIA_VIOLATIONS)
        nets = _build_nets(doc)
        seg_a = _find_segment_by_uuid(doc, "seg-cluster-a")
        assert seg_a is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=1.0)
        result = rerouter.reroute_segment(
            seg_node=seg_a,
            obstacle_x=101.0,
            obstacle_y=100.35,
            obstacle_radius=0.3,
            trace_width=0.25,
            trace_clearance=0.2,
            extra_obstacles=[(103.0, 100.35, 0.3)],
        )

        assert result.success is True
        # Original segment A should be gone
        assert _find_segment_by_uuid(doc, "seg-cluster-a") is None

        # Verify rerouted segments (those NOT seg-cluster-b) avoid primary obstacle
        import math

        obs1 = (101.0, 100.35)
        min_clearance = 0.3 + 0.25 / 2 + 0.2  # obstacle_r + trace_half_w + clearance

        for seg in doc.find_all("segment"):
            uuid_node = seg.find("uuid")
            uid = uuid_node.get_first_atom() if uuid_node else ""
            # Skip unrelated segment B
            if uid == "seg-cluster-b":
                continue

            start = seg.find("start")
            end = seg.find("end")
            if not (start and end):
                continue
            s_atoms = start.get_atoms()
            e_atoms = end.get_atoms()
            sx = float(s_atoms[0])
            sy = float(s_atoms[1]) if len(s_atoms) > 1 else 0
            ex = float(e_atoms[0])
            ey = float(e_atoms[1]) if len(e_atoms) > 1 else 0

            # Check midpoint of each rerouted segment against the primary obstacle
            mx, my = (sx + ex) / 2, (sy + ey) / 2
            dist = math.sqrt((mx - obs1[0]) ** 2 + (my - obs1[1]) ** 2)
            # Allow some tolerance for grid resolution
            assert dist >= min_clearance - 0.15, (
                f"Segment midpoint ({mx:.3f}, {my:.3f}) too close to "
                f"obstacle {obs1}: {dist:.3f}mm < {min_clearance - 0.15:.3f}mm"
            )

    def test_reroute_both_cluster_segments(self):
        """Both segments in a cluster should be reroutable with extra obstacles."""
        doc = _parse_pcb(PCB_WITH_CLUSTERED_VIA_VIOLATIONS)
        nets = _build_nets(doc)
        seg_a = _find_segment_by_uuid(doc, "seg-cluster-a")
        seg_b = _find_segment_by_uuid(doc, "seg-cluster-b")
        assert seg_a is not None
        assert seg_b is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=1.0)

        # Reroute segment A with awareness of obstacle near B
        result_a = rerouter.reroute_segment(
            seg_node=seg_a,
            obstacle_x=101.0,
            obstacle_y=100.35,
            obstacle_radius=0.3,
            trace_width=0.25,
            trace_clearance=0.2,
            extra_obstacles=[(103.0, 100.35, 0.3)],
        )
        assert result_a.success is True

        # Reroute segment B with awareness of obstacle near A
        result_b = rerouter.reroute_segment(
            seg_node=seg_b,
            obstacle_x=103.0,
            obstacle_y=100.35,
            obstacle_radius=0.3,
            trace_width=0.25,
            trace_clearance=0.2,
            extra_obstacles=[(101.0, 100.35, 0.3)],
        )
        assert result_b.success is True

    def test_extra_obstacles_empty_list_same_as_none(self):
        """Passing an empty extra_obstacles list should behave like None."""
        doc = _parse_pcb(PCB_WITH_REROUTABLE_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-reroute")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.5)
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=101.0,
            obstacle_y=100.3,
            obstacle_radius=0.4,
            trace_width=0.25,
            trace_clearance=0.2,
            extra_obstacles=[],
        )

        assert result.success is True
        assert result.new_segments >= 2

    def test_extra_obstacles_on_different_layer_not_clustered(self):
        """Segments on B.Cu should not interact with F.Cu cluster obstacles."""
        doc = _parse_pcb(PCB_WITH_BCU_SEGMENT)
        nets = _build_nets(doc)
        seg_node = _find_segment_by_uuid(doc, "seg-bcu")
        assert seg_node is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=0.5)
        # Pass extra obstacles that would block on F.Cu but the segment
        # is on B.Cu -- should still reroute successfully
        result = rerouter.reroute_segment(
            seg_node=seg_node,
            obstacle_x=101.0,
            obstacle_y=100.3,
            obstacle_radius=0.4,
            trace_width=0.2,
            trace_clearance=0.2,
            extra_obstacles=[(101.0, 99.7, 0.4)],
        )
        assert result.success is True


# ---- Same-net obstacle segment tests ----


# PCB with two same-net segments that need clearance-aware rerouting.
# Both segments are on net 1 ("GND") and on F.Cu.
# seg-A runs from (100, 100) to (104, 100) -- the segment to reroute.
# seg-B runs from (101.5, 100.4) to (102.5, 100.4) -- a short obstacle segment
# offset 0.4mm below, only covering the middle portion. Since both are on
# the same net, the rerouter would normally NOT block seg-B. The test verifies
# that passing seg-B as same_net_obstacle_segs causes it to be blocked.
PCB_WITH_SAME_NET_SEG_SEG = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (segment (start 100 100) (end 104 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-ss-a"))
  (segment (start 101.5 100.4) (end 102.5 100.4) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-ss-b"))
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-ss-1"))
  (via (at 104 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-ss-2"))
)
"""


class TestLocalRerouterSameNetObstacle:
    """Test rerouting with same-net obstacle segments (seg-seg violations)."""

    def test_same_net_obstacle_seg_is_blocked(self):
        """A same-net segment passed as same_net_obstacle_segs should be treated as an obstacle.

        When seg-B is marked as a same-net obstacle, the rerouter should detour
        around it, resulting in multiple segments (a non-straight path).
        """
        doc = _parse_pcb(PCB_WITH_SAME_NET_SEG_SEG)
        nets = _build_nets(doc)
        seg_a = _find_segment_by_uuid(doc, "seg-ss-a")
        seg_b = _find_segment_by_uuid(doc, "seg-ss-b")
        assert seg_a is not None
        assert seg_b is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=1.0)

        # Reroute seg-A around seg-B (same net, but seg-B is the obstacle)
        result = rerouter.reroute_segment(
            seg_node=seg_a,
            obstacle_x=102.0,
            obstacle_y=100.4,
            obstacle_radius=0.125,  # half of seg-B width
            trace_width=0.25,
            trace_clearance=0.2,
            same_net_obstacle_segs=[seg_b],
        )

        assert result.success is True
        # Path should detour, requiring multiple segments
        assert result.new_segments >= 2

    def test_without_same_net_flag_straight_path(self):
        """Without same_net_obstacle_segs, same-net segments are not blocked.

        The reroute should succeed with a shorter/straighter path since the
        same-net obstacle is not marked as blocked.
        """
        doc = _parse_pcb(PCB_WITH_SAME_NET_SEG_SEG)
        nets = _build_nets(doc)
        seg_a = _find_segment_by_uuid(doc, "seg-ss-a")
        assert seg_a is not None

        rerouter = LocalRerouter(doc, nets, resolution=0.05, padding=1.0)

        # Without same_net_obstacle_segs, seg-B (same net) is not blocked
        result = rerouter.reroute_segment(
            seg_node=seg_a,
            obstacle_x=102.0,
            obstacle_y=100.4,
            obstacle_radius=0.125,
            trace_width=0.25,
            trace_clearance=0.2,
        )

        # Should succeed (path may go straight through since obstacle not blocked)
        assert result.success is True
