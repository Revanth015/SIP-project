"""CAD ingestion and semantic geometry detection for the warehouse Digital Twin."""

from pathlib import Path
import math
from typing import Any

import numpy as np
from shapely.geometry import LineString, Polygon, MultiPolygon
from shapely.ops import polygonize, snap, unary_union


def clean_polygon(poly):
    """Repair a polygon and return its largest valid component."""
    if poly is None:
        return None
    try:
        poly = poly.buffer(0)
        if poly.is_empty:
            return None
        if isinstance(poly, MultiPolygon):
            poly = max(poly.geoms, key=lambda g: g.area)
        return poly if poly.area > 1e-8 else None
    except Exception:
        return None


def points_are_closed(points, tolerance=0.01):
    """Detect geometric closure even when a DXF closed flag is false."""
    if len(points) < 3:
        return False
    a = np.asarray(points[0], dtype=float)
    b = np.asarray(points[-1], dtype=float)
    return float(np.linalg.norm(a - b)) <= tolerance


def polygon_from_points(points):
    if len(points) < 3:
        return None
    try:
        return clean_polygon(Polygon(points))
    except Exception:
        return None


def read_dxf(path: str | Path) -> dict[str, Any]:
    """Read computational DXF geometry; raster IMAGE entities are ignored."""
    try:
        import ezdxf
    except ImportError as exc:
        raise ImportError("Install ezdxf with: pip install ezdxf") from exc

    path = Path(path)
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    entities = []
    linework = []
    polygons = []
    layers = set()

    for entity in msp:
        try:
            typ = entity.dxftype()
            layer = str(getattr(entity.dxf, "layer", "0"))
            layers.add(layer)

            if typ == "LINE":
                p1 = (float(entity.dxf.start.x), float(entity.dxf.start.y))
                p2 = (float(entity.dxf.end.x), float(entity.dxf.end.y))
                if p1 != p2:
                    linework.append(LineString([p1, p2]))
                    entities.append({"type": typ, "layer": layer, "points": [p1, p2]})

            elif typ == "LWPOLYLINE":
                pts = [(float(p[0]), float(p[1])) for p in entity.get_points()]
                if len(pts) >= 2:
                    entities.append({"type": typ, "layer": layer, "points": pts})
                    closed = bool(entity.closed) or points_are_closed(pts)
                    if closed and len(pts) >= 3:
                        poly = polygon_from_points(pts)
                        if poly:
                            polygons.append({"polygon": poly, "layer": layer, "source": typ})
                    else:
                        linework.append(LineString(pts))

            elif typ == "POLYLINE":
                pts = []
                for vertex in entity.vertices():
                    loc = vertex.dxf.location
                    pts.append((float(loc.x), float(loc.y)))
                if len(pts) >= 2:
                    entities.append({"type": typ, "layer": layer, "points": pts})
                    closed = bool(entity.is_closed) or points_are_closed(pts)
                    if closed and len(pts) >= 3:
                        poly = polygon_from_points(pts)
                        if poly:
                            polygons.append({"polygon": poly, "layer": layer, "source": typ})
                    else:
                        linework.append(LineString(pts))

            elif typ == "CIRCLE":
                center = entity.dxf.center
                entities.append({
                    "type": typ,
                    "layer": layer,
                    "center": (float(center.x), float(center.y)),
                    "radius": float(entity.dxf.radius),
                })

            elif typ == "ARC":
                center = entity.dxf.center
                radius = float(entity.dxf.radius)
                start = math.radians(float(entity.dxf.start_angle))
                end = math.radians(float(entity.dxf.end_angle))
                if end <= start:
                    end += 2 * math.pi
                pts = [
                    (center.x + radius * math.cos(a), center.y + radius * math.sin(a))
                    for a in np.linspace(start, end, 32)
                ]
                entities.append({"type": typ, "layer": layer, "points": pts})
                linework.append(LineString(pts))

        except Exception as exc:
            entities.append({"type": "SKIPPED", "error": str(exc)})

    # Reconstruct polygons from open/segmented linework.
    if not polygons and linework:
        merged = unary_union(linework)
        for tolerance in (0.02, 0.05, 0.10, 0.25, 0.50, 1.00):
            try:
                snapped = snap(merged, merged, tolerance)
                generated = [p for p in polygonize(snapped) if p.area > 1e-8]
                if generated:
                    polygons.extend({
                        "polygon": clean_polygon(p),
                        "layer": "RECONSTRUCTED_FROM_LINEWORK",
                        "source": f"polygonize_{tolerance}m",
                    } for p in generated if clean_polygon(p) is not None)
                    break
            except Exception:
                continue

    return {
        "path": str(path),
        "entities": entities,
        "linework": linework,
        "polygons": polygons,
        "layers": sorted(layers),
    }


def detect_warehouse(data: dict[str, Any]):
    """Prefer explicit warehouse/building layers; fall back to largest polygon."""
    polygons = data["polygons"]
    if not polygons:
        raise ValueError("No closed warehouse geometry could be detected in the DXF.")

    explicit = [
        x for x in polygons
        if "WAREHOUSE" in str(x["layer"]).upper()
    ]
    semantic = [
        x for x in polygons
        if any(k in str(x["layer"]).upper() for k in (
            "BUILDING", "FACTORY", "PLANT", "STORAGE", "STORE", "STOCK"
        ))
    ]

    candidates = explicit or semantic or polygons
    selected = max(candidates, key=lambda x: x["polygon"].area)
    return selected["polygon"], selected["layer"]


def detect_doors(data: dict[str, Any]):
    """Return point candidates from entities on door/gate/entry/exit layers."""
    result = []
    keywords = ("DOOR", "GATE", "ENTRY", "EXIT")

    for entity in data["entities"]:
        layer = str(entity.get("layer", ""))
        if not any(k in layer.upper() for k in keywords):
            continue
        points = entity.get("points", [])
        if len(points) < 2:
            continue
        p1, p2 = points[0], points[-1]
        result.append({
            "x": (p1[0] + p2[0]) / 2,
            "y": (p1[1] + p2[1]) / 2,
            "layer": layer,
            "type": entity.get("type", "UNKNOWN"),
        })

    return result


def detect_obstacles(data: dict[str, Any], warehouse):
    """Identify small internal polygons not semantically labelled as storage."""
    storage_keywords = ("STORAGE", "STORE", "WAREHOUSE", "PALLET", "RACK", "POLY", "FG", "STOCK")
    obstacles = []

    for item in data["polygons"]:
        poly = item["polygon"]
        if poly.equals(warehouse) or not warehouse.contains(poly.centroid):
            continue
        if poly.area >= warehouse.area * 0.30:
            continue
        layer = str(item["layer"]).upper()
        if any(k in layer for k in storage_keywords):
            continue
        obstacles.append(poly)

    return obstacles
