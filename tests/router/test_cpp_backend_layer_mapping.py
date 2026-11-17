"""C++ backend pad-layer-to-index mapping invariant.

Issue #3304: The C++ ``_route_impl`` (and the legacy
``find_blocking_nets`` Bresenham path) historically computed the
search-time start/end layer index as ``pad.layer.value % num_layers``.
The modulo trick works for ``F.Cu`` (value=0 maps to index 0 on every
stack) and for ``B.Cu`` only on 6-layer stacks (5 % 6 == 5), but it
mismaps ``B.Cu`` on every other layer count:

  - 2-layer: 5 % 2 == 1  ✓ (also correct)
  - 4-layer: 5 % 4 == 1  ✗ (should be 3; B.Cu is at index 3)
  - 6-layer: 5 % 6 == 5  ✓

On 4-layer boards the bug made the A* terminate on ``In1.Cu`` (index 1)
whenever the destination virtual-pad was on ``B.Cu``.  The escape route
laid its inner stub on B.Cu but the main router ended on In1.Cu --
same XY, different layer, no via to bridge the gap.  The union-find
connectivity check then counted the pad as disconnected.

This test pins the invariant: for every supported layer stack, the
mapping the C++ backend uses (``self._grid.layer_to_index`` after the
#3304 fix) must agree with the Python pathfinder's mapping (the
canonical reference at ``pathfinder.py`` line 2348) and must NOT
produce the wrong index for ``B.Cu`` on 4-layer boards.

The board-03 regression test (``test_board03_routing_baseline.py``)
covers the end-to-end behavioural consequence; this test pins the
underlying invariant so future refactors of the layer-indexing path
do not silently re-introduce the modulo-arithmetic regression.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.rules import DesignRules

_STACKS = [
    ("two_layer", LayerStack.two_layer()),
    ("four_layer_sig_gnd_pwr_sig", LayerStack.four_layer_sig_gnd_pwr_sig()),
    ("four_layer_all_signal", LayerStack.four_layer_all_signal()),
    ("six_layer_sig_gnd_sig_sig_pwr_sig", LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
]


@pytest.mark.parametrize("stack_name,stack", _STACKS)
def test_b_cu_maps_to_outer_bottom_index(stack_name, stack):
    """``B.Cu`` always maps to the LAST grid index on every stack.

    This is the invariant the modulo-arithmetic regression
    (``B.Cu.value % num_layers``) violated on 4-layer boards.
    """
    rules = DesignRules()
    grid = RoutingGrid(width=60, height=40, rules=rules, layer_stack=stack)
    expected_index = stack.num_layers - 1
    actual_index = grid.layer_to_index(Layer.B_CU.value)
    assert actual_index == expected_index, (
        f"On stack {stack_name!r} (num_layers={stack.num_layers}), "
        f"B.Cu should map to grid index {expected_index} (the last layer) "
        f"but layer_to_index gave {actual_index}.  This is the index the "
        f"C++ A* search uses as end_layer when the destination virtual-pad "
        f"sits on B.Cu (the inner-escape-layer fallback the escape "
        f"generator picks when no inner SIGNAL layer is available)."
    )


@pytest.mark.parametrize("stack_name,stack", _STACKS)
def test_f_cu_maps_to_index_zero(stack_name, stack):
    """``F.Cu`` always maps to grid index 0 (the first / top outer layer)."""
    rules = DesignRules()
    grid = RoutingGrid(width=60, height=40, rules=rules, layer_stack=stack)
    actual_index = grid.layer_to_index(Layer.F_CU.value)
    assert actual_index == 0, (
        f"On stack {stack_name!r}, F.Cu should map to grid index 0 "
        f"but layer_to_index gave {actual_index}."
    )


def test_modulo_trick_disagrees_with_layer_to_index_on_4l():
    """Document the divergence the #3304 fix closes.

    The historical ``layer.value % num_layers`` shortcut produced index
    ``1`` (In1.Cu's slot in the 4L SIG-GND-PWR-SIG stack) when the
    correct answer is ``3`` (B.Cu's slot).  This test pins the
    regression scenario so future readers can locate the bug-and-fix
    pair in code archaeology without needing to bisect.
    """
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    rules = DesignRules()
    grid = RoutingGrid(width=60, height=40, rules=rules, layer_stack=stack)

    modulo_index = Layer.B_CU.value % grid.num_layers
    correct_index = grid.layer_to_index(Layer.B_CU.value)

    assert modulo_index == 1
    assert correct_index == 3
    assert modulo_index != correct_index, (
        "If this assertion ever fires it means the modulo shortcut "
        "and ``layer_to_index`` have come back into accidental "
        "agreement on 4L SIG-GND-PWR-SIG -- delete this test, but "
        "first verify the test_b_cu_maps_to_outer_bottom_index "
        "parametrization above is still passing for ALL stacks."
    )


@pytest.mark.parametrize("stack_name,stack", _STACKS)
def test_inner_layer_round_trip(stack_name, stack):
    """For 4L/6L stacks, ``In1.Cu`` maps to a non-zero, non-outer index."""
    if stack.num_layers < 3:
        pytest.skip("Inner layers don't exist on 2-layer stacks")
    rules = DesignRules()
    grid = RoutingGrid(width=60, height=40, rules=rules, layer_stack=stack)
    in1_index = grid.layer_to_index(Layer.IN1_CU.value)
    assert 0 < in1_index < stack.num_layers - 1, (
        f"On stack {stack_name!r}, In1.Cu should be an INNER layer "
        f"(index strictly between 0 and {stack.num_layers - 1}); got {in1_index}."
    )
