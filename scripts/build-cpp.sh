#!/bin/bash
# Build the C++ router extension module
#
# This script builds the nanobind-based C++ router core for 10-100x
# speedup over the pure Python implementation.
#
# Requirements:
#   - C++20 compiler (clang 14+, gcc 11+, MSVC 2022+)
#   - CMake 3.15+
#   - Python 3.10+ with nanobind installed
#
# Usage:
#   ./scripts/build-cpp.sh         # Build and install
#   ./scripts/build-cpp.sh clean   # Clean build artifacts
#   ./scripts/build-cpp.sh check   # Check if C++ backend is available
#
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build"
ROUTER_DIR="$PROJECT_ROOT/src/kicad_tools/router"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

case "${1:-build}" in
    clean)
        echo -e "${YELLOW}Cleaning C++ build artifacts...${NC}"
        rm -rf "$BUILD_DIR"
        rm -f "$ROUTER_DIR"/router_cpp*.so
        rm -f "$ROUTER_DIR"/router_cpp*.pyd
        echo -e "${GREEN}Clean complete${NC}"
        ;;

    check)
        echo "Checking C++ router backend availability..."
        python3 -c "
from kicad_tools.router.cpp_backend import is_cpp_available, get_backend_info
info = get_backend_info()
print(f'Backend: {info[\"backend\"]}')
print(f'Available: {info[\"available\"]}')
if info['available']:
    print(f'Version: {info[\"version\"]}')
" || echo -e "${RED}C++ backend not available${NC}"
        ;;

    build)
        echo -e "${YELLOW}Building C++ router extension...${NC}"

        # Check for nanobind
        if ! python3 -c "import nanobind" 2>/dev/null; then
            echo -e "${RED}Error: nanobind not installed${NC}"
            echo "Install with: pip install nanobind"
            exit 1
        fi

        # Check for cmake
        if ! command -v cmake &> /dev/null; then
            echo -e "${RED}Error: cmake not found${NC}"
            echo "Install CMake 3.15+ to build the C++ extension"
            exit 1
        fi

        # Create build directory
        mkdir -p "$BUILD_DIR"

        # Configure with CMake
        echo "Configuring..."
        cmake -B "$BUILD_DIR" -S "$PROJECT_ROOT" \
            -DCMAKE_BUILD_TYPE=Release \
            -DPYTHON_EXECUTABLE="$(which python3)"

        # Build
        echo "Building..."
        cmake --build "$BUILD_DIR" --config Release -j

        # Find and copy the built module
        echo "Installing..."
        MODULE=$(find "$BUILD_DIR" -name "router_cpp*.so" -o -name "router_cpp*.pyd" | head -1)
        if [ -n "$MODULE" ]; then
            cp "$MODULE" "$ROUTER_DIR/"
            echo -e "${GREEN}C++ router module installed to:${NC}"
            echo "  $ROUTER_DIR/$(basename "$MODULE")"

            # Verify it works
            echo ""
            echo "Verifying installation..."
            "$SCRIPT_DIR/build-cpp.sh" check
        else
            echo -e "${RED}Error: Built module not found${NC}"
            exit 1
        fi
        ;;

    *)
        echo "Usage: $0 [build|clean|check]"
        echo ""
        echo "Commands:"
        echo "  build   Build and install the C++ router module (default)"
        echo "  clean   Remove build artifacts"
        echo "  check   Check if C++ backend is available"
        exit 1
        ;;
esac
