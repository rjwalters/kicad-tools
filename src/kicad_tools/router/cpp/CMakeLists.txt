cmake_minimum_required(VERSION 3.15...3.27)
set(PROJECT_NAME router_cpp)

project(${PROJECT_NAME} LANGUAGES CXX)

# Python and nanobind setup
if(SKBUILD)
    set(Python_EXECUTABLE "${PYTHON_EXECUTABLE}")
    set(Python_INCLUDE_DIR "${PYTHON_INCLUDE_DIR}")
    set(Python_LIBRARY "${PYTHON_LIBRARY}")
endif()

if(CMAKE_VERSION VERSION_LESS 3.18)
    set(DEV_MODULE Development)
else()
    set(DEV_MODULE Development.Module)
endif()

find_package(Python COMPONENTS Interpreter ${DEV_MODULE} REQUIRED)
execute_process(
    COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
    OUTPUT_STRIP_TRAILING_WHITESPACE OUTPUT_VARIABLE nanobind_ROOT)
find_package(nanobind CONFIG REQUIRED)

message(STATUS "Python_EXECUTABLE: ${Python_EXECUTABLE}")
message(STATUS "nanobind_ROOT: ${nanobind_ROOT}")

# Build type
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Choose the type of build." FORCE)
    set_property(CACHE CMAKE_BUILD_TYPE PROPERTY STRINGS "Debug" "Release" "MinSizeRel" "RelWithDebInfo")
endif()

# C++ standard
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Compiler flags
if(MSVC)
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} /std:c++20 /Zc:__cplusplus /O2")
else()
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -O2")
endif()

# Source files
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)
file(GLOB_RECURSE SOURCE_FILES "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp")

# Vendored poly2tri (BSD-3) constrained-Delaunay mesher (issue #4268).
# poly2tri sources are ``.cc`` (not picked up by the ``src/*.cpp`` glob), so
# they are added explicitly.  Its headers include each other relative to the
# ``poly2tri/`` directory (e.g. ``#include "common/shapes.h"``), so that
# directory is the include root.  ``P2T_STATIC_EXPORTS`` neutralises the
# DLL import/export decoration for a static in-module link.
set(POLY2TRI_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/third_party/poly2tri/poly2tri)
include_directories(${POLY2TRI_ROOT})
file(GLOB_RECURSE POLY2TRI_SOURCES "${POLY2TRI_ROOT}/*.cc")

# Build nanobind module
nanobind_add_module(${PROJECT_NAME} ${SOURCE_FILES} ${POLY2TRI_SOURCES})
target_compile_features(${PROJECT_NAME} PRIVATE cxx_std_20)
target_compile_definitions(${PROJECT_NAME} PRIVATE P2T_STATIC_EXPORTS)

install(TARGETS ${PROJECT_NAME} DESTINATION kicad_tools/router)

message(STATUS "CMAKE_CXX_FLAGS: ${CMAKE_CXX_FLAGS}")
