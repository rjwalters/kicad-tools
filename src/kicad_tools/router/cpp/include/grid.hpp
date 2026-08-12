/*
 * Router C++ Core - 3D Grid
 * Part of kicad-tools router performance optimization (Phase 4)
 *
 * High-performance 3D grid with contiguous memory layout for cache efficiency.
 * Uses flat array indexing for O(1) cell access.
 */

#pragma once

#include "types.hpp"
#include <vector>
#include <cmath>
#include <algorithm>

namespace router {

class Grid3D {
public:
    Grid3D(int cols, int rows, int layers, float resolution,
           float origin_x, float origin_y);

    // Cell access - inline for performance
    inline GridCell& at(int x, int y, int layer) {
        return cells_[index(x, y, layer)];
    }

    inline const GridCell& at(int x, int y, int layer) const {
        return cells_[index(x, y, layer)];
    }

    inline bool is_valid(int x, int y, int layer) const {
        return x >= 0 && x < cols_ && y >= 0 && y < rows_ &&
               layer >= 0 && layer < layers_;
    }

    inline bool is_valid_and_free(int x, int y, int layer, int net) const {
        if (!is_valid(x, y, layer)) return false;
        const auto& cell = at(x, y, layer);
        if (!cell.blocked) return true;
        // Blocked cell is passable if it's the same net
        return cell.net == net && !cell.is_obstacle;
    }

    // Coordinate conversion
    inline std::pair<int, int> world_to_grid(float x, float y) const {
        int gx = static_cast<int>(std::round((x - origin_x_) / resolution_));
        int gy = static_cast<int>(std::round((y - origin_y_) / resolution_));
        return {std::clamp(gx, 0, cols_ - 1), std::clamp(gy, 0, rows_ - 1)};
    }

    inline std::pair<float, float> grid_to_world(int gx, int gy) const {
        return {origin_x_ + gx * resolution_, origin_y_ + gy * resolution_};
    }

    // Bulk operations for obstacle marking.
    //
    // Issue #3224: ``pad_blocked`` (default ``false``) marks the cell as
    // foreign pad copper -- the A* clearance branch in ``pathfinder.cpp``
    // refuses to step the trace centerline into a foreign pad's metal
    // even during pad-exit (where clearance-halo cells are otherwise
    // allowed).  Without this bit, ``cell.pad_blocked`` defaults to
    // ``false`` for every cell and the pad-exit exemption permits traces
    // to step through foreign pad metal, producing the
    // ``clearance_pad_segment`` regression on dense fine-pitch packages
    // (board 05 / U3 QFN-56 / U10 LQFP-32).  The Python sync
    // (``cpp_backend.py::from_routing_grid`` and
    // ``grid.py::_sync_pad_to_cpp_grid``) sets this bit only for cells
    // inside a pad's metal area (NOT the surrounding clearance halo),
    // mirroring the Python grid's ``_pad_blocked[metal_slice] = True``
    // at ``grid.py:4458``.
    void mark_blocked(int x, int y, int layer, int net, bool is_obstacle = false,
                      bool pad_blocked = false);
    void mark_rect_blocked(int x1, int y1, int x2, int y2, int layer, int net,
                           bool is_obstacle = false);

    // Route marking with clearance buffer using Bresenham.
    // Issue #4079: like ``mark_via``, ``mark_segment`` NOW consults the
    // per-cell corridor reservation owner set (``GridCell::reserved_nets``)
    // for lateral-trace keep-out, mirroring Python ``_mark_segment``.  A
    // cell reserved for a net set that EXCLUDES ``net`` is SKIPPED (the
    // foreign net's trace does not claim/block it), so a foreign lateral
    // trace cannot colonise a reserved continuation corridor.  Cells
    // reserved for a set that INCLUDES ``net`` are ordinary blockable
    // cells.  Fast path: when ``has_reservations_ == false`` the check is
    // skipped, preserving byte-identical behaviour on boards without
    // reservations.  See ``cpp/src/grid.cpp`` and
    // ``tests/test_grid_cpp_parity.py``.
    void mark_segment(int x1, int y1, int x2, int y2, int layer, int net,
                      int clearance_cells);
    // Issue #4071: ``mark_via`` NOW consults the per-cell corridor
    // reservation owner set (``GridCell::reserved_nets``), mirroring
    // Python's ``RoutingGrid._mark_via`` (Issue #2677).  A cell reserved
    // for a net set that EXCLUDES ``net`` is SKIPPED (the via halo does
    // not claim/block it), so partner-net through-hole vias cannot
    // colonise a reserved continuation corridor.  Cells reserved for a
    // set that INCLUDES ``net`` are treated as ordinary blockable cells.
    // Fast path: when no cell is reserved (``has_reservations_ == false``)
    // the check is skipped entirely, preserving byte-identical behaviour.
    // See ``cpp/src/grid.cpp`` and ``tests/test_grid_cpp_parity.py``.
    void mark_via(int x, int y, int net, int radius_cells);

    // Unmark route (rip-up)
    void unmark_segment(int x1, int y1, int x2, int y2, int layer, int net,
                        int clearance_cells);
    void unmark_via(int x, int y, int net, int radius_cells);

    // -----------------------------------------------------------------------
    // Issue #4071: corridor-reservation API (mirrors
    // ``RoutingGrid.reserve_corridor_cells`` / ``is_reserved_for`` /
    // ``clear_corridor_reservations`` / ``reserved_cell_count``).
    // -----------------------------------------------------------------------

    // Reserve a single cell on ``layer`` for the given owner net set.
    // Owner sets larger than ``RESERVED_NETS_CAP`` are truncated to the
    // first K nets (documented ceiling; no current caller exceeds 2).
    // Overlapping reservations REPLACE (last-writer-wins), matching the
    // Python semantics.  A single reserved cell flips the grid-wide
    // ``has_reservations_`` fast-path flag.
    // Issue #4079: ``soft`` selects the keep-out strength.  ``false``
    // (default) is a HARD reservation -- foreign vias AND lateral traces
    // are fenced out (``is_reserved_excluding`` true).  ``true`` is a SOFT
    // reservation -- only the A* attractor bonus applies for the owner
    // net; foreign traces may still cross (``is_reserved_excluding``
    // false).  See the #4079 note in ``types.hpp``.
    void reserve_cell(int x, int y, int layer,
                      const std::vector<int>& net_ids, bool soft = false);

    // Clear every corridor reservation (owner sets + fast-path flag).
    void clear_reservations();

    // Number of cells with a non-empty owner set (instrumentation /
    // parity tests, mirrors ``RoutingGrid.reserved_cell_count``).
    int reserved_cell_count() const;

    // True iff the cell is reserved AND ``net`` is in its owner set.
    // Mirrors ``RoutingGrid.is_reserved_for``.  Inline for the A* cost
    // loop and ``mark_via`` hot paths.
    inline bool is_reserved_for(int x, int y, int layer, int net) const {
        if (!has_reservations_) return false;
        if (!is_valid(x, y, layer)) return false;
        const auto& cell = at(x, y, layer);
        for (int i = 0; i < cell.reserved_count; ++i) {
            if (cell.reserved_nets[i] == net) return true;
        }
        return false;
    }

    // Issue #4079: True iff the cell is reserved for a net set that
    // EXCLUDES ``net`` -- i.e. a foreign net whose lateral trace (or via
    // halo) must be fenced out of the reserved corridor cell.  Returns
    // false for an unreserved cell OR a cell reserved for a set that
    // INCLUDES ``net`` (the owner may use its own reservation).  This is
    // the lateral-trace keep-out primitive consulted by
    // ``mark_segment`` and ``Pathfinder::is_trace_blocked`` (parity with
    // the Python ``_reserved_for_nets`` "owners is not None and net not
    // in owners" check in ``_mark_via`` / ``_mark_segment`` /
    // ``_is_trace_blocked``).  Inline for the A* hot path.
    inline bool is_reserved_excluding(int x, int y, int layer, int net) const {
        if (!has_reservations_) return false;
        if (!is_valid(x, y, layer)) return false;
        const auto& cell = at(x, y, layer);
        if (cell.reserved_count <= 0) return false;
        // Issue #4079: a SOFT reservation never fences foreign traffic out
        // -- it contributes only the A* attractor bonus for its owner net.
        // Only HARD reservations produce a foreign keep-out.
        if (cell.reserved_soft) return false;
        for (int i = 0; i < cell.reserved_count; ++i) {
            if (cell.reserved_nets[i] == net) return false;  // owner net
        }
        return true;  // hard-reserved, but not for this net
    }

    // True iff ANY cell is currently reserved (grid-wide fast path).
    inline bool has_reservations() const { return has_reservations_; }

    // Issue #4071: soft corridor-attractor bonus for a single cell.
    // Returns ``bonus`` when the cell is reserved for ``net``, else 0.0.
    // Mirrors ``RoutingGrid.get_corridor_attractor_bonus``.  Inline for
    // the A* cost loop.
    inline float corridor_attractor_bonus(int x, int y, int layer, int net,
                                          float bonus) const {
        if (!has_reservations_ || bonus <= 0.0f) return 0.0f;
        return is_reserved_for(x, y, layer, net) ? bonus : 0.0f;
    }

    // Congestion tracking
    float get_congestion(int x, int y, int layer) const;
    void update_congestion(int x, int y, int layer, int delta = 1);

    // DRC avoidance feedback
    void boost_region_cost(int center_x, int center_y, int layer,
                           int radius_cells, float amount);
    void clear_avoidance_costs();

    // Negotiated routing support
    void reset_usage();
    void increment_usage(int x, int y, int layer);
    // Issue #3438: rip-up parity.  Mirrors RoutingGrid.unmark_route_usage
    // so the Python negotiated loop can keep the C++ usage counts in
    // lock-step with the Python grid (previously usage was never synced,
    // so the C++ sharing-mode clauses saw usage_count == 0 everywhere and
    // treated ALL foreign copper as hard).  Clamps at zero.
    void decrement_usage(int x, int y, int layer);
    // Issue #2963: optional ``net`` parameter — when nonzero AND the
    // cell's net matches, the ``is_obstacle`` hard-reject is skipped
    // (the destination pad's own metal stays reachable for its own
    // routing net post-PR #2928's first-touch obstacle marking).
    float get_negotiated_cost(int x, int y, int layer, float present_factor,
                              int net = 0) const;
    void update_history_costs(float increment);
    int get_total_overflow() const;

    // Accessors
    int cols() const { return cols_; }
    int rows() const { return rows_; }
    int layers() const { return layers_; }
    float resolution() const { return resolution_; }
    size_t total_cells() const { return cells_.size(); }

    // Statistics
    int count_blocked() const;
    float memory_mb() const;

    // -----------------------------------------------------------------------
    // Geometric validation storage and methods (Issue #2439)
    // Eliminates Python callback overhead for post-route clearance checks.
    // -----------------------------------------------------------------------

    // Register pads for clearance validation.
    // clearance_override: pre-computed from rules.get_clearance_for_component()
    // is_plane_net (Issue #2908): True when the pad's net carries plane
    // (power/ground) topology -- used by validate_route() to skip the
    // same-component-ref carve-out so plane pads remain validated even
    // when the routing context excludes their component (mirrors the
    // Python ``_is_plane_net_pad`` helper).
    void add_pad(float x, float y, float width, float height,
                 int net, int layer_idx, uint32_t ref_hash,
                 float clearance_override,
                 bool is_plane_net = false);

    // Register a completed route's segments for clearance validation.
    void add_stored_segment(float x1, float y1, float x2, float y2,
                            float width, int layer_idx, int net);

    // Register a completed route's via for clearance validation.
    void add_stored_via(float x, float y, float drill, float diameter, int net);

    // Clear all stored validation data (pads, segments, vias).
    void clear_validation_data();

    // Clear only stored routes (segments + vias), keeping pads.
    // Issue #2481: Used by CppGrid.invalidate_stored_routes() after a
    // rip-up on the Python side.  ``Pathfinder::is_via_blocked_diag``
    // (Issue #2466) consults ``stored_vias_`` to refuse via placements
    // that would violate cross-net clearance with already-placed routes.
    // Without this clearing path, those entries remain even when the
    // owning route has been ripped up, leading to false rejections (and,
    // when the surviving routes are later re-synced, double-counted
    // vias).  Pads are intentionally left untouched: they are intrinsic
    // board geometry and never change between sync points.
    void clear_stored_routes();

    // Validate a candidate route against all stored pads, segments, and vias.
    // Ports the 4 Python validation methods from grid.py lines 905-1317:
    //   - validate_segment_clearance (seg vs pads + stored segs + stored vias)
    //   - validate_via_clearance (via vs stored segs)
    //   - validate_via_to_via_clearance (via vs stored vias, different net)
    //   - validate_same_net_drill_spacing (via vs stored vias, same net)
    //
    // exclude_net: net ID of the route being validated (same-net OK)
    // exclude_ref_hashes: FNV-1a hashes of component refs to exclude
    //                     (start/end pad components, Issue #1764)
    // trace_clearance: default clearance for segments
    // via_clearance: default clearance for vias
    // min_drill_clearance: minimum drill-to-drill spacing (same-net)
    // partner_net: Issue #2559 / Phase 1C -- diff-pair partner net id, or
    //              -1 to disable the partner branch (default).  When set,
    //              segment-vs-segment / segment-vs-via comparisons against
    //              partner_net use intra_pair_clearance instead of
    //              trace_clearance.  Defaults preserve pre-#2559 behavior.
    // intra_pair_clearance: tighter clearance applied only to the partner.
    //
    // Issue #4510 / Epic #4431 Phase 2a: when ``set_pairwise_domains`` has
    // installed a domain matrix, the required clearance for a pair whose
    // BOTH nets have a known domain becomes
    // ``max(effective_scalar, matrix[dom_a][dom_b])``.  The diff-pair
    // partner relaxation keeps precedence (it *tightens within* a pair; the
    // matrix *widens across* domains), and an installed attach zone
    // (``set_attach_zones``) covering the gap point waives the widening --
    // never the scalar floor.  Issue #4507: the zone consult is layer-scoped,
    // keyed on the layer the compared copper shares (via-vs-via, where no
    // single layer applies, stays layer-agnostic; the segment-vs-pad branch
    // keys on the pad's own layer, so a through-hole pad stays agnostic too).
    ValidationResult validate_route(
        const std::vector<Segment>& segments,
        const std::vector<Via>& vias,
        int exclude_net,
        const std::vector<uint32_t>& exclude_ref_hashes,
        float trace_clearance,
        float via_clearance,
        float min_drill_clearance,
        int partner_net = -1,
        float intra_pair_clearance = 0.0f) const;

    // -----------------------------------------------------------------------
    // Pairwise (HV-isolation) domain clearance -- Issue #4510, Phase 2a of
    // epic #4431.  Both setters are DORMANT by default: until one is called
    // the validator behaves byte-identically to the pre-#4510 code path.
    // -----------------------------------------------------------------------

    // Install the per-net domain-id array plus the dense domain-pair
    // clearance matrix (mm).
    //
    // net_to_domain: indexed by net id; entry is the net's domain index, or
    //                -1 for "no domain" (the overwhelming majority of nets on
    //                a board with a partial voltage map).  Net ids beyond the
    //                array's length are treated as domain-less.
    // matrix:        ``matrix[dom_a][dom_b]`` is the REQUIRED clearance (mm)
    //                between the two domains -- a pure *widening* value, i.e.
    //                0.0 for pairs that need no HV widening beyond the scalar
    //                floor.  Must be square and symmetric; Python builds it
    //                from ``PairwiseClearanceTable.required_by_pair``.
    //
    // Passing empty containers returns the grid to the dormant state.
    void set_pairwise_domains(const std::vector<int>& net_to_domain,
                              const std::vector<std::vector<float>>& matrix);

    // Install the rated-footprint attach zones (Issue #4506) used to waive
    // the pairwise widening (never the scalar floor) inside a domain-bridging
    // footprint's necking region.  Zones are computed once per routing
    // session on the Python side; passing an empty vector clears them.
    void set_attach_zones(const std::vector<AttachZone>& zones);

    // True once a non-empty domain matrix has been installed.
    bool pairwise_active() const { return pairwise_active_; }
    size_t attach_zone_count() const { return attach_zones_.size(); }

    // Required pairwise clearance (mm) between two net ids.  Returns 0.0 when
    // the matrix is dormant, when either net id is out of range, or when
    // either net has no domain -- so ``max(scalar, this)`` is the scalar.
    float pairwise_required_clearance(int net_a, int net_b) const;

    // Issue #4511: the largest widening value in the installed domain matrix
    // (mm), or 0.0 when dormant.  The search-time pairwise avoidance
    // (``Pathfinder``) sizes its widened blocking kernel from this bound so
    // it never scans further than any domain pair could ever require.  Cached
    // by ``set_pairwise_domains`` -- an O(1) read on the A* hot path.
    float max_pairwise_clearance() const { return max_pairwise_clearance_; }

    // True when an installed attach zone contains ``(x, y)`` AND has BOTH
    // ``net_a`` and ``net_b`` among its member net ids.
    //
    // Issue #4507: ``layer`` is the grid layer index the two conflicting
    // objects share; a zone waives the pair there only when BOTH nets have pad
    // copper on that layer (``AttachZone::net_layers``).  Pass ``-1`` -- the
    // default -- when no single layer applies (a through-via against a
    // through-via, or a caller with no layer context), which keeps the
    // layer-agnostic pre-#4507 verdict.  This mirrors the ``layer=None``
    // convention of ``AttachZone.exempts`` in ``pairwise_clearance.py`` and of
    // ``LatticePairwise.exempt`` in ``lattice/pairwise.py``, so the three
    // engines waive on the same layers for every comparison they all perform
    // (segment-vs-segment, segment-vs-via, via-vs-via).
    //
    // The one place they cannot be compared is ``validate_route``'s
    // segment-vs-pad branch: it passes a through-hole pad's ``layer_idx ==
    // -1``, waiving on layers ``AttachZone.exempts`` would not, but it has no
    // Python counterpart to disagree with -- ``route_pairwise_violation`` and
    // ``find_pairwise_violations`` are segment-vs-segment only and never
    // compare a segment against a pad.  That asymmetry is unchanged v18
    // behaviour; see the comment at the ``pad_zone_layer`` assignment in
    // ``grid.cpp``.
    bool attach_zone_exempts(float x, float y, int net_a, int net_b,
                             int layer = -1) const;

    // Accessors for validation data sizes (for testing/debugging)
    size_t pad_count() const { return pads_.size(); }
    size_t stored_segment_count() const { return stored_segments_.size(); }
    size_t stored_via_count() const { return stored_vias_.size(); }

    // Accessor for stored vias (Issue #2466).
    // Used by Pathfinder::is_via_blocked to perform a geometric via-vs-via
    // clearance check that mirrors validate_route() exactly, so the search
    // refuses placements the post-route validator would later reject.
    const std::vector<StoredVia>& stored_vias() const { return stored_vias_; }

private:
    inline size_t index(int x, int y, int layer) const {
        return static_cast<size_t>(layer) * rows_ * cols_ +
               static_cast<size_t>(y) * cols_ +
               static_cast<size_t>(x);
    }

    std::vector<GridCell> cells_;  // Flat array for cache efficiency
    // Issue #4071: grid-wide fast-path flag -- true once any cell has a
    // non-empty reservation owner set.  ``mark_via`` and the A* cost loop
    // skip the per-cell reservation read entirely when this is false, so
    // boards without active reservations keep byte-identical behaviour.
    bool has_reservations_ = false;
    int cols_, rows_, layers_;
    float resolution_;
    float origin_x_, origin_y_;

    // Congestion grid (coarser)
    std::vector<int> congestion_;
    int congestion_cols_, congestion_rows_;
    int congestion_size_ = 8;  // Cells per congestion region

    // Geometric validation storage (Issue #2439)
    std::vector<PadInfo> pads_;
    std::vector<StoredSegment> stored_segments_;
    std::vector<StoredVia> stored_vias_;

    // Pairwise (HV-isolation) domain clearance storage (Issue #4510).
    // ``pairwise_active_`` is the grid-wide fast-path flag: when false the
    // widening branch in validate_route() is skipped entirely, so boards
    // without a voltage map keep byte-identical behaviour and cost.
    bool pairwise_active_ = false;
    int domain_count_ = 0;
    std::vector<int> net_domain_;        // net id -> domain index (-1 = none)
    std::vector<float> domain_matrix_;   // row-major domain_count_ x domain_count_
    std::vector<AttachZone> attach_zones_;
    // Issue #4511: cached max widening across the installed matrix (mm); 0.0
    // when dormant.  Consumed by the search-time widened kernel sizing.
    float max_pairwise_clearance_ = 0.0f;
};

}  // namespace router
