"""
Standard dimensions for common IC packages.

These dimensions are based on JEDEC and IPC-7351 standards.
"""

# SOIC (Small Outline Integrated Circuit) - JEDEC MS-012
# Narrow body (3.9mm) packages
SOIC_STANDARDS = {
    8: {"pitch": 1.27, "body_width": 3.9, "body_length": 4.9, "pad_width": 1.95, "pad_height": 0.6},
    14: {
        "pitch": 1.27,
        "body_width": 3.9,
        "body_length": 8.65,
        "pad_width": 1.95,
        "pad_height": 0.6,
    },
    16: {
        "pitch": 1.27,
        "body_width": 3.9,
        "body_length": 9.9,
        "pad_width": 1.95,
        "pad_height": 0.6,
    },
    18: {
        "pitch": 1.27,
        "body_width": 7.5,
        "body_length": 11.55,
        "pad_width": 1.95,
        "pad_height": 0.6,
    },
    20: {
        "pitch": 1.27,
        "body_width": 7.5,
        "body_length": 12.8,
        "pad_width": 1.95,
        "pad_height": 0.6,
    },
    24: {
        "pitch": 1.27,
        "body_width": 7.5,
        "body_length": 15.4,
        "pad_width": 1.95,
        "pad_height": 0.6,
    },
    28: {
        "pitch": 1.27,
        "body_width": 7.5,
        "body_length": 17.9,
        "pad_width": 1.95,
        "pad_height": 0.6,
    },
}

# TSSOP (Thin Shrink Small Outline Package) - JEDEC MO-153
TSSOP_STANDARDS = {
    8: {"pitch": 0.65, "body_width": 3.0, "body_length": 3.0, "pad_width": 1.5, "pad_height": 0.4},
    14: {"pitch": 0.65, "body_width": 4.4, "body_length": 5.0, "pad_width": 1.5, "pad_height": 0.4},
    16: {"pitch": 0.65, "body_width": 4.4, "body_length": 5.0, "pad_width": 1.5, "pad_height": 0.4},
    20: {"pitch": 0.65, "body_width": 4.4, "body_length": 6.5, "pad_width": 1.5, "pad_height": 0.4},
    24: {"pitch": 0.65, "body_width": 4.4, "body_length": 7.8, "pad_width": 1.5, "pad_height": 0.4},
    28: {"pitch": 0.65, "body_width": 4.4, "body_length": 9.7, "pad_width": 1.5, "pad_height": 0.4},
}

# LQFP (Low-profile Quad Flat Package) - JEDEC MS-026
LQFP_STANDARDS = {
    32: {"pitch": 0.8, "body_size": 7.0, "pad_width": 1.5, "pad_height": 0.55},
    44: {"pitch": 0.8, "body_size": 10.0, "pad_width": 1.5, "pad_height": 0.55},
    48: {"pitch": 0.5, "body_size": 7.0, "pad_width": 1.2, "pad_height": 0.3},
    64: {"pitch": 0.5, "body_size": 10.0, "pad_width": 1.2, "pad_height": 0.3},
    80: {"pitch": 0.5, "body_size": 12.0, "pad_width": 1.2, "pad_height": 0.3},
    100: {"pitch": 0.5, "body_size": 14.0, "pad_width": 1.2, "pad_height": 0.3},
    144: {"pitch": 0.5, "body_size": 20.0, "pad_width": 1.2, "pad_height": 0.3},
}

# QFN (Quad Flat No-lead) - common sizes
QFN_STANDARDS = {
    # (pins, body_size): specs
    (8, 2.0): {"pitch": 0.5, "pad_width": 0.8, "pad_height": 0.3, "exposed_pad": 0.9},
    (16, 3.0): {"pitch": 0.5, "pad_width": 0.8, "pad_height": 0.3, "exposed_pad": 1.7},
    (20, 4.0): {"pitch": 0.5, "pad_width": 0.8, "pad_height": 0.3, "exposed_pad": 2.4},
    (24, 4.0): {"pitch": 0.5, "pad_width": 0.8, "pad_height": 0.3, "exposed_pad": 2.4},
    (32, 5.0): {"pitch": 0.5, "pad_width": 0.8, "pad_height": 0.3, "exposed_pad": 3.4},
    (48, 7.0): {"pitch": 0.5, "pad_width": 0.8, "pad_height": 0.3, "exposed_pad": 5.2},
}

# SOT (Small Outline Transistor) packages
SOT_STANDARDS = {
    "SOT-23": {
        "pins": 3,
        "pitch": 0.95,
        "body_width": 1.3,
        "body_length": 2.9,
        "pad_width": 1.0,
        "pad_height": 0.6,
        "pad_positions": [(-0.95, -1.0), (0.95, -1.0), (0, 1.0)],  # Pin 1, 2, 3
    },
    "SOT-23-5": {
        "pins": 5,
        "pitch": 0.95,
        "body_width": 1.6,
        "body_length": 2.9,
        "pad_width": 1.06,
        "pad_height": 0.6,
        "pad_positions": [
            (-0.95, -1.3),
            (0, -1.3),
            (0.95, -1.3),  # Pins 1, 2, 3
            (0.95, 1.3),
            (-0.95, 1.3),  # Pins 4, 5
        ],
    },
    "SOT-23-6": {
        "pins": 6,
        "pitch": 0.95,
        "body_width": 1.6,
        "body_length": 2.9,
        "pad_width": 1.06,
        "pad_height": 0.6,
        "pad_positions": [
            (-0.95, -1.3),
            (0, -1.3),
            (0.95, -1.3),  # Pins 1, 2, 3
            (0.95, 1.3),
            (0, 1.3),
            (-0.95, 1.3),  # Pins 4, 5, 6
        ],
    },
    "SOT-223": {
        "pins": 4,  # 3 small + 1 large tab
        "pitch": 2.3,
        "body_width": 3.5,
        "body_length": 6.5,
        "pad_width": 1.6,
        "pad_height": 0.9,
        "tab_width": 3.0,
        "tab_height": 1.6,
        "pad_positions": [
            (-2.3, 3.15),
            (0, 3.15),
            (2.3, 3.15),  # Pins 1, 2, 3
            (0, -3.15),  # Tab (pin 4)
        ],
    },
    "SOT-89": {
        "pins": 3,
        "pitch": 1.5,
        "body_width": 2.5,
        "body_length": 4.5,
        "pad_width": 1.5,
        "pad_height": 0.6,
        "tab_width": 1.5,
        "tab_height": 2.0,
        "pad_positions": [
            (-1.5, 2.05),
            (0, 2.05),
            (1.5, 2.05),  # Pins 1, 2, 3
        ],
    },
}

# Chip components (resistors, capacitors, etc.)
# Imperial size -> metric size (mm)
CHIP_SIZES = {
    "0201": {
        "length": 0.6,
        "width": 0.3,
        "pad_width": 0.4,
        "pad_height": 0.35,
        "pad_gap": 0.3,
        "metric": "0603",
    },
    "0402": {
        "length": 1.0,
        "width": 0.5,
        "pad_width": 0.6,
        "pad_height": 0.55,
        "pad_gap": 0.5,
        "metric": "1005",
    },
    "0603": {
        "length": 1.6,
        "width": 0.8,
        "pad_width": 0.9,
        "pad_height": 0.95,
        "pad_gap": 0.8,
        "metric": "1608",
    },
    "0805": {
        "length": 2.0,
        "width": 1.25,
        "pad_width": 1.0,
        "pad_height": 1.35,
        "pad_gap": 1.0,
        "metric": "2012",
    },
    "1206": {
        "length": 3.2,
        "width": 1.6,
        "pad_width": 1.15,
        "pad_height": 1.8,
        "pad_gap": 1.8,
        "metric": "3216",
    },
    "1210": {
        "length": 3.2,
        "width": 2.5,
        "pad_width": 1.15,
        "pad_height": 2.7,
        "pad_gap": 1.8,
        "metric": "3225",
    },
    "1812": {
        "length": 4.5,
        "width": 3.2,
        "pad_width": 1.3,
        "pad_height": 3.4,
        "pad_gap": 2.6,
        "metric": "4532",
    },
    "2010": {
        "length": 5.0,
        "width": 2.5,
        "pad_width": 1.3,
        "pad_height": 2.7,
        "pad_gap": 3.0,
        "metric": "5025",
    },
    "2512": {
        "length": 6.3,
        "width": 3.2,
        "pad_width": 1.5,
        "pad_height": 3.4,
        "pad_gap": 4.0,
        "metric": "6332",
    },
}

# BGA (Ball Grid Array) - common sizes
BGA_STANDARDS = {
    # Package name: specs
    "BGA-256_17x17_0.8mm": {
        "rows": 16,
        "cols": 16,
        "pitch": 0.8,
        "ball_diameter": 0.4,
        "body_size": 17.0,
    },
    "BGA-324_18x18_0.8mm": {
        "rows": 18,
        "cols": 18,
        "pitch": 0.8,
        "ball_diameter": 0.4,
        "body_size": 19.0,
    },
    "BGA-484_22x22_1.0mm": {
        "rows": 22,
        "cols": 22,
        "pitch": 1.0,
        "ball_diameter": 0.5,
        "body_size": 23.0,
    },
    "BGA-100_10x10_0.8mm": {
        "rows": 10,
        "cols": 10,
        "pitch": 0.8,
        "ball_diameter": 0.4,
        "body_size": 12.0,
    },
    "BGA-144_12x12_0.8mm": {
        "rows": 12,
        "cols": 12,
        "pitch": 0.8,
        "ball_diameter": 0.4,
        "body_size": 14.0,
    },
    # Fine-pitch BGA (FBGA)
    "FBGA-256_17x17_0.5mm": {
        "rows": 16,
        "cols": 16,
        "pitch": 0.5,
        "ball_diameter": 0.25,
        "body_size": 14.0,
    },
}

# DFN (Dual Flat No-lead) - common sizes
DFN_STANDARDS = {
    # Package name: specs (for create_dfn_standard)
    "DFN-8_3x3_0.5mm": {
        "pins": 8,
        "pitch": 0.5,
        "body_width": 3.0,
        "body_length": 3.0,
        "pad_width": 0.8,
        "pad_height": 0.3,
        "exposed_pad": (1.5, 2.0),
    },
    "DFN-6_2x2_0.65mm": {
        "pins": 6,
        "pitch": 0.65,
        "body_width": 2.0,
        "body_length": 2.0,
        "pad_width": 0.7,
        "pad_height": 0.35,
        "exposed_pad": (0.9, 1.2),
    },
    "DFN-8_2x3_0.5mm": {
        "pins": 8,
        "pitch": 0.5,
        "body_width": 2.0,
        "body_length": 3.0,
        "pad_width": 0.7,
        "pad_height": 0.3,
        "exposed_pad": (0.9, 1.7),
    },
    "DFN-10_3x3_0.5mm": {
        "pins": 10,
        "pitch": 0.5,
        "body_width": 3.0,
        "body_length": 3.0,
        "pad_width": 0.8,
        "pad_height": 0.25,
        "exposed_pad": (1.5, 2.0),
    },
    "DFN-12_4x4_0.5mm": {
        "pins": 12,
        "pitch": 0.5,
        "body_width": 4.0,
        "body_length": 4.0,
        "pad_width": 0.8,
        "pad_height": 0.25,
        "exposed_pad": (2.4, 2.8),
    },
    "DFN-16_5x5_0.5mm": {
        "pins": 16,
        "pitch": 0.5,
        "body_width": 5.0,
        "body_length": 5.0,
        "pad_width": 0.8,
        "pad_height": 0.25,
        "exposed_pad": (3.2, 3.6),
    },
    # Keyed by tuple for lookup in create_dfn
    (8, 3.0, 3.0): {
        "pitch": 0.5,
        "pad_width": 0.8,
        "pad_height": 0.3,
        "exposed_pad": (1.5, 2.0),
    },
    (6, 2.0, 2.0): {
        "pitch": 0.65,
        "pad_width": 0.7,
        "pad_height": 0.35,
        "exposed_pad": (0.9, 1.2),
    },
}

# DIP (Dual In-line Package) standards
DIP_STANDARDS = {
    # Narrow DIP (0.3" = 7.62mm row spacing)
    8: {"pitch": 2.54, "row_spacing": 7.62, "pad_diameter": 1.6, "drill": 0.8},
    14: {"pitch": 2.54, "row_spacing": 7.62, "pad_diameter": 1.6, "drill": 0.8},
    16: {"pitch": 2.54, "row_spacing": 7.62, "pad_diameter": 1.6, "drill": 0.8},
    18: {"pitch": 2.54, "row_spacing": 7.62, "pad_diameter": 1.6, "drill": 0.8},
    20: {"pitch": 2.54, "row_spacing": 7.62, "pad_diameter": 1.6, "drill": 0.8},
    # Wide DIP (0.6" = 15.24mm row spacing)
    24: {"pitch": 2.54, "row_spacing": 15.24, "pad_diameter": 1.6, "drill": 0.8},
    28: {"pitch": 2.54, "row_spacing": 15.24, "pad_diameter": 1.6, "drill": 0.8},
    40: {"pitch": 2.54, "row_spacing": 15.24, "pad_diameter": 1.6, "drill": 0.8},
}
