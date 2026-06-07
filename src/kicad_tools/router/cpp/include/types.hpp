/*
 * Router C++ Core - Common Types
 * Part of kicad-tools router performance optimization (Phase 4)
 */

#pragma once

#include <cstdint>
#include <limits>
#include <vector>
#include <tuple>
#include <optional>

namespace router {

// Build version for the C++ binding surface (Issue #2501).
//
// Bump this constant in any PR that changes the bindings.cpp surface
// (added/removed/renamed symbols, struct fields, function signatures).
// The Python side mirrors this as ``_REQUIRED_CPP_BUILD_VERSION`` in
// ``cpp_backend.py``; on import the two are compared and a mismatch
// disables the C++ backend with a clear "kct build-native" error,
// preventing silent ``AttributeError`` failures from a stale .so.
//
// Bump to 6 for Issue #3144 (A* tie-break determinism fix).  The
// public binding surface is unchanged, but AStarNode gained a ``seq``
// monotonic-insertion-counter field used for deterministic ordering
// of equal-f_score nodes.  Existing .so files lack this field, so
// stale builds running against the post-#3144 Python side would
// produce a confusing ABI mismatch.  Bumping the version forces a
// rebuild via ``kct build-native``.
//
// Bump to 7 for Issue #3143 (per-pad channel budget).  Adds a new
// ``PadChannelBudget`` struct in this header, exposes it through the
// nanobind layer, and threads a ``std::vector<PadChannelBudget>``
// parameter into ``Pathfinder::route()`` / ``route_resumable()``.  Old
// .so files lack the new struct definition and would mismatch the
// Python-side caller; the build-version bump forces a rebuild via
// ``kct build-native``.
//
// Bump to 8 for Issue #3199 (A* tie-break greedy-on-g_score).  The
// public binding surface is unchanged, but ``AStarNode::operator>``
// gained a ``g_score`` tertiary key between ``f_score`` and ``seq``.
// Existing .so files use the pre-#3199 (f_score, seq) comparison
// which regressed softstart unaided routing reach from 6/10 -> 5/10;
// the post-#3199 (f_score, g_score, seq) comparison restores 6/10.
// Bumping the version forces a rebuild so the regression fix takes
// effect.
//
// Bump to 9 for Issue #3224 (foreign-pad-metal A* rejection).
// ``Grid3D::mark_blocked`` gained an optional trailing ``pad_blocked``
// parameter so the Python sync (``cpp_backend.py::from_routing_grid``
// and ``grid.py::_sync_pad_to_cpp_grid``) can forward the
// ``_pad_blocked[metal_slice] = True`` bit set by
// ``RoutingGrid._add_pad_unsafe``.  Without the rebuild, ``cell.pad_blocked``
// remains ``false`` for every cell, and the ``is_clearance_only =
// !cell.pad_blocked`` check at ``pathfinder.cpp:680`` / ``pathfinder.cpp:1173``
// always reports "clearance halo", allowing the A* pad-exit branch to
// step trace centerlines through foreign pad metal -- the 16
// ``clearance_pad_segment`` errors on board 05 with --backend cpp.
// Bumping the build version forces ``kct build-native`` so the fix takes
// effect.
//
// Bump to 10 for Issue #3309 (A* flat-array g_score / closed-set storage).
// The resumable A* hot loop replaced ``std::unordered_map<tuple<int,int,int>,
// float>`` / ``std::unordered_set<tuple<int,int,int>>`` member tables with
// generation-stamped flat ``std::vector<float>`` / ``std::vector<uint32_t>``
// arrays indexed by ``layer * rows * cols + y * cols + x``.  The public
// binding surface is unchanged; the Pathfinder ABI gained new member
// vectors and an ``ensure_search_arrays_sized()`` helper.  Pre-#3309 .so
// files would still link but exercise the slower hashmap path -- bumping
// the version forces a rebuild so the post-#3309 performance fix takes
// effect and the regression test (``tests/test_router_cpp_astar_flat_arrays_3309.py``)
// passes its build-version guard.
constexpr int ROUTER_CPP_BUILD_VERSION = 10;

// Grid cell state
struct GridCell {
    bool blocked = false;
    int32_t net = 0;
    int16_t usage_count = 0;
    float history_cost = 0.0f;
    float avoidance_cost = 0.0f;
    bool is_obstacle = false;
    bool is_zone = false;
    bool pad_blocked = false;
    int32_t original_net = 0;
};

// A* node for priority queue
struct AStarNode {
    float f_score;
    float g_score;
    int x;
    int y;
    int layer;
    int parent_idx;  // Index in closed set, -1 if no parent
    bool via_from_parent;
    int dx;  // Direction from parent
    int dy;
    // Issue #3144: monotonic insertion counter for deterministic
    // tie-breaking when ``f_score`` is equal between nodes.  Without
    // this secondary key, ``std::priority_queue`` falls through to
    // implementation-defined pop order for f_score-equal nodes; on a
    // CI runner under load this manifests as run-to-run drift in the
    // explored A* path, which propagates downstream into different
    // diff-pair budget-classification outcomes and ultimately
    // different DRC error counts.  ``seq`` is assigned at push-time
    // from a search-local counter so older-pushed nodes (lower seq)
    // pop first on f_score ties.  Comparison cost is one extra
    // ``int`` compare per heap operation; negligible vs the
    // surrounding heap reshuffle.
    uint64_t seq = 0;

    // Comparison for min-heap.  Issue #3144 / #3199:
    //   Primary:   lower f_score first.
    //   Secondary: HIGHER g_score first on f_score ties.  This is the
    //              standard "greedy on ties" A* tie-break -- when two
    //              nodes have the same projected total cost, prefer the
    //              one with more g (= lower h = closer to the goal).
    //              This pushes the search toward the goal frontier faster
    //              and avoids exploring symmetric equal-cost detours.
    //              Empirically (issue #3199) the post-#3144 FIFO-on-seq
    //              tie-break (without the g_score key) regressed softstart
    //              unaided routing reach from 6/10 -> 5/10 on dense
    //              packages; adding the g_score tertiary key restores
    //              the 6/10 baseline while keeping the run-to-run
    //              determinism property #3144 required (board 06 / #3144
    //              + board 07 / #3146 determinism tests still pass).
    //   Tertiary:  lower seq (FIFO insertion) so pop order is
    //              deterministic even when both f_score and g_score are
    //              equal.  Determinism is the binding invariant for the
    //              board 06 / board 07 byte-identical-route tests.
    bool operator>(const AStarNode& other) const {
        if (f_score != other.f_score) {
            return f_score > other.f_score;
        }
        if (g_score != other.g_score) {
            // HIGHER g_score wins (= pops first), so this node pops
            // later iff its g_score is LOWER.
            return g_score < other.g_score;
        }
        return seq > other.seq;
    }
};

// Route segment
struct Segment {
    float x1, y1, x2, y2;
    float width;
    int layer;
    int net;
};

// Via
struct Via {
    float x, y;
    float drill;
    float diameter;
    int layer_from;
    int layer_to;
    int net;
};

// Failure reason codes for RouteResult (Issue #2476).
//
// When ``success == false`` the search was unable to produce a route.  The
// numbering mirrors ``ValidationResult::violation_type`` so Python callers
// can dispatch on the same vocabulary across both search-time failures and
// post-route validator violations.
//
// FAILURE_VIA_VIA_BLOCKED is set when every via candidate considered during
// the A* expansion was refused by the geometric via-vs-via clearance check
// in ``Pathfinder::is_via_blocked`` (the path-side mirror of the validator's
// type-5 violation).  When set, ``RouteResult::blocking_via_net`` carries
// the net id of the most recently observed offending stored via, and
// ``RouteResult::failure_x``/``failure_y`` carry the world-coordinate
// location of the candidate via that was rejected.  The negotiated strategy
// uses these fields to target rip-up at the specific net whose stored via
// blocked progress, rather than blanket retry.
enum FailureReason : int {
    FAILURE_NONE = 0,
    FAILURE_NO_PATH = 1,            // Open set exhausted, no candidates remained.
    FAILURE_ITERATION_LIMIT = 2,    // Reached max_iterations cap (memory backstop).
    FAILURE_TIMEOUT = 3,            // Per-net wall-clock deadline exceeded (Issue #2610).
    FAILURE_VIA_VIA_BLOCKED = 5,    // All via candidates refused by stored-via geometry.
};

// Complete route result
struct RouteResult {
    std::vector<Segment> segments;
    std::vector<Via> vias;
    int net = 0;
    bool success = false;

    // Issue #2476: Structured failure diagnostics.
    //
    // Populated when ``success == false`` so the negotiated strategy can
    // dispatch retry/rip-up intelligently (e.g. rip up the specific net
    // whose stored via blocked our path, rather than a blanket retry).
    int failure_reason = FAILURE_NONE;
    int blocking_via_net = 0;       // Net of the offending stored via (if any).
    float failure_x = 0.0f;         // World-coord of last rejected candidate.
    float failure_y = 0.0f;
};

// Neighbor direction: dx, dy, dlayer, cost_multiplier
struct Neighbor {
    int dx;
    int dy;
    int dlayer;
    float cost_mult;
};

// Pad bounds in grid coordinates for metal area and approach zone
struct PadBounds {
    int metal_gx1 = 0;
    int metal_gy1 = 0;
    int metal_gx2 = 0;
    int metal_gy2 = 0;
    int approach_gx1 = 0;
    int approach_gy1 = 0;
    int approach_gx2 = 0;
    int approach_gy2 = 0;
};

// Per-pad lateral-channel budget (Issue #3143).
//
// Tags a rectangular "lateral channel" region adjacent to a dense-package
// pad with a soft per-cell penalty proportional to how many distinct nets
// are already routing through it.  The penalty is consulted on every A*
// neighbor-expansion inside the cell box; nets that share the channel with
// fewer prior occupants see a smaller cost, while nets that would push the
// channel past ``capacity`` see ``overflow_penalty`` accumulated on each
// cell.  This nudges the A* search toward a less-contested escape path,
// without hard-blocking any route -- the budget is a *cost shaping* term,
// not a barrier.
//
// Why this is needed:
//   Dense packages like softstart's U1 (TSSOP-20, 0.65mm pitch) generate
//   escape stubs that all terminate in the same narrow lateral channel
//   adjacent to the package edge.  The standard A* cost function treats
//   every cell equally, so the first net to enter the channel "wins" it
//   for free; subsequent nets that COULD reach the goal via a slightly
//   longer detour instead pile onto the same channel until the negotiated
//   rip-up loop runs out of options.  The per-pad channel budget makes
//   the contested channel proportionally more expensive as more nets
//   claim it, so the search naturally redistributes onto adjacent
//   channels.
//
// Fields:
//   gx1/gy1/gx2/gy2 -- inclusive grid-coordinate bounding box of the
//     channel cells (only cells inside this rect are penalised).
//   layer -- routing layer this channel applies to.  -1 means "any layer".
//   capacity -- soft capacity (number of distinct nets allowed to share
//     this channel before overflow_penalty fires).  0 means the channel
//     is unmetered; the budget is inert.
//   overflow_penalty -- per-cell cost added to each cell expansion for
//     nets that would push the channel beyond ``capacity``.  Tuned to
//     be roughly equivalent to a few extra cells of detour -- large
//     enough to redirect when a near-by alternative exists, small enough
//     that no alternative path is preferred over a 2x-longer detour.
//   origin_pad_ref_hash -- FNV-1a hash of the originating component's
//     refdes (e.g. "U1").  Reserved for future per-package-aware budgets;
//     not consumed by the current cost calculation.
//
// Used by:
//   Pathfinder::run_astar_loop / Pathfinder::route (the per-cell cost
//   helper ``get_pad_channel_cost`` consults a pre-built lookup table).
struct PadChannelBudget {
    int gx1 = 0;
    int gy1 = 0;
    int gx2 = 0;
    int gy2 = 0;
    int layer = -1;        // -1 = applies to all routing layers
    int capacity = 0;      // 0 = inert (no penalty enforced)
    float overflow_penalty = 0.0f;
    uint32_t origin_pad_ref_hash = 0;  // Reserved for future per-package use.
    // Source net of the originating escape pad.  When > 0, the cost
    // shaping is "soft against this net" -- i.e. the penalty fires only
    // on nets DIFFERENT from ``source_net``.  This is the per-pad-aware
    // semantics that lets the budget gate cross-net contention without
    // penalising the originating net's own A* expansion out of its
    // escape endpoint.  The Python adapter filters by net before the
    // C++ call (see ``cpp_backend.py::_route_impl``), so the C++ side
    // does not need to inspect ``source_net`` directly -- it stays in
    // the struct for diagnostics and so the Python side can round-trip
    // the field without losing it.
    int source_net = 0;
};

// Design rules (simplified for C++ core)
struct DesignRules {
    float trace_width = 0.127f;
    float trace_clearance = 0.127f;
    float via_drill = 0.3f;
    float via_diameter = 0.6f;
    float via_clearance = 0.127f;
    float grid_resolution = 0.127f;
    float cost_straight = 1.0f;
    float cost_turn = 1.5f;
    float cost_via = 10.0f;
    float cost_congestion = 5.0f;
    float congestion_threshold = 0.5f;
    float min_drill_clearance = 0.102f;
};

// Pad info for geometric validation (Issue #2439)
// Stores pad geometry needed for clearance checking without Python callbacks.
struct PadInfo {
    float x = 0.0f;
    float y = 0.0f;
    float width = 0.0f;
    float height = 0.0f;
    int net = 0;
    int layer_idx = -1;         // -1 means through-hole (all layers)
    uint32_t ref_hash = 0;      // FNV-1a hash of component reference
    float clearance_override = 0.0f;  // Pre-computed clearance for this pad's component
    // Issue #2908: True when the pad's net carries plane (power/ground)
    // topology, regardless of whether the net id is 0 (skipped-pour
    // convention) or a real net number (e.g. board 04 routes ``+3.3V``
    // and ``GND`` as real nets so the GND zone can stitch up after
    // routing).  Set from ``cpp_backend.py::CppGrid.from_routing_grid``
    // by classifying ``pad.net_name`` (the C++ side has no string
    // table, so the boolean is computed in Python and passed in).
    bool is_plane_net = false;
};

// Stored segment for validation (Issue #2439)
// Segments from completed routes, used for clearance checking.
struct StoredSegment {
    float x1, y1, x2, y2;
    float width;
    int layer_idx;
    int net;
};

// Stored via for validation (Issue #2439)
// Vias from completed routes, used for clearance checking.
struct StoredVia {
    float x, y;
    float drill;
    float diameter;
    int net;
};

// Validation result (Issue #2439)
// Returned by validate_route() with pass/fail and violation location.
struct ValidationResult {
    bool valid = true;
    float min_clearance = std::numeric_limits<float>::infinity();
    float violation_x = 0.0f;
    float violation_y = 0.0f;
    int violation_type = 0;  // 0=none, 1=seg-pad, 2=seg-seg, 3=seg-via, 4=via-seg, 5=via-via, 6=drill
};

}  // namespace router
