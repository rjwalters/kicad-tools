/*
 * Placement C++ Core - nanobind Python bindings
 *
 * Exposes AABB overlap/clearance operations and the BatchCostEvaluator
 * for high-performance placement cost evaluation.
 */

#include "aabb.hpp"
#include "cost_evaluator.hpp"
#include <nanobind/nanobind.h>
#include <nanobind/stl/vector.h>

namespace nb = nanobind;
using namespace nb::literals;
using namespace placement;

NB_MODULE(placement_cpp, m) {
    m.doc() = "C++ placement core for high-performance AABB cost evaluation";

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

    // Version info
    m.def("version", []() { return "1.0.0"; });
    m.def("is_available", []() { return true; });
}
