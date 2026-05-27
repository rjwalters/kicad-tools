/*
 * Router C++ Core - A* Pathfinder Implementation
 * Part of kicad-tools router performance optimization (Phase 4)
 */

#include "pathfinder.hpp"
#include <queue>
#include <unordered_set>
#include <cmath>
#include <algorithm>
#include <chrono>

namespace router {

Pathfinder::Pathfinder(Grid3D& grid, const DesignRules& rules, bool diagonal_routing)
    : grid_(grid), rules_(rules), diagonal_routing_(diagonal_routing) {

    // Pre-compute neighbor offsets for 2D moves
    neighbors_2d_ = {
        {1, 0, 0, 1.0f},   // Right
        {-1, 0, 0, 1.0f},  // Left
        {0, 1, 0, 1.0f},   // Down
        {0, -1, 0, 1.0f},  // Up
    };

    if (diagonal_routing) {
        // Add 45-degree diagonal moves (cost = sqrt(2) ~= 1.414)
        neighbors_2d_.push_back({1, 1, 0, 1.414f});   // Down-Right
        neighbors_2d_.push_back({-1, 1, 0, 1.414f});  // Down-Left
        neighbors_2d_.push_back({1, -1, 0, 1.414f});  // Up-Right
        neighbors_2d_.push_back({-1, -1, 0, 1.414f}); // Up-Left
    }

    // Pre-compute trace clearance radius in grid cells
    // This is the total radius from trace centerline that must be clear:
    // - trace_width/2: half-width of the trace copper
    // - trace_clearance: required clearance from trace edge to obstacles
    // This enforces clearance as a hard constraint during routing.
    // Issue #553: Previously only checked trace_width/2, causing DRC violations
    // when traces were placed too close to obstacles.
    trace_half_width_cells_ = std::max(
        1, static_cast<int>(std::ceil(
            (rules.trace_width / 2 + rules.trace_clearance) / grid.resolution())));

    // Pre-compute via blocking radius in grid cells
    via_half_cells_ = std::max(
        1, static_cast<int>(std::ceil(
            (rules.via_diameter / 2 + rules.via_clearance) / grid.resolution())));

    // Default: all layers are routable
    for (int i = 0; i < grid.layers(); ++i) {
        routable_layers_.push_back(i);
    }
}

void Pathfinder::set_routable_layers(const std::vector<int>& layers) {
    routable_layers_ = layers;
}

bool Pathfinder::is_trace_blocked(int x, int y, int layer, int net,
                                  bool allow_sharing, int radius_override,
                                  int partner_net, int partner_radius) const {
    int radius = (radius_override > 0) ? radius_override : trace_half_width_cells_;

    // Issue #2559 / Epic #2556 Phase 1C: when the partner branch is active
    // (partner_net >= 0 && partner_radius > 0 && partner_radius < radius),
    // partner-owned blocked cells in the slack ring (Chebyshev distance
    // > partner_radius) are treated as passable.  All other cells use the
    // wider radius as before.
    bool partner_active =
        (partner_net >= 0) && (partner_radius > 0) && (partner_radius < radius);

    for (int dy = -radius; dy <= radius; ++dy) {
        for (int dx = -radius; dx <= radius; ++dx) {
            int cx = x + dx, cy = y + dy;
            if (!grid_.is_valid(cx, cy, layer)) {
                return true;  // Out of bounds
            }

            const auto& cell = grid_.at(cx, cy, layer);
            if (cell.blocked) {
                // Issue #2559: relax partner cells outside the tighter
                // intra-pair radius (Chebyshev metric matches the kernel
                // shape used by Python compute_expanded_blocked()).
                if (partner_active && cell.net == partner_net) {
                    int cheb = std::max(std::abs(dx), std::abs(dy));
                    if (cheb > partner_radius) {
                        // Partner cell in the slack ring -- skip.
                        continue;
                    }
                }

                if (allow_sharing) {
                    // Negotiated mode: mirror Python
                    // pathfinder.py::_is_trace_blocked rect-mask
                    // (lines 1271-1296).  Issue #2989: own-net
                    // ``is_obstacle`` cells (destination/partner pad
                    // metal painted by PR #2928 + PR #2942) MUST
                    // remain passable for the routing net.  Without
                    // this gate, diff-pair partner B's pad rejects
                    // unconditionally and the trace cannot reach the
                    // partner endpoint (USB3/PCIE/MIPI partial
                    // completion on board 06; USB_D-/USB_CC2/JOY_Y
                    // on board 03).  Foreign-net obstacles still
                    // hard-reject.
                    if (cell.is_obstacle && cell.net != net) {
                        return true;  // Foreign-net obstacle blocks
                    }
                    if (cell.net == 0 && cell.usage_count == 0) {
                        return true;  // Static no-net obstacle
                    }
                    if (cell.net != net && cell.usage_count == 0) {
                        return true;  // Static obstacle from another net
                    }
                    // Allow with cost penalty for routed cells or
                    // own-net obstacle cells.
                } else {
                    // Standard mode: block if obstacle or different net
                    if (cell.is_obstacle || cell.net != net) {
                        return true;
                    }
                }
            }
        }
    }
    return false;
}

bool Pathfinder::is_diagonal_blocked(int x, int y, int dx, int dy, int layer,
                                     int net, bool allow_sharing) const {
    // Only check for diagonal moves
    if (dx == 0 || dy == 0) return false;

    // Check the two adjacent orthogonal cells (prevent corner cutting)
    std::vector<std::pair<int, int>> adjacent = {
        {x, y + dy},  // Vertical neighbor
        {x + dx, y},  // Horizontal neighbor
    };

    for (const auto& [cx, cy] : adjacent) {
        if (!grid_.is_valid(cx, cy, layer)) {
            return true;  // Out of bounds
        }

        const auto& cell = grid_.at(cx, cy, layer);
        if (cell.blocked) {
            if (allow_sharing) {
                // Negotiated mode: own-net obstacle cells remain
                // passable (Issue #2989 sibling fix; see
                // ``is_trace_blocked`` for the diff-pair rationale).
                if (cell.is_obstacle && cell.net != net) {
                    return true;
                }
                if (cell.net == 0 && cell.usage_count == 0) {
                    return true;
                }
                if (cell.net != net && cell.usage_count == 0) {
                    return true;
                }
            } else {
                if (cell.is_obstacle || cell.net != net) {
                    return true;
                }
            }
        }
    }
    return false;
}

bool Pathfinder::is_via_blocked(int x, int y, int net, bool allow_sharing,
                                int radius_override) const {
    int dummy_net = 0;
    float dummy_x = 0.0f, dummy_y = 0.0f;
    return is_via_blocked_diag(x, y, net, allow_sharing, radius_override,
                               dummy_net, dummy_x, dummy_y);
}

bool Pathfinder::is_via_blocked_diag(int x, int y, int net, bool allow_sharing,
                                     int radius_override,
                                     int& out_blocking_net,
                                     float& out_world_x,
                                     float& out_world_y) const {
    out_blocking_net = 0;
    out_world_x = 0.0f;
    out_world_y = 0.0f;

    int radius = (radius_override > 0) ? radius_override : via_half_cells_;
    for (int layer = 0; layer < grid_.layers(); ++layer) {
        for (int dy = -radius; dy <= radius; ++dy) {
            for (int dx = -radius; dx <= radius; ++dx) {
                int cx = x + dx, cy = y + dy;
                if (!grid_.is_valid(cx, cy, layer)) {
                    // Grid-cell rejection: no specific blocking net.
                    return true;
                }

                const auto& cell = grid_.at(cx, cy, layer);
                if (cell.blocked) {
                    if (allow_sharing) {
                        // Negotiated mode: mirror Python
                        // pathfinder.py::_is_via_blocked SoA branch
                        // (lines 1428-1453).  Issue #2989: own-net
                        // ``is_obstacle`` cells (destination /
                        // diff-pair-partner pad metal painted by
                        // PR #2928 first-touch + PR #2942 rect-aware
                        // halo) MUST remain passable so the routing
                        // net's own via can land inside its
                        // destination pad's footprint.  Without this
                        // gate, partner B's pad rejects every via
                        // candidate -> A* cannot reach partner ->
                        // diff-pair lands 1-of-2 endpoints.  Matches
                        // USB3/PCIE/MIPI failure on board 06 and
                        // USB_D-/USB_CC2/JOY_Y on board 03.
                        // Foreign-net obstacles still hard-reject.
                        if (cell.is_obstacle && cell.net != net) {
                            return true;  // Foreign-net obstacle blocks
                        }
                        if (cell.net == 0 && cell.usage_count == 0) {
                            return true;
                        }
                        if (cell.net != net && cell.usage_count == 0) {
                            return true;
                        }
                        // Allow with cost for routed cells / own-net
                        // obstacle.
                    } else {
                        if (cell.is_obstacle || cell.net != net) {
                            return true;
                        }
                    }
                }
            }
        }
    }

    // Issue #2466: Geometric via-vs-via clearance against ``stored_vias_``.
    // Issue #2476: When this branch causes a rejection, record the offending
    // stored-via net so the negotiated strategy can target the rip-up at the
    // specific net whose via is blocking us, rather than blanket retry.
    auto candidate_world = grid_.grid_to_world(x, y);
    float candidate_x = candidate_world.first;
    float candidate_y = candidate_world.second;
    float candidate_radius = rules_.via_diameter / 2.0f;
    float clearance_required = rules_.via_clearance;

    for (const auto& sv : grid_.stored_vias()) {
        if (sv.net == net) continue;  // same-net spacing handled elsewhere
        float dxw = candidate_x - sv.x;
        float dyw = candidate_y - sv.y;
        float distance = std::sqrt(dxw * dxw + dyw * dyw);
        float clearance = distance - candidate_radius - sv.diameter / 2.0f;
        if (clearance < clearance_required) {
            out_blocking_net = sv.net;
            out_world_x = candidate_x;
            out_world_y = candidate_y;
            return true;
        }
    }

    return false;
}

float Pathfinder::heuristic(int x, int y, int layer,
                            int goal_x, int goal_y, int goal_layer) const {
    float dx = static_cast<float>(std::abs(x - goal_x));
    float dy = static_cast<float>(std::abs(y - goal_y));

    // Octile distance for diagonal routing
    float h;
    if (diagonal_routing_) {
        h = std::max(dx, dy) + (1.414f - 1.0f) * std::min(dx, dy);
    } else {
        h = dx + dy;  // Manhattan distance
    }

    // Add layer change cost estimate
    if (layer != goal_layer) {
        h += rules_.cost_via;
    }

    return h * rules_.cost_straight;
}

float Pathfinder::get_congestion_cost(int x, int y, int layer) const {
    float congestion = grid_.get_congestion(x, y, layer);
    if (congestion > rules_.congestion_threshold) {
        float excess = congestion - rules_.congestion_threshold;
        return rules_.cost_congestion * (1.0f + excess * 2.0f);
    }
    return 0.0f;
}

// Helper: Initialize pad bounds from arguments, applying default single-cell
// fallback when no explicit bounds are provided (Issue #2427).
static void init_pad_bounds(
    PadBounds& sp, PadBounds& ep,
    int start_gx, int start_gy, int end_gx, int end_gy,
    const PadBounds& start_pad_bounds, const PadBounds& end_pad_bounds
) {
    sp = start_pad_bounds;
    ep = end_pad_bounds;
    bool has_start_bounds = (sp.metal_gx1 != sp.metal_gx2 || sp.metal_gy1 != sp.metal_gy2
                             || (sp.metal_gx1 == start_gx && sp.metal_gy1 == start_gy));
    bool has_end_bounds = (ep.metal_gx1 != ep.metal_gx2 || ep.metal_gy1 != ep.metal_gy2
                           || (ep.metal_gx1 == end_gx && ep.metal_gy1 == end_gy));
    if (!has_start_bounds && sp.metal_gx1 == 0 && sp.metal_gy1 == 0
        && sp.metal_gx2 == 0 && sp.metal_gy2 == 0) {
        sp.metal_gx1 = sp.metal_gx2 = start_gx;
        sp.metal_gy1 = sp.metal_gy2 = start_gy;
        sp.approach_gx1 = start_gx - 2;
        sp.approach_gy1 = start_gy - 2;
        sp.approach_gx2 = start_gx + 2;
        sp.approach_gy2 = start_gy + 2;
    }
    if (!has_end_bounds && ep.metal_gx1 == 0 && ep.metal_gy1 == 0
        && ep.metal_gx2 == 0 && ep.metal_gy2 == 0) {
        ep.metal_gx1 = ep.metal_gx2 = end_gx;
        ep.metal_gy1 = ep.metal_gy2 = end_gy;
        ep.approach_gx1 = end_gx - 2;
        ep.approach_gy1 = end_gy - 2;
        ep.approach_gx2 = end_gx + 2;
        ep.approach_gy2 = end_gy + 2;
    }
}

RouteResult Pathfinder::route(
    float start_x, float start_y, int start_layer,
    float end_x, float end_y, int end_layer,
    int net,
    const std::vector<int>& start_layers,
    const std::vector<int>& end_layers,
    bool negotiated_mode,
    float present_cost_factor,
    float weight,
    int trace_radius_cells,
    int via_radius_cells,
    const PadBounds& start_pad_bounds,
    const PadBounds& end_pad_bounds,
    int partner_net,
    int intra_pair_radius_cells,
    double per_net_timeout_seconds,
    int max_search_iterations,
    float emit_trace_width,
    float emit_via_diameter,
    float emit_via_drill
) {
    // Non-resumable route: use local A* state, no member state touched.
    // This preserves backward compatibility for callers that don't need retry.
    //
    // Issue #2610: silence -Wunused-parameter for the partner_net/intra_pair
    // and timeout arguments when the inline loop path is not yet wired to
    // honor them past this entry point.  The one-shot route() is mostly
    // exercised by unit tests today; production traffic goes through
    // route_resumable() which has the full implementation.  We still accept
    // the parameters so callers see a consistent ABI.
    (void)partner_net;
    (void)intra_pair_radius_cells;

    RouteResult result;
    result.net = net;
    result.success = false;

    // Issue #2610: Establish a per-net wall-clock deadline up front so the
    // inline A* loop can check it every N iterations without recomputing
    // the absolute deadline each tick.  ``has_deadline == false`` skips
    // all timeout checks (zero overhead vs. the pre-#2610 hot loop).
    bool has_deadline = (per_net_timeout_seconds > 0.0);
    std::chrono::steady_clock::time_point deadline{};
    if (has_deadline) {
        deadline = std::chrono::steady_clock::now()
            + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                std::chrono::duration<double>(per_net_timeout_seconds));
    }
    bool timed_out = false;
    constexpr int kTimeoutCheckInterval = 1024;

    auto [start_gx, start_gy] = grid_.world_to_grid(start_x, start_y);
    auto [end_gx, end_gy] = grid_.world_to_grid(end_x, end_y);

    std::vector<int> valid_start_layers = start_layers.empty()
        ? std::vector<int>{start_layer} : start_layers;
    std::vector<int> valid_end_layers = end_layers.empty()
        ? std::vector<int>{end_layer} : end_layers;

    PadBounds sp, ep;
    init_pad_bounds(sp, ep, start_gx, start_gy, end_gx, end_gy,
                    start_pad_bounds, end_pad_bounds);

    // Local A* data structures (not stored as members)
    PQ open_set;
    std::unordered_set<std::tuple<int, int, int>, GridPosHash> closed_set;
    std::unordered_map<std::tuple<int, int, int>, float, GridPosHash> g_scores;
    std::vector<AStarNode> closed_list;

    // Seed start nodes
    for (int sgx = sp.metal_gx1; sgx <= sp.metal_gx2; ++sgx) {
        for (int sgy = sp.metal_gy1; sgy <= sp.metal_gy2; ++sgy) {
            if (!grid_.is_valid(sgx, sgy, 0)) continue;
            for (int sl : valid_start_layers) {
                float h = heuristic(sgx, sgy, sl, end_gx, end_gy, valid_end_layers[0]);
                AStarNode start_node{h, 0.0f, sgx, sgy, sl, -1, false, 0, 0};
                auto key = std::make_tuple(sgx, sgy, sl);
                auto it = g_scores.find(key);
                if (it == g_scores.end() || 0.0f < it->second) {
                    g_scores[key] = 0.0f;
                    open_set.push(start_node);
                }
            }
        }
    }

    // Issue #2610: ``max_search_iterations`` overrides the historical
    // ``cols * rows * 4`` ceiling so callers (CLI ``--max-search-iterations``,
    // ``cpp_backend.py``) can trade memory for completeness on dense boards.
    // The default (<= 0) preserves pre-#2610 behavior.
    int max_iterations = (max_search_iterations > 0)
        ? max_search_iterations
        : grid_.cols() * grid_.rows() * 4;
    last_iterations_ = 0;
    last_nodes_explored_ = 0;

    // Issue #2476: Track via-vs-via blocked rejections so the negotiated
    // strategy can target rip-up at the specific net whose stored via is
    // blocking us, rather than blanket retry.  We record the most-recently
    // observed offending stored via along with the world-coord of the
    // candidate slot that was rejected.  When the search ends with
    // success=false, these are written into ``result``'s diagnostic fields.
    int via_block_count = 0;
    int last_blocking_net = 0;
    float last_block_world_x = 0.0f;
    float last_block_world_y = 0.0f;

    // Inline A* loop (uses local variables, no rejected goals check)
    while (!open_set.empty() && last_iterations_ < max_iterations) {
        last_iterations_++;

        // Issue #2610: Per-net wall-clock deadline check.  Sampled every
        // ``kTimeoutCheckInterval`` iterations to amortize the syscall to
        // ``steady_clock::now()`` -- mirrors the Python pathfinder's
        // approach at line 1791.  When the deadline fires we set
        // ``timed_out`` so the epilogue reports FAILURE_TIMEOUT rather
        // than FAILURE_NO_PATH / FAILURE_ITERATION_LIMIT.
        if (has_deadline && (last_iterations_ % kTimeoutCheckInterval == 0)) {
            if (std::chrono::steady_clock::now() >= deadline) {
                timed_out = true;
                break;
            }
        }

        AStarNode current = open_set.top();
        open_set.pop();

        auto current_key = std::make_tuple(current.x, current.y, current.layer);
        if (closed_set.count(current_key)) {
            continue;
        }
        closed_set.insert(current_key);

        int current_idx = static_cast<int>(closed_list.size());
        closed_list.push_back(current);
        last_nodes_explored_++;

        // Goal check
        bool in_end_metal = (
            current.x >= ep.metal_gx1 && current.x <= ep.metal_gx2 &&
            current.y >= ep.metal_gy1 && current.y <= ep.metal_gy2
        );
        if (in_end_metal) {
            bool layer_ok = std::find(valid_end_layers.begin(), valid_end_layers.end(),
                                      current.layer) != valid_end_layers.end();
            if (layer_ok) {
                // Issue #3130: forward per-net emit widths so the
                // reconstructed Segment/Via carry per-net values instead
                // of the global ``rules_`` defaults.
                result = reconstruct_path(closed_list, current_idx,
                                          start_x, start_y, end_x, end_y, net,
                                          emit_trace_width, emit_via_diameter,
                                          emit_via_drill);
                result.success = true;
                return result;
            }
        }

        // Pad exit relaxation
        bool is_exiting_start_pad = (
            current.x >= sp.metal_gx1 && current.x <= sp.metal_gx2 &&
            current.y >= sp.metal_gy1 && current.y <= sp.metal_gy2 &&
            std::find(valid_start_layers.begin(), valid_start_layers.end(),
                      current.layer) != valid_start_layers.end()
        );
        bool is_exiting_end_pad = (
            current.x >= ep.metal_gx1 && current.x <= ep.metal_gx2 &&
            current.y >= ep.metal_gy1 && current.y <= ep.metal_gy2 &&
            std::find(valid_end_layers.begin(), valid_end_layers.end(),
                      current.layer) != valid_end_layers.end()
        );

        // Explore 2D neighbors
        for (const auto& [dx, dy, dlayer, cost_mult] : neighbors_2d_) {
            int nx = current.x + dx;
            int ny = current.y + dy;
            int nlayer = current.layer;

            if (!grid_.is_valid(nx, ny, nlayer)) continue;

            if (dx != 0 && dy != 0) {
                if (is_diagonal_blocked(current.x, current.y, dx, dy, nlayer, net,
                                        negotiated_mode)) {
                    continue;
                }
            }

            const auto& cell = grid_.at(nx, ny, nlayer);

            bool layer_in_start = std::find(valid_start_layers.begin(),
                valid_start_layers.end(), nlayer) != valid_start_layers.end();
            bool layer_in_end = std::find(valid_end_layers.begin(),
                valid_end_layers.end(), nlayer) != valid_end_layers.end();

            bool is_in_start_metal = (
                nx >= sp.metal_gx1 && nx <= sp.metal_gx2 &&
                ny >= sp.metal_gy1 && ny <= sp.metal_gy2 && layer_in_start
            );
            bool is_in_end_metal = (
                nx >= ep.metal_gx1 && nx <= ep.metal_gx2 &&
                ny >= ep.metal_gy1 && ny <= ep.metal_gy2 && layer_in_end
            );

            bool is_start_adjacent = (
                nx >= sp.approach_gx1 && nx <= sp.approach_gx2 &&
                ny >= sp.approach_gy1 && ny <= sp.approach_gy2 && layer_in_start
            );
            bool is_end_adjacent = (
                nx >= ep.approach_gx1 && nx <= ep.approach_gx2 &&
                ny >= ep.approach_gy1 && ny <= ep.approach_gy2 && layer_in_end
            );

            if (cell.blocked) {
                if (is_in_start_metal || is_in_end_metal) {
                    // Allow entry into own pad's metal area
                } else if (cell.net == net) {
                    // Same-net blocked cell - allow
                } else if (cell.net == 0) {
                    if (is_trace_blocked(nx, ny, nlayer, net, negotiated_mode,
                                         trace_radius_cells,
                                         partner_net, intra_pair_radius_cells)) {
                        continue;
                    }
                } else {
                    bool is_clearance_only = !cell.pad_blocked;
                    bool is_pad_exit = is_exiting_start_pad || is_exiting_end_pad;
                    if (is_clearance_only && is_pad_exit) {
                        // Clearance zone cell while exiting pad - allow
                    } else {
                        continue;
                    }
                }
            } else {
                bool is_pad_exit_or_approach = (
                    is_start_adjacent || is_end_adjacent ||
                    is_exiting_start_pad || is_exiting_end_pad
                );
                if (!is_pad_exit_or_approach) {
                    if (is_trace_blocked(nx, ny, nlayer, net, negotiated_mode,
                                         trace_radius_cells,
                                         partner_net, intra_pair_radius_cells)) {
                        continue;
                    }
                }
            }

            auto neighbor_key = std::make_tuple(nx, ny, nlayer);
            if (closed_set.count(neighbor_key)) continue;

            float turn_cost = 0.0f;
            if (current.dx != 0 || current.dy != 0) {
                if (current.dx != dx || current.dy != dy) {
                    turn_cost = rules_.cost_turn;
                }
            }

            float congestion_cost = get_congestion_cost(nx, ny, nlayer);
            float negotiated_cost = 0.0f;
            if (negotiated_mode) {
                // Issue #2963: pass routing net so own-net obstacle
                // cells (destination pad metal post-PR #2928) stay
                // reachable with finite cost.
                negotiated_cost = grid_.get_negotiated_cost(
                    nx, ny, nlayer, present_cost_factor, net);
            }

            float avoidance = grid_.at(nx, ny, nlayer).avoidance_cost;

            float new_g = current.g_score +
                          cost_mult * rules_.cost_straight +
                          turn_cost + congestion_cost + negotiated_cost +
                          avoidance;

            auto it = g_scores.find(neighbor_key);
            if (it == g_scores.end() || new_g < it->second) {
                g_scores[neighbor_key] = new_g;
                float h = heuristic(nx, ny, nlayer, end_gx, end_gy, valid_end_layers[0]);
                float f = new_g + weight * h;

                AStarNode neighbor{f, new_g, nx, ny, nlayer, current_idx, false, dx, dy};
                open_set.push(neighbor);
            }
        }

        // Try layer change (via)
        for (int new_layer : routable_layers_) {
            if (new_layer == current.layer) continue;

            // Issue #2476: Use diagnostic-aware variant so we can record the
            // offending stored-via net when the geometric via-vs-via clearance
            // rule is what caused the rejection.
            int blocking_net = 0;
            float block_wx = 0.0f, block_wy = 0.0f;
            if (is_via_blocked_diag(current.x, current.y, net, negotiated_mode,
                                    via_radius_cells,
                                    blocking_net, block_wx, block_wy)) {
                if (blocking_net != 0) {
                    ++via_block_count;
                    last_blocking_net = blocking_net;
                    last_block_world_x = block_wx;
                    last_block_world_y = block_wy;
                }
                continue;
            }

            auto neighbor_key = std::make_tuple(current.x, current.y, new_layer);
            if (closed_set.count(neighbor_key)) continue;

            float congestion_cost = get_congestion_cost(current.x, current.y, new_layer);
            float negotiated_cost = 0.0f;
            if (negotiated_mode) {
                // Issue #2963: pass routing net so own-net obstacle
                // cells stay reachable with finite cost (via-drop into
                // destination pad).
                negotiated_cost = grid_.get_negotiated_cost(
                    current.x, current.y, new_layer, present_cost_factor, net);
            }

            float avoidance = grid_.at(current.x, current.y, new_layer).avoidance_cost;

            float new_g = current.g_score + rules_.cost_via + congestion_cost +
                          negotiated_cost + avoidance;

            auto it = g_scores.find(neighbor_key);
            if (it == g_scores.end() || new_g < it->second) {
                g_scores[neighbor_key] = new_g;
                float h = heuristic(current.x, current.y, new_layer,
                                    end_gx, end_gy, valid_end_layers[0]);
                float f = new_g + weight * h;

                AStarNode neighbor{f, new_g, current.x, current.y, new_layer,
                                   current_idx, true, current.dx, current.dy};
                open_set.push(neighbor);
            }
        }
    }

    // Search ended without reaching the goal.  Populate structured failure
    // diagnostics (Issues #2476 / #2610) so the negotiated strategy can
    // dispatch targeted retry/rip-up and so the router log can distinguish
    // wall-clock TIMEOUT from ITERATION_LIMIT (memory backstop hit) from
    // genuine NO_PATH (open set drained).
    if (timed_out) {
        result.failure_reason = FAILURE_TIMEOUT;
    } else if (last_iterations_ >= max_iterations) {
        result.failure_reason = FAILURE_ITERATION_LIMIT;
    } else {
        result.failure_reason = FAILURE_NO_PATH;
    }
    if (via_block_count > 0 && last_blocking_net != 0) {
        // At least one via expansion was refused by stored-via geometry.
        // Surface this regardless of why the open set ultimately drained --
        // a Python caller can then choose to rip up the blocking net.
        // Note: VIA_VIA_BLOCKED takes precedence over TIMEOUT/ITERATION_LIMIT
        // because the geometric blocker is the most actionable signal.
        result.failure_reason = FAILURE_VIA_VIA_BLOCKED;
        result.blocking_via_net = last_blocking_net;
        result.failure_x = last_block_world_x;
        result.failure_y = last_block_world_y;
    }
    return result;
}

RouteResult Pathfinder::route_resumable(
    float start_x, float start_y, int start_layer,
    float end_x, float end_y, int end_layer,
    int net,
    const std::vector<int>& start_layers,
    const std::vector<int>& end_layers,
    bool negotiated_mode,
    float present_cost_factor,
    float weight,
    int trace_radius_cells,
    int via_radius_cells,
    const PadBounds& start_pad_bounds,
    const PadBounds& end_pad_bounds,
    int partner_net,
    int intra_pair_radius_cells,
    double per_net_timeout_seconds,
    int max_search_iterations,
    float emit_trace_width,
    float emit_via_diameter,
    float emit_via_drill
) {
    // Clear any previous search state
    clear_search_state();

    // Store parameters for resume()
    search_start_x_ = start_x;
    search_start_y_ = start_y;
    search_end_x_ = end_x;
    search_end_y_ = end_y;
    search_net_ = net;
    search_negotiated_mode_ = negotiated_mode;
    search_present_cost_factor_ = present_cost_factor;
    search_weight_ = weight;
    search_trace_radius_cells_ = trace_radius_cells;
    search_via_radius_cells_ = via_radius_cells;

    // Issue #2559 / Epic #2556 Phase 1C: cache diff-pair partner state so
    // resume() and run_astar_loop() can apply the partner-aware radius
    // branch on every neighbor expansion.
    search_partner_net_ = partner_net;
    search_intra_pair_radius_cells_ = intra_pair_radius_cells;

    // Issue #3130: cache per-net emit widths/diameters so
    // ``reconstruct_path()`` (called from ``run_astar_loop()``) writes
    // per-net values into the returned Segment/Via objects instead of
    // the global ``rules_`` defaults.  Cached across resume() so an
    // (initial + resume*) sequence for a single net stays consistent.
    search_emit_trace_width_ = emit_trace_width;
    search_emit_via_diameter_ = emit_via_diameter;
    search_emit_via_drill_ = emit_via_drill;

    auto [start_gx, start_gy] = grid_.world_to_grid(start_x, start_y);
    auto [end_gx, end_gy] = grid_.world_to_grid(end_x, end_y);
    search_end_gx_ = end_gx;
    search_end_gy_ = end_gy;

    search_valid_start_layers_ = start_layers.empty()
        ? std::vector<int>{start_layer} : start_layers;
    search_valid_end_layers_ = end_layers.empty()
        ? std::vector<int>{end_layer} : end_layers;

    init_pad_bounds(search_start_pad_bounds_, search_end_pad_bounds_,
                    start_gx, start_gy, end_gx, end_gy,
                    start_pad_bounds, end_pad_bounds);

    // Seed start nodes into member open set
    const auto& sp = search_start_pad_bounds_;
    for (int sgx = sp.metal_gx1; sgx <= sp.metal_gx2; ++sgx) {
        for (int sgy = sp.metal_gy1; sgy <= sp.metal_gy2; ++sgy) {
            if (!grid_.is_valid(sgx, sgy, 0)) continue;
            for (int sl : search_valid_start_layers_) {
                float h = heuristic(sgx, sgy, sl, end_gx, end_gy,
                                    search_valid_end_layers_[0]);
                AStarNode start_node{h, 0.0f, sgx, sgy, sl, -1, false, 0, 0};
                auto key = std::make_tuple(sgx, sgy, sl);
                auto it = search_g_scores_.find(key);
                if (it == search_g_scores_.end() || 0.0f < it->second) {
                    search_g_scores_[key] = 0.0f;
                    search_open_set_.push(start_node);
                }
            }
        }
    }

    // Issue #2610: ``max_search_iterations`` overrides the historical
    // ``cols * rows * 4`` ceiling.  Default (<= 0) preserves pre-#2610 behavior.
    search_max_iterations_ = (max_search_iterations > 0)
        ? max_search_iterations
        : grid_.cols() * grid_.rows() * 4;
    last_iterations_ = 0;
    last_nodes_explored_ = 0;
    search_state_active_ = true;

    // Issue #2476: Reset via-vs-via failure trackers at the start of a
    // fresh resumable search.  resume() must NOT reset these -- a candidate
    // observed during the original route_resumable() may still be the
    // best diagnostic when resume() fails.
    search_via_block_count_ = 0;
    search_last_blocking_net_ = 0;
    search_last_block_world_x_ = 0.0f;
    search_last_block_world_y_ = 0.0f;

    // Issue #2610: Compute the per-net wall-clock deadline ONCE here and
    // share it with subsequent resume() calls.  This way a single per-net
    // budget covers the (initial search + up to N resume attempts), which
    // matches how cpp_backend.py orchestrates retries on validation
    // failure.  Without this, each resume() would get a fresh budget and
    // a pathological net could blow well past --per-net-timeout.
    search_has_deadline_ = (per_net_timeout_seconds > 0.0);
    if (search_has_deadline_) {
        search_deadline_ = std::chrono::steady_clock::now()
            + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                std::chrono::duration<double>(per_net_timeout_seconds));
    }
    search_timed_out_ = false;

    return run_astar_loop();
}

RouteResult Pathfinder::resume(int reject_x, int reject_y, int reject_layer) {
    RouteResult result;
    result.net = search_net_;
    result.success = false;

    if (!search_state_active_) {
        return result;  // No active search to resume
    }

    // Add rejected goal to skip set; it stays in closed_set (already expanded)
    rejected_goals_.insert(std::make_tuple(reject_x, reject_y, reject_layer));

    return run_astar_loop();
}

RouteResult Pathfinder::run_astar_loop() {
    RouteResult result;
    result.net = search_net_;
    result.success = false;

    const auto& sp = search_start_pad_bounds_;
    const auto& ep = search_end_pad_bounds_;

    constexpr int kTimeoutCheckInterval = 1024;

    while (!search_open_set_.empty() && last_iterations_ < search_max_iterations_) {
        last_iterations_++;

        // Issue #2610: Per-net wall-clock deadline check.  Sampled every
        // ``kTimeoutCheckInterval`` iterations to amortize the syscall to
        // ``steady_clock::now()`` -- mirrors pathfinder.py:1791.  The deadline
        // was computed once in route_resumable() and is shared with resume()
        // so a single per-net budget covers all attempts.
        if (search_has_deadline_ &&
            (last_iterations_ % kTimeoutCheckInterval == 0)) {
            if (std::chrono::steady_clock::now() >= search_deadline_) {
                search_timed_out_ = true;
                break;
            }
        }

        AStarNode current = search_open_set_.top();
        search_open_set_.pop();

        auto current_key = std::make_tuple(current.x, current.y, current.layer);
        if (search_closed_set_.count(current_key)) {
            continue;
        }
        search_closed_set_.insert(current_key);

        int current_idx = static_cast<int>(search_closed_list_.size());
        search_closed_list_.push_back(current);
        last_nodes_explored_++;

        // Goal check (with rejected goals skip)
        bool in_end_metal = (
            current.x >= ep.metal_gx1 && current.x <= ep.metal_gx2 &&
            current.y >= ep.metal_gy1 && current.y <= ep.metal_gy2
        );
        if (in_end_metal) {
            bool layer_ok = std::find(search_valid_end_layers_.begin(),
                                      search_valid_end_layers_.end(),
                                      current.layer) != search_valid_end_layers_.end();
            if (layer_ok) {
                // Issue #2447: Skip rejected goals (mirrors Python pathfinder's
                // continue at line 1553 when _reconstruct_route fails).
                if (!rejected_goals_.count(current_key)) {
                    // Issue #3130: forward cached per-net emit widths so
                    // the reconstructed Segment/Via carry per-net values
                    // for the (initial + resume*) sequence.
                    result = reconstruct_path(search_closed_list_, current_idx,
                                              search_start_x_, search_start_y_,
                                              search_end_x_, search_end_y_,
                                              search_net_,
                                              search_emit_trace_width_,
                                              search_emit_via_diameter_,
                                              search_emit_via_drill_);
                    result.success = true;
                    return result;
                }
                // Goal rejected, continue searching from open set
            }
        }

        // Pad exit relaxation
        bool is_exiting_start_pad = (
            current.x >= sp.metal_gx1 && current.x <= sp.metal_gx2 &&
            current.y >= sp.metal_gy1 && current.y <= sp.metal_gy2 &&
            std::find(search_valid_start_layers_.begin(),
                      search_valid_start_layers_.end(),
                      current.layer) != search_valid_start_layers_.end()
        );
        bool is_exiting_end_pad = (
            current.x >= ep.metal_gx1 && current.x <= ep.metal_gx2 &&
            current.y >= ep.metal_gy1 && current.y <= ep.metal_gy2 &&
            std::find(search_valid_end_layers_.begin(),
                      search_valid_end_layers_.end(),
                      current.layer) != search_valid_end_layers_.end()
        );

        // Explore 2D neighbors
        for (const auto& [dx, dy, dlayer, cost_mult] : neighbors_2d_) {
            int nx = current.x + dx;
            int ny = current.y + dy;
            int nlayer = current.layer;

            if (!grid_.is_valid(nx, ny, nlayer)) continue;

            if (dx != 0 && dy != 0) {
                if (is_diagonal_blocked(current.x, current.y, dx, dy, nlayer,
                                        search_net_, search_negotiated_mode_)) {
                    continue;
                }
            }

            const auto& cell = grid_.at(nx, ny, nlayer);

            bool layer_in_start = std::find(search_valid_start_layers_.begin(),
                search_valid_start_layers_.end(), nlayer) != search_valid_start_layers_.end();
            bool layer_in_end = std::find(search_valid_end_layers_.begin(),
                search_valid_end_layers_.end(), nlayer) != search_valid_end_layers_.end();

            bool is_in_start_metal = (
                nx >= sp.metal_gx1 && nx <= sp.metal_gx2 &&
                ny >= sp.metal_gy1 && ny <= sp.metal_gy2 && layer_in_start
            );
            bool is_in_end_metal = (
                nx >= ep.metal_gx1 && nx <= ep.metal_gx2 &&
                ny >= ep.metal_gy1 && ny <= ep.metal_gy2 && layer_in_end
            );

            bool is_start_adjacent = (
                nx >= sp.approach_gx1 && nx <= sp.approach_gx2 &&
                ny >= sp.approach_gy1 && ny <= sp.approach_gy2 && layer_in_start
            );
            bool is_end_adjacent = (
                nx >= ep.approach_gx1 && nx <= ep.approach_gx2 &&
                ny >= ep.approach_gy1 && ny <= ep.approach_gy2 && layer_in_end
            );

            if (cell.blocked) {
                if (is_in_start_metal || is_in_end_metal) {
                    // Allow entry into own pad's metal area
                } else if (cell.net == search_net_) {
                    // Same-net blocked cell - allow
                } else if (cell.net == 0) {
                    if (is_trace_blocked(nx, ny, nlayer, search_net_,
                                         search_negotiated_mode_,
                                         search_trace_radius_cells_,
                                         search_partner_net_,
                                         search_intra_pair_radius_cells_)) {
                        continue;
                    }
                } else {
                    bool is_clearance_only = !cell.pad_blocked;
                    bool is_pad_exit = is_exiting_start_pad || is_exiting_end_pad;
                    if (is_clearance_only && is_pad_exit) {
                        // Clearance zone cell while exiting pad - allow
                    } else {
                        continue;
                    }
                }
            } else {
                bool is_pad_exit_or_approach = (
                    is_start_adjacent || is_end_adjacent ||
                    is_exiting_start_pad || is_exiting_end_pad
                );
                if (!is_pad_exit_or_approach) {
                    if (is_trace_blocked(nx, ny, nlayer, search_net_,
                                         search_negotiated_mode_,
                                         search_trace_radius_cells_,
                                         search_partner_net_,
                                         search_intra_pair_radius_cells_)) {
                        continue;
                    }
                }
            }

            auto neighbor_key = std::make_tuple(nx, ny, nlayer);
            if (search_closed_set_.count(neighbor_key)) continue;

            float turn_cost = 0.0f;
            if (current.dx != 0 || current.dy != 0) {
                if (current.dx != dx || current.dy != dy) {
                    turn_cost = rules_.cost_turn;
                }
            }

            float congestion_cost = get_congestion_cost(nx, ny, nlayer);
            float negotiated_cost = 0.0f;
            if (search_negotiated_mode_) {
                // Issue #2963: pass routing net so own-net obstacle
                // cells (destination pad metal) stay reachable.
                negotiated_cost = grid_.get_negotiated_cost(
                    nx, ny, nlayer, search_present_cost_factor_, search_net_);
            }

            float avoidance = grid_.at(nx, ny, nlayer).avoidance_cost;

            float new_g = current.g_score +
                          cost_mult * rules_.cost_straight +
                          turn_cost + congestion_cost + negotiated_cost +
                          avoidance;

            auto it = search_g_scores_.find(neighbor_key);
            if (it == search_g_scores_.end() || new_g < it->second) {
                search_g_scores_[neighbor_key] = new_g;
                float h = heuristic(nx, ny, nlayer, search_end_gx_, search_end_gy_,
                                    search_valid_end_layers_[0]);
                float f = new_g + search_weight_ * h;

                AStarNode neighbor{f, new_g, nx, ny, nlayer, current_idx, false, dx, dy};
                search_open_set_.push(neighbor);
            }
        }

        // Try layer change (via)
        for (int new_layer : routable_layers_) {
            if (new_layer == current.layer) continue;

            // Issue #2476: Diagnostic-aware via blocking check so we can
            // record which stored-via net rejected our candidate slot.
            int blocking_net = 0;
            float block_wx = 0.0f, block_wy = 0.0f;
            if (is_via_blocked_diag(current.x, current.y, search_net_,
                                    search_negotiated_mode_,
                                    search_via_radius_cells_,
                                    blocking_net, block_wx, block_wy)) {
                if (blocking_net != 0) {
                    ++search_via_block_count_;
                    search_last_blocking_net_ = blocking_net;
                    search_last_block_world_x_ = block_wx;
                    search_last_block_world_y_ = block_wy;
                }
                continue;
            }

            auto neighbor_key = std::make_tuple(current.x, current.y, new_layer);
            if (search_closed_set_.count(neighbor_key)) continue;

            float congestion_cost = get_congestion_cost(current.x, current.y, new_layer);
            float negotiated_cost = 0.0f;
            if (search_negotiated_mode_) {
                // Issue #2963: pass routing net (search_net_) so own-net
                // obstacle cells stay reachable for via drop into the
                // destination pad metal.
                negotiated_cost = grid_.get_negotiated_cost(
                    current.x, current.y, new_layer, search_present_cost_factor_,
                    search_net_);
            }

            float avoidance = grid_.at(current.x, current.y, new_layer).avoidance_cost;

            float new_g = current.g_score + rules_.cost_via + congestion_cost +
                          negotiated_cost + avoidance;

            auto it = search_g_scores_.find(neighbor_key);
            if (it == search_g_scores_.end() || new_g < it->second) {
                search_g_scores_[neighbor_key] = new_g;
                float h = heuristic(current.x, current.y, new_layer,
                                    search_end_gx_, search_end_gy_,
                                    search_valid_end_layers_[0]);
                float f = new_g + search_weight_ * h;

                AStarNode neighbor{f, new_g, current.x, current.y, new_layer,
                                   current_idx, true, current.dx, current.dy};
                search_open_set_.push(neighbor);
            }
        }
    }

    // Open set exhausted, iteration limit hit, or wall-clock deadline fired.
    // Surface structured failure diagnostics (Issues #2476 / #2610) so the
    // negotiated strategy can dispatch targeted retries and the router log
    // can distinguish TIMEOUT (wall-clock) from ITERATION_LIMIT (memory
    // backstop) from NO_PATH (open set drained).
    search_state_active_ = false;
    if (search_timed_out_) {
        result.failure_reason = FAILURE_TIMEOUT;
    } else if (last_iterations_ >= search_max_iterations_) {
        result.failure_reason = FAILURE_ITERATION_LIMIT;
    } else {
        result.failure_reason = FAILURE_NO_PATH;
    }
    if (search_via_block_count_ > 0 && search_last_blocking_net_ != 0) {
        // VIA_VIA_BLOCKED takes precedence over TIMEOUT/ITERATION_LIMIT
        // because the geometric blocker is the most actionable signal.
        result.failure_reason = FAILURE_VIA_VIA_BLOCKED;
        result.blocking_via_net = search_last_blocking_net_;
        result.failure_x = search_last_block_world_x_;
        result.failure_y = search_last_block_world_y_;
    }
    return result;
}

void Pathfinder::clear_search_state() {
    // Clear all A* member state to release memory
    search_open_set_ = PQ();  // priority_queue has no clear(); swap with empty
    search_closed_set_.clear();
    search_g_scores_.clear();
    search_closed_list_.clear();
    rejected_goals_.clear();
    search_state_active_ = false;
    // Issue #2476: Reset failure trackers as well so a stale blocker from
    // the previous net does not leak into the next route().
    search_via_block_count_ = 0;
    search_last_blocking_net_ = 0;
    search_last_block_world_x_ = 0.0f;
    search_last_block_world_y_ = 0.0f;
    // Issue #2559 / Phase 1C: reset partner-net state so a stale partner
    // from a previous net does not leak into the next route().
    search_partner_net_ = -1;
    search_intra_pair_radius_cells_ = 0;
    // Issue #2610: reset deadline state so a stale per-net deadline from
    // the previous net does not leak into the next route().
    search_has_deadline_ = false;
    search_timed_out_ = false;
    // Issue #3130: reset per-net emit widths so a stale value from the
    // previous net does not leak into the next route().
    search_emit_trace_width_ = 0.0f;
    search_emit_via_diameter_ = 0.0f;
    search_emit_via_drill_ = 0.0f;
}

RouteResult Pathfinder::reconstruct_path(
    const std::vector<AStarNode>& closed_list,
    int end_idx,
    float start_x, float start_y,
    float end_x, float end_y,
    int net,
    float emit_trace_width,
    float emit_via_diameter,
    float emit_via_drill
) {
    RouteResult result;
    result.net = net;
    result.success = true;

    // Issue #3130: Resolve per-net emit values up front.  A caller-supplied
    // value > 0 wins; otherwise fall back to the global ``rules_`` defaults
    // so existing callers (and the pre-#3130 ABI) see identical behavior.
    const float seg_width = (emit_trace_width > 0.0f)
        ? emit_trace_width : rules_.trace_width;
    const float via_diameter = (emit_via_diameter > 0.0f)
        ? emit_via_diameter : rules_.via_diameter;
    const float via_drill = (emit_via_drill > 0.0f)
        ? emit_via_drill : rules_.via_drill;

    // Build path from end to start
    std::vector<std::tuple<float, float, int, bool>> path;
    int idx = end_idx;
    while (idx >= 0 && idx < static_cast<int>(closed_list.size())) {
        const auto& node = closed_list[idx];
        auto [wx, wy] = grid_.grid_to_world(node.x, node.y);
        path.emplace_back(wx, wy, node.layer, node.via_from_parent);
        idx = node.parent_idx;
    }
    std::reverse(path.begin(), path.end());

    if (path.size() < 2) {
        return result;
    }

    // Convert path to segments and vias
    float current_x = start_x;
    float current_y = start_y;
    int current_layer = std::get<2>(path[0]);

    for (size_t i = 0; i < path.size(); ++i) {
        auto [wx, wy, layer, is_via] = path[i];

        if (is_via) {
            // Add via
            Via via;
            via.x = current_x;
            via.y = current_y;
            via.drill = via_drill;
            via.diameter = via_diameter;
            via.layer_from = current_layer;
            via.layer_to = layer;
            via.net = net;
            result.vias.push_back(via);
            current_layer = layer;
        } else {
            // Add segment if position changed
            if (std::abs(wx - current_x) > 0.01f || std::abs(wy - current_y) > 0.01f) {
                Segment seg;
                seg.x1 = current_x;
                seg.y1 = current_y;
                seg.x2 = wx;
                seg.y2 = wy;
                seg.width = seg_width;
                seg.layer = layer;
                seg.net = net;
                result.segments.push_back(seg);
                current_x = wx;
                current_y = wy;
                current_layer = layer;
            }
        }
    }

    // Final segment to end
    if (std::abs(end_x - current_x) > 0.01f || std::abs(end_y - current_y) > 0.01f) {
        Segment seg;
        seg.x1 = current_x;
        seg.y1 = current_y;
        seg.x2 = end_x;
        seg.y2 = end_y;
        seg.width = seg_width;
        seg.layer = current_layer;
        seg.net = net;
        result.segments.push_back(seg);
    }

    return result;
}

}  // namespace router
