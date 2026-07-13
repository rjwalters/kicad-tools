/*
 * Router C++ Core - 3D Grid Implementation
 * Part of kicad-tools router performance optimization (Phase 4)
 */

#include "grid.hpp"
#include "geometry.hpp"
#include <cmath>
#include <algorithm>
#include <limits>

namespace router {

// Floating-point tolerance for clearance comparisons (Issue #2465).
// IEEE-754 rounding in radius/distance math can leave computed
// clearances at values like 0.14999999... when the design intent is
// exactly 0.150mm, producing spurious sub-micron false-positive
// violations during routing validation.  0.1 micron (1e-4 mm) is well
// below any manufacturing precision and matches the epsilon used in
// the Python paths (drc/incremental.py, router/io.py, validate/rules/edge.py).
constexpr float CLEARANCE_EPSILON_MM = 1e-4f;

Grid3D::Grid3D(int cols, int rows, int layers, float resolution,
               float origin_x, float origin_y)
    : cols_(cols), rows_(rows), layers_(layers),
      resolution_(resolution), origin_x_(origin_x), origin_y_(origin_y) {

    // Allocate contiguous cell storage
    size_t total = static_cast<size_t>(cols) * rows * layers;
    cells_.resize(total);

    // Initialize congestion grid
    congestion_cols_ = std::max(1, cols / congestion_size_);
    congestion_rows_ = std::max(1, rows / congestion_size_);
    congestion_.resize(static_cast<size_t>(layers) * congestion_rows_ * congestion_cols_, 0);
}

void Grid3D::mark_blocked(int x, int y, int layer, int net, bool is_obstacle,
                          bool pad_blocked) {
    if (!is_valid(x, y, layer)) return;
    auto& cell = at(x, y, layer);
    cell.blocked = true;
    cell.net = net;
    cell.is_obstacle = is_obstacle;
    // Issue #3224: Forward the pad-metal bit so the A* clearance branch in
    // ``pathfinder.cpp`` can distinguish foreign-pad metal (where the trace
    // centerline must NOT enter) from foreign-pad clearance halo (where
    // pad-exit may step through to escape an adjacent same-net pad).  The
    // OR keeps the bit "sticky" -- if a cell is touched by two pads and
    // either is pad metal, the cell remains pad-blocked.  ``original_net``
    // mirrors the Python grid's bookkeeping: ``unmark_segment`` /
    // ``unmark_via`` consult it to restore the pad's net id when ripping
    // up a route that crossed the pad's clearance halo (see grid.cpp:146,
    // grid.cpp:188).
    if (pad_blocked) {
        cell.pad_blocked = true;
    }
    // Issue #3545: every ``mark_blocked`` call registers STATIC board
    // geometry (pad metal, clearance halos, keepouts) -- route copper
    // goes through ``mark_segment`` / ``mark_via`` instead.  Record the
    // static flag and the owning net (previously recorded for pad metal
    // only) so ``unmark_segment`` / ``unmark_via`` can RESTORE the cell
    // after a rip-up whose clearance envelope swept it, instead of
    // erasing the static blockage and letting foreign nets route
    // through a pad's clearance halo.
    cell.static_blocked = true;
    cell.original_net = net;
}

void Grid3D::mark_rect_blocked(int x1, int y1, int x2, int y2, int layer, int net,
                               bool is_obstacle) {
    x1 = std::clamp(x1, 0, cols_ - 1);
    y1 = std::clamp(y1, 0, rows_ - 1);
    x2 = std::clamp(x2, 0, cols_ - 1);
    y2 = std::clamp(y2, 0, rows_ - 1);

    for (int y = y1; y <= y2; ++y) {
        for (int x = x1; x <= x2; ++x) {
            mark_blocked(x, y, layer, net, is_obstacle);
        }
    }
}

void Grid3D::mark_segment(int x1, int y1, int x2, int y2, int layer, int net,
                          int clearance_cells) {
    // Issue #4079: fast-path the reservation consult behind the grid-wide
    // flag so unreserved boards pay zero extra cost (byte-identical).
    const bool check_reservations = has_reservations_;
    auto mark_with_clearance = [&](int gx, int gy) {
        for (int dy = -clearance_cells; dy <= clearance_cells; ++dy) {
            for (int dx = -clearance_cells; dx <= clearance_cells; ++dx) {
                int nx = gx + dx, ny = gy + dy;
                if (is_valid(nx, ny, layer)) {
                    // Issue #4079: skip cells reserved for a net set that
                    // EXCLUDES ``net`` (lateral-trace keep-out, mirrors
                    // Python ``_mark_segment`` and the ``mark_via`` skip).
                    if (check_reservations &&
                        is_reserved_excluding(nx, ny, layer, net)) {
                        continue;
                    }
                    auto& cell = at(nx, ny, layer);
                    if (!cell.blocked) {
                        cell.net = net;
                        update_congestion(nx, ny, layer, 1);
                    }
                    cell.blocked = true;
                }
            }
        }
    };

    // Bresenham's line algorithm
    int dx = std::abs(x2 - x1);
    int dy = std::abs(y2 - y1);
    int sx = (x1 < x2) ? 1 : -1;
    int sy = (y1 < y2) ? 1 : -1;
    int err = dx - dy;

    int x = x1, y = y1;
    while (true) {
        mark_with_clearance(x, y);
        if (x == x2 && y == y2) break;

        int e2 = 2 * err;
        if (e2 > -dy) {
            err -= dy;
            x += sx;
        }
        if (e2 < dx) {
            err += dx;
            y += sy;
        }
    }
}

// Issue #4071: corridor-reservation keep-out (ported from Python).
//
// The Python sibling ``RoutingGrid._mark_via`` (src/kicad_tools/router/grid.py)
// consults a ``_reserved_for_nets`` map (introduced by Issue #2677 / PR #2686)
// to skip cells reserved for paired-escape continuation corridors when the
// via's net is not in the reservation owner set.  Issue #2709 originally left
// this C++ implementation WITHOUT that check (a documented deliberate omission
// with a contract-locking test in ``tests/test_grid_cpp_parity.py``), because
// no reservation-writing consumer reached the C++ ``mark_via`` at the time.
//
// That changed: #2983's inner-corner lane reservations (unconditional on
// qualifying match groups) and #4053/PR #4070's bundle-river via-hop
// reservations both write reservations that the production C++ backend was
// silently ignoring.  Issue #4071 ports the semantics: a cell reserved for a
// net set that EXCLUDES ``net`` is SKIPPED (the via halo does not claim/block
// it), so a foreign-net through-hole via cannot colonise a reserved corridor.
// Cells reserved for a set that INCLUDES ``net`` are treated as ordinary
// blockable cells (the owning net may still use its own reservation).  When
// no cell is reserved (``has_reservations_ == false``) the whole check is
// skipped, preserving byte-identical behaviour on boards without reservations.
void Grid3D::mark_via(int x, int y, int net, int radius_cells) {
    const bool check_reservations = has_reservations_;
    for (int layer = 0; layer < layers_; ++layer) {
        for (int dy = -radius_cells; dy <= radius_cells; ++dy) {
            for (int dx = -radius_cells; dx <= radius_cells; ++dx) {
                int nx = x + dx, ny = y + dy;
                if (is_valid(nx, ny, layer)) {
                    auto& cell = at(nx, ny, layer);
                    // Issue #4071: skip cells reserved for a net set that
                    // excludes ``net`` (matches Python ``_mark_via``).
                    if (check_reservations && cell.reserved_count > 0) {
                        bool owned = false;
                        for (int i = 0; i < cell.reserved_count; ++i) {
                            if (cell.reserved_nets[i] == net) {
                                owned = true;
                                break;
                            }
                        }
                        if (!owned) continue;
                    }
                    if (!cell.blocked) {
                        update_congestion(nx, ny, layer, 1);
                        cell.net = net;
                    }
                    cell.blocked = true;
                }
            }
        }
    }
}

// Issue #4071: corridor-reservation write API (mirrors Python
// ``RoutingGrid.reserve_corridor_cells`` on a per-cell basis).  Owner
// sets larger than ``RESERVED_NETS_CAP`` are truncated; overlapping
// reservations REPLACE (last-writer-wins).
void Grid3D::reserve_cell(int x, int y, int layer,
                          const std::vector<int>& net_ids) {
    if (!is_valid(x, y, layer)) return;
    auto& cell = at(x, y, layer);
    int n = 0;
    for (int nid : net_ids) {
        if (n >= RESERVED_NETS_CAP) break;
        cell.reserved_nets[n++] = nid;
    }
    // Zero the unused slots so a shrinking re-reservation cannot leave a
    // stale owner behind (defence-in-depth; reads use reserved_count).
    for (int i = n; i < RESERVED_NETS_CAP; ++i) {
        cell.reserved_nets[i] = 0;
    }
    cell.reserved_count = static_cast<int8_t>(n);
    if (n > 0) has_reservations_ = true;
}

void Grid3D::clear_reservations() {
    for (auto& cell : cells_) {
        if (cell.reserved_count > 0) {
            cell.reserved_count = 0;
            for (int i = 0; i < RESERVED_NETS_CAP; ++i) {
                cell.reserved_nets[i] = 0;
            }
        }
    }
    has_reservations_ = false;
}

int Grid3D::reserved_cell_count() const {
    int count = 0;
    for (const auto& cell : cells_) {
        if (cell.reserved_count > 0) ++count;
    }
    return count;
}

void Grid3D::unmark_segment(int x1, int y1, int x2, int y2, int layer, int net,
                            int clearance_cells) {
    auto unmark_with_clearance = [&](int gx, int gy) {
        for (int dy = -clearance_cells; dy <= clearance_cells; ++dy) {
            for (int dx = -clearance_cells; dx <= clearance_cells; ++dx) {
                int nx = gx + dx, ny = gy + dy;
                if (is_valid(nx, ny, layer)) {
                    auto& cell = at(nx, ny, layer);
                    if (cell.pad_blocked) {
                        cell.net = cell.original_net;
                    } else if (cell.net == net) {
                        // Issue #3545: STATICALLY blocked cells (pad
                        // clearance halos, keepouts) must survive
                        // rip-up.  Pre-fix, ripping a route whose
                        // clearance envelope overlapped its own pads'
                        // halo cells erased them (blocked=false,
                        // net=0), after which foreign nets routed
                        // straight through the halo and shipped
                        // sub-clearance copper.  Restore the static
                        // owner instead of freeing.
                        if (cell.static_blocked) {
                            cell.net = cell.original_net;
                        } else {
                            cell.blocked = false;
                            cell.net = 0;
                        }
                    }
                }
            }
        }
    };

    // Bresenham's line algorithm
    int dx = std::abs(x2 - x1);
    int dy = std::abs(y2 - y1);
    int sx = (x1 < x2) ? 1 : -1;
    int sy = (y1 < y2) ? 1 : -1;
    int err = dx - dy;

    int x = x1, y = y1;
    while (true) {
        unmark_with_clearance(x, y);
        if (x == x2 && y == y2) break;

        int e2 = 2 * err;
        if (e2 > -dy) {
            err -= dy;
            x += sx;
        }
        if (e2 < dx) {
            err += dx;
            y += sy;
        }
    }
}

void Grid3D::unmark_via(int x, int y, int net, int radius_cells) {
    for (int layer = 0; layer < layers_; ++layer) {
        for (int dy = -radius_cells; dy <= radius_cells; ++dy) {
            for (int dx = -radius_cells; dx <= radius_cells; ++dx) {
                int nx = x + dx, ny = y + dy;
                if (is_valid(nx, ny, layer)) {
                    auto& cell = at(nx, ny, layer);
                    if (cell.pad_blocked) {
                        cell.net = cell.original_net;
                    } else if (cell.net == net) {
                        // Issue #3545: restore static halo / keepout
                        // cells instead of freeing them (see
                        // ``unmark_segment`` for rationale).
                        if (cell.static_blocked) {
                            cell.net = cell.original_net;
                        } else {
                            cell.blocked = false;
                            cell.net = 0;
                        }
                    }
                }
            }
        }
    }
}

float Grid3D::get_congestion(int x, int y, int layer) const {
    int cx = std::min(x / congestion_size_, congestion_cols_ - 1);
    int cy = std::min(y / congestion_size_, congestion_rows_ - 1);
    size_t idx = static_cast<size_t>(layer) * congestion_rows_ * congestion_cols_ +
                 static_cast<size_t>(cy) * congestion_cols_ + cx;
    int count = congestion_[idx];
    int max_cells = congestion_size_ * congestion_size_;
    return std::min(1.0f, static_cast<float>(count) / max_cells);
}

void Grid3D::update_congestion(int x, int y, int layer, int delta) {
    int cx = std::min(x / congestion_size_, congestion_cols_ - 1);
    int cy = std::min(y / congestion_size_, congestion_rows_ - 1);
    size_t idx = static_cast<size_t>(layer) * congestion_rows_ * congestion_cols_ +
                 static_cast<size_t>(cy) * congestion_cols_ + cx;
    congestion_[idx] += delta;
}

void Grid3D::boost_region_cost(int center_x, int center_y, int layer,
                               int radius_cells, float amount) {
    int x1 = std::clamp(center_x - radius_cells, 0, cols_ - 1);
    int y1 = std::clamp(center_y - radius_cells, 0, rows_ - 1);
    int x2 = std::clamp(center_x + radius_cells, 0, cols_ - 1);
    int y2 = std::clamp(center_y + radius_cells, 0, rows_ - 1);

    for (int y = y1; y <= y2; ++y) {
        for (int x = x1; x <= x2; ++x) {
            // Chebyshev distance: max of dx, dy
            int dist = std::max(std::abs(x - center_x), std::abs(y - center_y));
            // Scale cost inversely with distance: full amount at center, tapering off
            float scale = 1.0f - static_cast<float>(dist) / (radius_cells + 1);
            at(x, y, layer).avoidance_cost += amount * scale;
        }
    }
}

void Grid3D::clear_avoidance_costs() {
    for (auto& cell : cells_) {
        cell.avoidance_cost = 0.0f;
    }
}

void Grid3D::reset_usage() {
    for (auto& cell : cells_) {
        cell.usage_count = 0;
    }
}

void Grid3D::increment_usage(int x, int y, int layer) {
    if (is_valid(x, y, layer)) {
        at(x, y, layer).usage_count++;
    }
}

void Grid3D::decrement_usage(int x, int y, int layer) {
    // Issue #3438: rip-up parity with RoutingGrid.unmark_route_usage.
    if (is_valid(x, y, layer)) {
        auto& cell = at(x, y, layer);
        if (cell.usage_count > 0) {
            cell.usage_count--;
        }
    }
}

float Grid3D::get_negotiated_cost(int x, int y, int layer, float present_factor,
                                  int net) const {
    if (!is_valid(x, y, layer)) {
        return std::numeric_limits<float>::infinity();
    }

    const auto& cell = at(x, y, layer);
    // Issue #2963: own-net obstacle cells (e.g. destination pad metal
    // marked is_obstacle=True on first touch by PR #2928) must remain
    // reachable for the routing net.  When ``net`` is 0 the caller
    // has no net context, so the legacy conservative reject stands;
    // when ``net`` is nonzero AND matches the cell's net the obstacle
    // gate is bypassed (foreign-net obstacles still hard-reject).
    if (cell.is_obstacle && (net == 0 || cell.net != net)) {
        return std::numeric_limits<float>::infinity();
    }

    float present_cost = present_factor * cell.usage_count;
    return present_cost + cell.history_cost;
}

void Grid3D::update_history_costs(float increment) {
    for (auto& cell : cells_) {
        if (cell.usage_count > 1) {
            cell.history_cost += increment * (cell.usage_count - 1);
        }
    }
}

int Grid3D::get_total_overflow() const {
    int overflow = 0;
    for (const auto& cell : cells_) {
        if (cell.usage_count > 1) {
            overflow += cell.usage_count - 1;
        }
    }
    return overflow;
}

int Grid3D::count_blocked() const {
    int count = 0;
    for (const auto& cell : cells_) {
        if (cell.blocked) count++;
    }
    return count;
}

float Grid3D::memory_mb() const {
    size_t bytes = cells_.size() * sizeof(GridCell) +
                   congestion_.size() * sizeof(int);
    return static_cast<float>(bytes) / (1024 * 1024);
}

// -----------------------------------------------------------------------
// Geometric validation (Issue #2439)
// -----------------------------------------------------------------------

void Grid3D::add_pad(float x, float y, float width, float height,
                     int net, int layer_idx, uint32_t ref_hash,
                     float clearance_override, bool is_plane_net) {
    pads_.push_back({x, y, width, height, net, layer_idx, ref_hash,
                     clearance_override, is_plane_net});
}

void Grid3D::add_stored_segment(float x1, float y1, float x2, float y2,
                                float width, int layer_idx, int net) {
    stored_segments_.push_back({x1, y1, x2, y2, width, layer_idx, net});
}

void Grid3D::add_stored_via(float x, float y, float drill, float diameter, int net) {
    stored_vias_.push_back({x, y, drill, diameter, net});
}

void Grid3D::clear_validation_data() {
    pads_.clear();
    stored_segments_.clear();
    stored_vias_.clear();
}

void Grid3D::clear_stored_routes() {
    // Issue #2481: Drop only stored route data (segments + vias).
    // Pads represent board geometry registered once at grid build time
    // and must survive rip-up cycles.
    stored_segments_.clear();
    stored_vias_.clear();
}

ValidationResult Grid3D::validate_route(
    const std::vector<Segment>& segments,
    const std::vector<Via>& vias,
    int exclude_net,
    const std::vector<uint32_t>& exclude_ref_hashes,
    float trace_clearance,
    float via_clearance,
    float min_drill_clearance,
    int partner_net,
    float intra_pair_clearance) const
{
    ValidationResult result;
    result.valid = true;
    result.min_clearance = std::numeric_limits<float>::infinity();

    // Issue #2559 / Epic #2556 Phase 1C: diff-pair within-pair clearance.
    // The partner branch is active when partner_net is a real net id (>= 0)
    // and intra_pair_clearance is a tighter (non-negative) override.  When
    // the branch is dormant (default), validation behaves exactly as before.
    bool partner_active =
        (partner_net >= 0) && (partner_net != exclude_net) && (intra_pair_clearance >= 0.0f);

    // Helper: check if a ref_hash is in the exclusion set
    auto is_excluded_ref = [&](uint32_t ref_hash) -> bool {
        for (auto h : exclude_ref_hashes) {
            if (h == ref_hash) return true;
        }
        return false;
    };

    // ---------------------------------------------------------------
    // 1. Validate segment clearance (port of grid.py:905-1118)
    //    Each candidate segment vs all pads, stored segments, stored vias
    // ---------------------------------------------------------------
    for (const auto& seg : segments) {
        float seg_half_width = seg.width / 2.0f;

        // 1a. Segment vs pads
        for (const auto& pad : pads_) {
            // Skip same-net pads
            if (pad.net == exclude_net) continue;

            // Issue #1764 + #2871 + #2908: the same-component-ref exclusion
            // is intended to permit signal-pin escape routing through the
            // chip's own perimeter (Issue #1764 reachability fix). It must
            // NOT permit signal traces to clip plane-net pads on the same
            // chip.
            //
            // PR #2873 narrowed the exclusion to ``pad.net != 0`` so the
            // SKIPPED-net convention (``skip_nets`` rewriting in
            // ``cpp_backend.py:596-605``) kept plane pads in the validator.
            // That convention only covers boards that pass ``--skip-nets``;
            // board 04 routes ``+3.3V`` / ``GND`` as real nets, so the
            // U2.1 / U2.8 / U2.23 / U2.24 plane pads retained ``net != 0``
            // and were silently exempted -- 44 ``clearance_pad_segment``
            // violations on the west edge cluster (Issue #2908).
            //
            // Issue #2908 broadens the carve-out: a same-component pad is
            // excluded ONLY when it is a SIGNAL pad (``!pad.is_plane_net``).
            // Plane-net pads -- whether ``net == 0`` (skipped-pour) or
            // ``net > 0`` with a power/ground name (classified in
            // ``cpp_backend.py``) -- are kept in the validator.  The
            // rect-aware geometry below ensures the disc-bound short-axis
            // over-rejection does not regress legitimate signal-vs-signal
            // corridor traces (the corridor relaxation in
            // ``_relax_same_component_clearance`` only operates between
            // non-plane pads).
            //
            // Issue #2933: The same-component signal-pad carve-out is
            // further narrowed -- it now only suppresses clearance complaints
            // when the trace stays OUTSIDE the neighbour pad's metal.  On
            // standard-pitch passives like 0805 resistors (R1 at 2mm pitch
            // on board 02), the router was emitting traces whose centerline
            // passed THROUGH the opposite pad's metal because the carve-out
            // silently exempted them.  This produced 144
            // ``clearance_pad_segment`` errors on board 02 at jlcpcb tier-1.
            // The metal-overlap check (clearance < 0) is preserved for all
            // same-component pads via the deferred-skip pattern below.
            const bool same_component_signal_carveout =
                !pad.is_plane_net && is_excluded_ref(pad.ref_hash);

            // Skip pads on different layers (unless through-hole: layer_idx == -1)
            if (pad.layer_idx != -1 && pad.layer_idx != seg.layer) continue;

            // Per-component clearance (Issue #1016)
            float required_clearance = pad.clearance_override;

            // Issue #2908: Rect-aware geometry for rectangular SMD pads.
            // The previous disc bound (``pad_radius = max(w, h) / 2``)
            // over-rejected along the pad's SHORT axis -- a 1.475 x 0.3 mm
            // LQFP-48 pad became a 0.7375 mm-radius disc, 0.587 mm of
            // phantom inflation above / below the pad metal.  Vias and
            // square pads (w == h within 1 micron) keep the disc model;
            // it is exact for circular obstacles and cheaper to evaluate.
            // Mirrors PR #2787 (validate/rules/clearance.py) and the
            // Python validator at ``router/grid.py``.
            float clearance;
            const bool is_circular_pad = std::abs(pad.width - pad.height) < 0.001f;
            if (is_circular_pad) {
                const float pad_radius = std::max(pad.width, pad.height) / 2.0f;
                const float dist = point_to_segment_distance(
                    pad.x, pad.y, seg.x1, seg.y1, seg.x2, seg.y2);
                clearance = dist - seg_half_width - pad_radius;
            } else {
                // Rect-aware: signed centerline-to-rect distance.  Negative
                // means the segment centerline lies inside the pad rectangle
                // (a real DRC defect).
                const float center_dist = rect_segment_centerline_distance(
                    pad.x, pad.y, pad.width, pad.height,
                    seg.x1, seg.y1, seg.x2, seg.y2);
                clearance = center_dist - seg_half_width;
            }

            // Issue #2933: Apply the same-component carve-out only when the
            // trace clears the neighbour pad's metal (clearance >= 0).
            // Negative clearance means the trace overlaps pad copper -- a
            // true defect that no carve-out should silence.
            //
            // Issue #3490: EXCEPTION -- a same-component pad on net 0
            // (an unconnected / NC pad) carries no electrical net, so a
            // trace endpoint that geometrically overlaps its metal is not
            // a real short.  When such a pad sits within metal-overlap
            // distance of the signal pad being routed (an LED whose
            // net=0 pin is 0.2mm from its signal pin -- the #1764
            // scenario), reaching the signal pad's center is impossible
            // without entering the net=0 pad's rectangle.  The footprint
            // guarantees the geometry, so silence the violation for
            // negative clearance too -- but ONLY for net=0 pads.  Foreign
            // SIGNAL pads (net != 0) keep the strict >= 0 guard so the
            // trace-through-pad-copper pathology (#2933) stays caught.
            if (same_component_signal_carveout && (clearance >= 0.0f || pad.net == 0)) {
                continue;
            }

            if (clearance < result.min_clearance) {
                result.min_clearance = clearance;
            }

            if (clearance < required_clearance - CLEARANCE_EPSILON_MM) {
                result.valid = false;
                result.violation_x = pad.x;
                result.violation_y = pad.y;
                result.violation_type = 1;  // seg-pad
                return result;
            }
        }

        // 1b. Segment vs stored segments (brute-force, no R-tree in C++)
        for (const auto& other : stored_segments_) {
            // Skip same-net segments
            if (other.net == exclude_net) continue;

            // Skip segments on different layers
            if (other.layer_idx != seg.layer) continue;

            float dist = segment_to_segment_distance(
                seg.x1, seg.y1, seg.x2, seg.y2,
                other.x1, other.y1, other.x2, other.y2);

            float clearance = dist - seg_half_width - other.width / 2.0f;

            if (clearance < result.min_clearance) {
                result.min_clearance = clearance;
            }

            // Issue #2559 / Phase 1C: tighter clearance for the diff-pair
            // partner only.  All other foreign nets keep the wider rule.
            float effective_clearance =
                (partner_active && other.net == partner_net)
                ? intra_pair_clearance
                : trace_clearance;

            if (clearance < effective_clearance - CLEARANCE_EPSILON_MM) {
                result.valid = false;
                result.violation_x = (seg.x1 + seg.x2 + other.x1 + other.x2) / 4.0f;
                result.violation_y = (seg.y1 + seg.y2 + other.y1 + other.y2) / 4.0f;
                result.violation_type = 2;  // seg-seg
                return result;
            }
        }

        // 1c. Segment vs stored vias
        for (const auto& sv : stored_vias_) {
            if (sv.net == exclude_net) continue;

            float via_radius = sv.diameter / 2.0f;
            float dist = point_to_segment_distance(
                sv.x, sv.y, seg.x1, seg.y1, seg.x2, seg.y2);

            float clearance = dist - seg_half_width - via_radius;

            if (clearance < result.min_clearance) {
                result.min_clearance = clearance;
            }

            // Issue #2559 / Phase 1C: tighter clearance for the partner.
            float effective_clearance =
                (partner_active && sv.net == partner_net)
                ? intra_pair_clearance
                : trace_clearance;

            if (clearance < effective_clearance - CLEARANCE_EPSILON_MM) {
                result.valid = false;
                result.violation_x = sv.x;
                result.violation_y = sv.y;
                result.violation_type = 3;  // seg-via
                return result;
            }
        }
    }

    // ---------------------------------------------------------------
    // 2. Validate via clearance (port of grid.py:1120-1192)
    //    Each candidate via vs stored segments on all layers
    // ---------------------------------------------------------------
    for (const auto& via : vias) {
        float via_radius = via.diameter / 2.0f;

        // Via spans from layer_from to layer_to
        int layer_lo = std::min(via.layer_from, via.layer_to);
        int layer_hi = std::max(via.layer_from, via.layer_to);

        for (const auto& seg : stored_segments_) {
            if (seg.net == exclude_net) continue;

            // Only check segments on layers the via spans
            if (seg.layer_idx < layer_lo || seg.layer_idx > layer_hi) continue;

            float seg_half_width = seg.width / 2.0f;
            float dist = point_to_segment_distance(
                via.x, via.y, seg.x1, seg.y1, seg.x2, seg.y2);

            float clearance = dist - via_radius - seg_half_width;

            if (clearance < result.min_clearance) {
                result.min_clearance = clearance;
            }

            if (clearance < via_clearance - CLEARANCE_EPSILON_MM) {
                result.valid = false;
                result.violation_x = via.x;
                result.violation_y = via.y;
                result.violation_type = 4;  // via-seg
                return result;
            }
        }

        // ---------------------------------------------------------------
        // 3. Via-to-via clearance (port of grid.py:1194-1251)
        //    Candidate via vs stored vias from different nets
        // ---------------------------------------------------------------
        for (const auto& sv : stored_vias_) {
            if (sv.net == exclude_net) continue;

            float dx = via.x - sv.x;
            float dy = via.y - sv.y;
            float distance = std::sqrt(dx * dx + dy * dy);
            float existing_via_radius = sv.diameter / 2.0f;
            float clearance = distance - via_radius - existing_via_radius;

            if (clearance < result.min_clearance) {
                result.min_clearance = clearance;
            }

            if (clearance < via_clearance - CLEARANCE_EPSILON_MM) {
                result.valid = false;
                result.violation_x = via.x;
                result.violation_y = via.y;
                result.violation_type = 5;  // via-via
                return result;
            }
        }

        // ---------------------------------------------------------------
        // 4. Same-net drill spacing (port of grid.py:1253-1317)
        //    Candidate via vs stored vias from SAME net
        // ---------------------------------------------------------------
        float drill_radius = via.drill / 2.0f;
        for (const auto& sv : stored_vias_) {
            if (sv.net != exclude_net) continue;

            // Skip self (exact same position)
            float ddx = via.x - sv.x;
            float ddy = via.y - sv.y;
            if (std::abs(ddx) < 1e-6f && std::abs(ddy) < 1e-6f) continue;

            float distance = std::sqrt(ddx * ddx + ddy * ddy);
            float existing_drill_radius = sv.drill / 2.0f;
            float clearance = distance - drill_radius - existing_drill_radius;

            if (clearance < result.min_clearance) {
                result.min_clearance = clearance;
            }

            if (clearance < min_drill_clearance - CLEARANCE_EPSILON_MM) {
                result.valid = false;
                result.violation_x = via.x;
                result.violation_y = via.y;
                result.violation_type = 6;  // drill spacing
                return result;
            }
        }
    }

    return result;
}

}  // namespace router
