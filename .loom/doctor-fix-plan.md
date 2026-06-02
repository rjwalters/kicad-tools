PR #3193 rebase plan (post-#3192-merge):

DROP (duplicated by #3192 already on main):
- src/kicad_tools/router/cpp/include/pathfinder.hpp (C++ tie-break)
- src/kicad_tools/router/cpp/include/types.hpp (C++ AStarNode operator>)
- src/kicad_tools/router/cpp/src/pathfinder.cpp (counter plumbing)
- src/kicad_tools/router/cpp_backend.py (_REQUIRED_CPP_BUILD_VERSION)
- src/kicad_tools/router/pathfinder.py (Python AStarNode + route_bidirectional)
- tests/test_router_determinism.py (lines 428-617 append; preserve original 427)

KEEP (legitimately #3146-specific):
- .github/routed-drc-tolerance.yml (board-07 floor 48→26 + rationale)
- .github/workflows/ci.yml (board-07 soft-fail removal, kct build-native, PYTHONHASHSEED=42)
- boards/07-matchgroup-test/generate_design.py (subprocess PYTHONHASHSEED=42)
- boards/07-matchgroup-test/diagnostic-runs/* (evidence)

ACTIONS:
1. Reset C++ + pathfinder.py + cpp_backend.py to origin/main versions
2. Reset tests/test_router_determinism.py to origin/main version
3. Regenerate board 07 routed PCB against main's seq tie-break
4. Re-validate matchgroup gate at floor 26 (or whatever new det count is)
5. Update diagnostic-runs/README.md if md5 changed
6. Single squashed commit replacing the two existing ones
