/*
 * Placement C++ Core - nanobind Python bindings
 *
 * Exposes AABB overlap/clearance operations, the BatchCostEvaluator,
 * force-directed placement engine, and evolutionary fitness evaluation
 * for high-performance placement cost and force evaluation.
 */

#include "aabb.hpp"
#include "cost_evaluator.hpp"
#include "fitness_evaluator.hpp"
#include "force_engine.hpp"
#include <nanobind/nanobind.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/unordered_map.h>
#include <nanobind/stl/vector.h>

namespace nb = nanobind;
using namespace nb::literals;
using namespace placement;

NB_MODULE(placement_cpp, m) {
    m.doc() = "C++ placement core for high-performance AABB cost, force, and fitness evaluation";

    // AABB struct
    nb::class_<AABB>(m, "AABB")
        .def(nb::init<double, double, double, double>(),
             "min_x"_a, "min_y"_a, "max_x"_a, "max_y"_a)
        .def_rw("min_x", &AABB::min_x)
        .def_rw("min_y", &AABB::min_y)
        .def_rw("max_x", &AABB::max_x)
        .def_rw("max_y", &AABB::max_y);

    // CostResult struct
    nb::class_<CostResult>(m, "CostResult")
        .def(nb::init<>())
        .def_rw("overlap", &CostResult::overlap)
        .def_rw("boundary", &CostResult::boundary)
        .def_rw("drc", &CostResult::drc);

    // Free functions matching cost.py signatures
    m.def("compute_overlap", &compute_overlap,
          "boxes"_a,
          "Compute total pairwise overlap area between AABBs.");

    m.def("compute_boundary_violation", &compute_boundary_violation,
          "boxes"_a, "board"_a,
          "Compute total boundary violation depth.");

    m.def("compute_drc_violations", &compute_drc_violations,
          "boxes"_a, "min_gap"_a,
          "Compute count of DRC clearance violations.");

    // BatchCostEvaluator class
    nb::class_<BatchCostEvaluator>(m, "BatchCostEvaluator")
        .def(nb::init<double, double, double, double, double>(),
             "board_min_x"_a, "board_min_y"_a,
             "board_max_x"_a, "board_max_y"_a,
             "min_clearance"_a)
        .def("evaluate", &BatchCostEvaluator::evaluate,
             "xs"_a, "ys"_a, "widths"_a, "heights"_a,
             "Evaluate all cost components (overlap, boundary, drc).")
        .def("evaluate_overlap", &BatchCostEvaluator::evaluate_overlap,
             "xs"_a, "ys"_a, "widths"_a, "heights"_a,
             "Compute only pairwise overlap area.")
        .def("evaluate_boundary", &BatchCostEvaluator::evaluate_boundary,
             "xs"_a, "ys"_a, "widths"_a, "heights"_a,
             "Compute only boundary violations.")
        .def("evaluate_drc", &BatchCostEvaluator::evaluate_drc,
             "xs"_a, "ys"_a, "widths"_a, "heights"_a,
             "Compute only DRC violations.");

    // --- Force engine types and functions ---

    // ForceConfig struct
    nb::class_<ForceConfig>(m, "ForceConfig")
        .def(nb::init<>())
        .def_rw("charge_density", &ForceConfig::charge_density)
        .def_rw("min_distance", &ForceConfig::min_distance)
        .def_rw("edge_samples", &ForceConfig::edge_samples)
        .def_rw("boundary_charge", &ForceConfig::boundary_charge);

    // ForceResult struct
    nb::class_<ForceResult>(m, "ForceResult")
        .def(nb::init<>())
        .def_rw("forces_x", &ForceResult::forces_x)
        .def_rw("forces_y", &ForceResult::forces_y)
        .def_rw("torques", &ForceResult::torques);

    // Force computation functions
    m.def("compute_all_repulsion", &compute_all_repulsion,
          "positions_x"_a, "positions_y"_a,
          "edges_flat"_a, "edge_offsets"_a,
          "n_components"_a, "config"_a, "fixed_mask"_a,
          "Compute all pairwise component repulsion forces and torques.");

    m.def("compute_boundary_forces", &compute_boundary_forces,
          "positions_x"_a, "positions_y"_a,
          "edges_flat"_a, "edge_offsets"_a,
          "board_edges"_a, "n_board_edges"_a,
          "n_components"_a, "config"_a,
          "fixed_mask"_a, "inside_flags"_a,
          "Compute boundary forces from board edges on all components.");

    // --- Evolutionary fitness evaluation ---

    // FitnessComponentData struct
    nb::class_<FitnessComponentData>(m, "FitnessComponentData")
        .def(nb::init<>())
        .def_rw("x", &FitnessComponentData::x)
        .def_rw("y", &FitnessComponentData::y)
        .def_rw("rotation", &FitnessComponentData::rotation)
        .def_rw("width", &FitnessComponentData::width)
        .def_rw("height", &FitnessComponentData::height)
        .def_rw("pin_offsets", &FitnessComponentData::pin_offsets);

    // FitnessSpring struct
    nb::class_<FitnessSpring>(m, "FitnessSpring")
        .def(nb::init<>())
        .def_rw("comp1_ref", &FitnessSpring::comp1_ref)
        .def_rw("pin1_num", &FitnessSpring::pin1_num)
        .def_rw("comp2_ref", &FitnessSpring::comp2_ref)
        .def_rw("pin2_num", &FitnessSpring::pin2_num);

    // FitnessWeights struct
    nb::class_<FitnessWeights>(m, "FitnessWeights")
        .def(nb::init<>())
        .def_rw("wire_length_weight", &FitnessWeights::wire_length_weight)
        .def_rw("conflict_weight", &FitnessWeights::conflict_weight)
        .def_rw("routability_weight", &FitnessWeights::routability_weight)
        .def_rw("boundary_violation_weight", &FitnessWeights::boundary_violation_weight)
        .def_rw("pin_alignment_weight", &FitnessWeights::pin_alignment_weight)
        .def_rw("pin_alignment_tolerance", &FitnessWeights::pin_alignment_tolerance);

    // evaluate_fitness function
    m.def("evaluate_fitness", &evaluate_fitness,
          "ind_positions"_a, "ind_rotations"_a,
          "components"_a, "springs"_a,
          "board_vertices"_a, "weights"_a,
          "Evaluate fitness for a single individual placement.\n\n"
          "Mirrors _evaluate_fitness_worker() in evolutionary.py.\n"
          "Returns a fitness value (higher is better).");

    // Version info
    m.def("version", []() { return "2.1.0"; });
    m.def("is_available", []() { return true; });
}
