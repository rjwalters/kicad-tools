/*
 * Placement C++ Core - Evolutionary fitness evaluator implementation
 *
 * Mirrors _evaluate_fitness_worker() in evolutionary.py exactly.
 * Every sub-score computation matches the Python implementation to
 * produce numerically identical results (within floating-point tolerance).
 */

#include "fitness_evaluator.hpp"

#include <algorithm>
#include <cmath>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace placement {

namespace {

/// Degrees to radians conversion.
constexpr double DEG_TO_RAD = M_PI / 180.0;

/// Build component states from individual genotype + component data.
///
/// For each component, applies the individual's position/rotation (if present),
/// computes absolute pin positions, and stores them in comp_state.
void build_component_states(
    const std::unordered_map<std::string, std::pair<double, double>>& ind_positions,
    const std::unordered_map<std::string, double>& ind_rotations,
    const std::unordered_map<std::string, FitnessComponentData>& components,
    std::unordered_map<std::string, ComponentState>& comp_state) {

    comp_state.reserve(components.size());

    for (const auto& [ref, comp] : components) {
        double x, y, rotation;

        auto pos_it = ind_positions.find(ref);
        if (pos_it != ind_positions.end()) {
            x = pos_it->second.first;
            y = pos_it->second.second;
            auto rot_it = ind_rotations.find(ref);
            rotation = (rot_it != ind_rotations.end()) ? rot_it->second : comp.rotation;
        } else {
            x = comp.x;
            y = comp.y;
            rotation = comp.rotation;
        }

        double cos_r = std::cos(rotation * DEG_TO_RAD);
        double sin_r = std::sin(rotation * DEG_TO_RAD);

        ComponentState state;
        state.x = x;
        state.y = y;
        state.rotation = rotation;
        state.width = comp.width;
        state.height = comp.height;
        state.pin_positions.reserve(comp.pin_offsets.size());

        for (const auto& [ox, oy, pin_num] : comp.pin_offsets) {
            double pin_x = x + ox * cos_r - oy * sin_r;
            double pin_y = y + ox * sin_r + oy * cos_r;
            state.pin_positions.emplace_back(pin_x, pin_y, pin_num);
        }

        comp_state.emplace(ref, std::move(state));
    }
}

/// Compute total wire length from springs.
///
/// For each spring, finds the matching pins by number and computes
/// Euclidean distance. Mirrors lines 194-209 of evolutionary.py.
double compute_wire_length(
    const std::vector<FitnessSpring>& springs,
    const std::unordered_map<std::string, ComponentState>& comp_state) {

    double wire_length = 0.0;

    for (const auto& spring : springs) {
        auto it1 = comp_state.find(spring.comp1_ref);
        auto it2 = comp_state.find(spring.comp2_ref);
        if (it1 == comp_state.end() || it2 == comp_state.end()) {
            continue;
        }

        const auto& pins1 = it1->second.pin_positions;
        const auto& pins2 = it2->second.pin_positions;

        // Find matching pins by number
        const double* pin1_x = nullptr;
        const double* pin1_y = nullptr;
        for (const auto& [px, py, pn] : pins1) {
            if (pn == spring.pin1_num) {
                pin1_x = &px;
                pin1_y = &py;
                break;
            }
        }

        const double* pin2_x = nullptr;
        const double* pin2_y = nullptr;
        for (const auto& [px, py, pn] : pins2) {
            if (pn == spring.pin2_num) {
                pin2_x = &px;
                pin2_y = &py;
                break;
            }
        }

        if (pin1_x && pin2_x) {
            double dx = *pin2_x - *pin1_x;
            double dy = *pin2_y - *pin1_y;
            wire_length += std::sqrt(dx * dx + dy * dy);
        }
    }

    return wire_length;
}

/// Compute pin alignment score.
///
/// Returns percentage (0-100) of connected pin pairs that are aligned
/// horizontally or vertically within tolerance.
/// Mirrors lines 212-234 of evolutionary.py.
double compute_pin_alignment(
    const std::vector<FitnessSpring>& springs,
    const std::unordered_map<std::string, ComponentState>& comp_state,
    double tolerance) {

    int aligned_pins = 0;
    int total_pin_pairs = 0;

    for (const auto& spring : springs) {
        auto it1 = comp_state.find(spring.comp1_ref);
        auto it2 = comp_state.find(spring.comp2_ref);
        if (it1 == comp_state.end() || it2 == comp_state.end()) {
            continue;
        }

        const auto& pins1 = it1->second.pin_positions;
        const auto& pins2 = it2->second.pin_positions;

        const double* pin1_x = nullptr;
        const double* pin1_y = nullptr;
        for (const auto& [px, py, pn] : pins1) {
            if (pn == spring.pin1_num) {
                pin1_x = &px;
                pin1_y = &py;
                break;
            }
        }

        const double* pin2_x = nullptr;
        const double* pin2_y = nullptr;
        for (const auto& [px, py, pn] : pins2) {
            if (pn == spring.pin2_num) {
                pin2_x = &px;
                pin2_y = &py;
                break;
            }
        }

        if (pin1_x && pin2_x) {
            total_pin_pairs += 1;
            double dx = std::abs(*pin2_x - *pin1_x);
            double dy = std::abs(*pin2_y - *pin1_y);
            if (dx < tolerance || dy < tolerance) {
                aligned_pins += 1;
            }
        }
    }

    return (total_pin_pairs > 0)
               ? (static_cast<double>(aligned_pins) / total_pin_pairs * 100.0)
               : 0.0;
}

/// Count AABB overlap conflicts between components.
///
/// Mirrors lines 237-249 of evolutionary.py.
int count_conflicts(
    const std::unordered_map<std::string, ComponentState>& comp_state) {

    // Build flat list for O(N^2) pairwise comparison
    std::vector<const ComponentState*> comp_list;
    comp_list.reserve(comp_state.size());
    for (const auto& [ref, state] : comp_state) {
        comp_list.push_back(&state);
    }

    int conflicts = 0;
    const size_t n = comp_list.size();

    for (size_t i = 0; i < n; ++i) {
        double x1 = comp_list[i]->x;
        double y1 = comp_list[i]->y;
        double hw1 = comp_list[i]->width / 2.0;
        double hh1 = comp_list[i]->height / 2.0;

        for (size_t j = i + 1; j < n; ++j) {
            double x2 = comp_list[j]->x;
            double y2 = comp_list[j]->y;
            double hw2 = comp_list[j]->width / 2.0;
            double hh2 = comp_list[j]->height / 2.0;

            double dx = std::abs(x1 - x2);
            double dy = std::abs(y1 - y2);

            if (dx < (hw1 + hw2) && dy < (hh1 + hh2)) {
                conflicts += 1;
            }
        }
    }

    return conflicts;
}

/// Count boundary violations using ray-casting point-in-polygon.
///
/// Mirrors lines 251-267 of evolutionary.py.
int count_boundary_violations(
    const std::unordered_map<std::string, ComponentState>& comp_state,
    const std::vector<std::pair<double, double>>& board_vertices) {

    const size_t n_verts = board_vertices.size();
    if (n_verts < 3) {
        return 0;
    }

    int boundary_violations = 0;

    for (const auto& [ref, state] : comp_state) {
        double x = state.x;
        double y = state.y;

        // Ray casting algorithm for point-in-polygon
        bool inside = false;
        size_t j = n_verts - 1;
        for (size_t i = 0; i < n_verts; ++i) {
            double xi = board_vertices[i].first;
            double yi = board_vertices[i].second;
            double xj = board_vertices[j].first;
            double yj = board_vertices[j].second;

            if (((yi > y) != (yj > y)) &&
                (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) {
                inside = !inside;
            }
            j = i;
        }

        if (!inside) {
            boundary_violations += 1;
        }
    }

    return boundary_violations;
}

/// Estimate routability based on average spacing.
///
/// Mirrors lines 269-286 of evolutionary.py.
double estimate_routability(
    const std::unordered_map<std::string, ComponentState>& comp_state) {

    const size_t n = comp_state.size();
    if (n < 2) {
        return 100.0;
    }

    // Build flat list for pairwise iteration
    std::vector<const ComponentState*> comp_list;
    comp_list.reserve(n);
    for (const auto& [ref, state] : comp_state) {
        comp_list.push_back(&state);
    }

    double total_spacing = 0.0;
    int count = 0;

    for (size_t i = 0; i < n; ++i) {
        double x1 = comp_list[i]->x;
        double y1 = comp_list[i]->y;
        for (size_t j = i + 1; j < n; ++j) {
            double x2 = comp_list[j]->x;
            double y2 = comp_list[j]->y;
            double dx = x1 - x2;
            double dy = y1 - y2;
            total_spacing += std::sqrt(dx * dx + dy * dy);
            count += 1;
        }
    }

    double avg_spacing = (count > 0) ? (total_spacing / count) : 0.0;
    return std::min(100.0, avg_spacing * 5.0);
}

}  // anonymous namespace

double evaluate_fitness(
    const std::unordered_map<std::string, std::pair<double, double>>& ind_positions,
    const std::unordered_map<std::string, double>& ind_rotations,
    const std::unordered_map<std::string, FitnessComponentData>& components,
    const std::vector<FitnessSpring>& springs,
    const std::vector<std::pair<double, double>>& board_vertices,
    const FitnessWeights& weights) {

    // Build component states with absolute pin positions
    std::unordered_map<std::string, ComponentState> comp_state;
    build_component_states(ind_positions, ind_rotations, components, comp_state);

    // Compute sub-scores
    double wire_length = compute_wire_length(springs, comp_state);
    double alignment_score = compute_pin_alignment(springs, comp_state, weights.pin_alignment_tolerance);
    int conflicts = count_conflicts(comp_state);
    int boundary_violations = count_boundary_violations(comp_state, board_vertices);
    double routability_score = estimate_routability(comp_state);

    // Compute fitness (higher is better) - mirrors lines 288-296
    double fitness =
        1000.0
        - wire_length * weights.wire_length_weight
        - conflicts * weights.conflict_weight
        - boundary_violations * weights.boundary_violation_weight
        + routability_score * weights.routability_weight
        + alignment_score * weights.pin_alignment_weight;

    return fitness;
}

}  // namespace placement
