# Root CMakeLists.txt for building C++ native extensions
# This is used by scikit-build-core for pip install with native support
#
# Usage:
#   pip install .[native]   # Build with C++ acceleration
#   pip install .           # Pure Python (no C++ build)
#
# Manual build (for development):
#   cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
#   cmake --build build
#   cp build/router_cpp.*.so src/kicad_tools/router/
#   cp build/placement_cpp.*.so src/kicad_tools/placement/
#   cp build/drc_cpp.*.so src/kicad_tools/drc/

cmake_minimum_required(VERSION 3.15...3.27)
project(kicad_tools_native LANGUAGES CXX)

# Include the router C++ module
add_subdirectory(src/kicad_tools/router/cpp)

# Include the placement C++ module
add_subdirectory(src/kicad_tools/placement/cpp)

# Include the DRC C++ module (pad-to-pad clearance checking, issue #1719).
# Sat unbuilt for the whole project history: this directory was never added
# as a subdirectory of the root project, so `kct build-native` never
# compiled or installed it and the DRC clearance hot loop always ran the
# pure-Python fallback -- see issue #5240.
add_subdirectory(src/kicad_tools/drc/cpp)
