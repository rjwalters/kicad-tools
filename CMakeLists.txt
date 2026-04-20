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

cmake_minimum_required(VERSION 3.15...3.27)
project(kicad_tools_native LANGUAGES CXX)

# Include the router C++ module
add_subdirectory(src/kicad_tools/router/cpp)

# Include the placement C++ module
add_subdirectory(src/kicad_tools/placement/cpp)
