/*
 * DRC C++ Core - nanobind Python bindings
 * Part of kicad-tools DRC performance optimization (Phase 4)
 */

#include "drc_clearance.hpp"
#include <nanobind/nanobind.h>
#include <nanobind/stl/vector.h>

namespace nb = nanobind;
using namespace nb::literals;
using namespace drc;

NB_MODULE(drc_cpp, m) {
    m.doc() = "C++ DRC core for high-performance pad-to-pad clearance checking";

    // ClearanceResult struct
    nb::class_<ClearanceResult>(m, "ClearanceResult")
        .def(nb::init<>())
        .def_ro("min_clearance", &ClearanceResult::min_clearance)
        .def_ro("location_x", &ClearanceResult::location_x)
        .def_ro("location_y", &ClearanceResult::location_y)
        .def_ro("pad1_index", &ClearanceResult::pad1_index)
        .def_ro("pad2_index", &ClearanceResult::pad2_index)
        .def_ro("has_result", &ClearanceResult::has_result);

    // Batch clearance check (vector-based for Python interop)
    m.def("check_pair_clearance", &check_pair_clearance_vec,
        "pad1_local_x"_a, "pad1_local_y"_a,
        "pad1_radius"_a, "pad1_net"_a,
        "fp1_x"_a, "fp1_y"_a, "fp1_rotation_rad"_a,
        "pad2_local_x"_a, "pad2_local_y"_a,
        "pad2_radius"_a, "pad2_net"_a,
        "fp2_x"_a, "fp2_y"_a, "fp2_rotation_rad"_a,
        "Batch pad-to-pad clearance check.\n\n"
        "Checks all pads of component 1 against all pads of component 2.\n"
        "Uses squared-distance optimization to minimize sqrt calls.\n"
        "Pad data is passed as flat arrays (struct-of-arrays layout).\n\n"
        "Args:\n"
        "    pad1_local_x: Component 1 pad local X coordinates\n"
        "    pad1_local_y: Component 1 pad local Y coordinates\n"
        "    pad1_radius: Component 1 pad radii (max(w,h)/2)\n"
        "    pad1_net: Component 1 pad net numbers\n"
        "    fp1_x: Component 1 footprint X position\n"
        "    fp1_y: Component 1 footprint Y position\n"
        "    fp1_rotation_rad: Component 1 rotation in radians\n"
        "    pad2_local_x: Component 2 pad local X coordinates\n"
        "    pad2_local_y: Component 2 pad local Y coordinates\n"
        "    pad2_radius: Component 2 pad radii (max(w,h)/2)\n"
        "    pad2_net: Component 2 pad net numbers\n"
        "    fp2_x: Component 2 footprint X position\n"
        "    fp2_y: Component 2 footprint Y position\n"
        "    fp2_rotation_rad: Component 2 rotation in radians\n\n"
        "Returns:\n"
        "    ClearanceResult with minimum clearance and violation location"
    );

    // Version info
    m.def("version", []() { return "1.0.0"; });
    m.def("is_available", []() { return true; });
}
