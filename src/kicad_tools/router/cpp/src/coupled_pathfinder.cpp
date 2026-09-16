/*
 * Router C++ Core - Coupled differential-pair A* pathfinder (Issue #4065)
 *
 * Implementation of the joint-state A* search declared in
 * coupled_pathfinder.hpp.  This is a faithful C++ port of the pure-Python
 * ``CoupledPathfinder.route_coupled`` / ``_get_coupled_neighbors`` /
 * ``_heuristic`` hot loop; line-number references below point at the Python
 * source (``src/kicad_tools/router/diffpair_routing.py``) each block mirrors.
 */

#include "coupled_pathfinder.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <queue>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace router {

namespace {

// Pack (x, y) into a single 64-bit key for the corridor / bucket maps.
inline uint64_t xy_key(int x, int y) {
    return (static_cast<uint64_t>(static_cast<uint32_t>(x)) << 32) |
           static_cast<uint32_t>(y);
}

// Pack (x, y, layer) into a 64-bit key for the visited-cell sets.  Layers
// are tiny (<16) so 8 bits is plenty; x/y fit in 28 bits each (grids are far
// below 2^28 cells on a side).
inline uint64_t xyl_key(int x, int y, int layer) {
    return (static_cast<uint64_t>(static_cast<uint32_t>(x)) << 36) |
           (static_cast<uint64_t>(static_cast<uint32_t>(y)) << 8) |
           static_cast<uint64_t>(static_cast<uint32_t>(layer) & 0xFF);
}

}  // namespace

CoupledPathfinder::CoupledPathfinder(Grid3D& grid,
                                     const DesignRules& rules,
                                     int target_spacing_cells,
                                     int min_spacing_cells,
                                     int trace_half_width_cells,
                                     int via_extra_cells,
                                     int via_drill_cells,
                                     double spacing_penalty_factor,
                                     double heuristic_weight,
                                     double min_via_pitch_cells,
                                     double p_via_trace_clearance_cells,
                                     double n_via_trace_clearance_cells)
    : grid_(grid),
      rules_(rules),
      target_spacing_cells_(target_spacing_cells),
      min_spacing_cells_(std::max(0, min_spacing_cells)),
      trace_half_width_cells_(trace_half_width_cells),
      via_extra_cells_(std::max(1, via_extra_cells)),
      via_drill_cells_(std::max(0, via_drill_cells)),
      spacing_penalty_factor_(std::clamp(spacing_penalty_factor, 0.0, 1.0)),
      heuristic_weight_(std::max(1.0, heuristic_weight)),
      min_via_pitch_cells_(std::max(0.0, min_via_pitch_cells)),
      p_via_trace_clearance_cells_(std::max(0.0, p_via_trace_clearance_cells)),
      n_via_trace_clearance_cells_(std::max(0.0, n_via_trace_clearance_cells)),
      cols_(grid.cols()),
      rows_(grid.rows()),
      num_layers_(grid.layers()) {}

// Mirror of Python ``_is_via_blocked`` (diffpair_routing.py:793-832).
bool CoupledPathfinder::is_via_blocked(int gx, int gy, int net, bool allow_own_pad) const {
    allow_own_pad = allow_own_pad && rules_.allow_smd_vias;
    for (int layer = 0; layer < num_layers_; ++layer) {
        for (int dy = -via_extra_cells_; dy <= via_extra_cells_; ++dy) {
            for (int dx = -via_extra_cells_; dx <= via_extra_cells_; ++dx) {
                if (is_cell_blocked(gx + dx, gy + dy, layer, net)) return true;
            }
        }
        // Endpoint exception applies only to pad copper belonging to this net.
        for (int dy = -via_drill_cells_; dy <= via_drill_cells_; ++dy) {
            for (int dx = -via_drill_cells_; dx <= via_drill_cells_; ++dx) {
                int cgx = gx + dx, cgy = gy + dy;
                if (cgx < 0 || cgx >= cols_ || cgy < 0 || cgy >= rows_) return true;
                const auto& cell = grid_.at(cgx, cgy, layer);
                if (cell.pad_blocked && !(allow_own_pad && cell.net == net)) return true;
            }
        }
    }
    return false;
}

// Mirror of Python ``_heuristic`` partner_aware branch
// (diffpair_routing.py:1393-1458).  Only ``partner_aware`` is ported; the
// Python wrapper routes ``manhattan_sum`` callers to the Python fallback.
double CoupledPathfinder::heuristic(int p_x, int p_y, int p_layer,
                                    int n_x, int n_y, int n_layer,
                                    int p_goal_x, int p_goal_y, int p_goal_layer,
                                    int n_goal_x, int n_goal_y, int n_goal_layer) const {
    int p_dist = std::abs(p_x - p_goal_x) + std::abs(p_y - p_goal_y);
    int n_dist = std::abs(n_x - n_goal_x) + std::abs(n_y - n_goal_y);
    double layer_cost = 0.0;
    if (p_layer != p_goal_layer) layer_cost += rules_.cost_via;
    if (n_layer != n_goal_layer) layer_cost += rules_.cost_via;

    int max_dist = std::max(p_dist, n_dist);
    double spacing_dx = static_cast<double>(p_x - n_x);
    double spacing_dy = static_cast<double>(p_y - n_y);
    double current_spacing = std::sqrt(spacing_dx * spacing_dx + spacing_dy * spacing_dy);
    double spacing_divergence = std::abs(current_spacing - target_spacing_cells_);
    double spacing_penalty =
        spacing_divergence * rules_.cost_straight * spacing_penalty_factor_;
    return max_dist * rules_.cost_straight + spacing_penalty + layer_cost;
}

CoupledRouteResult CoupledPathfinder::route(
    int p_start_x, int p_start_y,
    int n_start_x, int n_start_y,
    int start_layer,
    int p_goal_x, int p_goal_y,
    int n_goal_x, int n_goal_y,
    int end_layer,
    int p_net, int n_net,
    int effective_target_spacing,
    int effective_approach_radius,
    int effective_departure_radius,
    const std::vector<int>& routable_layers,
    const std::vector<uint8_t>& corridor_bitset,
    int max_iterations_budget,
    double timeout_seconds,
    const std::vector<std::array<int, 6>>& departure_prefix) {

    CoupledRouteResult result;

    const bool have_corridor = !corridor_bitset.empty();
    // Corridor-exempt endpoint (x,y) cells (diffpair_routing.py:1630-1637).
    std::unordered_set<uint64_t> corridor_exempt;
    corridor_exempt.reserve(4);
    corridor_exempt.insert(xy_key(p_start_x, p_start_y));
    corridor_exempt.insert(xy_key(p_goal_x, p_goal_y));
    corridor_exempt.insert(xy_key(n_start_x, n_start_y));
    corridor_exempt.insert(xy_key(n_goal_x, n_goal_y));

    auto in_corridor = [&](int x, int y) -> bool {
        if (!have_corridor) return true;
        if (x < 0 || x >= cols_ || y < 0 || y >= rows_) {
            return corridor_exempt.count(xy_key(x, y)) != 0;
        }
        if (corridor_bitset[static_cast<size_t>(y) * cols_ + x]) return true;
        return corridor_exempt.count(xy_key(x, y)) != 0;
    };

    // Node pool (contiguous) + index-based parent chain.
    std::vector<CoupledAStarNode> pool;
    pool.reserve(4096);

    // Priority queue over pool INDICES, ordered by a copy of the node's keys.
    // We store the node itself in the heap so the comparator is self-contained
    // (the pool may reallocate; storing indices + comparator-by-lookup would
    // be invalidated).  Heap entries are small PODs.
    std::priority_queue<CoupledAStarNode, std::vector<CoupledAStarNode>,
                        CoupledNodeGreater>
        open_set;

    // closed_set / g_scores keyed by the joint (p_pos, n_pos) IGNORING
    // direction, exactly as the Python loop keys ``(current.state.p_pos,
    // current.state.n_pos)`` (diffpair_routing.py:1746, 1873).  The key packs
    // both heads' (x,y,layer) into 128 bits via a pair of 64-bit keys.
    struct JointKey {
        uint64_t p, n;
        bool operator==(const JointKey& o) const { return p == o.p && n == o.n; }
    };
    struct JointKeyHash {
        size_t operator()(const JointKey& k) const {
            // 64-bit mix (splitmix64-ish) of the two halves.
            uint64_t h = k.p * 0x9E3779B97F4A7C15ULL ^ (k.n + 0x9E3779B97F4A7C15ULL +
                                                        (k.p << 6) + (k.p >> 2));
            return static_cast<size_t>(h);
        }
    };
    std::unordered_set<JointKey, JointKeyHash> closed_set;
    std::unordered_map<JointKey, float, JointKeyHash> g_scores;

    auto joint_key = [](int px, int py, int pl, int nx, int ny, int nl) -> JointKey {
        return JointKey{xyl_key(px, py, pl), xyl_key(nx, ny, nl)};
    };

    const int directions[4][2] = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};

    uint64_t seq_counter = 0;

    // Root node.
    double start_h = heuristic_weight_ *
                     heuristic(p_start_x, p_start_y, start_layer,
                               n_start_x, n_start_y, start_layer,
                               p_goal_x, p_goal_y, end_layer,
                               n_goal_x, n_goal_y, end_layer);
    CoupledAStarNode root;
    root.p_x = p_start_x; root.p_y = p_start_y; root.p_layer = start_layer;
    root.n_x = n_start_x; root.n_y = n_start_y; root.n_layer = start_layer;
    root.dir_dx = 0; root.dir_dy = 0;
    root.f_score = static_cast<float>(start_h);
    root.g_score = 0.0f;
    root.parent_idx = -1;
    root.via_from_parent = false;
    root.seq = seq_counter++;
    // The heap holds a copy; the pool records nodes as they are POPPED and
    // become the parent of expanded neighbors (mirrors the single-ended
    // pathfinder, whose closed set is the parent store).
    open_set.push(root);
    g_scores[joint_key(p_start_x, p_start_y, start_layer,
                       n_start_x, n_start_y, start_layer)] = 0.0f;

    const long max_iterations = static_cast<long>(cols_) * rows_ * 4;
    long iterations = 0;

    using clock = std::chrono::steady_clock;
    const bool have_deadline = timeout_seconds > 0.0;
    auto deadline = clock::now() +
                    std::chrono::duration_cast<clock::duration>(
                        std::chrono::duration<double>(timeout_seconds));
    const bool have_iter_budget = max_iterations_budget > 0;

    double best_progress = -1.0;  // -1 sentinel = "nothing popped yet".
    int best_node_idx = -1;
    // Preserve geometry for failed-search diagnosis without returning it as
    // a successful route. Pool indices remain valid across reallocations.
    auto partial_path = [&pool](int idx) {
        std::vector<CoupledPathNode> path;
        while (idx >= 0) {
            const CoupledAStarNode& nd = pool[static_cast<size_t>(idx)];
            CoupledPathNode pn;
            pn.p_x = nd.p_x; pn.p_y = nd.p_y; pn.p_layer = nd.p_layer;
            pn.n_x = nd.n_x; pn.n_y = nd.n_y; pn.n_layer = nd.n_layer;
            pn.via_from_parent = nd.via_from_parent;
            path.push_back(pn);
            idx = nd.parent_idx;
        }
        std::reverse(path.begin(), path.end());
        return path;
    };

    auto best_partial_path = [&partial_path, &best_node_idx]() {
        return partial_path(best_node_idx);
    };

    // Issue #4459: per-reason move-rejection histogram.  Counts which guard
    // pruned each candidate so a 0/9 budget-exit can be triaged (which
    // constraint dominates the frontier).  Keys mirror the pure-Python
    // ``last_rejections`` vocabulary (diffpair_routing.py) so the two backends
    // report the same taxonomy; the via-guard keys are the C++ path's superset
    // (the Python via move does not currently count rejections).
    std::unordered_map<std::string, int64_t> rejections;
    auto rej = [&rejections](const char* reason) { ++rejections[reason]; };

    // Endpoint (x,y,layer) cells stripped from the trail sets, mirroring the
    // Python discard at diffpair_routing.py:1829-1838.
    const uint64_t p_start_cell = xyl_key(p_start_x, p_start_y, start_layer);
    const uint64_t p_goal_cell = xyl_key(p_goal_x, p_goal_y, end_layer);
    const uint64_t n_start_cell = xyl_key(n_start_x, n_start_y, start_layer);
    const uint64_t n_goal_cell = xyl_key(n_goal_x, n_goal_y, end_layer);

    const int prox_r = min_spacing_cells_;
    const int prox_bucket = std::max(1, prox_r);
    const double prox_r_sq = static_cast<double>(prox_r) * prox_r;

    // Issue #4065: trail-history containers hoisted OUT of the loop and
    // ``.clear()``-reused every pop.  Rebuilding fresh ``unordered_set`` /
    // ``unordered_map`` objects per iteration (the naive port) dominated a
    // short converging run's wall-clock -- clearing retained buckets keeps
    // the allocated capacity across pops so only the growth beyond the
    // previous high-water mark allocates.  This is the per-pop-allocation
    // half of the curator's O(depth^2) caveat; the walk itself stays O(depth).
    std::unordered_set<uint64_t> p_visited, n_visited;
    std::unordered_map<uint64_t, std::vector<uint64_t>> p_prox, n_prox;

    while (!open_set.empty() && iterations < max_iterations) {
        ++iterations;

        // Iteration-budget classifier (diffpair_routing.py:1707-1721).
        if (have_iter_budget && iterations >= max_iterations_budget) {
            result.success = false;
            result.best_path = best_partial_path();
            result.iterations = static_cast<int>(iterations);
            result.best_progress = best_progress;
            result.timeout_exceeded = true;
            result.iteration_limited = true;  // #3921: iteration budget bound.
            result.rejections = std::move(rejections);  // #4459
            return result;
        }
        // Wall-clock check every 64 iters (diffpair_routing.py:1728-1743).
        if (have_deadline && (iterations & 63) == 0 && clock::now() >= deadline) {
            result.success = false;
            result.best_path = best_partial_path();
            result.iterations = static_cast<int>(iterations);
            result.best_progress = best_progress;
            result.timeout_exceeded = true;
            result.iteration_limited = false;  // wall-clock bound.
            result.rejections = std::move(rejections);  // #4459
            return result;
        }

        CoupledAStarNode current = open_set.top();
        open_set.pop();

        JointKey ckey = joint_key(current.p_x, current.p_y, current.p_layer,
                                  current.n_x, current.n_y, current.n_layer);
        if (closed_set.count(ckey)) continue;
        closed_set.insert(ckey);

        // Record this node in the pool so its children can reference it.
        int current_idx = static_cast<int>(pool.size());
        pool.push_back(current);

        // Issue #5333: deepest required departure step actually expanded.
        // Recorded on the POP (not the push) so it counts steps that survived
        // the closed-set/goal checks, and carried on every return path below
        // because it lives on ``result``.  Diagnostic only.
        result.departure_prefix_progress = std::max(
            result.departure_prefix_progress, static_cast<int>(current.prefix_step));

        if (!departure_prefix.empty() && result.validated_departure_path.empty() &&
            current.prefix_step == departure_prefix.size()) {
            result.validated_departure_path = partial_path(current_idx);
        }

        // Goal check (diffpair_routing.py:1762-1771).
        bool p_at_goal = (current.p_x == p_goal_x && current.p_y == p_goal_y &&
                          current.p_layer == end_layer);
        bool n_at_goal = (current.n_x == n_goal_x && current.n_y == n_goal_y &&
                          current.n_layer == end_layer);
        if (p_at_goal && n_at_goal && current.prefix_step == departure_prefix.size()) {
            // Reconstruct root->goal path from the pool parent chain.
            std::vector<CoupledPathNode> rev;
            int idx = current_idx;
            while (idx >= 0) {
                const CoupledAStarNode& nd = pool[static_cast<size_t>(idx)];
                CoupledPathNode pn;
                pn.p_x = nd.p_x; pn.p_y = nd.p_y; pn.p_layer = nd.p_layer;
                pn.n_x = nd.n_x; pn.n_y = nd.n_y; pn.n_layer = nd.n_layer;
                pn.via_from_parent = nd.via_from_parent;
                rev.push_back(pn);
                idx = nd.parent_idx;
            }
            std::reverse(rev.begin(), rev.end());
            result.path = std::move(rev);
            result.success = true;
            result.iterations = static_cast<int>(iterations);
            result.best_progress = best_progress;
            result.rejections = std::move(rejections);  // #4459
            return result;
        }

        // Progress diagnostics (diffpair_routing.py:1780-1789).
        int progress = std::max(
            std::abs(current.p_x - p_goal_x) + std::abs(current.p_y - p_goal_y),
            std::abs(current.n_x - n_goal_x) + std::abs(current.n_y - n_goal_y));
        if (best_progress < 0.0 || progress < best_progress) {
            best_progress = progress;
            best_node_idx = current_idx;
        }

        // ------------------------------------------------------------------
        // Build path-history sets + proximity buckets by walking the parent
        // chain (diffpair_routing.py:1791-1840).
        //
        // NOTE (Issue #4065, curator's O(depth^2) caveat): this walk is the
        // second hot function.  In v1 we walk the contiguous ``pool`` parent
        // chain by index -- a tight pointer-chase, 10-100x faster than the
        // Python object-attribute walk, but still O(depth) per pop.  The
        // fully-incremental restructure (append-only per-node trail carried
        // forward) is deferred; the C++ walk keeps behavioral parity with
        // Python while removing the interpreter overhead that dominated the
        // #4052 profile.  This is the honest v1 boundary called out on the
        // issue.
        // ------------------------------------------------------------------
        p_visited.clear();
        n_visited.clear();
        // ``.clear()`` on the bucket maps keeps the bucket vectors' capacity;
        // clear each retained vector too so membership from a prior pop's
        // longer chain does not leak into this one.
        for (auto& kv : p_prox) kv.second.clear();
        for (auto& kv : n_prox) kv.second.clear();
        std::vector<std::pair<int, int>> p_via_sites, n_via_sites;
        {
            int walk = current_idx;
            while (walk >= 0) {
                const CoupledAStarNode& nd = pool[static_cast<size_t>(walk)];
                uint64_t pc = xyl_key(nd.p_x, nd.p_y, nd.p_layer);
                uint64_t nc = xyl_key(nd.n_x, nd.n_y, nd.n_layer);
                p_visited.insert(pc);
                n_visited.insert(nc);
                if (nd.via_from_parent) {
                    p_via_sites.emplace_back(nd.p_x, nd.p_y);
                    n_via_sites.emplace_back(nd.n_x, nd.n_y);
                }
                if (prox_r > 1) {
                    p_prox[xy_key(nd.p_x / prox_bucket, nd.p_y / prox_bucket)].push_back(pc);
                    n_prox[xy_key(nd.n_x / prox_bucket, nd.n_y / prox_bucket)].push_back(nc);
                }
                walk = nd.parent_idx;
            }
        }
        // Strip endpoint pad cells (diffpair_routing.py:1829-1838).
        p_visited.erase(p_start_cell);
        p_visited.erase(p_goal_cell);
        n_visited.erase(n_start_cell);
        n_visited.erase(n_goal_cell);

        // Proximity guard, mirror of ``_too_close_to_trail``
        // (diffpair_routing.py:937-954).  ``buckets`` selects which trail's
        // spatial buckets to probe.
        auto too_close = [&](int cx, int cy, int clayer,
                             const std::unordered_map<uint64_t, std::vector<uint64_t>>& prox)
            -> bool {
            if (prox.empty() || prox_r <= 1) return false;
            int bx = cx / prox_bucket, by = cy / prox_bucket;
            for (int dbx = -1; dbx <= 1; ++dbx) {
                for (int dby = -1; dby <= 1; ++dby) {
                    auto it = prox.find(xy_key(bx + dbx, by + dby));
                    if (it == prox.end()) continue;
                    for (uint64_t packed : it->second) {
                        int tlayer = static_cast<int>(packed & 0xFF);
                        if (tlayer != clayer) continue;
                        int tx = static_cast<int>((packed >> 36) & 0xFFFFFFF);
                        int ty = static_cast<int>((packed >> 8) & 0xFFFFFFF);
                        // xyl_key stored x in bits 36+, y in bits 8..35.
                        double dx = static_cast<double>(tx - cx);
                        double dy = static_cast<double>(ty - cy);
                        if (dx * dx + dy * dy < prox_r_sq - 1e-9) return true;
                    }
                }
            }
            return false;
        };

        // Self-intersection guard, mirror of ``_self_intersects``
        // (diffpair_routing.py:956-1005).
        auto self_intersects = [&](int np_x, int np_y, int np_l,
                                   int nn_x, int nn_y, int nn_l,
                                   bool p_adv, bool n_adv,
                                   bool p_ep, bool n_ep) -> bool {
            // Regular vias span the board: a different trace layer or an
            // endpoint exemption cannot make crossing a partner barrel legal.
            if (p_adv) for (const auto& site : n_via_sites) {
                if (std::hypot(np_x - site.first, np_y - site.second) + 1e-9 <
                    p_via_trace_clearance_cells_) return true;
            }
            if (n_adv) for (const auto& site : p_via_sites) {
                if (std::hypot(nn_x - site.first, nn_y - site.second) + 1e-9 <
                    n_via_trace_clearance_cells_) return true;
            }
            if (p_visited.empty() && n_visited.empty()) return false;
            uint64_t pk = xyl_key(np_x, np_y, np_l);
            uint64_t nk = xyl_key(nn_x, nn_y, nn_l);
            if (p_adv && !p_ep && n_visited.count(pk)) return true;
            if (n_adv && !n_ep && p_visited.count(nk)) return true;
            if (p_adv && !p_ep && too_close(np_x, np_y, np_l, n_prox)) return true;
            if (n_adv && !n_ep && too_close(nn_x, nn_y, nn_l, p_prox)) return true;
            if (p_adv && !p_ep && p_visited.count(pk)) return true;
            if (n_adv && !n_ep && n_visited.count(nk)) return true;
            return false;
        };

        // ------------------------------------------------------------------
        // Neighbor generation (diffpair_routing.py:840-1391).
        // ------------------------------------------------------------------
        int target_spacing = effective_target_spacing;

        // Approach / departure relaxation (diffpair_routing.py:1009-1069).
        bool approach_relaxed = false;
        {
            int p_dist_to_goal = std::abs(current.p_x - p_goal_x) + std::abs(current.p_y - p_goal_y);
            int n_dist_to_goal = std::abs(current.n_x - n_goal_x) + std::abs(current.n_y - n_goal_y);
            int approach_radius = effective_approach_radius;
            if (p_dist_to_goal <= approach_radius && n_dist_to_goal <= approach_radius) {
                approach_relaxed = true;
            }
        }
        bool departure_relaxed = false;
        {
            int p_dist_from_start =
                std::abs(current.p_x - p_start_x) + std::abs(current.p_y - p_start_y);
            int n_dist_from_start =
                std::abs(current.n_x - n_start_x) + std::abs(current.n_y - n_start_y);
            int departure_radius = effective_departure_radius;
            if (p_dist_from_start <= departure_radius && n_dist_from_start <= departure_radius) {
                departure_relaxed = true;
            }
        }
        bool spacing_relaxed = approach_relaxed || departure_relaxed;
        int relaxed_tolerance = std::max(1, target_spacing);
        if (approach_relaxed) relaxed_tolerance = std::max(relaxed_tolerance, effective_approach_radius);
        if (departure_relaxed) relaxed_tolerance = std::max(relaxed_tolerance, effective_departure_radius);

        // Collected neighbors: (px,py,pl, nx,ny,nl, dir_dx,dir_dy, cost, is_via).
        struct Cand {
            int px, py, pl, nx, ny, nl, ddx, ddy;
            double cost;
            bool is_via;
        };
        std::vector<Cand> neighbors;
        neighbors.reserve(16);

        // Symmetric moves (diffpair_routing.py:1071-1153).
        for (auto& d : directions) {
            int dx = d[0], dy = d[1];
            int np_x = current.p_x + dx, np_y = current.p_y + dy, np_l = current.p_layer;
            int nn_x = current.n_x + dx, nn_y = current.n_y + dy, nn_l = current.n_layer;

            bool p_ep = at_goal(np_x, np_y, p_goal_x, p_goal_y) ||
                        at_goal(np_x, np_y, p_start_x, p_start_y);
            bool n_ep = at_goal(nn_x, nn_y, n_goal_x, n_goal_y) ||
                        at_goal(nn_x, nn_y, n_start_x, n_start_y);
            if (!p_ep && is_trace_blocked(np_x, np_y, np_l, p_net)) { rej("sym_blocked_p"); continue; }
            if (!n_ep && is_trace_blocked(nn_x, nn_y, nn_l, n_net)) { rej("sym_blocked_n"); continue; }

            double sdx = np_x - nn_x, sdy = np_y - nn_y;
            double new_spacing = std::sqrt(sdx * sdx + sdy * sdy);
            int tolerance = spacing_relaxed ? relaxed_tolerance : 1;
            if (std::abs(new_spacing - target_spacing) > tolerance) { rej("sym_spacing"); continue; }

            if (min_spacing_cells_ > 0 && !(p_ep && n_ep)) {
                if (new_spacing + 1e-9 < min_spacing_cells_) { rej("sym_floor"); continue; }
            }
            if (self_intersects(np_x, np_y, np_l, nn_x, nn_y, nn_l,
                                true, true, p_ep, n_ep)) { rej("sym_trail"); continue; }

            double cost = rules_.cost_straight;
            bool dir_changed = !(current.dir_dx == 0 && current.dir_dy == 0) &&
                               !(current.dir_dx == dx && current.dir_dy == dy);
            if (dir_changed) cost += rules_.cost_turn;
            // Issue #4080: corridor attractor for the coupled joint-state
            // loop.  A symmetric move advances BOTH nets, so query each
            // landing cell against its own net (mirrors the single-ended
            // attractor in ``pathfinder.cpp``).  Clamped at 0 so the joint
            // step cost stays non-negative and A* admissibility holds.
            double attractor_bonus =
                grid_.corridor_attractor_bonus(np_x, np_y, np_l, p_net,
                                               rules_.cost_corridor_attractor) +
                grid_.corridor_attractor_bonus(nn_x, nn_y, nn_l, n_net,
                                               rules_.cost_corridor_attractor);
            if (attractor_bonus > 0.0) cost = std::max(0.0, cost - attractor_bonus);
            neighbors.push_back({np_x, np_y, np_l, nn_x, nn_y, nn_l, dx, dy, cost, false});
        }

        // Asymmetric converge moves (diffpair_routing.py:1186-1305).
        {
            int asym_tolerance = spacing_relaxed ? relaxed_tolerance : 1;
            for (auto& d : directions) {
                int dx = d[0], dy = d[1];

                // P advances, N holds.
                {
                    int cp_x = current.p_x + dx, cp_y = current.p_y + dy, cp_l = current.p_layer;
                    int cn_x = current.n_x, cn_y = current.n_y, cn_l = current.n_layer;
                    bool p_ep = at_goal(cp_x, cp_y, p_goal_x, p_goal_y) ||
                                at_goal(cp_x, cp_y, p_start_x, p_start_y);
                    bool blocked = !(p_ep || !is_trace_blocked(cp_x, cp_y, cp_l, p_net));
                    if (blocked) rej("asym_blocked_p");  // #4459
                    if (!blocked) {
                        double sdx = cp_x - cn_x, sdy = cp_y - cn_y;
                        double new_spacing = std::sqrt(sdx * sdx + sdy * sdy);
                        if (std::abs(new_spacing - target_spacing) > asym_tolerance) {
                            rej("asym_spacing_p");  // #4459
                        } else {
                            bool n_ep = at_goal(cn_x, cn_y, n_goal_x, n_goal_y) ||
                                        at_goal(cn_x, cn_y, n_start_x, n_start_y);
                            bool bypass_floor = p_ep && n_ep;
                            bool floor_ok =
                                !(min_spacing_cells_ > 0 && !bypass_floor &&
                                  new_spacing + 1e-9 < min_spacing_cells_);
                            if (!floor_ok) {
                                rej("asym_floor_p");  // #4459
                            } else if (self_intersects(cp_x, cp_y, cp_l, cn_x, cn_y, cn_l,
                                                       true, false, p_ep, n_ep)) {
                                rej("asym_trail_p");  // #4459
                            } else {
                                double cost = rules_.cost_straight;
                                bool dir_changed =
                                    !(current.dir_dx == 0 && current.dir_dy == 0) &&
                                    !(current.dir_dx == dx && current.dir_dy == dy);
                                if (dir_changed) cost += rules_.cost_turn;
                                // Issue #4080: corridor attractor (per-net)
                                // for the asymmetric P-advance move.  See the
                                // symmetric-move comment above.
                                double attractor_bonus =
                                    grid_.corridor_attractor_bonus(
                                        cp_x, cp_y, cp_l, p_net,
                                        rules_.cost_corridor_attractor) +
                                    grid_.corridor_attractor_bonus(
                                        cn_x, cn_y, cn_l, n_net,
                                        rules_.cost_corridor_attractor);
                                if (attractor_bonus > 0.0)
                                    cost = std::max(0.0, cost - attractor_bonus);
                                neighbors.push_back(
                                    {cp_x, cp_y, cp_l, cn_x, cn_y, cn_l, dx, dy, cost, false});
                            }
                        }
                    }
                }

                // N advances, P holds.
                {
                    int cp_x = current.p_x, cp_y = current.p_y, cp_l = current.p_layer;
                    int cn_x = current.n_x + dx, cn_y = current.n_y + dy, cn_l = current.n_layer;
                    bool n_ep = at_goal(cn_x, cn_y, n_goal_x, n_goal_y) ||
                                at_goal(cn_x, cn_y, n_start_x, n_start_y);
                    if (!(n_ep || !is_trace_blocked(cn_x, cn_y, cn_l, n_net))) {
                        rej("asym_blocked_n");  // #4459
                    } else {
                        double sdx = cp_x - cn_x, sdy = cp_y - cn_y;
                        double new_spacing = std::sqrt(sdx * sdx + sdy * sdy);
                        if (std::abs(new_spacing - target_spacing) > asym_tolerance) {
                            rej("asym_spacing_n");  // #4459
                        } else {
                            bool p_ep = at_goal(cp_x, cp_y, p_goal_x, p_goal_y) ||
                                        at_goal(cp_x, cp_y, p_start_x, p_start_y);
                            bool bypass_floor = p_ep && n_ep;
                            bool floor_ok =
                                !(min_spacing_cells_ > 0 && !bypass_floor &&
                                  new_spacing + 1e-9 < min_spacing_cells_);
                            if (!floor_ok) {
                                rej("asym_floor_n");  // #4459
                            } else if (self_intersects(cp_x, cp_y, cp_l, cn_x, cn_y, cn_l,
                                                       false, true, p_ep, n_ep)) {
                                rej("asym_trail_n");  // #4459
                            } else {
                                double cost = rules_.cost_straight;
                                bool dir_changed =
                                    !(current.dir_dx == 0 && current.dir_dy == 0) &&
                                    !(current.dir_dx == dx && current.dir_dy == dy);
                                if (dir_changed) cost += rules_.cost_turn;
                                // Issue #4080: corridor attractor (per-net)
                                // for the asymmetric N-advance move.  See the
                                // symmetric-move comment above.
                                double attractor_bonus =
                                    grid_.corridor_attractor_bonus(
                                        cp_x, cp_y, cp_l, p_net,
                                        rules_.cost_corridor_attractor) +
                                    grid_.corridor_attractor_bonus(
                                        cn_x, cn_y, cn_l, n_net,
                                        rules_.cost_corridor_attractor);
                                if (attractor_bonus > 0.0)
                                    cost = std::max(0.0, cost - attractor_bonus);
                                neighbors.push_back(
                                    {cp_x, cp_y, cp_l, cn_x, cn_y, cn_l, dx, dy, cost, false});
                            }
                        }
                    }
                }
            }
        }

        // Layer-change (via) moves (diffpair_routing.py:1307-1350).
        {
            bool p_at_ep = at_goal(current.p_x, current.p_y, p_goal_x, p_goal_y) ||
                           at_goal(current.p_x, current.p_y, p_start_x, p_start_y);
            bool n_at_ep = at_goal(current.n_x, current.n_y, n_goal_x, n_goal_y) ||
                           at_goal(current.n_x, current.n_y, n_start_x, n_start_y);
            double via_dx = current.p_x - current.n_x;
            double via_dy = current.p_y - current.n_y;
            bool pair_vias_clear = std::hypot(via_dx, via_dy) + 1e-9 >= min_via_pitch_cells_;
            // The candidate pair has not been published to grid_. Check
            // the complete parent chain, including endpoints and all layers.
            const char* history_rejection = nullptr;
            if (pair_vias_clear) {
                for (const auto& site : n_via_sites) {
                    if (std::hypot(current.p_x - site.first, current.p_y - site.second) + 1e-9 <
                        min_via_pitch_cells_) history_rejection = "via_partner_barrel";
                }
                for (const auto& site : p_via_sites) {
                    if (std::hypot(current.n_x - site.first, current.n_y - site.second) + 1e-9 <
                        min_via_pitch_cells_) history_rejection = "via_partner_barrel";
                }
                int walk = current_idx;
                while (!history_rejection && walk >= 0) {
                    const CoupledAStarNode& nd = pool[static_cast<size_t>(walk)];
                    double p_to_n = std::hypot(current.p_x - nd.n_x, current.p_y - nd.n_y);
                    double n_to_p = std::hypot(current.n_x - nd.p_x, current.n_y - nd.p_y);
                    if (p_to_n + 1e-9 < n_via_trace_clearance_cells_ ||
                        n_to_p + 1e-9 < p_via_trace_clearance_cells_) {
                        history_rejection = "via_partner_trail";
                        break;
                    }
                    walk = nd.parent_idx;
                }
            }
            for (int new_layer : routable_layers) {
                if (new_layer == current.p_layer) continue;
                if (history_rejection) { rej(history_rejection); continue; }
                // Candidate pair copper is not in grid_: enforce the mutual
                // copper and drill pitch even at endpoint cells.
                if (!pair_vias_clear) { rej("via_pair_pitch"); continue; }
                if (is_via_blocked(current.p_x, current.p_y, p_net, p_at_ep)) { rej("via_blocked_p"); continue; }
                if (is_via_blocked(current.n_x, current.n_y, n_net, n_at_ep)) { rej("via_blocked_n"); continue; }
                if (is_trace_blocked(current.p_x, current.p_y, new_layer, p_net)) { rej("via_trace_blocked_p"); continue; }
                if (is_trace_blocked(current.n_x, current.n_y, new_layer, n_net)) { rej("via_trace_blocked_n"); continue; }
                double cost = rules_.cost_via * 2.0;
                // Issue #4080: corridor attractor on the via-drop
                // destination cells -- the reservation is what makes the
                // coupled router prefer to actually via-hop INTO the
                // reserved channel (mirrors the single-ended via-drop
                // attractor in ``pathfinder.cpp``).  Per-net query,
                // clamped at 0.
                double attractor_bonus =
                    grid_.corridor_attractor_bonus(current.p_x, current.p_y, new_layer,
                                                   p_net, rules_.cost_corridor_attractor) +
                    grid_.corridor_attractor_bonus(current.n_x, current.n_y, new_layer,
                                                   n_net, rules_.cost_corridor_attractor);
                if (attractor_bonus > 0.0) cost = std::max(0.0, cost - attractor_bonus);
                neighbors.push_back({current.p_x, current.p_y, new_layer,
                                     current.n_x, current.n_y, new_layer,
                                     current.dir_dx, current.dir_dy, cost, true});
            }
        }

        // Expand neighbors into the open set (diffpair_routing.py:1842-1890).
        for (const Cand& c : neighbors) {
            if (grid_.has_fixed_fills()) {
                auto rail_clear = [&](int ax, int ay, int bx, int by, int layer, double rail_half, double rail_gap) {
                    auto [wx, wy] = grid_.grid_to_world(ax, ay);
                    auto [vx, vy] = grid_.grid_to_world(bx, by);
                    double half = c.is_via ? rules_.via_diameter / 2.0 : (rail_half >= 0 ? rail_half : rules_.trace_width / 2.0);
                    double gap = c.is_via ? rules_.via_clearance : (rail_gap >= 0 ? rail_gap : rules_.trace_clearance);
                    if (c.is_via) {
                        for (int l = 0; l < num_layers_; ++l)
                            if (!grid_.fixed_fill_clear(wx, wy, vx, vy, l, half, half+gap, true)) return false;
                        return true;
                    }
                    return grid_.fixed_fill_clear(wx, wy, vx, vy, layer, half, half+gap);
                };
                if (!rail_clear(current.p_x, current.p_y, c.px, c.py, c.pl, p_fill_half_, p_fill_gap_) ||
                    !rail_clear(current.n_x, current.n_y, c.nx, c.ny, c.nl, n_fill_half_, n_fill_gap_)) continue;
            }
            // Constrain initial expansion, never inject a pre-built path.
            // Every requested step has passed the ordinary physical/history
            // guards above and consumes the same search budget as any move.
            if (current.prefix_step < departure_prefix.size()) {
                const auto& next = departure_prefix[current.prefix_step];
                if (c.px != next[0] || c.py != next[1] || c.pl != next[2] ||
                    c.nx != next[3] || c.ny != next[4] || c.nl != next[5]) {
                    rej("departure_prefix");
                    continue;
                }
            }
            // Corridor pruning (diffpair_routing.py:1864-1871).
            if (have_corridor) {
                if (!in_corridor(c.px, c.py) || !in_corridor(c.nx, c.ny)) {
                    rej("corridor");  // #4459
                    continue;
                }
            }
            JointKey nkey = joint_key(c.px, c.py, c.pl, c.nx, c.ny, c.nl);
            if (closed_set.count(nkey)) continue;

            float new_g = current.g_score + static_cast<float>(c.cost);
            auto git = g_scores.find(nkey);
            if (git == g_scores.end() || new_g < git->second) {
                g_scores[nkey] = new_g;
                double h = heuristic(c.px, c.py, c.pl, c.nx, c.ny, c.nl,
                                     p_goal_x, p_goal_y, end_layer,
                                     n_goal_x, n_goal_y, end_layer);
                float f = new_g + static_cast<float>(heuristic_weight_ * h);
                CoupledAStarNode node;
                node.p_x = c.px; node.p_y = c.py; node.p_layer = c.pl;
                node.n_x = c.nx; node.n_y = c.ny; node.n_layer = c.nl;
                node.dir_dx = c.ddx; node.dir_dy = c.ddy;
                node.f_score = f;
                node.g_score = new_g;
                node.parent_idx = current_idx;
                node.via_from_parent = c.is_via;
                node.prefix_step = std::min(current.prefix_step + 1, departure_prefix.size());
                node.seq = seq_counter++;
                open_set.push(node);
            }
        }
    }

    // No path found (open set exhausted or memory backstop hit).
    result.success = false;
    result.best_path = best_partial_path();
    result.iterations = static_cast<int>(iterations);
    result.best_progress = best_progress;
    result.rejections = std::move(rejections);  // #4459
    return result;
}

}  // namespace router
