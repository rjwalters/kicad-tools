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
constexpr int ROUTER_CPP_BUILD_VERSION = 6;

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

    // Comparison for min-heap (lower f_score first; on ties, lower seq
    // first so the pop order is deterministic regardless of std::vector
    // realloc behaviour or hash-map iteration order in surrounding
    // bookkeeping structures).  Issue #3144.
    bool operator>(const AStarNode& other) const {
        if (f_score != other.f_score) {
            return f_score > other.f_score;
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
