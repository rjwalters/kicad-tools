"""Native KiCad worker: read original-context per-UUID material, never save/refill.

Run with the Python interpreter belonging to the selected KiCad installation.
Only stdlib and pcbnew are imported so the worker can run outside the kct venv.
"""

import hashlib
import json
import sys
from pathlib import Path


def pad_plot_polygon(pcbnew, item, layer, is_mask, margin, max_error):
    """Mirror KiCad 10 plotPadLayer on an in-memory copy, retaining context.

    Source: KiCad 10.0.5 pcbnew/plot_board_layers.cpp. SetSize on the
    requested mask layer is intentional: custom padstacks resolve const
    reads through their outer copper layer, exactly as the native plotter.
    No authored board object or file is modified.
    """
    poly = pcbnew.SHAPE_POLY_SET()
    if is_mask and item.GetBoard().GetDesignSettings().m_SolderMaskMinWidth:
        item.TransformShapeToPolygon(poly, layer, margin, max_error, pcbnew.ERROR_OUTSIDE)
        return poly
    shape = item.GetShape(layer)
    advanced = shape in (pcbnew.PAD_SHAPE_CUSTOM, pcbnew.PAD_SHAPE_CHAMFERED_RECT)
    error = max_error if advanced else 1
    if not is_mask:
        item.TransformShapeToPolygon(poly, layer, 0, error, pcbnew.ERROR_INSIDE)
        return poly
    dummy = pcbnew.PAD(item)
    size = item.GetSize(layer)
    enlarged = pcbnew.VECTOR2I(size.x + 2 * margin, size.y + 2 * margin)
    if shape == pcbnew.PAD_SHAPE_CUSTOM:
        item.MergePrimitivesAsPolygon(layer, poly)
        poly.InflateWithLinkedHoles(margin, pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, max_error)
        dummy.DeletePrimitivesList()
        dummy.AddPrimitivePoly(layer, poly, 0, True)
        if margin < 0:
            dummy.SetSize(layer, pcbnew.VECTOR2I(max(0, enlarged.x), max(0, enlarged.y)))
        result = pcbnew.SHAPE_POLY_SET()
        dummy.TransformShapeToPolygon(result, layer, 0, max_error, pcbnew.ERROR_INSIDE)
        return result
    if shape == pcbnew.PAD_SHAPE_CHAMFERED_RECT and margin > 0:
        item.TransformShapeToPolygon(poly, layer, margin, max_error, pcbnew.ERROR_INSIDE)
        return poly
    if enlarged.x <= 0 or enlarged.y <= 0:
        return poly
    radius = item.GetRoundRectCornerRadius(layer) if shape == pcbnew.PAD_SHAPE_ROUNDRECT else 0
    dummy.SetSize(layer, enlarged)
    if shape == pcbnew.PAD_SHAPE_RECT and margin > 0:
        dummy.SetShape(layer, pcbnew.PAD_SHAPE_ROUNDRECT)
        dummy.SetRoundRectCornerRadius(layer, margin)
    elif shape == pcbnew.PAD_SHAPE_ROUNDRECT:
        dummy.SetRoundRectCornerRadius(layer, max(0, radius + margin))
    dummy.TransformShapeToPolygon(poly, layer, 0, error, pcbnew.ERROR_INSIDE)
    return poly


def main():
    import pcbnew

    path = Path(sys.argv[1])
    board = pcbnew.LoadBoard(str(path))
    request = json.loads(Path(sys.argv[2]).read_text())
    layers = {
        "F.Cu": pcbnew.F_Cu,
        "B.Cu": pcbnew.B_Cu,
        "F.Mask": pcbnew.F_Mask,
        "B.Mask": pcbnew.B_Mask,
    }
    objects = (
        list(board.GetPads())
        + list(board.GetTracks())
        + list(board.GetDrawings())
        + list(board.Zones())
    )
    for footprint in board.GetFootprints():
        objects.extend(footprint.GraphicalItems())
        if hasattr(footprint, "GetFields"):
            objects.extend(footprint.GetFields())
        else:
            objects.extend([footprint.Reference(), footprint.Value()])
    by_uuid = {}
    for item in objects:
        identity = item.m_Uuid.AsString()
        by_uuid.setdefault(identity, []).append(item)
    result = {
        "schema": "kct.native-mask-objects.v1",
        "native_version": pcbnew.GetBuildVersion(),
        "objects": {},
        "errors": [],
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "max_error_mm": board.GetDesignSettings().m_MaxError / 1e6,
    }
    for identity in request["source_uuids"]:
        found = by_uuid.get(identity, [])
        if len(found) != 1:
            result["errors"].append(f"{identity}: missing/ambiguous native object")
            continue
        item = found[0]
        entry = {
            "kind": type(item).__name__,
            "layers": {},
            "margin_mm": {},
            "net": item.GetNetname() if hasattr(item, "GetNetname") else "",
        }
        for name, layer in layers.items():
            on_layer = item.IsOnLayer(layer)
            if isinstance(item, pcbnew.PCB_VIA) and name.endswith(".Mask"):
                on_layer = item.IsOnLayer(
                    layers[name.replace(".Mask", ".Cu")]
                ) and not item.IsTented(layer)
            if isinstance(item, pcbnew.PAD) and name.endswith(".Mask"):
                on_layer = on_layer and not item.IsTented(layer)
            if name.endswith(".Cu") and hasattr(item, "FlashLayer"):
                on_layer = on_layer and item.FlashLayer(layer)
            if not on_layer:
                continue
            if hasattr(item, "IsVisible") and not item.IsVisible():
                continue
            poly = pcbnew.SHAPE_POLY_SET()
            try:
                margin = 0
                if name.endswith(".Mask") and isinstance(item, (pcbnew.PAD, pcbnew.PCB_TRACK)):
                    margin = (
                        item.GetSolderMaskExpansion(layer)
                        if isinstance(item, pcbnew.PAD)
                        else item.GetSolderMaskExpansion()
                    )
                if isinstance(item, pcbnew.PAD):
                    poly = pad_plot_polygon(
                        pcbnew,
                        item,
                        layer,
                        name.endswith(".Mask"),
                        margin,
                        board.GetDesignSettings().m_MaxError,
                    )
                elif isinstance(item, pcbnew.ZONE):
                    item.TransformSolidAreasShapesToPolygon(layer, poly)
                elif isinstance(item, pcbnew.PCB_TEXT):
                    item.TransformTextToPolySet(poly, 0, 1, pcbnew.ERROR_INSIDE)
                elif hasattr(item, "TransformShapeToPolygon"):
                    item.TransformShapeToPolygon(poly, layer, margin, 1, pcbnew.ERROR_INSIDE)
                else:
                    item.TransformShapeToPolySet(poly, layer, 0, 1, pcbnew.ERROR_INSIDE)

                def points(chain):
                    return [
                        [chain.CPoint(j).x / 1e6, chain.CPoint(j).y / 1e6]
                        for j in range(chain.PointCount())
                    ]

                entry["layers"][name] = [
                    {
                        "shell": points(poly.COutline(i)),
                        "holes": [points(poly.CHole(i, j)) for j in range(poly.HoleCount(i))],
                    }
                    for i in range(poly.OutlineCount())
                ]
                if name.endswith(".Mask"):
                    entry["margin_mm"][name] = margin / 1e6
            except Exception as exc:
                result["errors"].append(f"{identity}/{name}: {exc}")
        result["objects"][identity] = entry
    Path(sys.argv[3]).write_text(json.dumps(result))


if __name__ == "__main__":
    main()
