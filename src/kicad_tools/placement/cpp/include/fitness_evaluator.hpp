/*
 * Placement C++ Core - Evolutionary fitness evaluator
 *
 * Stateless fitness evaluation for the evolutionary placement optimizer.
 * Mirrors _evaluate_fitness_worker() in evolutionary.py and must produce
 * numerically identical results.
 *
 * Designed for ProcessPoolExecutor: no global state, fork-safe.
 */

#pragma once

#include <cmath>
#include <cstddef>
#include <string>
#include <unordered_map>
#include <vector>

namespace placement {

/// Component data for fitness evaluation.
struct FitnessComponentData {
    double x;
    double y;
    double rotation;  // degrees
    double width;
    double height;
    // Pin offsets relative to component center: (offset_x, offset_y, pin_number)
    std::vector<std::tuple<double, double, std::string>> pin_offsets;
};

/// Spring (net connection) between two component pins.
struct FitnessSpring {
    std::string comp1_ref;
    std::string pin1_num;
    std::string comp2_ref;
    std::string pin2_num;
};

/// Fitness evaluation weights.
struct FitnessWeights {
    double wire_length_weight;
    double conflict_weight;
    double routability_weight;
    double boundary_violation_weight;
    double pin_alignment_weight;
    double pin_alignment_tolerance;
};

/// Individual component state after applying genotype positions/rotations.
struct ComponentState {
    double x;
    double y;
    double rotation;
    double width;
    double height;
    // Absolute pin positions: (pin_x, pin_y, pin_number)
    std::vector<std::tuple<double, double, std::string>> pin_positions;
};

/// Compute fitness for a single individual placement.
///
/// This is a stateless function that mirrors _evaluate_fitness_worker() in
/// evolutionary.py. It takes the individual's positions/rotations, the
/// component data, springs, board outline, and fitness weights, and returns
/// a single fitness value (higher is better).
///
/// @param ind_positions   Individual's component positions: ref -> (x, y)
/// @param ind_rotations   Individual's component rotations: ref -> degrees
/// @param components      Component data: ref -> FitnessComponentData
/// @param springs         Spring connections between pins
/// @param board_vertices  Board outline vertices: list of (x, y)
/// @param weights         Fitness evaluation weights
/// @return Fitness value (higher is better)
double evaluate_fitness(
    const std::unordered_map<std::string, std::pair<double, double>>& ind_positions,
    const std::unordered_map<std::string, double>& ind_rotations,
    const std::unordered_map<std::string, FitnessComponentData>& components,
    const std::vector<FitnessSpring>& springs,
    const std::vector<std::pair<double, double>>& board_vertices,
    const FitnessWeights& weights);

}  // namespace placement
