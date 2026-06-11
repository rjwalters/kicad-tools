/*
 * Router C++ Core - A* Pathfinder
 * Part of kicad-tools router performance optimization (Phase 4)
 *
 * High-performance A* pathfinding with:
 * - Multi-layer support with via transitions
 * - Diagonal routing (45-degree angles)
 * - Congestion-aware routing costs
 * - Negotiated routing support for rip-up and reroute
 */

#pragma once

#include "types.hpp"
#include "grid.hpp"
#include <vector>
#include <queue>
#include <unordered_set>
#include <unordered_map>
#include <chrono>
#include <cstdint>
#include <limits>

namespace router {

// Hash for 3D grid coordinates in unordered_set/unordered_map
struct GridPosHash {
    size_t operator()(const std::tuple<int, int, int>& pos) const {
        auto [x, y, layer] = pos;
        return std::hash<int>()(x) ^
               (std::hash<int>()(y) << 10) ^
               (std::hash<int>()(layer) << 20);
    }
};

class Pathfinder {
public:
    Pathfinder(Grid3D& grid, const DesignRules& rules, bool diagonal_routing = true);

    // Main routing function (non-resumable, backward compatible)
    //
    // Issue #2610: ``per_net_timeout_seconds`` is the wall-clock budget for
    // this A* invocation.  When <= 0 (default), no wall-clock deadline is
    // enforced and the search runs until success, open-set exhaustion, or
    // the memory-backstop iteration cap.  ``max_search_iterations`` is the
    // hard iteration ceiling; when <= 0 (default) it falls back to the
    // historical ``cols * rows * 4`` heuristic.  When the search aborts
    // because the deadline fired, ``RouteResult::failure_reason`` is set to
    // ``FAILURE_TIMEOUT``; iteration-cap aborts set ``FAILURE_ITERATION_LIMIT``.
    RouteResult route(
        float start_x, float start_y, int start_layer,
        float end_x, float end_y, int end_layer,
        int net,
        const std::vector<int>& start_layers = {},  // For PTH pads
        const std::vector<int>& end_layers = {},    // For PTH pads
        bool negotiated_mode = false,
        float present_cost_factor = 0.0f,
        float weight = 1.0f,  // A* weight (1.0 = optimal, >1.0 = faster)
        int trace_radius_cells = 0,  // Per-net trace clearance radius (0 = use default)
        int via_radius_cells = 0,    // Per-net via clearance radius (0 = use default)
        const PadBounds& start_pad_bounds = {},  // Start pad metal/approach bounds
        const PadBounds& end_pad_bounds = {},    // End pad metal/approach bounds
        // Issue #2559 / Epic #2556 Phase 1C: diff-pair within-pair clearance.
        // partner_net = -1 disables the partner branch (default; pre-#2559 behavior).
        // intra_pair_radius_cells = 0 means "no tighter radius" -- the partner
        // (when set) is treated with the wider trace_radius_cells.
        int partner_net = -1,
        int intra_pair_radius_cells = 0,
        // Issue #2610: per-net wall-clock deadline (seconds; <= 0 disables)
        // and override for the iteration backstop (<= 0 = use cols*rows*4).
        double per_net_timeout_seconds = 0.0,
        int max_search_iterations = 0,
        // Issue #3130: per-net emit widths/diameters.  When > 0, override the
        // values written into the reconstructed Segment::width /
        // Via::diameter / Via::drill so the C++-internal RouteResult carries
        // per-net widths instead of the global ``rules_`` defaults.  This
        // matches the per-net ``trace_radius_cells`` / ``via_radius_cells``
        // plumbing already used by the A* expansion (the search behaviour
        // is unchanged -- only the emit values are affected).
        float emit_trace_width = 0.0f,
        float emit_via_diameter = 0.0f,
        float emit_via_drill = 0.0f,
        // Issue #3143: per-pad lateral-channel budget.  Empty (the default)
        // disables the new cost term and preserves pre-#3143 behaviour
        // identically.  When non-empty, each PadChannelBudget defines a
        // grid-coordinate bbox + soft capacity + per-cell overflow penalty
        // that nudges the A* search toward less-contested escape paths in
        // the lateral channels adjacent to dense-package pad rows.  See
        // ``types.hpp::PadChannelBudget`` for the per-field contract.
        const std::vector<PadChannelBudget>& pad_channel_budgets = {}
    );

    // Resumable A* routing: initializes search state and runs to first goal.
    // Returns a RouteResult; if success=true, state is preserved for resume().
    // Caller must call clear_search_state() when done (success or failure).
    //
    // Issue #2610: see ``route()`` for ``per_net_timeout_seconds`` and
    // ``max_search_iterations`` semantics.  The deadline is computed once
    // at ``route_resumable()`` entry and shared across the initial search
    // and any subsequent ``resume()`` calls, so a single per-net budget
    // covers all retry attempts.
    RouteResult route_resumable(
        float start_x, float start_y, int start_layer,
        float end_x, float end_y, int end_layer,
        int net,
        const std::vector<int>& start_layers = {},
        const std::vector<int>& end_layers = {},
        bool negotiated_mode = false,
        float present_cost_factor = 0.0f,
        float weight = 1.0f,
        int trace_radius_cells = 0,
        int via_radius_cells = 0,
        const PadBounds& start_pad_bounds = {},
        const PadBounds& end_pad_bounds = {},
        // Issue #2559 / Epic #2556 Phase 1C: diff-pair within-pair clearance.
        // See comment on route() above; defaults preserve pre-#2559 behavior.
        int partner_net = -1,
        int intra_pair_radius_cells = 0,
        // Issue #2610: deadline + iteration override; see ``route()`` above.
        double per_net_timeout_seconds = 0.0,
        int max_search_iterations = 0,
        // Issue #3130: per-net emit widths/diameters.  See ``route()`` above
        // for semantics.  Cached on the Pathfinder member fields so
        // ``resume()`` -> ``reconstruct_path()`` honours the same per-net
        // values across the (initial + resume*) sequence for a single net.
        float emit_trace_width = 0.0f,
        float emit_via_diameter = 0.0f,
        float emit_via_drill = 0.0f,
        // Issue #3143: per-pad lateral-channel budget.  See ``route()``
        // above for semantics.  Cached on the Pathfinder member fields
        // (search_pad_budget_cost_lookup_) so resume() consults the same
        // per-cell penalty map across an (initial + resume*) sequence.
        const std::vector<PadChannelBudget>& pad_channel_budgets = {}
    );

    // Resume A* search after rejecting a goal cell.
    // Adds (reject_x, reject_y, reject_layer) to skip set and continues
    // from the preserved open set. Returns next-best result.
    RouteResult resume(int reject_x, int reject_y, int reject_layer);

    // Clear all A* search state (open set, closed set, g scores, etc.).
    // Must be called after route_resumable()/resume() to release memory.
    void clear_search_state();

    // Configure routable layers (skip plane layers)
    void set_routable_layers(const std::vector<int>& layers);

    // Statistics from last route
    int get_iterations() const { return last_iterations_; }
    int get_nodes_explored() const { return last_nodes_explored_; }

    // Check if via placement is blocked on all layers.
    // radius_override: if > 0, use this instead of via_half_cells_.
    //
    // Public to allow direct exercise from regression tests.  See
    // tests/test_router_cpp_via_clearance.py (Issue #2466) which verifies
    // that the search refuses placements the post-route validator would
    // later reject.
    bool is_via_blocked(int x, int y, int net, bool allow_sharing,
                        int radius_override = 0) const;

    // Issue #2476: Diagnostic-aware variant of is_via_blocked.
    //
    // Identical semantics to the boolean overload above, but additionally
    // records the offending stored-via net (when the geometric via-vs-via
    // clearance rule is what caused the rejection).  When the function
    // returns true:
    //   * out_blocking_net != 0 indicates a geometric via-vs-via reject; the
    //     value is the net id of the stored via that triggered the
    //     clearance violation.  ``out_world_x``/``out_world_y`` carry the
    //     world-coordinate location of the rejected candidate.
    //   * out_blocking_net == 0 indicates the rejection came from the
    //     grid-cell blocking heuristic (out-of-bounds or another net's
    //     marked clearance), not the stored-via geometry.
    bool is_via_blocked_diag(int x, int y, int net, bool allow_sharing,
                             int radius_override,
                             int& out_blocking_net,
                             float& out_world_x,
                             float& out_world_y) const;

    // Issue #3226: Check whether any foreign-net pad-metal cell sits within
    // ``radius`` Chebyshev cells of (x, y, layer).  This is a strict subset
    // of ``is_trace_blocked`` -- it only treats cells with
    // ``pad_blocked == true`` AND ``cell.net != net`` as obstacles, ignoring
    // pure clearance-halo cells, copper-pour cells, and routed-trace cells.
    //
    // Used by the pad-exit relaxation in the A* loop.  The relaxation lets
    // the trace step into a foreign-pad clearance halo cell while exiting
    // a same-net pad, but must still refuse to land a trace centerline so
    // close to FOREIGN pad metal that the trace edge (radius cells beyond
    // the centerline) would encroach on the foreign pad copper.
    //
    // Returns true when there is at least one foreign-pad-metal cell within
    // ``radius`` (i.e. the centerline placement here would violate
    // pad clearance).  Returns false when the relaxation is safe.
    //
    // Public for binding + unit-test access; the pad-exit relaxation in the
    // A* loop calls this internally, but having it on the public surface
    // lets the regression tests assert symmetry with the Python sibling
    // ``Router._is_foreign_pad_metal_within_radius`` without forcing the
    // tests to drive a full route() call.
    bool is_foreign_pad_metal_within_radius(int x, int y, int layer, int net,
                                            int radius) const;

    // Issue #3438: Relief-probe mode for zero-overflow hard failures.
    //
    // In negotiated (sharing) mode, foreign-net cells with
    // ``usage_count == 0`` -- escape stubs, committed-route clearance
    // halos, via halo rings -- are HARD obstacles.  On dense pad-array
    // bundles (board 07's full-bus-reversal DDR byte) sibling stubs and
    // vias can seal a pin's only exit corridor, producing an instant
    // empty-frontier A* abort with ZERO overflow: PathFinder gets no
    // congestion signal to negotiate, and the failed net is permanently
    // stranded.
    //
    // When relief mode is enabled, those foreign usage-0 non-obstacle
    // cells become passable at a finite penalty
    // (``relief_conflict_penalty_`` per conflicted step) instead of
    // hard-blocking.  The negotiated outer loop runs a one-shot relief
    // PROBE for each zero-overflow hard failure, extracts the owner nets
    // of the conflicted cells along the probe path, and feeds them to the
    // transactional targeted rip-up.  The probe route itself is never
    // committed.  Foreign ``is_obstacle`` cells (pads, keepouts) and
    // net-0 static blockage remain hard in relief mode.
    void set_relief_mode(bool enabled) { relief_mode_ = enabled; }
    bool relief_mode() const { return relief_mode_; }

private:
    // Check if trace placement is blocked (accounts for trace width)
    // radius_override: if > 0, use this instead of trace_half_width_cells_
    //
    // Issue #2559 / Epic #2556 Phase 1C: when partner_net >= 0 and
    // partner_radius > 0 and partner_radius < radius, cells whose net
    // matches partner_net are checked against the smaller partner_radius
    // instead of the wider radius.  This implements within-pair clearance
    // for differential pairs.  Defaults preserve pre-#2559 behavior.
    bool is_trace_blocked(int x, int y, int layer, int net, bool allow_sharing,
                          int radius_override = 0,
                          int partner_net = -1,
                          int partner_radius = 0) const;

    // Check if diagonal move cuts through obstacles
    bool is_diagonal_blocked(int x, int y, int dx, int dy, int layer, int net,
                             bool allow_sharing) const;

    // Heuristic: Manhattan distance with layer change cost
    float heuristic(int x, int y, int layer, int goal_x, int goal_y, int goal_layer) const;

    // Get congestion cost for a cell
    float get_congestion_cost(int x, int y, int layer) const;

    // Issue #3143: per-cell pad-channel cost lookup.
    //
    // Returns the cached overflow penalty for cell (x, y, layer) when the
    // current search has a per-pad channel budget configured AND the cell
    // falls inside one of the budget bboxes AND the channel is at-or-above
    // capacity.  Returns 0.0 otherwise.  Hot-path constant-time lookup
    // against a pre-built ``unordered_map`` keyed by (gx, gy, layer);
    // populated once at the top of ``route_resumable()`` and held
    // constant across resume() calls.  The penalty is additive on the
    // A* g_score, so its scale is comparable to ``rules_.cost_straight``
    // (typically 1.0) -- a penalty of 5.0 is roughly 5 cells of detour.
    float get_pad_channel_cost(int x, int y, int layer) const;

    // Core A* loop shared by route(), route_resumable(), and resume().
    // Returns RouteResult with success=true if goal reached, or success=false
    // if open set exhausted / iteration limit hit.
    RouteResult run_astar_loop();

    // Reconstruct path from A* result.
    //
    // Issue #3130: ``emit_trace_width`` / ``emit_via_diameter`` /
    // ``emit_via_drill`` override the global ``rules_`` defaults when > 0.
    // Defaults preserve pre-#3130 emit behavior identically.
    RouteResult reconstruct_path(
        const std::vector<AStarNode>& closed_list,
        int end_idx,
        float start_x, float start_y,
        float end_x, float end_y,
        int net,
        float emit_trace_width = 0.0f,
        float emit_via_diameter = 0.0f,
        float emit_via_drill = 0.0f
    );

    Grid3D& grid_;
    DesignRules rules_;
    bool diagonal_routing_;

    // Issue #3438: relief-probe mode (see set_relief_mode above).  When
    // true, the sharing-mode hard clause for foreign usage-0 non-obstacle
    // cells is downgraded to a finite per-step penalty so the A* search
    // can produce a min-conflict probe path through sealed escape
    // corridors instead of an instant empty-frontier abort.
    bool relief_mode_ = false;
    float relief_conflict_penalty_ = 20.0f;

    // Issue #3438: per-step relief penalty helper.  Returns
    // ``relief_conflict_penalty_`` when (x, y, layer) holds a foreign-net
    // usage-0 non-obstacle blocked cell and relief mode is on; 0 otherwise.
    float relief_conflict_cost(int x, int y, int layer, int net) const;

    // Pre-computed neighbor offsets
    std::vector<Neighbor> neighbors_2d_;

    // Pre-computed radii in grid cells
    int trace_half_width_cells_;
    int via_half_cells_;

    // Issue #3229: Pre-computed circular (Euclidean) kernel offsets for
    // ``trace_half_width_cells_``.  Each pair ``(dx, dy)`` satisfies
    // ``dx*dx + dy*dy <= radius*radius``.  Replaces the square Chebyshev
    // scan used pre-#3229 (``for dy in [-r, r]; for dx in [-r, r]``) so
    // ``is_trace_blocked`` / ``is_foreign_pad_metal_within_radius`` match
    // the DRC's Euclidean clearance metric exactly.  At ``radius=2`` the
    // disc has 13 cells vs 25 for the square (-48%); at ``radius=3`` it
    // has 29 vs 49 (-41%); at ``radius=4`` it has 49 vs 81 (-40%).
    //
    // ``radius_override`` paths (per-net widths different from the
    // default ``trace_half_width_cells_``) compute a local offset list
    // on the fly -- those branches are not perf-critical.
    std::vector<std::pair<int8_t, int8_t>> circular_kernel_offsets_;

    // Issue #3234: Pre-computed circular (Euclidean) kernel offsets for
    // ``via_half_cells_``.  Sibling to ``circular_kernel_offsets_``;
    // closes the Chebyshev->Euclidean gap on the via-clearance side
    // (``is_via_blocked_diag``).  Each pair ``(dx, dy)`` satisfies
    // ``dx*dx + dy*dy <= via_half_cells_ * via_half_cells_``.  Replaces
    // the square Chebyshev scan that produced diagonal-corner
    // ``clearance_segment_via`` / ``clearance_pad_via`` violations whose
    // true Euclidean clearance fell up to
    // ``via_half_cells_ * (1 - 1/sqrt(2))`` cells short of the rule.
    //
    // ``radius_override > 0`` paths use an inline Euclidean filter over
    // the bounding square rather than a temporary vector allocation
    // (mirrors PR #3232's canonical pattern for ``is_trace_blocked``).
    std::vector<std::pair<int8_t, int8_t>> via_kernel_offsets_;

    // Routable layer indices
    std::vector<int> routable_layers_;

    // Statistics
    int last_iterations_ = 0;
    int last_nodes_explored_ = 0;

    // --- Resumable A* search state (promoted from route() locals) ---
    using PQ = std::priority_queue<AStarNode, std::vector<AStarNode>, std::greater<AStarNode>>;
    PQ search_open_set_;
    // Issue #3309: Per-net A* hot loop replaced the
    // ``std::unordered_set<tuple<int,int,int>, GridPosHash>`` /
    // ``std::unordered_map<tuple<int,int,int>, float, GridPosHash>`` storage
    // with flat ``std::vector`` arrays indexed by
    // ``layer * rows * cols + y * cols + x``.  Profiling on chorus's
    // multi-pad nets (DAC_CLK 217s, SPI_MOSI 270s) showed ~80% of wall-clock
    // was spent inside ``find()`` / ``count()`` on these tables:
    //   * Per 2D neighbor expansion (up to 8 / cell): one tuple
    //     allocation, one ``GridPosHash`` XOR-shift, one bucket walk,
    //     one tuple equality compare for the closed-set check, then
    //     another full lookup pair on g_score.
    //   * Per via-target layer (3+ for 4L): the same two-lookup pair.
    // Replacing with O(1) integer-index lookups into preallocated
    // contiguous vectors removes the hashing, tuple alloc, and bucket
    // walk entirely (~5-10x faster per cell visit, no per-expansion
    // heap allocations).
    //
    // Avoiding the O(N) ``clear()`` between routes:
    //   ``search_g_score_gen_`` holds a per-cell "generation stamp".
    //   ``search_current_gen_`` is bumped on every new search; a cell's
    //   g_score is treated as "uninserted" (== +infinity) when its gen
    //   stamp does not match the current generation.  Same trick for
    //   closed-set membership via ``search_closed_gen_``.  This makes
    //   per-net reset O(1) instead of O(rows * cols * layers).
    //
    // Determinism note (#3309 + #3144):
    //   We never iterated either table -- only looked up / inserted by
    //   key -- so the A* pop order, tie-break, and path selection are
    //   entirely a function of the priority queue and the ``operator>``
    //   defined on ``AStarNode``, both unchanged.  The byte-identical
    //   route invariant for boards 06/07 is preserved.
    std::vector<float> search_g_scores_flat_;
    std::vector<uint32_t> search_g_score_gen_;
    std::vector<uint32_t> search_closed_gen_;
    uint32_t search_current_gen_ = 0;
    // Cached grid dimensions matching the flat-array sizing.  When the
    // grid is resized between calls (rare), ``ensure_search_arrays_sized()``
    // grows the vectors and resets the generation counters.
    int search_flat_cols_ = 0;
    int search_flat_rows_ = 0;
    int search_flat_layers_ = 0;
    std::vector<AStarNode> search_closed_list_;

    // Flat-array helpers (Issue #3309).
    //
    // ``flat_index`` returns the linear index for ``(x, y, layer)``.  The
    // bounds invariant (``grid_.is_valid``) is checked by callers before
    // computing the index, so the helper is a pure arithmetic operation.
    inline size_t flat_index(int x, int y, int layer) const noexcept {
        return static_cast<size_t>(layer) *
                   static_cast<size_t>(search_flat_rows_) *
                   static_cast<size_t>(search_flat_cols_) +
               static_cast<size_t>(y) * static_cast<size_t>(search_flat_cols_) +
               static_cast<size_t>(x);
    }

    // Resize the flat arrays if the grid dimensions have changed, then
    // bump the generation counter so every cell's prior gen stamp is
    // invalidated in O(1).  Wraparound at ``UINT32_MAX`` triggers a full
    // ``std::fill`` reset (rare; ~4B searches between resets).
    void ensure_search_arrays_sized();

    // ``g_score_at`` returns the cell's current g_score or
    // ``+infinity`` if the stamp does not match the current generation.
    inline float g_score_at(size_t idx) const noexcept {
        return (search_g_score_gen_[idx] == search_current_gen_)
                   ? search_g_scores_flat_[idx]
                   : std::numeric_limits<float>::infinity();
    }

    // ``set_g_score`` writes the new value and stamps the cell with
    // the current generation.
    inline void set_g_score(size_t idx, float value) noexcept {
        search_g_scores_flat_[idx] = value;
        search_g_score_gen_[idx] = search_current_gen_;
    }

    // ``is_closed`` returns true iff the cell has been added to the
    // closed set in the current generation.
    inline bool is_closed(size_t idx) const noexcept {
        return search_closed_gen_[idx] == search_current_gen_;
    }

    // ``mark_closed`` stamps the cell as closed for the current
    // generation.
    inline void mark_closed(size_t idx) noexcept {
        search_closed_gen_[idx] = search_current_gen_;
    }

    // Set of rejected goal cells (skipped during goal test in resume())
    std::unordered_set<std::tuple<int, int, int>, GridPosHash> rejected_goals_;

    // Cached route parameters for resume()
    float search_start_x_ = 0, search_start_y_ = 0;
    float search_end_x_ = 0, search_end_y_ = 0;
    int search_end_gx_ = 0, search_end_gy_ = 0;
    int search_net_ = 0;
    int search_max_iterations_ = 0;
    std::vector<int> search_valid_start_layers_;
    std::vector<int> search_valid_end_layers_;
    bool search_negotiated_mode_ = false;
    float search_present_cost_factor_ = 0.0f;
    float search_weight_ = 1.0f;
    int search_trace_radius_cells_ = 0;
    int search_via_radius_cells_ = 0;
    PadBounds search_start_pad_bounds_{};
    PadBounds search_end_pad_bounds_{};
    bool search_state_active_ = false;  // True when resumable state is valid

    // Issue #2559 / Epic #2556 Phase 1C: diff-pair within-pair clearance.
    // Cached per-route-call so resume() and run_astar_loop() can apply
    // the partner-aware radius branch on every neighbor expansion.
    int search_partner_net_ = -1;
    int search_intra_pair_radius_cells_ = 0;

    // Issue #2476: Via-vs-via failure tracking for run_astar_loop().  Reset
    // by route_resumable() and accumulated across the search and any
    // subsequent resume() calls so that the failure reason surfaced to
    // Python reflects the blocker most recently observed before the open
    // set drained.
    int search_via_block_count_ = 0;
    int search_last_blocking_net_ = 0;
    float search_last_block_world_x_ = 0.0f;
    float search_last_block_world_y_ = 0.0f;

    // Issue #3130: Per-net emit widths/diameters cached so ``resume()`` ->
    // ``reconstruct_path()`` honors the same values across an entire
    // (initial + resume*) sequence.  0.0 preserves pre-#3130 behavior
    // (falls back to ``rules_.trace_width`` / ``rules_.via_diameter`` /
    // ``rules_.via_drill``).  Set in ``route_resumable()``; consumed by
    // ``reconstruct_path()`` when called from ``run_astar_loop()``.
    float search_emit_trace_width_ = 0.0f;
    float search_emit_via_diameter_ = 0.0f;
    float search_emit_via_drill_ = 0.0f;

    // Issue #2610: Per-net wall-clock deadline for the resumable search.
    // ``search_has_deadline_`` is true when the caller supplied a positive
    // ``per_net_timeout_seconds``; in that case ``search_deadline_`` is the
    // absolute ``steady_clock`` instant at which the A* loop must abort with
    // ``FAILURE_TIMEOUT``.  ``search_timed_out_`` is set when the loop
    // observes the deadline has fired so the run_astar_loop() epilogue can
    // distinguish TIMEOUT from ITERATION_LIMIT / NO_PATH.  The deadline is
    // computed once in route_resumable() and shared with resume() so a
    // single per-net budget covers the whole sequence of (initial, resume*)
    // attempts -- matching the Python pathfinder's behavior at line 1784.
    std::chrono::steady_clock::time_point search_deadline_{};
    bool search_has_deadline_ = false;
    bool search_timed_out_ = false;

    // Issue #3144: monotonic counter used as a secondary tie-break key for
    // ``AStarNode`` insertions into the resumable ``search_open_set_``.  Two
    // nodes with identical ``f_score`` would otherwise pop in
    // implementation-defined order, producing run-to-run differences in the
    // explored A* path under CI load.  The counter is reset at the top of
    // ``route_resumable()`` and incremented on every ``search_open_set_.push``
    // so older-pushed nodes pop first on f_score ties.  Shared with
    // ``resume()`` via member scope so the ordering is consistent across
    // multiple resume attempts on the same search.
    uint64_t search_seq_counter_ = 0;

    // Issue #3143: Per-cell pad-channel cost lookup populated once at the
    // top of ``route_resumable()`` and consulted by every A* neighbor
    // expansion via ``get_pad_channel_cost``.  Empty (the default) means
    // "no per-pad budget configured" and the cost helper returns 0 for
    // every cell -- preserving pre-#3143 behaviour identically.  The map
    // is held constant across ``resume()`` calls so the soft-budget cost
    // shaping is consistent across the (initial + resume*) sequence.
    //
    // Pre-computation rationale: scanning ``pad_channel_budgets`` per cell
    // expansion would be O(B) per cell where B = number of budgets; for
    // dense packages B can reach 10+ pads.  Pre-building the lookup once
    // amortises that scan over all expansions, and the membership check
    // becomes a single hash lookup.  Cells outside all budget bboxes are
    // never inserted, so the table stays small on typical boards (~few
    // hundred cells per active dense package).
    std::unordered_map<std::tuple<int, int, int>, float, GridPosHash>
        search_pad_budget_cost_lookup_;
};

}  // namespace router
