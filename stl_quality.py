"""STL-Qualitaetspruefung fuer simulationsfaehige Mesh-Ausgaben."""

from __future__ import annotations

import math
import os
import struct
from collections import Counter


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    return math.sqrt(_dot(a, a))


def _triangle_area(triangle):
    a, b, c = triangle
    return 0.5 * _norm(_cross(_sub(b, a), _sub(c, a)))


def _triangle_signed_volume(triangle):
    a, b, c = triangle
    return _dot(a, _cross(b, c)) / 6.0


def _quantize(point, tolerance):
    return tuple(int(round(coord / tolerance)) for coord in point)


def _looks_like_binary_stl(path):
    size = os.path.getsize(path)
    if size < 84:
        return False
    with open(path, "rb") as handle:
        handle.seek(80)
        raw_count = handle.read(4)
    if len(raw_count) != 4:
        return False
    triangle_count = struct.unpack("<I", raw_count)[0]
    return size == 84 + triangle_count * 50


def _load_binary_stl(path):
    triangles = []
    with open(path, "rb") as handle:
        handle.seek(80)
        triangle_count = struct.unpack("<I", handle.read(4))[0]
        for _ in range(triangle_count):
            data = handle.read(50)
            if len(data) != 50:
                break
            values = struct.unpack("<12fH", data)
            p1 = (values[3], values[4], values[5])
            p2 = (values[6], values[7], values[8])
            p3 = (values[9], values[10], values[11])
            triangles.append((p1, p2, p3))
    return triangles


def _load_ascii_stl(path):
    triangles = []
    vertices = []
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            parts = line.strip().split()
            if len(parts) == 4 and parts[0].lower() == "vertex":
                try:
                    vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    vertices = []
            if len(vertices) == 3:
                triangles.append(tuple(vertices))
                vertices = []
    return triangles


def load_stl_triangles(path):
    """Laedt Dreiecke aus Binary- oder ASCII-STL."""
    if _looks_like_binary_stl(path):
        return _load_binary_stl(path)
    triangles = _load_ascii_stl(path)
    if triangles:
        return triangles
    return _load_binary_stl(path)


def _bounds(triangles):
    points = [point for triangle in triangles for point in triangle]
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    zs = [point[2] for point in points]
    return {
        "x": max(xs) - min(xs),
        "y": max(ys) - min(ys),
        "z": max(zs) - min(zs),
        "xmin": min(xs),
        "xmax": max(xs),
        "ymin": min(ys),
        "ymax": max(ys),
        "zmin": min(zs),
        "zmax": max(zs),
    }


def validate_stl(path, *, expected_volume=None, expected_bbox=None, tolerance=1.0e-4):
    """Prueft, ob eine STL fuer Simulation/CAD-Rueckfuehrung sauber genug ist."""
    triangles = load_stl_triangles(path)
    report = {
        "path": path,
        "triangle_count": len(triangles),
        "file_size_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
        "degenerate_facets": 0,
        "boundary_edges": 0,
        "nonmanifold_edges": 0,
        "duplicate_facets": 0,
        "watertight": False,
        "orientation_ratio": 0.0,
        "mesh_volume_mm3": 0.0,
        "volume_deviation_percent": None,
        "bbox_deviation_mm": None,
        "status": "error",
        "errors": [],
        "warnings": [],
    }
    if not triangles:
        report["errors"].append("STL enthaelt keine Dreiecke")
        return report

    edge_counts = Counter()
    facet_counts = Counter()
    signed_volume = 0.0
    unsigned_signed_volume = 0.0
    area_tolerance = max(tolerance * tolerance, 1.0e-12)

    for triangle in triangles:
        area = _triangle_area(triangle)
        if area <= area_tolerance:
            report["degenerate_facets"] += 1
            continue
        q = [_quantize(point, tolerance) for point in triangle]
        facet_counts[tuple(sorted(q))] += 1
        for a, b in ((q[0], q[1]), (q[1], q[2]), (q[2], q[0])):
            edge_counts[tuple(sorted((a, b)))] += 1
        vol = _triangle_signed_volume(triangle)
        signed_volume += vol
        unsigned_signed_volume += abs(vol)

    report["duplicate_facets"] = sum(count - 1 for count in facet_counts.values() if count > 1)
    report["boundary_edges"] = sum(1 for count in edge_counts.values() if count == 1)
    report["nonmanifold_edges"] = sum(1 for count in edge_counts.values() if count != 2)
    report["watertight"] = report["nonmanifold_edges"] == 0 and report["boundary_edges"] == 0
    report["mesh_volume_mm3"] = abs(signed_volume)
    report["orientation_ratio"] = abs(signed_volume) / max(unsigned_signed_volume, 1.0e-12)

    if report["degenerate_facets"]:
        report["errors"].append(f"{report['degenerate_facets']} degenerierte Dreiecke")
    if not report["watertight"]:
        report["errors"].append(
            f"Mesh nicht wasserdicht: {report['boundary_edges']} Randkanten, {report['nonmanifold_edges']} nicht-manifold Kanten"
        )
    if report["duplicate_facets"]:
        report["warnings"].append(f"{report['duplicate_facets']} doppelte Dreiecke erkannt")
    if report["orientation_ratio"] < 0.92:
        report["errors"].append(f"Flaechennormalen uneinheitlich orientiert (Ratio {report['orientation_ratio']:.3f})")

    if expected_volume and expected_volume > 0 and report["mesh_volume_mm3"] > 0:
        deviation = abs(report["mesh_volume_mm3"] - expected_volume) / expected_volume * 100.0
        report["volume_deviation_percent"] = deviation
        if deviation > 2.0:
            report["errors"].append(f"STL-Volumen weicht {deviation:.2f}% vom CAD-Solid ab")

    bbox = _bounds(triangles)
    if expected_bbox and bbox:
        deviations = [
            abs(bbox["x"] - float(expected_bbox.get("x", bbox["x"]))),
            abs(bbox["y"] - float(expected_bbox.get("y", bbox["y"]))),
            abs(bbox["z"] - float(expected_bbox.get("z", bbox["z"]))),
        ]
        max_deviation = max(deviations)
        report["bbox_deviation_mm"] = max_deviation
        if max_deviation > max(0.35, max(expected_bbox.get("x", 0), expected_bbox.get("y", 0), expected_bbox.get("z", 0), 1.0) * 0.01):
            report["warnings"].append(f"STL-Bounding-Box weicht bis {max_deviation:.3f} mm vom CAD-Solid ab")

    report["status"] = "ok" if not report["errors"] else "error"
    return report


def format_quality_report(report):
    """Erzeugt kurze Logzeilen fuer die Debugausgabe."""
    return [
        (
            f"STL-Qualitaet: {report.get('status', 'error').upper()} | "
            f"Dreiecke {report.get('triangle_count', 0)} | "
            f"wasserdicht {'ja' if report.get('watertight') else 'nein'} | "
            f"degeneriert {report.get('degenerate_facets', 0)} | "
            f"nicht-manifold {report.get('nonmanifold_edges', 0)}"
        ),
        (
            f"STL-Volumen {report.get('mesh_volume_mm3', 0.0):.1f} mm3 | "
            f"Abweichung {report.get('volume_deviation_percent'):.3f}%"
            if report.get("volume_deviation_percent") is not None
            else f"STL-Volumen {report.get('mesh_volume_mm3', 0.0):.1f} mm3"
        ),
    ]
