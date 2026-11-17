/*
 * Placement C++ Core - Force-directed placement engine implementation
 *
 * Implements the full N^2 pairwise edge-to-edge repulsion loop and
 * board boundary force loop in C++ for maximum performance.
 */

#include "force_engine.hpp"

namespace placement {

ForceResult compute_all_repulsion(
    const std::vector<double>& positions_x,
    const std::vector<double>& positions_y,
    const std::vector<double>& edges_flat,
    const std::vector<int>& edge_offsets,
    size_t n_components,
    const ForceConfig& config,
    const std::vector<bool>& fixed_mask) {

    ForceResult result;
    result.forces_x.resize(n_components, 0.0);
    result.forces_y.resize(n_components, 0.0);
    result.torques.resize(n_components, 0.0);

    for (size_t i = 0; i < n_components; ++i) {
        for (size_t j = i + 1; j < n_components; ++j) {
            // Skip if both components are fixed
            if (fixed_mask[i] && fixed_mask[j]) continue;

            int i_start = edge_offsets[i];
            int i_end = edge_offsets[i + 1];
            int j_start = edge_offsets[j];
            int j_end = edge_offsets[j + 1];

            // Compute forces on comp_i from comp_j edges
            if (!fixed_mask[i]) {
                for (int ei = i_start; ei < i_end; ++ei) {
                    double e1_sx = edges_flat[ei * 4 + 0];
                    double e1_sy = edges_flat[ei * 4 + 1];
                    double e1_ex = edges_flat[ei * 4 + 2];
                    double e1_ey = edges_flat[ei * 4 + 3];

                    for (int ej = j_start; ej < j_end; ++ej) {
                        double e2_sx = edges_flat[ej * 4 + 0];
                        double e2_sy = edges_flat[ej * 4 + 1];
                        double e2_ex = edges_flat[ej * 4 + 2];
                        double e2_ey = edges_flat[ej * 4 + 3];

                        double fx, fy, edge_torque;
                        compute_edge_to_edge_force(
                            e1_sx, e1_sy, e1_ex, e1_ey,
                            e2_sx, e2_sy, e2_ex, e2_ey,
                            config, fx, fy, edge_torque);

                        result.forces_x[i] += fx;
                        result.forces_y[i] += fy;

                        // Convert edge torque to component torque
                        double edge_center_x = (e1_sx + e1_ex) * 0.5;
                        double edge_center_y = (e1_sy + e1_ey) * 0.5;
                        double rx = edge_center_x - positions_x[i];
                        double ry = edge_center_y - positions_y[i];
                        // r x F = rx*fy - ry*fx
                        result.torques[i] += rx * fy - ry * fx + edge_torque;
                    }
                }
            }

            // Symmetric: forces on comp_j from comp_i edges
            if (!fixed_mask[j]) {
                for (int ej = j_start; ej < j_end; ++ej) {
                    double e2_sx = edges_flat[ej * 4 + 0];
                    double e2_sy = edges_flat[ej * 4 + 1];
                    double e2_ex = edges_flat[ej * 4 + 2];
                    double e2_ey = edges_flat[ej * 4 + 3];

                    for (int ei = i_start; ei < i_end; ++ei) {
                        double e1_sx = edges_flat[ei * 4 + 0];
                        double e1_sy = edges_flat[ei * 4 + 1];
                        double e1_ex = edges_flat[ei * 4 + 2];
                        double e1_ey = edges_flat[ei * 4 + 3];

                        double fx, fy, edge_torque;
                        compute_edge_to_edge_force(
                            e2_sx, e2_sy, e2_ex, e2_ey,
                            e1_sx, e1_sy, e1_ex, e1_ey,
                            config, fx, fy, edge_torque);

                        result.forces_x[j] += fx;
                        result.forces_y[j] += fy;

                        double edge_center_x = (e2_sx + e2_ex) * 0.5;
                        double edge_center_y = (e2_sy + e2_ey) * 0.5;
                        double rx = edge_center_x - positions_x[j];
                        double ry = edge_center_y - positions_y[j];
                        result.torques[j] += rx * fy - ry * fx + edge_torque;
                    }
                }
            }
        }
    }

    return result;
}

ForceResult compute_boundary_forces(
    const std::vector<double>& positions_x,
    const std::vector<double>& positions_y,
    const std::vector<double>& edges_flat,
    const std::vector<int>& edge_offsets,
    const std::vector<double>& board_edges,
    size_t n_board_edges,
    size_t n_components,
    const ForceConfig& config,
    const std::vector<bool>& fixed_mask,
    const std::vector<bool>& inside_flags) {

    ForceResult result;
    result.forces_x.resize(n_components, 0.0);
    result.forces_y.resize(n_components, 0.0);
    result.torques.resize(n_components, 0.0);

    double scale = config.boundary_charge / config.charge_density;

    for (size_t i = 0; i < n_components; ++i) {
        if (fixed_mask[i]) continue;

        int i_start = edge_offsets[i];
        int i_end = edge_offsets[i + 1];

        for (int ei = i_start; ei < i_end; ++ei) {
            double e_sx = edges_flat[ei * 4 + 0];
            double e_sy = edges_flat[ei * 4 + 1];
            double e_ex = edges_flat[ei * 4 + 2];
            double e_ey = edges_flat[ei * 4 + 3];

            for (size_t bi = 0; bi < n_board_edges; ++bi) {
                double b_sx = board_edges[bi * 4 + 0];
                double b_sy = board_edges[bi * 4 + 1];
                double b_ex = board_edges[bi * 4 + 2];
                double b_ey = board_edges[bi * 4 + 3];

                double fx, fy, edge_torque;
                compute_edge_to_edge_force(
                    e_sx, e_sy, e_ex, e_ey,
                    b_sx, b_sy, b_ex, b_ey,
                    config, fx, fy, edge_torque);

                double applied_scale;
                if (inside_flags[i]) {
                    applied_scale = scale;
                } else {
                    // Strong repulsion to push back inside
                    applied_scale = -scale * 10.0;
                }

                fx *= applied_scale;
                fy *= applied_scale;

                result.forces_x[i] += fx;
                result.forces_y[i] += fy;

                double edge_center_x = (e_sx + e_ex) * 0.5;
                double edge_center_y = (e_sy + e_ey) * 0.5;
                double rx = edge_center_x - positions_x[i];
                double ry = edge_center_y - positions_y[i];
                result.torques[i] += rx * fy - ry * fx + edge_torque * scale;
            }
        }
    }

    return result;
}

}  // namespace placement
