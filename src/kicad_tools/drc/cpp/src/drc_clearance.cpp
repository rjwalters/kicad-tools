/*
 * DRC Clearance C++ Core - pad-to-pad clearance computation
 *
 * Key optimizations vs the Python version:
 * 1. Trig computed once per footprint (cos/sin), not per pad
 * 2. Inner loop uses squared-distance to avoid sqrt until the end
 * 3. Struct-of-arrays layout for contiguous memory access
 * 4. No Python interpreter overhead per iteration
 */

#include "drc_clearance.hpp"
#include <cmath>
#include <limits>
#include <vector>

namespace drc {

ClearanceResult check_pair_clearance(
    const float* pad1_local_x, const float* pad1_local_y,
    const float* pad1_radius, const int* pad1_net,
    int n_pads1,
    float fp1_x, float fp1_y, float fp1_rotation_rad,
    const float* pad2_local_x, const float* pad2_local_y,
    const float* pad2_radius, const int* pad2_net,
    int n_pads2,
    float fp2_x, float fp2_y, float fp2_rotation_rad
) {
    ClearanceResult result;

    if (n_pads1 <= 0 || n_pads2 <= 0) {
        return result;
    }

    // Precompute trig for each footprint (once, not per pad)
    const float cos1 = std::cos(fp1_rotation_rad);
    const float sin1 = std::sin(fp1_rotation_rad);
    const float cos2 = std::cos(fp2_rotation_rad);
    const float sin2 = std::sin(fp2_rotation_rad);

    // Precompute absolute pad positions for component 1
    // This avoids recomputing the rotation for every inner iteration
    std::vector<float> abs_x1(n_pads1), abs_y1(n_pads1);
    for (int i = 0; i < n_pads1; ++i) {
        float lx = pad1_local_x[i];
        float ly = pad1_local_y[i];
        abs_x1[i] = fp1_x + lx * cos1 - ly * sin1;
        abs_y1[i] = fp1_y + lx * sin1 + ly * cos1;
    }

    // Precompute absolute pad positions for component 2
    std::vector<float> abs_x2(n_pads2), abs_y2(n_pads2);
    for (int j = 0; j < n_pads2; ++j) {
        float lx = pad2_local_x[j];
        float ly = pad2_local_y[j];
        abs_x2[j] = fp2_x + lx * cos2 - ly * sin2;
        abs_y2[j] = fp2_y + lx * sin2 + ly * cos2;
    }

    // Track minimum using squared distance to avoid sqrt in the inner loop.
    // We track the best (min_clearance_sq_adjusted) which represents
    // (distance - r1 - r2)^2 effectively, but since we need the actual
    // clearance = dist - r1 - r2 and comparison is non-trivial with the
    // radius terms, we track the actual clearance value and defer sqrt
    // only to when we find a new minimum candidate.
    //
    // Optimization: we use a "best squared distance" threshold to skip
    // pairs where the center-to-center distance alone (ignoring radii)
    // already exceeds the current best clearance + max possible radii sum.
    float best_clearance = std::numeric_limits<float>::infinity();
    int best_i = -1, best_j = -1;

    // Find max radius sum to establish a tighter skip threshold
    float max_r1 = 0.0f, max_r2 = 0.0f;
    for (int i = 0; i < n_pads1; ++i) {
        if (pad1_radius[i] > max_r1) max_r1 = pad1_radius[i];
    }
    for (int j = 0; j < n_pads2; ++j) {
        if (pad2_radius[j] > max_r2) max_r2 = pad2_radius[j];
    }

    for (int i = 0; i < n_pads1; ++i) {
        const float ax1 = abs_x1[i];
        const float ay1 = abs_y1[i];
        const float r1 = pad1_radius[i];
        const int net1 = pad1_net[i];

        for (int j = 0; j < n_pads2; ++j) {
            // Skip same-net pads (same net can touch), but not unconnected (net 0)
            if (net1 == pad2_net[j] && net1 != 0) {
                continue;
            }

            const float dx = abs_x2[j] - ax1;
            const float dy = abs_y2[j] - ay1;
            const float dist_sq = dx * dx + dy * dy;

            // Early skip: if center-to-center distance squared is already
            // larger than (best_clearance + r1 + r2)^2, this pair cannot
            // produce a smaller clearance. This avoids sqrt for most pairs.
            const float r_sum = r1 + pad2_radius[j];
            const float threshold = best_clearance + r_sum;
            if (dist_sq > threshold * threshold) {
                continue;
            }

            // Only compute sqrt for candidates that pass the threshold
            const float dist = std::sqrt(dist_sq);
            const float clearance = dist - r_sum;

            if (clearance < best_clearance) {
                best_clearance = clearance;
                best_i = i;
                best_j = j;
            }
        }
    }

    if (best_i >= 0) {
        result.min_clearance = best_clearance;
        result.location_x = (abs_x1[best_i] + abs_x2[best_j]) / 2.0f;
        result.location_y = (abs_y1[best_i] + abs_y2[best_j]) / 2.0f;
        result.pad1_index = best_i;
        result.pad2_index = best_j;
        result.has_result = true;
    }

    return result;
}

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
) {
    int n1 = static_cast<int>(pad1_local_x.size());
    int n2 = static_cast<int>(pad2_local_x.size());

    return check_pair_clearance(
        pad1_local_x.data(), pad1_local_y.data(),
        pad1_radius.data(), pad1_net.data(),
        n1,
        fp1_x, fp1_y, fp1_rotation_rad,
        pad2_local_x.data(), pad2_local_y.data(),
        pad2_radius.data(), pad2_net.data(),
        n2,
        fp2_x, fp2_y, fp2_rotation_rad
    );
}

} // namespace drc
