#!/usr/bin/env python3
"""
Functional Clustering Demo

Demonstrates how kicad-tools v0.6.0 detects and groups functionally-related
components like MCU + bypass capacitors and crystal + load capacitors.
"""

from pathlib import Path

from kicad_tools.optim import (
    ClusterType,
    PlacementOptimizer,
    detect_functional_clusters,
)
from kicad_tools.schema.pcb import PCB


def main():
    """Run the clustering demo."""
    # Load the example PCB
    pcb_path = Path(__file__).parent / "fixtures" / "mcu_board.kicad_pcb"
    print(f"Loading PCB: {pcb_path.name}")
    pcb = PCB.load(str(pcb_path))

    # Create optimizer to get component list with net information
    print("Creating placement optimizer...")
    optimizer = PlacementOptimizer.from_pcb(pcb)

    print(f"Found {len(optimizer.components)} components")
    print(f"Found {len(optimizer.springs)} net connections")
    print()

    # Detect functional clusters
    print("=" * 60)
    print("Detecting Functional Clusters")
    print("=" * 60)
    print()

    clusters = detect_functional_clusters(optimizer.components)

    if not clusters:
        print("No functional clusters detected.")
        print("(Clusters require components with proper net connections)")
        return

    # Group clusters by type
    by_type: dict[ClusterType, list] = {}
    for cluster in clusters:
        if cluster.cluster_type not in by_type:
            by_type[cluster.cluster_type] = []
        by_type[cluster.cluster_type].append(cluster)

    # Display clusters by type
    for cluster_type, type_clusters in by_type.items():
        print(f"{cluster_type.value.upper()} Clusters ({len(type_clusters)} found)")
        print("-" * 40)

        for cluster in type_clusters:
            print(f"  Anchor: {cluster.anchor}")
            print(f"  Members: {', '.join(cluster.members)}")
            print(f"  Max distance: {cluster.max_distance_mm}mm")
            print()

    # Summary
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print()
    print("Cluster types detected:")
    for cluster_type, type_clusters in by_type.items():
        description = {
            ClusterType.POWER: "IC + bypass capacitors (keep within 3mm)",
            ClusterType.TIMING: "Crystal + load capacitors (keep within 5mm)",
            ClusterType.INTERFACE: "Connector + ESD/series resistors (keep within 8mm)",
            ClusterType.DRIVER: "Driver IC + gate resistors/diodes (keep within 6mm)",
        }.get(cluster_type, "Unknown")
        print(f"  - {cluster_type.value}: {len(type_clusters)} ({description})")

    print()
    print("Use these clusters during placement optimization with:")
    print("  optimizer = PlacementOptimizer.from_pcb(pcb, enable_clustering=True)")
    print()
    print("Or via CLI:")
    print("  kicad-tools placement optimize board.kicad_pcb --cluster")


if __name__ == "__main__":
    main()
