// Standalone executable linked to production geometry.cpp with fast FP math.
#include "geometry.hpp"
#include <cstdlib>
#include <iomanip>
#include <iostream>

int main(int argc, char** argv) {
    if (argc != 9) return 2;
    float p[8];
    for (int i = 0; i < 8; ++i) p[i] = std::strtof(argv[i + 1], nullptr);
    std::cout << router::segments_intersect(p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7])
              << ' ' << std::setprecision(10)
              << router::segment_to_segment_distance(p[0], p[1], p[2], p[3],
                                                     p[4], p[5], p[6], p[7]) << '\n';
}
