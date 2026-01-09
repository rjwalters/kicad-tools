"""Synthetic board generators for stress testing.

These generators create challenging routing scenarios without needing
real PCB files, enabling reproducible benchmarks.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router import Autorouter


def generate_bga_breakout(
    pin_count: int = 64,
    pitch: float = 0.8,
    board_margin: float = 5.0,
    seed: int | None = None,
) -> Autorouter:
    """Generate a BGA breakout routing challenge.

    Creates a grid of BGA pads in the center with corresponding edge
    connector pads, requiring escape routing.

    Args:
        pin_count: Total number of pins (will be rounded to nearest square)
        pitch: BGA pin pitch in mm
        board_margin: Margin around components in mm
        seed: Random seed for reproducibility

    Returns:
        Configured Autorouter ready for routing
    """
    from kicad_tools.router import Autorouter, DesignRules
    from kicad_tools.router.layers import Layer

    if seed is not None:
        random.seed(seed)

    # Calculate grid dimensions
    grid_size = int(math.ceil(math.sqrt(pin_count)))
    actual_pins = grid_size * grid_size

    # Calculate board size
    bga_size = grid_size * pitch
    board_width = bga_size + 2 * board_margin + 10  # Extra for edge connector
    board_height = bga_size + 2 * board_margin

    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.15,
        trace_clearance=0.15,
    )

    router = Autorouter(
        width=board_width,
        height=board_height,
        rules=rules,
    )

    # Generate BGA pads in center
    bga_start_x = board_margin
    bga_start_y = board_margin

    net_id = 1
    bga_pads = []

    for row in range(grid_size):
        for col in range(grid_size):
            x = bga_start_x + col * pitch + pitch / 2
            y = bga_start_y + row * pitch + pitch / 2

            pad_info = {
                "number": f"{chr(65 + row)}{col + 1}",  # A1, A2, ...
                "x": x,
                "y": y,
                "width": pitch * 0.5,
                "height": pitch * 0.5,
                "net": net_id,
                "net_name": f"NET_{net_id}",
                "layer": Layer.F_CU,
                "through_hole": False,
            }
            bga_pads.append(pad_info)
            net_id += 1

    router.add_component("U1", bga_pads)

    # Generate edge connector pads on the right side
    connector_x = board_width - board_margin
    connector_pitch = board_height / (actual_pins + 1)

    connector_pads = []
    for i in range(actual_pins):
        y = connector_pitch * (i + 1)

        pad_info = {
            "number": str(i + 1),
            "x": connector_x,
            "y": y,
            "width": 1.0,
            "height": 0.5,
            "net": i + 1,  # Match BGA pin net
            "net_name": f"NET_{i + 1}",
            "layer": Layer.F_CU,
            "through_hole": True,
            "drill": 0.3,
        }
        connector_pads.append(pad_info)

    router.add_component("J1", connector_pads)

    return router


def generate_random_board(
    num_nets: int = 50,
    density: float = 0.6,
    board_width: float = 50.0,
    board_height: float = 50.0,
    seed: int | None = None,
) -> Autorouter:
    """Generate a random routing challenge.

    Creates components with random pad positions and net assignments,
    useful for stress testing routing algorithms.

    Args:
        num_nets: Number of nets to create
        density: Component density (0.0-1.0)
        board_width: Board width in mm
        board_height: Board height in mm
        seed: Random seed for reproducibility

    Returns:
        Configured Autorouter ready for routing
    """
    from kicad_tools.router import Autorouter, DesignRules
    from kicad_tools.router.layers import Layer

    if seed is not None:
        random.seed(seed)

    rules = DesignRules(
        grid_resolution=0.15,
        trace_width=0.2,
        trace_clearance=0.15,
    )

    router = Autorouter(
        width=board_width,
        height=board_height,
        rules=rules,
    )

    # Calculate number of components based on density
    # Assume average of 10 pads per component
    pads_per_component = 10
    total_pads = num_nets * 2  # Minimum 2 pads per net
    num_components = max(2, int(total_pads / pads_per_component * density))

    # Create a pool of net IDs, each used 2-4 times
    net_pool: list[int] = []
    for net_id in range(1, num_nets + 1):
        count = random.randint(2, 4)  # 2-4 pads per net
        net_pool.extend([net_id] * count)
    random.shuffle(net_pool)

    # Distribute pads across components
    pad_index = 0
    margin = 3.0

    for comp_idx in range(num_components):
        # Random component position
        comp_x = random.uniform(margin, board_width - margin)
        comp_y = random.uniform(margin, board_height - margin)

        # Random number of pads (4-16)
        num_pads = random.randint(4, 16)

        # Determine pad layout (grid or linear)
        is_grid = random.random() > 0.5

        pads = []
        if is_grid:
            # Grid layout (like QFP)
            side_pads = int(math.ceil(num_pads / 4))
            pitch = 0.5
            for i in range(num_pads):
                side = i // side_pads
                pos = i % side_pads

                if side == 0:  # Top
                    x = comp_x - (side_pads / 2) * pitch + pos * pitch
                    y = comp_y - side_pads * pitch / 2
                elif side == 1:  # Right
                    x = comp_x + side_pads * pitch / 2
                    y = comp_y - (side_pads / 2) * pitch + pos * pitch
                elif side == 2:  # Bottom
                    x = comp_x + (side_pads / 2) * pitch - pos * pitch
                    y = comp_y + side_pads * pitch / 2
                else:  # Left
                    x = comp_x - side_pads * pitch / 2
                    y = comp_y + (side_pads / 2) * pitch - pos * pitch

                # Assign net from pool
                if pad_index < len(net_pool):
                    net_id = net_pool[pad_index]
                    pad_index += 1
                else:
                    net_id = random.randint(1, num_nets)

                pads.append(
                    {
                        "number": str(i + 1),
                        "x": x,
                        "y": y,
                        "width": 0.4,
                        "height": 0.4,
                        "net": net_id,
                        "net_name": f"NET_{net_id}",
                        "layer": Layer.F_CU,
                        "through_hole": False,
                    }
                )
        else:
            # Linear layout (like connector)
            pitch = 0.6
            for i in range(num_pads):
                x = comp_x
                y = comp_y - (num_pads / 2) * pitch + i * pitch

                if pad_index < len(net_pool):
                    net_id = net_pool[pad_index]
                    pad_index += 1
                else:
                    net_id = random.randint(1, num_nets)

                pads.append(
                    {
                        "number": str(i + 1),
                        "x": x,
                        "y": y,
                        "width": 1.0,
                        "height": 0.4,
                        "net": net_id,
                        "net_name": f"NET_{net_id}",
                        "layer": Layer.F_CU,
                        "through_hole": True,
                        "drill": 0.3,
                    }
                )

        ref = f"U{comp_idx + 1}" if is_grid else f"J{comp_idx + 1}"
        router.add_component(ref, pads)

    return router


def generate_grid_stress_test(
    rows: int = 4,
    cols: int = 4,
    pitch: float = 2.5,
    margin: float = 5.0,
    seed: int | None = None,
) -> Autorouter:
    """Generate a grid of ICs with cross-connected nets.

    Creates a worst-case routing scenario with many crossing nets,
    useful for testing congestion handling.

    Args:
        rows: Number of IC rows
        cols: Number of IC columns
        pitch: Distance between IC centers
        margin: Board margin in mm
        seed: Random seed for reproducibility

    Returns:
        Configured Autorouter ready for routing
    """
    from kicad_tools.router import Autorouter, DesignRules
    from kicad_tools.router.layers import Layer

    if seed is not None:
        random.seed(seed)

    board_width = cols * pitch + 2 * margin
    board_height = rows * pitch + 2 * margin

    rules = DesignRules(
        grid_resolution=0.15,
        trace_width=0.2,
        trace_clearance=0.15,
    )

    router = Autorouter(
        width=board_width,
        height=board_height,
        rules=rules,
    )

    # Create grid of ICs, each with 8 pads
    ic_pads = 8
    net_id = 1
    ic_positions: list[tuple[float, float]] = []

    for row in range(rows):
        for col in range(cols):
            ic_x = margin + col * pitch + pitch / 2
            ic_y = margin + row * pitch + pitch / 2
            ic_positions.append((ic_x, ic_y))

            pads = []
            for i in range(ic_pads):
                angle = 2 * math.pi * i / ic_pads
                pad_x = ic_x + 0.8 * math.cos(angle)
                pad_y = ic_y + 0.8 * math.sin(angle)

                # Net assignment creates cross-connections
                # Pads on opposite corners get same net
                peer_row = rows - 1 - row
                peer_col = cols - 1 - col
                peer_idx = peer_row * cols + peer_col
                current_idx = row * cols + col

                if peer_idx > current_idx:
                    # This IC creates the net
                    assigned_net = net_id
                    net_id += 1
                else:
                    # Connect to peer IC's net
                    assigned_net = (peer_idx * ic_pads) + i + 1

                pads.append(
                    {
                        "number": str(i + 1),
                        "x": pad_x,
                        "y": pad_y,
                        "width": 0.3,
                        "height": 0.3,
                        "net": assigned_net if assigned_net <= net_id else 0,
                        "net_name": f"NET_{assigned_net}" if assigned_net <= net_id else "",
                        "layer": Layer.F_CU,
                        "through_hole": False,
                    }
                )

            router.add_component(f"U{row * cols + col + 1}", pads)

    return router
