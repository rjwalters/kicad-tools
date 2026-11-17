/*
 * DRC Clearance C++ Core - pad-to-pad clearance computation
 * Part of kicad-tools DRC performance optimization (Phase 4)
 *
 * Uses struct-of-arrays layout and squared-distance optimization
 * to accelerate the O(P1 x P2) inner loop.
 */

#pragma once

#include <cmath>
#include <cstdint>
#include <vector>

namespace drc {

/// Result of a batch pad-to-pad clearance check.
struct ClearanceResult {
    float min_clearance = std::numeric_limits<float>::infinity();
    float location_x = 0.0f;
    float location_y = 0.0f;
    int pad1_index = -1;
    int pad2_index = -1;
    bool has_result = false;
};

/// Batch pad-to-pad clearance check using struct-of-arrays layout.
///
/// All pad data is passed as flat arrays to minimize marshaling overhead.
/// Trig is computed once per footprint. The inner loop uses squared-distance
/// comparison and only computes sqrt for the minimum-distance pair.
///
/// @param pad1_local_x   Component 1 pad local X coordinates
/// @param pad1_local_y   Component 1 pad local Y coordinates
/// @param pad1_radius    Component 1 pad radii (max(w,h)/2)
/// @param pad1_net       Component 1 pad net numbers
/// @param n_pads1        Number of pads in component 1
/// @param fp1_x          Component 1 footprint X position (board coords)
/// @param fp1_y          Component 1 footprint Y position (board coords)
/// @param fp1_rotation_rad Component 1 footprint rotation in radians
/// @param pad2_local_x   Component 2 pad local X coordinates
/// @param pad2_local_y   Component 2 pad local Y coordinates
/// @param pad2_radius    Component 2 pad radii (max(w,h)/2)
/// @param pad2_net       Component 2 pad net numbers
/// @param n_pads2        Number of pads in component 2
/// @param fp2_x          Component 2 footprint X position (board coords)
/// @param fp2_y          Component 2 footprint Y position (board coords)
/// @param fp2_rotation_rad Component 2 footprint rotation in radians
/// @return ClearanceResult with minimum clearance and location
ClearanceResult check_pair_clearance(
    const float* pad1_local_x, const float* pad1_local_y,
    const float* pad1_radius, const int* pad1_net,
    int n_pads1,
    float fp1_x, float fp1_y, float fp1_rotation_rad,
    const float* pad2_local_x, const float* pad2_local_y,
    const float* pad2_radius, const int* pad2_net,
    int n_pads2,
    float fp2_x, float fp2_y, float fp2_rotation_rad
);

/// Vector-based wrapper for Python bindings (copies data from vectors).
ClearanceResult check_pair_clearance_vec(
    const std::vector<float>& pad1_local_x,
    const std::vector<float>& pad1_local_y,
    const std::vector<float>& pad1_radius,
    const std::vector<int>& pad1_net,
    float fp1_x, float fp1_y, float fp1_rotation_rad,
    const std::vector<float>& pad2_local_x,
    const std::vector<float>& pad2_local_y,
    const std::vector<float>& pad2_radius,
    const std::vector<int>& pad2_net,
    float fp2_x, float fp2_y, float fp2_rotation_rad
);

} // namespace drc
