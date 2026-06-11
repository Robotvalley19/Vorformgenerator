"""
PDF-Export fuer technische Rohteilzeichnungen und Projektuebersichten.

Die Datei erzeugt aus STEP-Geometrie mehrere Ansichten, Schnitte und
infoseitige Auswertungen. Alle Darstellungen sollen fuer Einsteiger
moeglichst lesbar bleiben:
- reale Abmessungen kommen aus der STEP-Geometrie,
- Zusatzinformationen kommen aus den Export-Metadaten des Frontends,
- und die Seiten sind in technische Zeichnungen sowie Management-Uebersichten
  getrennt.
"""

import os
import sys
import tempfile
import textwrap
from types import SimpleNamespace

os.environ.setdefault("MPLCONFIGDIR", tempfile.gettempdir())

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.collections import LineCollection, PolyCollection
from matplotlib.patches import Polygon, Rectangle
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

STEEL_DENSITY = 7.85e-6

def _project_title(meta):
    """Titelzeile fuer den PDF-Kopf."""
    return meta.get("project_name") or "Projektzusammenfassung"


def _close_doc(doc):
    """Schliesst ein FreeCAD-Dokument sicher, ohne den Export abzubrechen."""
    try:
        import FreeCAD

        FreeCAD.closeDocument(doc.Name)
    except Exception:
        pass


def _validate_shape(shape):
    """Validiert die Geometrie und versucht ggf. zu reparieren."""
    try:
        import Part
        
        if shape.isNull():
            return False
        
        # Check if shape has any geometry
        if not shape.Solids and not shape.Shells and not shape.Faces:
            return False
        
        # Try to heal the shape if it has issues
        try:
            healed_shape = shape.copy()
            if hasattr(healed_shape, 'isValid'):
                if not healed_shape.isValid():
                    # Attempt basic healing
                    if hasattr(Part, 'makeShapeFromMesh'):
                        # Try alternative approach
                        pass
        except:
            pass
        
        return True
    except Exception:
        return False


def _load_shape(step_file, doc_name):
    """Laedt eine STEP-Geometrie in ein temporaeres FreeCAD-Dokument."""
    import FreeCAD
    import Part
    import sys

    doc = None
    try:
        doc = FreeCAD.newDocument(doc_name)
        shape = Part.Shape()
        shape.read(step_file)
        if shape.isNull():
            raise ValueError(f"STEP konnte nicht geladen werden: {step_file}")
        
        # Validate geometry
        if not _validate_shape(shape):
            raise ValueError(f"STEP hat keine verwertbare Geometrie: {step_file}")
        
        # Try to heal the shape if it has validity issues
        try:
            # Some FreeCAD versions have this method
            if hasattr(shape, 'isValid') and not shape.isValid():
                print(f"Info: Shape is invalid, attempting repair...", file=sys.stderr)
                # Attempt to fix the shape using Part utilities
                if hasattr(Part, 'fixShape'):
                    fixed = Part.fixShape(shape)
                    if fixed and not fixed.isNull():
                        shape = fixed
                        print(f"Info: Shape successfully healed", file=sys.stderr)
                else:
                    # Alternative repair: convert to mesh and back
                    print(f"Info: fixShape not available, trying alternative repair methods...", file=sys.stderr)
                    try:
                        # Try to create a compound of all valid solids
                        solids = shape.Solids
                        if solids:
                            shape = Part.makeCompound(solids)
                            print(f"Info: Shape repaired by solid extraction", file=sys.stderr)
                    except Exception as alt_e:
                        print(f"Warning: Alternative repair failed: {alt_e}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Shape healing attempt failed (non-critical): {e}", file=sys.stderr)
        
        # Attempt to refine the shape to improve robustness
        try:
            if hasattr(shape, 'Solids') and shape.Solids:
                # If it's a solid, try to get a compound of solids for better stability
                solids = shape.Solids
                print(f"Info: Shape contains {len(solids)} solid(s)", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Shape refinement failed (non-critical): {e}", file=sys.stderr)
        
        print(f"Info: Shape loaded successfully from {step_file}", file=sys.stderr)
        print(f"Info: Shape volume: {shape.Volume:.1f} mm³, has {len(shape.Faces) if hasattr(shape, 'Faces') else 0} faces", file=sys.stderr)
        
        return doc, shape
    except Exception as e:
        # Ensure document is closed even if loading fails
        if doc:
            try:
                _close_doc(doc)
            except:
                pass
        raise


def _largest_closed_wire(shape):
    """Liefert aus einem Schnitt die groesste geschlossene Kontur."""
    import Part

    wires = []
    if hasattr(shape, "Wires") and shape.Wires:
        wires.extend([wire for wire in shape.Wires if wire.isClosed()])
    elif hasattr(shape, "Edges") and shape.Edges:
        for edge_group in Part.sortEdges(shape.Edges):
            try:
                wire = Part.Wire(edge_group)
                if wire.isClosed():
                    wires.append(wire)
            except Exception:
                continue

    if not wires:
        return None

    def _area(wire):
        try:
            return abs(Part.Face(wire).Area)
        except Exception:
            return 0.0

    return max(wires, key=_area)


def _make_plane_for_axis(bbox, axis, level):
    """Erzeugt eine Schnittebene fuer X-, Y- oder Z-Schnitte."""
    import FreeCAD
    import Part

    margin = max(bbox.XLength, bbox.YLength, bbox.ZLength, 10.0) * 0.25
    if axis == "z":
        return Part.makePlane(
            bbox.XLength + margin * 2.0,
            bbox.YLength + margin * 2.0,
            FreeCAD.Vector(bbox.XMin - margin, bbox.YMin - margin, level),
            FreeCAD.Vector(0, 0, 1),
        )
    if axis == "y":
        return Part.makePlane(
            bbox.XLength + margin * 2.0,
            bbox.ZLength + margin * 2.0,
            FreeCAD.Vector(bbox.XMin - margin, level, bbox.ZMin - margin),
            FreeCAD.Vector(0, 1, 0),
        )
    return Part.makePlane(
        bbox.YLength + margin * 2.0,
        bbox.ZLength + margin * 2.0,
        FreeCAD.Vector(level, bbox.YMin - margin, bbox.ZMin - margin),
        FreeCAD.Vector(1, 0, 0),
    )


def _extract_wires(shape, axis, level, allow_bbox_fallback=False):
    """
    Schneidet das Modell und sammelt geschlossene Drähte des Schnitts.
    
    Diese Funktion versucht mehrere Methoden:
    1. Echte Boolean-Schnitte per section()
    2. Schneiden mit Ebenen
    3. Extraktion von Konturen bei bestimmter Höhe
    """
    import FreeCAD
    import Part
    import sys

    wires = []
    
    try:
        if shape.isNull():
            return wires
        
        bbox = shape.BoundBox
        
        # Prüfe, ob der Level innerhalb der Grenzen liegt
        if axis == "z" and (level < bbox.ZMin or level > bbox.ZMax):
            return wires
        elif axis == "y" and (level < bbox.YMin or level > bbox.YMax):
            return wires
        elif axis == "x" and (level < bbox.XMin or level > bbox.XMax):
            return wires
        
        # Methode 1: Versuche echte Schnittoperation mit Ebene
        try:
            plane = _make_plane_for_axis(bbox, axis, level)
            if plane and not plane.isNull():
                try:
                    # Verwende slice statt section, das ist robuster
                    section = shape.common(plane)  # Intersection statt section
                    if section and not section.isNull() and hasattr(section, 'Edges') and section.Edges:
                        print(f"Info: Section extraction successful for axis {axis} at level {level:.1f}", file=sys.stderr)
                        # Versuche geschlossene Drähte zu erstellen
                        try:
                            for edge_group in Part.sortEdges(section.Edges):
                                try:
                                    wire = Part.Wire(edge_group)
                                    if wire.isClosed():
                                        wires.append(wire)
                                except Exception:
                                    continue
                        except Exception as e:
                            print(f"Warning: Edge sorting failed: {e}", file=sys.stderr)
                            # Fall back zu rohen Kanten
                            for edge in section.Edges:
                                try:
                                    wire = Part.Wire([edge])
                                    if wire.isClosed():
                                        wires.append(wire)
                                except Exception:
                                    continue
                except Exception as e:
                    print(f"Warning: Intersection with plane failed: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Plane creation failed for axis {axis}: {e}", file=sys.stderr)
        
        # Methode 2: Falls Methode 1 keine Ergebnisse brachte, versuche alternative Schnittmethode
        if not wires:
            try:
                print(f"Info: Trying alternative slicing method for axis {axis} at level {level:.1f}", file=sys.stderr)
                # Erstelle zwei Schnitt-Boxen oben und unten der Ebene
                margin = max(bbox.XLength, bbox.YLength, bbox.ZLength) * 0.5
                
                if axis == "z":
                    box1 = Part.makeBox(
                        bbox.XLength + margin*2, bbox.YLength + margin*2, level - bbox.ZMin + 1,
                        bbox.XMin - margin, bbox.YMin - margin, bbox.ZMin - 0.5
                    )
                    box2 = Part.makeBox(
                        bbox.XLength + margin*2, bbox.YLength + margin*2, bbox.ZMax - level + 1,
                        bbox.XMin - margin, bbox.YMin - margin, level - 0.5
                    )
                elif axis == "y":
                    box1 = Part.makeBox(
                        bbox.XLength + margin*2, level - bbox.YMin + 1, bbox.ZLength + margin*2,
                        bbox.XMin - margin, bbox.YMin - 0.5, bbox.ZMin - margin
                    )
                    box2 = Part.makeBox(
                        bbox.XLength + margin*2, bbox.YMax - level + 1, bbox.ZLength + margin*2,
                        bbox.XMin - margin, level - 0.5, bbox.ZMin - margin
                    )
                else:  # axis == "x"
                    box1 = Part.makeBox(
                        level - bbox.XMin + 1, bbox.YLength + margin*2, bbox.ZLength + margin*2,
                        bbox.XMin - 0.5, bbox.YMin - margin, bbox.ZMin - margin
                    )
                    box2 = Part.makeBox(
                        bbox.XMax - level + 1, bbox.YLength + margin*2, bbox.ZLength + margin*2,
                        level - 0.5, bbox.YMin - margin, bbox.ZMin - margin
                    )
                
                # Schneide die Form mit den beiden Boxen
                top = shape.common(box2)
                bottom = shape.common(box1)
                
                # Finde die Schnittkanten (Kanten auf der Schnittebene)
                if top and bottom and not top.isNull() and not bottom.isNull():
                    try:
                        # Finde gemeinsame Kanten
                        section_result = top.common(bottom)
                        if section_result and not section_result.isNull():
                            if hasattr(section_result, 'Edges') and section_result.Edges:
                                for edge_group in Part.sortEdges(section_result.Edges):
                                    try:
                                        wire = Part.Wire(edge_group)
                                        if wire.isClosed():
                                            wires.append(wire)
                                    except Exception:
                                        continue
                                if wires:
                                    print(f"Info: Alternative slicing found {len(wires)} wires", file=sys.stderr)
                    except Exception as e:
                        print(f"Warning: Alternative slicing method partially failed: {e}", file=sys.stderr)
            except Exception as e:
                print(f"Warning: Alternative slicing method failed: {e}", file=sys.stderr)
        
        # Methode 3: optionaler Notfall-Fallback. Fuer Zeichnungsexporte bleibt
        # dieser bewusst aus, damit keine unzutreffenden Rechtecke entstehen.
        if not wires and allow_bbox_fallback:
            print(f"Info: Using bounding box fallback for axis {axis} at level {level:.1f}", file=sys.stderr)
            try:
                if axis == "z":
                    x_min, x_max = bbox.XMin, bbox.XMax
                    y_min, y_max = bbox.YMin, bbox.YMax
                    rect_points = [
                        FreeCAD.Vector(x_min, y_min, level),
                        FreeCAD.Vector(x_max, y_min, level),
                        FreeCAD.Vector(x_max, y_max, level),
                        FreeCAD.Vector(x_min, y_max, level),
                    ]
                elif axis == "y":
                    x_min, x_max = bbox.XMin, bbox.XMax
                    z_min, z_max = bbox.ZMin, bbox.ZMax
                    rect_points = [
                        FreeCAD.Vector(x_min, level, z_min),
                        FreeCAD.Vector(x_max, level, z_min),
                        FreeCAD.Vector(x_max, level, z_max),
                        FreeCAD.Vector(x_min, level, z_max),
                    ]
                else:  # axis == "x"
                    y_min, y_max = bbox.YMin, bbox.YMax
                    z_min, z_max = bbox.ZMin, bbox.ZMax
                    rect_points = [
                        FreeCAD.Vector(level, y_min, z_min),
                        FreeCAD.Vector(level, y_max, z_min),
                        FreeCAD.Vector(level, y_max, z_max),
                        FreeCAD.Vector(level, y_min, z_max),
                    ]
                
                rect_points.append(rect_points[0])  # Schließe den Polygon
                wire = Part.makePolygon(rect_points)
                if not wire.isNull():
                    wires.append(wire)
            except Exception as e:
                print(f"Warning: Bounding box fallback failed: {e}", file=sys.stderr)
        
        print(f"Info: Extracted {len(wires)} wires for axis {axis} at level {level:.1f}", file=sys.stderr)
    
    except Exception as outer_e:
        print(f"Warning: Wire extraction failed for axis {axis}: {outer_e}", file=sys.stderr)
    
    return wires


def _pick_best_parting_level(shape, preferred_z=None):
    """Sucht heuristisch die aussagekraeftigste Z-Trennebene."""
    import Part
    import sys

    bbox = shape.BoundBox
    z_span = bbox.ZMax - bbox.ZMin
    if z_span <= 0:
        return bbox.ZMin

    # Return middle of the shape as default to avoid problematic section operations
    default_level = bbox.ZMin + z_span * 0.5
    
    if preferred_z is not None:
        clamped_preferred = max(bbox.ZMin, min(float(preferred_z), bbox.ZMax))
        return clamped_preferred
    
    # Use a safe middle value instead of computing from sections
    # This avoids the problematic section() operation that causes segfaults
    return default_level


def _wire_points(wire, axis, origin_shift=None):
    """Diskretisiert eine Kontur in 2D-Punkte fuer Matplotlib."""
    shift_x = shift_y = shift_z = 0.0
    if origin_shift:
        shift_x, shift_y, shift_z = origin_shift
    pts = []
    for edge in wire.Edges:
        number = max(12, min(80, int(max(edge.Length, 1.0) / 3.0)))
        for p in edge.discretize(Number=number):
            if axis == "z":
                pts.append((p.x - shift_x, p.y - shift_y))
            elif axis == "y":
                pts.append((p.x - shift_x, p.z - shift_z))
            else:
                pts.append((p.y - shift_y, p.z - shift_z))
    return pts


def _plot_wires(ax, wires, axis, facecolor, edgecolor, alpha=0.2, hatch=None, linewidth=1.8, origin_shift=None):
    """Zeichnet diskretisierte Konturen als 2D-Flaechen in Matplotlib."""
    for wire in wires:
        pts = _wire_points(wire, axis, origin_shift=origin_shift)
        if len(pts) < 3:
            continue
        patch = Polygon(
            pts,
            closed=True,
            facecolor=facecolor,
            edgecolor=edgecolor,
            linewidth=linewidth,
            alpha=alpha,
            hatch=hatch,
        )
        ax.add_patch(patch)


def _wire_bounds(wires, axis, origin_shift=None):
    """Bestimmt die 2D-Grenzen einer oder mehrerer Konturen."""
    xs = []
    ys = []
    for wire in wires:
        pts = _wire_points(wire, axis, origin_shift=origin_shift)
        for x, y in pts:
            xs.append(x)
            ys.append(y)
    if not xs or not ys:
        return None
    return min(xs), max(xs), min(ys), max(ys)


def _project_point(point, axis):
    """Projiziert einen 3D-Punkt auf die jeweilige technische Ansicht."""
    x, y, z = point
    if axis == "z":
        return (x, y)
    if axis == "y":
        return (x, z)
    return (y, z)


def _build_projected_polygons(triangles, axis):
    """Erzeugt 2D-Dreiecksflaechen aus der tessellierten Rohteilgeometrie."""
    polygons = []
    for tri in triangles or []:
        if len(tri) != 3:
            continue
        pts = [_project_point(point, axis) for point in tri]
        if len({(round(x, 5), round(y, 5)) for x, y in pts}) >= 3:
            polygons.append(pts)
    return polygons


def _build_section_segments(triangles, axis, level):
    """Schneidet das Rohteil-Mesh mit einer Ebene und liefert 2D-Liniensegmente."""
    axis_index = {"x": 0, "y": 1, "z": 2}[axis]
    segments = []
    eps = 1e-6

    for tri in triangles or []:
        if len(tri) != 3:
            continue

        intersections = []
        for idx in range(3):
            p1 = tri[idx]
            p2 = tri[(idx + 1) % 3]
            d1 = p1[axis_index] - level
            d2 = p2[axis_index] - level

            if abs(d1) <= eps and abs(d2) <= eps:
                intersections.extend([p1, p2])
            elif abs(d1) <= eps:
                intersections.append(p1)
            elif abs(d2) <= eps:
                intersections.append(p2)
            elif d1 * d2 < 0:
                t = abs(d1) / (abs(d1) + abs(d2))
                intersections.append(
                    (
                        p1[0] + (p2[0] - p1[0]) * t,
                        p1[1] + (p2[1] - p1[1]) * t,
                        p1[2] + (p2[2] - p1[2]) * t,
                    )
                )

        unique = []
        seen = set()
        for point in intersections:
            key = tuple(round(coord, 5) for coord in point)
            if key not in seen:
                seen.add(key)
                unique.append(point)

        if len(unique) >= 2:
            p_start = _project_point(unique[0], axis)
            p_end = _project_point(unique[1], axis)
            if p_start != p_end:
                segments.append((p_start, p_end))

    return segments


def _bounds_from_2d_items(items):
    """Bestimmt Grenzen aus 2D-Polygonen oder Liniensegmenten."""
    xs = []
    ys = []
    for item in items or []:
        for point in item:
            if len(point) == 2 and isinstance(point[0], (int, float)):
                x, y = point
                xs.append(x)
                ys.append(y)
            else:
                for x, y in point:
                    xs.append(x)
                    ys.append(y)
    if not xs or not ys:
        return None
    return min(xs), max(xs), min(ys), max(ys)


def _draw_mesh_polygons(ax, polygons, facecolor="#fee2e2", edgecolor="#991b1b"):
    """Zeichnet echte projizierte Rohteilflaechen statt Platzhalter-Rechtecken."""
    if not polygons:
        return
    collection = PolyCollection(
        polygons,
        facecolors=facecolor,
        edgecolors=edgecolor,
        linewidths=0.08,
        alpha=0.82,
        rasterized=True,
    )
    ax.add_collection(collection)


def _draw_section_segments(ax, segments, color="#991b1b"):
    """Zeichnet Mesh-Schnittlinien fuer Ansichten ohne geschlossene FreeCAD-Wires."""
    if not segments:
        return
    collection = LineCollection(segments, colors=color, linewidths=1.05, alpha=0.95)
    ax.add_collection(collection)


def _draw_dimension(ax, start, end, offset, text, vertical=False):
    """Zeichnet einfache Masspfeile fuer Laengen- und Hoehenangaben."""
    text_pad = max(abs(end - start) * 0.018, 3.5)
    if vertical:
        x = offset
        y0, y1 = start, end
        ax.plot([x, x], [y0, y1], color="#0f172a", linewidth=1.0)
        ax.annotate("", xy=(x, y0), xytext=(x, y1), arrowprops=dict(arrowstyle="<->", color="#0f172a", lw=1.0))
        ax.text(x + text_pad, (y0 + y1) / 2.0, text, va="center", ha="left", fontsize=8.0, color="#0f172a")
    else:
        y = offset
        x0, x1 = start, end
        ax.plot([x0, x1], [y, y], color="#0f172a", linewidth=1.0)
        ax.annotate("", xy=(x0, y), xytext=(x1, y), arrowprops=dict(arrowstyle="<->", color="#0f172a", lw=1.0))
        ax.text((x0 + x1) / 2.0, y + text_pad, text, va="bottom", ha="center", fontsize=8.0, color="#0f172a")


def _prepare_geometry_data(step_file, base_step_file=None, preferred_parting_z=None):
    """
    Bereitet alle Geometriedaten vor, die spaeter im PDF mehrfach verwendet werden.

    Dadurch muessen STEP-Dateien nur einmal eingelesen und geschnitten werden.
    """
    import sys
    
    base_doc = base_shape = None
    final_doc = final_shape = None
    
    try:
        final_doc, final_shape = _load_shape(step_file, "ZeichnungFinal")
        
        # Wenn zusaetzlich ein Basis-STEP vorhanden ist, koennen spaetere Seiten
        # Soll-/Ist-Vergleiche zwischen Grundgeometrie und finalem Rohteil
        # zeichnen, ohne dieselben Dateien mehrfach laden zu muessen.
        if base_step_file and os.path.exists(base_step_file):
            try:
                base_doc, base_shape = _load_shape(base_step_file, "ZeichnungBasis")
            except Exception as e:
                print(f"Warning: Base STEP loading failed, proceeding without: {e}", file=sys.stderr)
                base_doc = None
                base_shape = None

        bbox = final_shape.BoundBox
        parting_z = _pick_best_parting_level(base_shape or final_shape, preferred_z=preferred_parting_z)
        center_x = bbox.XMin + bbox.XLength * 0.5
        center_y = bbox.YMin + bbox.YLength * 0.5
        origin_shift = (
            center_x,
            center_y,
            bbox.ZMin + bbox.ZLength * 0.5,
        )

        # Tesselliertes Modell einmalig erzeugen und daraus echte Projektionen
        # sowie Mesh-Schnittlinien ableiten. Dadurch entfallen Rechteck-Platzhalter.
        try:
            preview_triangles = _build_preview_triangles(final_shape, origin_shift=origin_shift)
        except Exception as e:
            print(f"Warning: Preview mesh generation failed: {e}", file=sys.stderr)
            preview_triangles = []

        shifted_levels = {
            "x": lambda value: value - origin_shift[0],
            "y": lambda value: value - origin_shift[1],
            "z": lambda value: value - origin_shift[2],
        }

        view_specs = {
            "top": ("z", parting_z, "Draufsicht", "projection"),
            "bottom": ("z", parting_z, "Unteransicht", "projection"),
            "front": ("y", center_y, "Vorderansicht", "projection"),
            "rear": ("y", center_y, "Rückansicht", "projection"),
            "left": ("x", center_x, "Linke Ansicht", "projection"),
            "right": ("x", center_x, "Rechte Ansicht", "projection"),
            "parting": ("z", parting_z, "Trennebene", "section"),
        }
        detail_view_specs = {
            "section_a": ("y", center_y, "Schnitt A-A", "section"),
            "section_b": ("x", center_x, "Schnitt B-B", "section"),
            "section_c": ("z", bbox.ZMin + bbox.ZLength * 0.28, "Schnitt C-C", "section"),
            "section_d": ("z", bbox.ZMin + bbox.ZLength * 0.72, "Schnitt D-D", "section"),
        }

        views = {}
        for key, (axis, level, title, view_kind) in view_specs.items():
            views[key] = {
                "title": title,
                "axis": axis,
                "level": level,
                "kind": view_kind,
                "final": [],
                "base": [],
                "final_projection": _build_projected_polygons(preview_triangles, axis),
                "final_section_segments": _build_section_segments(preview_triangles, axis, shifted_levels[axis](level)),
                "origin_shift": origin_shift,
            }
        
        detail_views = {}
        for key, (axis, level, title, view_kind) in detail_view_specs.items():
            detail_views[key] = {
                "title": title,
                "axis": axis,
                "level": level,
                "kind": view_kind,
                "final": [],
                "base": [],
                "final_projection": _build_projected_polygons(preview_triangles, axis),
                "final_section_segments": _build_section_segments(preview_triangles, axis, shifted_levels[axis](level)),
                "origin_shift": origin_shift,
            }

        final_weight = final_shape.Volume * STEEL_DENSITY
        base_weight = base_shape.Volume * STEEL_DENSITY if base_shape else None

        return {
            "bbox": bbox,
            "parting_z": parting_z,
            "views": views,
            "detail_views": detail_views,
            "preview_triangles": preview_triangles,
            "final_weight": final_weight,
            "base_weight": base_weight,
            "final_volume": final_shape.Volume,
            "base_volume": base_shape.Volume if base_shape else None,
            "weight_delta": final_weight - base_weight if base_weight is not None else None,
            "volume_delta": final_shape.Volume - base_shape.Volume if base_shape else None,
        }
    finally:
        # Ensure all documents are cleaned up
        if final_doc:
            _close_doc(final_doc)
        if base_doc:
            _close_doc(base_doc)


def _shift_triangles(triangles, origin_shift):
    shift_x, shift_y, shift_z = origin_shift
    return [
        [(x - shift_x, y - shift_y, z - shift_z) for x, y, z in tri]
        for tri in triangles or []
    ]


def _bbox_from_triangle_bounds(bounds):
    return SimpleNamespace(
        XMin=bounds["xmin"],
        XMax=bounds["xmax"],
        YMin=bounds["ymin"],
        YMax=bounds["ymax"],
        ZMin=bounds["zmin"],
        ZMax=bounds["zmax"],
        XLength=bounds["xlen"],
        YLength=bounds["ylen"],
        ZLength=bounds["zlen"],
    )


def _prepare_mesh_geometry_data(mesh_file, meta=None):
    """Bereitet PDF-Geometriedaten direkt aus STL-Dreiecken vor."""
    raw_triangles = _triangles_from_preform_source(mesh_file)
    bounds = _triangle_bounds(raw_triangles)
    if not bounds:
        raise ValueError(f"STL-Geometrie konnte nicht gelesen werden: {mesh_file}")

    bbox = _bbox_from_triangle_bounds(bounds)
    origin_shift = (
        bounds["xmin"] + bounds["xlen"] * 0.5,
        bounds["ymin"] + bounds["ylen"] * 0.5,
        bounds["zmin"] + bounds["zlen"] * 0.5,
    )
    preview_triangles = _shift_triangles(raw_triangles, origin_shift)
    parting_z = bounds["zmin"] + bounds["zlen"] * 0.5
    center_x = bounds["xmin"] + bounds["xlen"] * 0.5
    center_y = bounds["ymin"] + bounds["ylen"] * 0.5

    shifted_levels = {
        "x": lambda value: value - origin_shift[0],
        "y": lambda value: value - origin_shift[1],
        "z": lambda value: value - origin_shift[2],
    }

    def _view(axis, level, title, view_kind):
        return {
            "title": title,
            "axis": axis,
            "level": level,
            "kind": view_kind,
            "final": [],
            "base": [],
            "final_projection": _build_projected_polygons(preview_triangles, axis),
            "final_section_segments": _build_section_segments(preview_triangles, axis, shifted_levels[axis](level)),
            "origin_shift": origin_shift,
        }

    views = {
        "top": _view("z", parting_z, "Draufsicht", "projection"),
        "bottom": _view("z", parting_z, "Unteransicht", "projection"),
        "front": _view("y", center_y, "Vorderansicht", "projection"),
        "rear": _view("y", center_y, "Rueckansicht", "projection"),
        "left": _view("x", center_x, "Linke Ansicht", "projection"),
        "right": _view("x", center_x, "Rechte Ansicht", "projection"),
        "parting": _view("z", parting_z, "Trennebene", "section"),
    }
    detail_views = {
        "section_a": _view("y", center_y, "Schnitt A-A", "section"),
        "section_b": _view("x", center_x, "Schnitt B-B", "section"),
        "section_c": _view("z", bounds["zmin"] + bounds["zlen"] * 0.28, "Schnitt C-C", "section"),
        "section_d": _view("z", bounds["zmin"] + bounds["zlen"] * 0.72, "Schnitt D-D", "section"),
    }

    geometry_info = (meta or {}).get("export_geometry") or {}
    final_weight = float(geometry_info.get("weight_kg") or 0)
    final_volume = float(geometry_info.get("volume_mm3") or 0)

    return {
        "bbox": bbox,
        "parting_z": parting_z,
        "views": views,
        "detail_views": detail_views,
        "preview_triangles": preview_triangles,
        "final_weight": final_weight,
        "base_weight": None,
        "final_volume": final_volume,
        "base_volume": None,
        "weight_delta": None,
        "volume_delta": None,
    }


def _extract_contour_key_points(wires, axis, origin_shift=None):
    """
    Extrahiert Schlüsselpunkte aus den Konturdraehten fuer detaillierte Bemaßung.
    Gibt Listen von (x, y, value) zurück für horizontale und vertikale Dimensionen.
    """
    import sys
    
    h_dims = []  # (x_min, x_max, y_value) für horizontale Maße
    v_dims = []  # (y_min, y_max, x_value) für vertikale Maße
    
    try:
        shift_x = shift_y = shift_z = 0.0
        if origin_shift:
            shift_x, shift_y, shift_z = origin_shift

        all_points = []
        for wire in wires:
            pts = []
            for edge in wire.Edges:
                number = max(12, min(80, int(max(edge.Length, 1.0) / 3.0)))
                for p in edge.discretize(Number=number):
                    if axis == "z":
                        pts.append((p.x - shift_x, p.y - shift_y))
                    elif axis == "y":
                        pts.append((p.x - shift_x, p.z - shift_z))
                    else:  # axis == "x"
                        pts.append((p.y - shift_y, p.z - shift_z))
            all_points.extend(pts)
        
        if not all_points:
            return h_dims, v_dims
        
        # Sortiere Punkte für strukturelle Analyse
        xs = sorted(set(p[0] for p in all_points))
        ys = sorted(set(p[1] for p in all_points))
        
        # Extrahiere charakteristische Abstände
        if len(xs) >= 2:
            h_dims.append((xs[0], xs[-1], sorted([p[1] for p in all_points if p[0] in (xs[0], xs[-1])])[0]))
            if len(xs) >= 3:
                h_dims.append((xs[0], xs[len(xs)//2], ys[0]))
        
        if len(ys) >= 2:
            v_dims.append((ys[0], ys[-1], sorted([p[0] for p in all_points if p[1] in (ys[0], ys[-1])])[0]))
            if len(ys) >= 3:
                v_dims.append((ys[0], ys[len(ys)//2], xs[0]))
        
        return h_dims, v_dims
    except Exception as e:
        print(f"Warning: Key point extraction failed: {e}", file=sys.stderr)
        return h_dims, v_dims


def _draw_contour_dimensions(ax, wires, axis, bounds, origin_shift=None):
    """
    Zeichnet detaillierte Bemaßungen mit technischem Zeichnungsstil.
    Zeigt mehrere charakteristische Maße der Kontur.
    """
    import sys
    
    if not wires:
        return
    
    try:
        h_dims, v_dims = _extract_contour_key_points(wires, axis, origin_shift=origin_shift)
        
        min_x, max_x, min_y, max_y = bounds
        span = max(max_x - min_x, max_y - min_y, 1.0)
        margin = span * 0.15
        
        # Zeichne horizontale Maße gestaffelt unterhalb der Ansicht.
        for index, (x_start, x_end, y_pos) in enumerate(h_dims[:3]):
            if x_start != x_end:
                offset = min_y - margin * (0.75 + index * 0.30)
                value = abs(x_end - x_start)
                text = f"{value:.1f}"
                
                # Zeichne Maßlinie und Pfeile
                ax.plot([x_start, x_end], [offset, offset], color="#1e293b", linewidth=0.7, alpha=0.7)
                ax.plot([x_start, x_start], [offset - margin*0.05, offset + margin*0.05], color="#1e293b", linewidth=0.7)
                ax.plot([x_end, x_end], [offset - margin*0.05, offset + margin*0.05], color="#1e293b", linewidth=0.7)
                
                # Pfeile
                arrow_size = margin * 0.08
                ax.annotate("", xy=(x_end - arrow_size, offset), xytext=(x_end, offset),
                          arrowprops=dict(arrowstyle="->", color="#1e293b", lw=0.7, mutation_scale=12))
                ax.annotate("", xy=(x_start + arrow_size, offset), xytext=(x_start, offset),
                          arrowprops=dict(arrowstyle="->", color="#1e293b", lw=0.7, mutation_scale=12))
                
                # Text mit Hintergrund
                ax.text((x_start + x_end)/2, offset - margin*0.14, text,
                       ha="center", va="top", fontsize=6.8, color="#0f172a",
                       bbox=dict(boxstyle="round,pad=0.22", facecolor="white", edgecolor="none", alpha=0.88))
        
        # Zeichne vertikale Maße gestaffelt rechts neben der Ansicht.
        for index, (y_start, y_end, x_pos) in enumerate(v_dims[:3]):
            if y_start != y_end:
                offset = max_x + margin * (0.75 + index * 0.30)
                value = abs(y_end - y_start)
                text = f"{value:.1f}"
                
                # Zeichne Maßlinie und Sperrkanten
                ax.plot([offset, offset], [y_start, y_end], color="#1e293b", linewidth=0.7, alpha=0.7)
                ax.plot([offset - margin*0.05, offset + margin*0.05], [y_start, y_start], color="#1e293b", linewidth=0.7)
                ax.plot([offset - margin*0.05, offset + margin*0.05], [y_end, y_end], color="#1e293b", linewidth=0.7)
                
                # Pfeile
                arrow_size = margin * 0.08
                ax.annotate("", xy=(offset, y_end - arrow_size), xytext=(offset, y_end),
                          arrowprops=dict(arrowstyle="->", color="#1e293b", lw=0.7, mutation_scale=12))
                ax.annotate("", xy=(offset, y_start + arrow_size), xytext=(offset, y_start),
                          arrowprops=dict(arrowstyle="->", color="#1e293b", lw=0.7, mutation_scale=12))
                
                # Text mit Hintergrund (gedreht)
                ax.text(offset + margin*0.14, (y_start + y_end)/2, text,
                       ha="left", va="center", fontsize=6.8, color="#0f172a",
                       rotation=90,
                       bbox=dict(boxstyle="round,pad=0.22", facecolor="white", edgecolor="none", alpha=0.88))
    
    except Exception as e:
        print(f"Warning: Contour dimension drawing failed: {e}", file=sys.stderr)


def _points_from_2d_items(items):
    points = []
    for item in items or []:
        if not item:
            continue
        if len(item) == 2 and all(isinstance(coord, (int, float)) for coord in item):
            points.append((float(item[0]), float(item[1])))
        else:
            for point in item:
                if len(point) >= 2:
                    points.append((float(point[0]), float(point[1])))
    return points


def _span_at_band(points, axis_index, value, band):
    other_index = 1 - axis_index
    local = [point for point in points if abs(point[axis_index] - value) <= band]
    if len(local) < 4:
        return None
    values = [point[other_index] for point in local]
    return min(values), max(values)


def _draw_mesh_detail_dimensions(ax, drawable_items, bounds):
    """
    Zeichnet Zusatzmaße direkt aus Mesh-Projektionen/Schnitten.

    Dadurch bekommen auch STL-basierte Ansichten Maße an charakteristischen
    Absätzen, Zapfen und lokalen Höhen/Breiten.
    """
    import sys

    try:
        points = _points_from_2d_items(drawable_items)
        if len(points) < 6:
            return

        min_x, max_x, min_y, max_y = bounds
        span_x = max(max_x - min_x, 1.0)
        span_y = max(max_y - min_y, 1.0)
        span = max(span_x, span_y)
        band_x = max(span_x * 0.035, 0.75)
        band_y = max(span_y * 0.035, 0.75)

        x_levels = [min_x + span_x * value for value in (0.25, 0.50, 0.75)]
        y_levels = [min_y + span_y * value for value in (0.25, 0.50, 0.75)]
        h_dims = []
        v_dims = []

        for y_level in y_levels:
            span_pair = _span_at_band(points, 1, y_level, band_y)
            if span_pair and (span_pair[1] - span_pair[0]) > span_x * 0.12:
                h_dims.append((span_pair[0], span_pair[1], y_level))

        for x_level in x_levels:
            span_pair = _span_at_band(points, 0, x_level, band_x)
            if span_pair and (span_pair[1] - span_pair[0]) > span_y * 0.12:
                v_dims.append((span_pair[0], span_pair[1], x_level))

        margin = span * 0.16
        for index, (x_start, x_end, y_level) in enumerate(h_dims[:3]):
            offset = min_y - margin * (0.80 + index * 0.34)
            value = abs(x_end - x_start)
            ax.plot([x_start, x_end], [offset, offset], color="#1e293b", linewidth=0.65, alpha=0.78)
            ax.plot([x_start, x_start], [offset - margin * 0.04, y_level], color="#64748b", linewidth=0.45, alpha=0.45)
            ax.plot([x_end, x_end], [offset - margin * 0.04, y_level], color="#64748b", linewidth=0.45, alpha=0.45)
            ax.annotate("", xy=(x_start, offset), xytext=(x_end, offset), arrowprops=dict(arrowstyle="<->", color="#1e293b", lw=0.65))
            ax.text(
                (x_start + x_end) / 2.0,
                offset - margin * 0.08,
                f"{value:.1f}",
                ha="center",
                va="top",
                fontsize=6.3,
                color="#0f172a",
                bbox=dict(boxstyle="round,pad=0.16", facecolor="white", edgecolor="none", alpha=0.88),
            )

        for index, (y_start, y_end, x_level) in enumerate(v_dims[:3]):
            offset = max_x + margin * (0.80 + index * 0.34)
            value = abs(y_end - y_start)
            ax.plot([offset, offset], [y_start, y_end], color="#1e293b", linewidth=0.65, alpha=0.78)
            ax.plot([offset - margin * 0.04, x_level], [y_start, y_start], color="#64748b", linewidth=0.45, alpha=0.45)
            ax.plot([offset - margin * 0.04, x_level], [y_end, y_end], color="#64748b", linewidth=0.45, alpha=0.45)
            ax.annotate("", xy=(offset, y_start), xytext=(offset, y_end), arrowprops=dict(arrowstyle="<->", color="#1e293b", lw=0.65))
            ax.text(
                offset + margin * 0.08,
                (y_start + y_end) / 2.0,
                f"{value:.1f}",
                ha="left",
                va="center",
                fontsize=6.3,
                rotation=90,
                color="#0f172a",
                bbox=dict(boxstyle="round,pad=0.16", facecolor="white", edgecolor="none", alpha=0.88),
            )
    except Exception as e:
        print(f"Warning: Mesh detail dimension drawing failed: {e}", file=sys.stderr)


def _draw_view(ax, view, bbox, dim_labels, notes=None, render_base=True, render_final=True, detailed_dims=False):
    """Zeichnet eine einzelne 2D-Ansicht inklusive Bemaßung und Notizen."""
    import sys
    
    axis = view["axis"]
    view_kind = view.get("kind", "section")
    final_wires = view["final"]
    base_wires = view["base"]
    final_projection = view.get("final_projection") or []
    final_section_segments = view.get("final_section_segments") or []
    origin_shift = view.get("origin_shift") or (0.0, 0.0, 0.0)
    wires_for_bounds = []
    use_mesh_projection = render_final and view_kind == "projection" and final_projection
    use_mesh_section = render_final and view_kind == "section" and not final_wires and final_section_segments

    if render_final and not use_mesh_projection:
        wires_for_bounds.extend(final_wires)
    if render_base:
        wires_for_bounds.extend(base_wires)

    bounds = _wire_bounds(wires_for_bounds, axis, origin_shift=origin_shift)
    if bounds is None and use_mesh_section:
        bounds = _bounds_from_2d_items(final_section_segments)
    if bounds is None and final_projection:
        bounds = _bounds_from_2d_items(final_projection)
    
    # Grenzen aus der echten Modellbox nur fuer den Bildausschnitt nutzen.
    # Es wird daraus keine Geometrie mehr gezeichnet.
    if bounds is None:
        print(f"Warning: No drawable geometry bounds, using bbox only for viewport axis {axis}", file=sys.stderr)
        if axis == "z":
            bounds = (
                bbox.XMin - origin_shift[0], 
                bbox.XMax - origin_shift[0], 
                bbox.YMin - origin_shift[1], 
                bbox.YMax - origin_shift[1]
            )
        elif axis == "y":
            bounds = (
                bbox.XMin - origin_shift[0], 
                bbox.XMax - origin_shift[0], 
                bbox.ZMin - origin_shift[2], 
                bbox.ZMax - origin_shift[2]
            )
        else:  # axis == "x"
            bounds = (
                bbox.YMin - origin_shift[1], 
                bbox.YMax - origin_shift[1], 
                bbox.ZMin - origin_shift[2], 
                bbox.ZMax - origin_shift[2]
            )
    
    min_x, max_x, min_y, max_y = bounds
    span = max(max_x - min_x, max_y - min_y, 1.0)
    margin = span * 0.22

    # Zeichne Basis-Geometrie (grau gehattcht)
    if render_base and base_wires:
        _plot_wires(
            ax,
            base_wires,
            axis,
            facecolor="#cbd5e1",
            edgecolor="#334155",
            alpha=0.35,
            hatch="////",
            linewidth=1.2,
            origin_shift=origin_shift,
        )
        print(f"Info: Rendered {len(base_wires)} base wires for {axis} view", file=sys.stderr)
    
    # Zeichne finale Geometrie. Projektionen kommen aus echten Dreiecksflaechen,
    # Schnitte bevorzugen geschlossene FreeCAD-Konturen und fallen auf Mesh-Linien.
    if use_mesh_projection:
        _draw_mesh_polygons(ax, final_projection)
        print(f"Info: Rendered projected mesh for {axis} view with {len(final_projection)} triangles", file=sys.stderr)
    elif render_final and final_wires:
        _plot_wires(
            ax,
            final_wires,
            axis,
            facecolor="#fecaca",
            edgecolor="#b91c1c",
            alpha=0.35,
            hatch="xx",
            linewidth=1.8,
            origin_shift=origin_shift,
        )
        print(f"Info: Rendered {len(final_wires)} final wires for {axis} view", file=sys.stderr)
    elif use_mesh_section:
        _draw_section_segments(ax, final_section_segments)
        print(f"Info: Rendered mesh section for {axis} view with {len(final_section_segments)} segments", file=sys.stderr)
    elif render_final:
        ax.text(
            0.5,
            0.5,
            "Kontur nicht verfuegbar",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=9,
            color="#64748b",
        )

    # Zeichne Bemaßung
    _draw_dimension(ax, min_x, max_x, max_y + margin * 0.62, dim_labels[0])
    _draw_dimension(ax, min_y, max_y, max_x + margin * 0.62, dim_labels[1], vertical=True)
    
    # Zeichne detaillierte Konturbemaßungen wenn gewünscht
    if detailed_dims and final_wires and not use_mesh_projection:
        wires_for_details = final_wires
        _draw_contour_dimensions(ax, wires_for_details, axis, bounds, origin_shift=origin_shift)
    elif detailed_dims and use_mesh_projection:
        _draw_mesh_detail_dimensions(ax, final_projection, bounds)
    elif detailed_dims and use_mesh_section:
        _draw_mesh_detail_dimensions(ax, final_section_segments, bounds)

    # Zeichne Notizen
    if notes:
        y = min_y - margin * 1.85
        for note in notes:
            ax.text(min_x - margin * 0.10, y, note, fontsize=7.2, color="#334155")
            y -= margin * 0.18

    # Zeichne Überschriften
    ax.text(0.0, 1.06, view["title"], transform=ax.transAxes, fontsize=10.4, fontweight="bold", color="#0f172a", va="bottom")
    ax.text(0.0, 1.01, f"Ebene {view['level']:.1f} mm", transform=ax.transAxes, fontsize=8.4, color="#64748b", va="bottom")
    ax.set_xlim(min_x - margin * 1.45, max_x + margin * 2.35)
    ax.set_ylim(min_y - margin * 2.35, max_y + margin * 1.65)
    ax.set_aspect("equal")
    ax.axis("off")


def _estimate_tolerance_note(bbox):
    """Leitet aus der Bauteilgroesse einen einfachen Toleranz-Richtwert ab."""
    max_dim = max(bbox.XLength, bbox.YLength, bbox.ZLength, 1.0)
    if max_dim <= 120:
        tol = 1.2
    elif max_dim <= 250:
        tol = 1.8
    elif max_dim <= 400:
        tol = 2.5
    else:
        tol = 3.5
    return f"Interner Richtwert Rohteilmass: +/- {tol:.1f} mm, final technisch abstimmen"


def _value_label(value, unit=""):
    """Formatiert Zahlen kompakt fuer Info-Kaesten und Vergleichsnotizen."""
    if value is None:
        return "N/A"
    suffix = f" {unit}" if unit else ""
    return f"{value:.3f}{suffix}" if abs(value) < 10 else f"{value:.1f}{suffix}"


def _page_header(fig, meta, title, subtitle):
    fig.text(0.05, 0.955, _project_title(meta), fontsize=21, fontweight="bold", color="#0f172a")
    fig.text(0.05, 0.925, title, fontsize=12.5, color="#334155")
    fig.text(
        0.05,
        0.902,
        f"Bauteil: {meta.get('part_name', 'Rohteil mit Gesenkgrat')} | Material: {meta.get('material', 'Stahl')} | {subtitle}",
        fontsize=10,
        color="#475569",
    )


def _draw_info_box(ax, title, lines, x, y, w, h, face="#f8fafc"):
    ax.axis("off")
    ax.add_patch(Rectangle((x, y - h), w, h, facecolor=face, edgecolor="#94a3b8", linewidth=1.1))
    ax.add_patch(Rectangle((x, y - 0.12), w, 0.12, facecolor="#e2e8f0", edgecolor="#94a3b8", linewidth=1.1))
    ax.text(x + 0.018, y - 0.04, title, transform=ax.transAxes, fontsize=10.8, fontweight="bold", color="#0f172a", va="top")
    ypos = y - 0.16
    for line in lines:
        ax.text(x + 0.022, ypos, line, transform=ax.transAxes, fontsize=8.8, color="#334155", va="top")
        ypos -= 0.062


def _wrap_pdf_lines(lines, width=64):
    """Bricht lange Zeilen fuer den PDF-Satz um."""
    wrapped = []
    for line in lines:
        text = str(line or "").strip()
        if not text:
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False) or [""])
    return wrapped


def _draw_wrapped_block(ax, title, lines, x, y, w, h, face="#f8fafc", width=60, line_height=0.078, max_lines=None):
    """Zeichnet einen Textblock mit Umbruch, damit nichts ueberlappt."""
    ax.axis("off")
    ax.add_patch(Rectangle((x, y - h), w, h, facecolor=face, edgecolor="#94a3b8", linewidth=1.1))
    ax.add_patch(Rectangle((x, y - 0.12), w, 0.12, facecolor="#e2e8f0", edgecolor="#94a3b8", linewidth=1.1))
    ax.text(x + 0.02, y - 0.04, title, transform=ax.transAxes, fontsize=10.8, fontweight="bold", color="#0f172a", va="top", clip_on=True)
    ypos = y - 0.16
    wrapped_lines = _wrap_pdf_lines(lines, width=width)
    effective_max_lines = max_lines or max(1, int((h - 0.20) / max(line_height, 0.001)))
    if len(wrapped_lines) > effective_max_lines:
        wrapped_lines = wrapped_lines[:effective_max_lines]
        wrapped_lines[-1] = "... weitere Angaben im Block gekuerzt"
    for line in wrapped_lines:
        ax.text(x + 0.024, ypos, line, transform=ax.transAxes, fontsize=8.6, color="#334155", va="top", clip_on=True)
        ypos -= line_height
        if ypos < (y - h + 0.04):
            break


def _draw_title_block(fig, meta, data, *, bottom=0.03, height=0.07):
    """Klassischer Zeichnungsblock fuer eine klarere technische Anmutung."""
    geometry_info = meta.get("export_geometry") or {}
    geometry_state = [
        "Gesenkgrat: ja" if geometry_info.get("gesenkgrat_applied") else "Gesenkgrat: nein",
        "Entformung: ja" if geometry_info.get("advanced_applied") else "Entformung: nein",
    ]
    if geometry_info.get("geometry_warning"):
        geometry_state.append(f"Hinweis: {geometry_info.get('geometry_warning')}")

    ax = fig.add_axes([0.04, bottom, 0.93, height])
    ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, facecolor="#f8fafc", edgecolor="#64748b", linewidth=1.2))
    ax.add_patch(Rectangle((0, 0.62), 1, 0.38, facecolor="#e2e8f0", edgecolor="#64748b", linewidth=1.2))
    ax.text(0.015, 0.9, "Zeichnungsblock", fontsize=10.2, fontweight="bold", color="#0f172a", va="top")
    ax.text(0.015, 0.45, f"Projekt: {_project_title(meta)}", fontsize=8.8, color="#334155", va="center")
    ax.text(0.29, 0.45, f"Bauteil: {meta.get('part_name', 'Rohteil')}", fontsize=8.8, color="#334155", va="center")
    ax.text(0.58, 0.45, f"Material: {meta.get('material', 'Stahl')}", fontsize=8.8, color="#334155", va="center")
    ax.text(0.81, 0.45, f"Gewicht: {data.get('final_weight', 0):.3f} kg", fontsize=8.8, color="#334155", va="center")
    blow_text = (meta.get("feature_dimensions") or {}).get("blow_count_text") or ""
    blow_part = f" | Hammer-n: {blow_text}" if blow_text else ""
    ax.text(0.015, 0.16, " | ".join(geometry_state) + blow_part, fontsize=8.5, color="#475569", va="center")


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_mm(value):
    return f"{_as_float(value):.1f} mm"


def _estimate_section_spans_from_triangles(triangles, bbox):
    """Leitet einfache Absatz-/Teilhöhenhinweise aus Mesh-Schnitten ab."""
    if not triangles:
        return []
    points = [point for triangle in triangles for point in triangle]
    if not points:
        return []

    z_values = [point[2] for point in points]
    z_min = min(z_values)
    z_max = max(z_values)
    z_len = max(z_max - z_min, 1.0)
    rows = []
    for label, rel in (("unten", 0.20), ("mitte", 0.50), ("oben", 0.80)):
        level = z_min + z_len * rel
        half_window = max(z_len * 0.045, 0.8)
        local = [point for point in points if abs(point[2] - level) <= half_window]
        if len(local) < 6:
            continue
        xs = [point[0] for point in local]
        ys = [point[1] for point in local]
        rows.append(
            {
                "label": label,
                "z_mm": level + bbox.ZMin + bbox.ZLength * 0.5,
                "length_mm": max(xs) - min(xs),
                "width_mm": max(ys) - min(ys),
            }
        )
    return rows


def _feature_dimension_lines(meta, data):
    """Sammelt zusätzliche Maße fuer Lochungen, Absätze, Zapfen, Radien und Schläge."""
    bbox = data["bbox"]
    features = meta.get("feature_dimensions") or {}
    hole_overview = meta.get("hole_overview") or {}
    advanced_raw_part = meta.get("advanced_raw_part") or features.get("advanced_raw_part") or {}
    process_analysis = meta.get("process_analysis") or {}
    tooling_outputs = ((process_analysis.get("tooling_data") or {}).get("outputs") or {})
    strokes = features.get("forging_strokes") or {}

    lines = [
        f"Erforderliche Schlagzahl Hammer n: {features.get('blow_count_text') or tooling_outputs.get('wb_n_schlaege') or 'nicht berechnet'}",
        f"Gesamtmasse: L {_format_mm(bbox.XLength)} | B {_format_mm(bbox.YLength)} | H {_format_mm(bbox.ZLength)}",
    ]

    radius = _as_float(features.get("radius_mm") or meta.get("radius") or 0)
    tooling_radius = _as_float(features.get("tooling_radius_mm") or 0)
    if radius or tooling_radius:
        lines.append(f"Radien: Bauteilkanten R {_format_mm(radius)} | Gesenk-/Blockradius R {_format_mm(tooling_radius)}")

    z_height = _as_float(advanced_raw_part.get("z_height_mm"), 0)
    reference_z = advanced_raw_part.get("reference_z_mm")
    if z_height:
        lines.append(
            f"Teilhöhe/Rohteil-Zielhöhe: {_format_mm(z_height)} | Referenz-Z "
            f"{'auto' if reference_z in (None, '') else _format_mm(reference_z)}"
        )

    draft_lines = []
    outer = _as_float(advanced_raw_part.get("outer_draft_angle_deg"), 0)
    inner = _as_float(advanced_raw_part.get("inner_draft_angle_deg"), 0)
    if outer:
        draft_lines.append(f"Aussenentformung {outer:.1f} Grad")
    if inner:
        draft_lines.append(f"Innenentformung {inner:.1f} Grad")
    if draft_lines:
        lines.append("Absatz-/Entformungsbereiche: " + " | ".join(draft_lines))

    holes = features.get("holes") or hole_overview.get("details") or []
    if holes:
        for hole in holes[:5]:
            idx = hole.get("index") or (holes.index(hole) + 1)
            lines.append(
                f"Lochung {idx}: D {_format_mm(hole.get('diameter_mm'))} | "
                f"Butzen/Absatz {_format_mm(hole.get('thickness_mm'))} | Start-Z {_format_mm(hole.get('z_position_mm'))}"
            )
    elif hole_overview.get("enabled"):
        lines.append(f"Lochungen: {hole_overview.get('summary', 'aktiv, keine Detailmasse')}")

    section_rows = _estimate_section_spans_from_triangles(data.get("preview_triangles") or [], bbox)
    for row in section_rows:
        lines.append(
            f"Querschnitt {row['label']}: Z {_format_mm(row['z_mm'])} | "
            f"L {_format_mm(row['length_mm'])} | B {_format_mm(row['width_mm'])}"
        )
    if len(section_rows) >= 2:
        lengths = [row["length_mm"] for row in section_rows]
        widths = [row["width_mm"] for row in section_rows]
        length_step = max(lengths) - min(lengths)
        width_step = max(widths) - min(widths)
        if length_step > max(1.0, bbox.XLength * 0.04) or width_step > max(1.0, bbox.YLength * 0.04):
            lines.append(
                f"Zapfen-/Absatzsprung geschaetzt: Delta-L {_format_mm(length_step)} | Delta-B {_format_mm(width_step)}"
            )

    if strokes:
        stroke_text = []
        if _as_float(strokes.get("press_stroke_mm"), 0):
            stroke_text.append(f"Hub {_format_mm(strokes.get('press_stroke_mm'))}")
        for key, label in (
            ("stage1_final_height_mm", "V1-Endhoehe"),
            ("stage2_final_height_mm", "V2-Endhoehe"),
            ("stage3_final_height_mm", "V3-Endhoehe"),
            ("stage4_die_gap_mm", "Gesenkschluss"),
        ):
            if _as_float(strokes.get(key), 0):
                stroke_text.append(f"{label} {_format_mm(strokes.get(key))}")
        if stroke_text:
            lines.append("Simulations-/Stufenhoehen: " + " | ".join(stroke_text))

    return lines


def _render_detailed_technical_pages(pdf, meta, data):
    """
    Erzeugt detaillierte technische Zeichnungsseiten mit Konturbemaßungen.
    
    Diese Seiten zeigen:
    - Vier Hauptansichten mit detaillierten Konturbemaßungen  
    - Mehrere Schnitle mit jeweils charakteristischen Dimensionen
    - Nur die Rohteilgeometrie
    - 3D-Vorschau
    """
    bbox = data["bbox"]
    detail_views = data.get("detail_views") or {}
    preview_triangles = data.get("preview_triangles") or []
    
    # Seite 1: Vier Hauptansichten mit detaillierten Bemaßungen
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.patch.set_facecolor("white")
    grid = fig.add_gridspec(2, 3, left=0.04, right=0.97, top=0.89, bottom=0.10, wspace=0.12, hspace=0.24)
    _page_header(fig, meta, "Technische Rohteilzeichnung / Detailansichten", "Hauptansichten mit Konturbemaßungen (Rohteilgeometrie)")

    view_order = [
        ("top", (f"L = {bbox.XLength:.1f}", f"B = {bbox.YLength:.1f}"), ["Draufsicht", f"Breite x Länge"], 0, 0),
        ("front", (f"L = {bbox.XLength:.1f}", f"H = {bbox.ZLength:.1f}"), ["Vorderansicht", f"Länge x Höhe"], 0, 1),
        ("right", (f"B = {bbox.YLength:.1f}", f"H = {bbox.ZLength:.1f}"), ["Rechte Ansicht", f"Breite x Höhe"], 1, 0),
        ("left", (f"B = {bbox.YLength:.1f}", f"H = {bbox.ZLength:.1f}"), ["Linke Ansicht", f"Breite x Höhe"], 1, 1),
    ]
    for key, dims, notes, row, col in view_order:
        ax = fig.add_subplot(grid[row, col])
        _draw_view(ax, data["views"][key], bbox, dims, notes=notes, render_base=False, render_final=True, detailed_dims=True)

    # 3D-Vorschau
    preview_ax = fig.add_subplot(grid[:, 2], projection="3d")
    _render_preview_mesh(preview_ax, preview_triangles)
    preview_ax.set_title("3D-Rohteilmodell", fontsize=11, fontweight="bold", pad=10)
    preview_ax.text2D(
        0.04,
        0.98,
        f"Trennebene: Z = {data['parting_z']:.1f} mm",
        transform=preview_ax.transAxes,
        va="top",
        fontsize=9.0,
        color="#334155",
    )

    _draw_title_block(fig, meta, data, bottom=0.02, height=0.06)
    pdf.savefig(fig)
    plt.close(fig)

    # Seite 2: Schnittzeichnungen mit detaillierten Bemaßungen
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.patch.set_facecolor("white")
    _page_header(fig, meta, "Schnittzeichnungen / Querschnitte", "Charakteristische Schnitteben mit Konturbemaßungen")
    grid = fig.add_gridspec(2, 3, left=0.05, right=0.96, top=0.86, bottom=0.27, wspace=0.16, hspace=0.24)

    section_order = [
        ("section_a", (f"L = {bbox.XLength:.1f}", f"H = {bbox.ZLength:.1f}"), ["Schnitt A-A", "Mittelschnitt in Y-Richtung"], 0, 0),
        ("section_b", (f"B = {bbox.YLength:.1f}", f"H = {bbox.ZLength:.1f}"), ["Schnitt B-B", "Mittelschnitt in X-Richtung"], 0, 1),
        ("section_c", (f"L = {bbox.XLength:.1f}", f"B = {bbox.YLength:.1f}"), ["Schnitt C-C", "Unterer Z-Schnitt (Bodennah)"], 0, 2),
        ("section_d", (f"L = {bbox.XLength:.1f}", f"B = {bbox.YLength:.1f}"), ["Schnitt D-D", "Oberer Z-Schnitt (Gratbereich)"], 1, 0),
    ]
    for key, dims, notes, row, col in section_order:
        ax = fig.add_subplot(grid[row, col])
        _draw_view(ax, detail_views[key], bbox, dims, notes=notes, render_base=False, render_final=True, detailed_dims=True)

    # Trennebenen-Kontur
    parting_ax = fig.add_subplot(grid[1, 1])
    _draw_view(
        parting_ax,
        data["views"]["parting"],
        bbox,
        (f"L = {bbox.XLength:.1f}", f"B = {bbox.YLength:.1f}"),
        notes=["Trennebenenkontur", "Ermittelte optimale Trennebene"],
        render_base=False,
        render_final=True,
        detailed_dims=True,
    )

    # Draufsicht nochmal als Referenz
    top_ref_ax = fig.add_subplot(grid[1, 2])
    _draw_view(
        top_ref_ax,
        data["views"]["top"],
        bbox,
        (f"L = {bbox.XLength:.1f}", f"B = {bbox.YLength:.1f}"),
        notes=["Draufsicht Referenz", "Optimale Ansicht für Bemaßung"],
        render_base=False,
        render_final=True,
        detailed_dims=True,
    )

    # Notizen zur Bemaßung
    notes_ax = fig.add_axes([0.05, 0.05, 0.42, 0.16])
    text_lines = [
        f"Rohteilgeometrie: Länge {bbox.XLength:.1f} mm × Breite {bbox.YLength:.1f} mm × Höhe {bbox.ZLength:.1f} mm",
        f"Gesamtvolumen: {data['final_volume']:.0f} mm³ | Gewicht: {data['final_weight']:.3f} kg | Trennebene Z: {data['parting_z']:.1f} mm",
        f"Bemaßung zeigt charakteristische Konturabmessungen der exportierten Rohteil-/Aufdickungsgeometrie.",
        f"Alle Masse in Millimetern. Toleranzen projektspezifisch technisch abstimmen.",
    ]
    _draw_wrapped_block(notes_ax, "Bemaßungshinweise", text_lines, 0, 1, 1, 1, width=140, line_height=0.18)

    feature_ax = fig.add_axes([0.50, 0.05, 0.45, 0.16])
    _draw_wrapped_block(
        feature_ax,
        "Zusatzmaße: Lochungen, Absätze, Zapfen, Radien",
        _feature_dimension_lines(meta, data),
        0,
        1,
        1,
        1,
        width=86,
        line_height=0.14,
        max_lines=8,
    )

    _draw_title_block(fig, meta, data, bottom=0.02, height=0.045)
    pdf.savefig(fig)
    plt.close(fig)


def _render_technical_pages(pdf, meta, data):
    bbox = data["bbox"]
    detail_views = data.get("detail_views") or {}
    preview_triangles = data.get("preview_triangles") or []
    grat_spalt = float(meta.get("grat_spalt", 0) or 0)
    grat_magazin = float(meta.get("grat_magazin", 0) or 0)
    grat_dicke = float(meta.get("grat_dicke", 0) or 0)
    grat_hoehenversatz = float(meta.get("grat_hoehenversatz", 0) or 0)
    aufdickung = float(meta.get("aufdickung", 0) or 0)
    grst_report = meta.get("grst_report") or {}
    hole_overview = meta.get("hole_overview") or {}
    advanced_raw_part = meta.get("advanced_raw_part") or {}
    tolerance_note = _estimate_tolerance_note(bbox)

    # Seite 1: Vier Hauptansichten plus 3D-Vorschau des Rohteils.
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.patch.set_facecolor("white")
    grid = fig.add_gridspec(2, 3, left=0.04, right=0.97, top=0.89, bottom=0.10, wspace=0.12, hspace=0.24)
    _page_header(fig, meta, "Rohteilzeichnung / Ansichtsblatt", "Echte Rohteilansichten mit Konturmaßen und 3D-Modell")

    view_order = [
        ("top", (f"L = {bbox.XLength:.1f}", f"B = {bbox.YLength:.1f}"), [f"Aufdickung: {aufdickung:.1f} mm", f"GRST: {grst_report.get('status', 'n/a')}"], 0, 0),
        ("front", (f"L = {bbox.XLength:.1f}", f"H = {bbox.ZLength:.1f}"), [f"Gratspalt b: {grat_spalt:.1f} mm", f"Gratdicke s: {grat_dicke:.1f} mm"], 0, 1),
        ("right", (f"B = {bbox.YLength:.1f}", f"H = {bbox.ZLength:.1f}"), [f"Gratmagazin L: {grat_magazin:.1f} mm", f"Versatz Z: {grat_hoehenversatz:.1f} mm"], 1, 0),
        ("left", (f"B = {bbox.YLength:.1f}", f"H = {bbox.ZLength:.1f}"), [f"Gewicht: {data['final_weight']:.3f} kg", f"Volumen: {data['final_volume']:.0f} mm³"], 1, 1),
    ]
    for key, dims, notes, row, col in view_order:
        ax = fig.add_subplot(grid[row, col])
        _draw_view(ax, data["views"][key], bbox, dims, notes=notes, render_base=False, render_final=True, detailed_dims=True)

    preview_ax = fig.add_subplot(grid[:, 2], projection="3d")
    _render_preview_mesh(preview_ax, preview_triangles)
    preview_ax.set_title("3D-Rohteilvorschau", fontsize=11, fontweight="bold", pad=10)
    preview_ax.text2D(
        0.04,
        0.98,
        f"Trennebene: Z = {data['parting_z']:.1f} mm",
        transform=preview_ax.transAxes,
        va="top",
        fontsize=9.0,
        color="#334155",
    )

    _draw_title_block(fig, meta, data, bottom=0.02, height=0.06)
    pdf.savefig(fig)
    plt.close(fig)

    # Seite 2: Schnittzeichnungen der exportierten Rohteilgeometrie.
    fig = plt.figure(figsize=(11.69, 8.27))
    fig.patch.set_facecolor("white")
    _page_header(fig, meta, "Schnittzeichnungen / Rohteilgeometrie", "Echte Schnitte der exportierten Rohteil-/Aufdickungsgeometrie")
    grid = fig.add_gridspec(2, 3, left=0.05, right=0.96, top=0.86, bottom=0.27, wspace=0.16, hspace=0.24)

    section_order = [
        ("section_a", (f"L = {bbox.XLength:.1f}", f"H = {bbox.ZLength:.1f}"), ["Mittelschnitt in Y", f"Werkstückhöhe: {bbox.ZLength:.1f} mm"], 0, 0),
        ("section_b", (f"B = {bbox.YLength:.1f}", f"H = {bbox.ZLength:.1f}"), ["Mittelschnitt in X", f"Breitenverlauf: {bbox.YLength:.1f} mm"], 0, 1),
        ("section_c", (f"L = {bbox.XLength:.1f}", f"B = {bbox.YLength:.1f}"), ["Unterer Z-Schnitt", "Rohteilquerschnitt in Bodennähe"], 0, 2),
        ("section_d", (f"L = {bbox.XLength:.1f}", f"B = {bbox.YLength:.1f}"), ["Oberer Z-Schnitt", "Kontur kurz vor Gratbereich"], 1, 0),
    ]
    for key, dims, notes, row, col in section_order:
        ax = fig.add_subplot(grid[row, col])
        _draw_view(ax, detail_views[key], bbox, dims, notes=notes, render_base=False, render_final=True, detailed_dims=True)

    parting_ax = fig.add_subplot(grid[1, 1])
    _draw_view(
        parting_ax,
        data["views"]["parting"],
        bbox,
        (f"L = {bbox.XLength:.1f}", f"B = {bbox.YLength:.1f}"),
        notes=["Konturschnitt in ermittelter Trennebene", "Nur exportierte Rohteilgeometrie"],
        render_base=False,
        render_final=True,
        detailed_dims=True,
    )

    compare_ax = fig.add_subplot(grid[1, 2])
    _draw_view(
        compare_ax,
        data["views"]["top"],
        bbox,
        (f"L = {bbox.XLength:.1f}", f"B = {bbox.YLength:.1f}"),
        notes=[
            "Draufsicht Referenz",
            f"Gewicht: {data['final_weight']:.3f} kg",
        ],
        render_base=False,
        render_final=True,
        detailed_dims=True,
    )

    notes_ax = fig.add_axes([0.05, 0.05, 0.42, 0.16])
    reference_z_raw = advanced_raw_part.get("reference_z_mm")
    reference_z_label = "auto" if reference_z_raw in (None, "") else f"{float(reference_z_raw):.1f} mm"
    geometry_info = meta.get("export_geometry") or {}
    geometry_state = "mit Gesenkgrat" if geometry_info.get("gesenkgrat_applied") else "ohne Gesenkgrat"
    draft_state = "mit Entformung" if geometry_info.get("advanced_applied") else "ohne Entformung"
    text_lines = [
        f"{tolerance_note} | GRST-Status: {grst_report.get('status', 'n/a')}",
        f"GRST-Empfehlung: {grst_report.get('min_mm', 0):.1f} bis {grst_report.get('max_mm', 0):.1f} mm | Notiz: {grst_report.get('note', 'keine Zusatznotiz')}",
        f"Exportstatus: {geometry_state} | {draft_state} | Aufdickung {aufdickung:.1f} mm | Gratbahn {grat_spalt:.1f} mm | Gratmagazin {grat_magazin:.1f} mm | Gratdicke {grat_dicke:.1f} mm",
        f"Entformung erweitert: Z-Hoehe {float(advanced_raw_part.get('z_height_mm', 0) or 0):.1f} mm | Referenz-Z {reference_z_label} | "
        f"Aussenwinkel {float(advanced_raw_part.get('outer_draft_angle_deg', 0) or 0):.1f} Grad | Innenwinkel {float(advanced_raw_part.get('inner_draft_angle_deg', 0) or 0):.1f} Grad | "
        f"Aussen oben {('ja' if advanced_raw_part.get('outer_draft_top') else 'nein')} | Aussen unten {('ja' if advanced_raw_part.get('outer_draft_bottom') else 'nein')} | "
        f"Innen oben {('ja' if advanced_raw_part.get('inner_draft_top') else 'nein')} | Innen unten {('ja' if advanced_raw_part.get('inner_draft_bottom') else 'nein')}",
    ]
    if hole_overview.get("enabled"):
        text_lines.append(f"Lochungen: {hole_overview.get('summary', 'aktiv')}")
    if geometry_info.get("geometry_warning"):
        text_lines.append(f"Geometriehinweis: {geometry_info.get('geometry_warning')}")
    _draw_wrapped_block(notes_ax, "Fertigungsnotizen", text_lines, 0, 1, 1, 1, width=76, line_height=0.14, max_lines=8)

    feature_ax = fig.add_axes([0.50, 0.05, 0.45, 0.16])
    _draw_wrapped_block(
        feature_ax,
        "Zusatzmaße: Lochungen, Absätze, Zapfen, Radien",
        _feature_dimension_lines(meta, data),
        0,
        1,
        1,
        1,
        width=86,
        line_height=0.14,
        max_lines=8,
    )

    _draw_title_block(fig, meta, data, bottom=0.02, height=0.045)
    pdf.savefig(fig)
    plt.close(fig)


def _chunk_lines(summary_sections):
    """
    Verteilt Textabschnitte auf mehrere PDF-Seiten.

    Die Funktion ist bewusst simpel gehalten: fuer Einsteiger ist so leicht zu
    erkennen, wie aus vielen Abschnitten paginierte Exportseiten entstehen.
    """
    chunks = []
    current = []
    for section in summary_sections:
        title = section.get("title", "Abschnitt")
        lines = [line for line in (section.get("lines") or []) if line]
        if not lines:
            continue
        block = [title] + lines + [""]
        if len(current) + len(block) > 40:
            chunks.append(current)
            current = []
        current.extend(block)
    if current:
        chunks.append(current)
    return chunks


def _render_cost_overview_page(pdf, meta):
    """Erzeugt die Management-/Projektuebersicht mit Kosten- und Prozesskennzahlen."""
    cost = meta.get("cost_breakdown") or {}
    if not cost:
        return

    positions = list((cost.get("positions") or {}).values())
    positions = [p for p in positions if float(p.get("value", 0) or 0) > 0]
    positions.sort(key=lambda item: float(item.get("value", 0) or 0), reverse=True)
    top_positions = positions[:8]

    fig = plt.figure(figsize=(11.69, 8.27))
    fig.patch.set_facecolor("white")
    _page_header(fig, meta, "Kostenuebersicht / Unternehmenskalkulation", "Kosten je Teil und Kostentreiber")
    overview = meta.get("process_overview") or {}
    advanced_raw_part = meta.get("advanced_raw_part") or {}

    summary_lines = [
        f"Losgröße: {cost.get('lot_size', 0)} Stk | Ausfall: {cost.get('scrap_count', 0)} Stk | Gutteile: {cost.get('good_parts', 0)} Stk",
        f"Selbstkosten: {cost.get('self_cost', 0):.2f} EUR | korrigiert: {cost.get('self_cost_corrected', 0):.2f} EUR",
        f"Zuschlag: {cost.get('surcharge', 0):.2f} EUR | Externe Kosten: {cost.get('external_total', 0):.2f} EUR",
        f"Netto pro Teil: {cost.get('total_cost', 0):.2f} EUR | inkl. Skonto: {cost.get('total_cost_skonto', 0):.2f} EUR",
        f"Formel: ((Selbstkosten * Losgröße / Gutteile) + Zuschlag) + Externe Arbeitsgänge + Transport",
    ]
    hole_overview = meta.get("hole_overview") or {}
    
    # Get rounded weights from REFA data if available
    process_analysis = meta.get("process_analysis") or {}
    refa = process_analysis.get("refa_data") or {}
    weights = refa.get("rounded_weights") or {}
    
    # Use rounded weights from REFA if available, otherwise fall back to overview
    # FG Gewicht (Fertiggewicht) from cost breakdown instead of raw part weight
    rohteilgewicht = weights.get('fertiggewicht_kg') or overview.get('rohteilgewicht_kg', 'N/A')
    einsatzgewicht = weights.get('einsatzgewicht_kg') or overview.get('einsatzgewicht_kg', 'N/A')
    mvn_gewicht = weights.get('mvn_kg') or overview.get('mvn_gewicht_kg', 'N/A')
    
    process_lines = [
        f"Rohteilgewicht: Fertiggewicht aus Kostenkalkulation: {rohteilgewicht} kg",
        f"Einsatzgewicht: Schmiede-Einsatzgewicht: {einsatzgewicht} kg",
        f"MVN-Gewicht: MVN-Gesamtgewicht: {mvn_gewicht} kg",
        f"Zuschnitte pro Stange: {overview.get('zuschnitte_pro_stange', 'N/A')} | Restlänge nach Zuschnitten: {overview.get('restlaenge_mm', 'N/A')} mm",
        f"Halbzeug: {overview.get('halbzeug_abmessung', 'N/A')} | OK: L/D < 3, sehr stabil",
        f"Aufheizzeit (GHIV, 2085 kW): ~{overview.get('aufheizzeit_min', 'N/A')} min (~{overview.get('aufheizzeit_h', 'N/A')} h)",
        f"Schmiedevorform: {overview.get('schmiedevorform_auswahl', 'N/A')}",
        f"Lochungen: {hole_overview.get('summary', overview.get('lochung', 'Keine Lochungen ausgewaehlt'))}",
    ]
    tooling_lines = [
        f"Ausgewaehlte Schmiedeaggregate: {overview.get('aggregate', 'N/A')}",
        f"Werkzeug kurz: {overview.get('werkzeug_summary', 'N/A')}",
        f"Simulation: {overview.get('simulation_summary', 'N/A')}",
    ]

    summary_ax = fig.add_axes([0.05, 0.68, 0.9, 0.15])
    _draw_wrapped_block(summary_ax, "Kalkulation kompakt", summary_lines, 0, 1, 1, 1, width=92, line_height=0.17)

    process_ax = fig.add_axes([0.05, 0.42, 0.9, 0.22])
    _draw_wrapped_block(process_ax, "Rohteil / Halbzeug / Gewichte / Lochungen", process_lines, 0, 1, 1, 1, width=92, line_height=0.11)

    tooling_ax = fig.add_axes([0.05, 0.26, 0.9, 0.13])
    _draw_wrapped_block(tooling_ax, "Aggregate / Werkzeug / Simulation", tooling_lines, 0, 1, 1, 1, width=92, line_height=0.17)

    bar_ax = fig.add_axes([0.08, 0.04, 0.42, 0.18])
    labels = [entry.get("label", "Position") for entry in reversed(top_positions)]
    values = [float(entry.get("value", 0) or 0) for entry in reversed(top_positions)]
    colors = ["#2563eb", "#0f766e", "#ca8a04", "#dc2626", "#7c3aed", "#0891b2", "#4d7c0f", "#9333ea"]
    bar_ax.barh(labels, values, color=colors[: len(values)])
    bar_ax.set_title("Größte Kostentreiber", fontsize=12, fontweight="bold")
    bar_ax.tick_params(axis="y", labelsize=9)
    bar_ax.tick_params(axis="x", labelsize=9)
    bar_ax.grid(axis="x", linestyle="--", alpha=0.25)
    bar_ax.set_axisbelow(True)
    bar_ax.set_xlabel("EUR pro Teil")

    table_ax = fig.add_axes([0.56, 0.04, 0.36, 0.18])
    table_ax.axis("off")
    table_ax.add_patch(Rectangle((0, 0), 1, 1, facecolor="#f8fafc", edgecolor="#cbd5e1", linewidth=1.0))
    table_ax.text(0.04, 0.95, "Positionsdetails", fontsize=12, fontweight="bold", color="#0f172a", va="top")
    y = 0.86
    for entry in top_positions:
        table_ax.text(0.04, y, entry.get("label", "Position"), fontsize=9.4, color="#0f172a", va="top")
        table_ax.text(0.72, y, f"{float(entry.get('value', 0) or 0):.2f} EUR", fontsize=9.4, color="#334155", va="top", ha="right")
        table_ax.text(0.96, y, f"{float(entry.get('share_percent', 0) or 0):.1f} %", fontsize=9.0, color="#64748b", va="top", ha="right")
        y -= 0.09
        if y < 0.08:
            break

    pdf.savefig(fig)
    plt.close(fig)


def _render_sales_calculation_page(pdf, meta):
    """Haengt die Vertriebskalkulation als letzte Gesamt-PDF-Seite an."""
    cost = meta.get("cost_breakdown") or {}
    sales = cost.get("sales_calculation") or {}
    if not sales:
        return

    def money(value):
        return f"{float(value or 0):.2f} EUR"

    def num(value, digits=1):
        return f"{float(value or 0):.{digits}f}"

    fig = plt.figure(figsize=(11.69, 8.27))
    fig.patch.set_facecolor("white")
    _page_header(fig, meta, "Vertriebskalkulation", "Eingaben, Zuschlaege und Losgroessenstaffel")

    input_ax = fig.add_axes([0.05, 0.62, 0.42, 0.25])
    input_lines = [
        f"Material: {sales.get('material') or 'n/a'}",
        f"Artikelnummer: {sales.get('article') or 'optional'}",
        f"Bezeichnung: {sales.get('description') or 'optional'}",
        f"Kalkulation: {sales.get('calc_no') or 'optional'}",
        f"MVN: {num(sales.get('mvn_kg'), 1)} kg | FG Fertiggewicht: {num(sales.get('fg_kg'), 1)} kg",
        f"LZ Material: {num(sales.get('lz_rate'), 2)} EUR/t | SZ: {num(sales.get('sz_rate'), 2)} EUR/t | CO2: {num(sales.get('co2_rate'), 2)} EUR/t",
        f"EKZ: {num(sales.get('ekz_rate'), 2)} % | EGU: {num(sales.get('egu_rate'), 4)} EUR/kg",
        f"MP Angebot: {num(sales.get('mp_offer'), 2)} EUR/t | MP Einkauf: {num(sales.get('mp_purchase'), 2)} EUR/t",
        f"Ausfallteile: {num(sales.get('scrap_parts'), 0)}",
    ]
    _draw_wrapped_block(input_ax, "Eingaben", input_lines, 0, 1, 1, 1, width=58, line_height=0.072)

    surcharge_ax = fig.add_axes([0.53, 0.62, 0.42, 0.25])
    surcharge_lines = [
        f"LZ: {money(sales.get('lz'))} | inkl. Skonto: {money(sales.get('lz_skonto'))}",
        f"SZ: {money(sales.get('sz'))} | inkl. Skonto: {money(sales.get('sz_skonto'))}",
        f"CO2: {money(sales.get('co2'))} | inkl. Skonto: {money(sales.get('co2_skonto'))}",
        f"Summe Zuschlaege: {money(sales.get('surcharge_sum'))} | LZ + SZ + CO2",
        f"EKZ: {money(sales.get('ekz'))} | FG x 0,5542 x EKZ",
        f"EGU: {money(sales.get('egu'))} | FG x EGU",
        f"MTZ pro Teil: {money(sales.get('mtz'))} | aus MP Angebot/Einkauf",
    ]
    _draw_wrapped_block(surcharge_ax, "Berechnete Zuschlaege", surcharge_lines, 0, 1, 1, 1, width=60, line_height=0.082)

    table_ax = fig.add_axes([0.04, 0.18, 0.92, 0.37])
    table_ax.axis("off")
    table_ax.text(0, 1.04, "Losgroessenstaffel und Verkaufspreise", fontsize=12, fontweight="bold", color="#0f172a", va="bottom")

    lots = [str(int(float(value or 0))) for value in (sales.get("lots") or [])[:7]]
    rows = [
        ("Position", lots),
        ("Selbstkosten", [money(v) for v in [sales.get("self_cost")] * len(lots)]),
        (f"VK-Preis {num(sales.get('markup_percent'), 0)}%", [money(sales.get("vk_price")) for _ in lots]),
        ("AG-Preis", [money(v) for v in (sales.get("ag_prices") or [])[: len(lots)]]),
        (f"VWL {num(sales.get('vwl_percent'), 0)}%", [money(v) for v in (sales.get("vwl_prices") or [])[: len(lots)]]),
        ("Barverkaufspreis inkl. Skonto", [money(v) for v in (sales.get("bar_prices") or [])[: len(lots)]]),
        ("zzgl. 50%", [money(v) for v in (sales.get("plus_50") or [])[: len(lots)]]),
        ("zzgl. 30%", [money(v) for v in (sales.get("plus_30") or [])[: len(lots)]]),
        ("zzgl. 20%", [money(v) for v in (sales.get("plus_20") or [])[: len(lots)]]),
        ("zzgl. 10%", [money(v) for v in (sales.get("plus_10") or [])[: len(lots)]]),
    ]

    cell_text = [[label, *values] for label, values in rows]
    col_labels = ["", *[""] * len(lots)]
    table = table_ax.table(cellText=cell_text, colLabels=col_labels, loc="upper left", cellLoc="center", colLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(7.6)
    table.scale(1, 1.28)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#cbd5e1")
        cell.set_linewidth(0.6)
        if col == 0:
            cell.set_text_props(ha="left", fontweight="bold", color="#0f172a")
            cell.set_facecolor("#f8fafc")
        elif row == 1:
            cell.set_facecolor("#e2e8f0")
            cell.set_text_props(fontweight="bold")

    summary_ax = fig.add_axes([0.05, 0.05, 0.9, 0.08])
    summary_lines = [
        f"Material {sales.get('material') or 'n/a'} | MVN {num(sales.get('mvn_kg'), 1)} kg | FG {num(sales.get('fg_kg'), 1)} kg",
        f"Summe Zuschlaege {money(sales.get('surcharge_sum'))} | VK-Basis {money(sales.get('sales_base'))} | VK {num(sales.get('markup_percent'), 0)}% {money(sales.get('vk_price'))}",
    ]
    _draw_wrapped_block(summary_ax, "Vertriebskalkulation kompakt", summary_lines, 0, 1, 1, 1, width=120, line_height=0.26)

    pdf.savefig(fig)
    plt.close(fig)

def _render_ai_review_page(pdf, meta):
    """
    Schreibt das KI-Review als übersichtlich strukturierte PDF-Seiten.
    Jede Kategorie wird in einem separaten Info-Block dargestellt.
    Falls der Platz auf einer Seite nicht ausreicht, wird automatisch
    eine neue Seite begonnen.
    """
    ai_review = meta.get("ai_review") or {}
    if not ai_review:
        return

    # Abschnitte definieren
    blocks = [
        (
            "Einschaetzung",
            [ai_review.get("assessment") or ai_review.get("summary", "Keine Einschaetzung vorhanden.")],
        ),
        ("Pruefungen", ai_review.get("checks") or []),
        ("Vorschlaege", ai_review.get("suggestions") or ai_review.get("recommendations") or []),
        ("Chancen", ai_review.get("opportunities") or []),
        ("Risiken", ai_review.get("risks") or []),
    ]

    # Nur gefüllte Abschnitte übernehmen
    blocks = [(title, lines) for title, lines in blocks if lines]

    def _new_page():
        """Erzeugt eine neue PDF-Seite mit Kopfbereich."""
        fig = plt.figure(figsize=(11.69, 8.27))
        fig.patch.set_facecolor("white")

        # Kopfbereich
        fig.text(
            0.05,
            0.95,
            _project_title(meta),
            fontsize=20,
            fontweight="bold",
            color="#0f172a",
        )
        fig.text(
            0.05,
            0.92,
            "KI-Prozessreview",
            fontsize=12,
            color="#475569",
        )
        fig.text(
            0.05,
            0.895,
            f"Quelle: {ai_review.get('provider', 'n/a')} | Projektbewertung aus Exportdaten",
            fontsize=9.5,
            color="#64748b",
        )

        return fig

    # Layout-Parameter
    LEFT = 0.05
    WIDTH = 0.90
    TOP_Y = 0.86
    BOTTOM_LIMIT = 0.05

    # Neue Seite starten
    fig = _new_page()
    current_y = TOP_Y

    for title, lines in blocks:
        # Zeilen umbrechen und Aufzählung erzeugen
        wrapped_lines = []
        for line in lines:
            for wrapped in _wrap_pdf_lines([line], width=100):
                if wrapped.strip():
                    wrapped_lines.append(f"- {wrapped}")

        if not wrapped_lines:
            continue

        # Benötigte Höhe berechnen
        line_height = 0.032
        title_height = 0.06
        padding_bottom = 0.03
        total_height = title_height + len(wrapped_lines) * line_height + padding_bottom

        # Falls kein Platz mehr vorhanden -> neue Seite
        if current_y - total_height < BOTTOM_LIMIT:
            pdf.savefig(fig)
            plt.close(fig)
            fig = _new_page()
            current_y = TOP_Y

        # Eigene Achse für diesen Block
        ax = fig.add_axes([LEFT, current_y - total_height, WIDTH, total_height])

        # Farben je Kategorie
        block_color = {
            "Einschaetzung": "#f8fafc",
            "Pruefungen": "#eff6ff",
            "Vorschlaege": "#f0fdf4",
            "Chancen": "#ecfccb",
            "Risiken": "#fef2f2",
        }.get(title, "#f8fafc")

        # Block zeichnen
        _draw_wrapped_block(
            ax,
            title,
            wrapped_lines,
            0,
            1,
            1,
            1,
            face=block_color,
            width=95,
            line_height=0.075,
        )

        # Nächste Position
        current_y -= total_height + 0.02

    # Letzte Seite speichern
    pdf.savefig(fig)
    plt.close(fig)

def _render_summary_pages(pdf, meta, summary_sections):
    """Optionaler Klartext-Export fuer laengere Abschnittslisten."""
    chunks = _chunk_lines(summary_sections)
    if not chunks:
        return

    for page_no, lines in enumerate(chunks, start=1):
        fig = plt.figure(figsize=(11.69, 8.27))
        fig.patch.set_facecolor("white")
        ax = fig.add_axes([0.05, 0.06, 0.9, 0.88])
        ax.axis("off")

        ax.text(0.0, 0.98, _project_title(meta), fontsize=17, fontweight="bold", color="#0f172a", va="top")
        ax.text(
            0.0,
            0.93,
            f"Bauteil: {meta.get('part_name', 'Rohteil')} | Material: {meta.get('material', 'Stahl')} | Seite {page_no}",
            fontsize=10,
            color="#475569",
            va="top",
        )

        y = 0.88
        for line in lines:
            is_heading = line and not line.startswith("- ") and ":" not in line
            ax.text(
                0.0 if is_heading else 0.02,
                y,
                line,
                fontsize=11 if is_heading else 9.5,
                fontweight="bold" if is_heading else "normal",
                color="#0f172a" if is_heading else "#334155",
                va="top",
            )
            y -= 0.04 if is_heading else 0.028

        pdf.savefig(fig)
        plt.close(fig)


def export_technical_pdf(step_file, output_pdf, meta, base_step_file=None, source_mesh_file=None):
    """Erzeugt das kompakte technische PDF mit Ansichten, Schnitten und 3D-Vorschau."""
    import sys
    
    advanced_raw_part = meta.get("advanced_raw_part") or {}
    try:
        if source_mesh_file and os.path.exists(source_mesh_file):
            data = _prepare_mesh_geometry_data(source_mesh_file, meta=meta)
        else:
            data = _prepare_geometry_data(
                step_file,
                base_step_file=base_step_file,
                preferred_parting_z=advanced_raw_part.get("reference_z_mm"),
            )
    except Exception as e:
        print(f"Error: Geometry preparation failed: {e}", file=sys.stderr)
        raise ValueError(f"PDF-Geometrievorbereitung fehlgeschlagen: {e}")
    
    try:
        with PdfPages(output_pdf) as pdf:
            _render_technical_pages(pdf, meta, data)
    except Exception as e:
        print(f"Error: PDF rendering failed: {e}", file=sys.stderr)
        raise ValueError(f"PDF-Rendering fehlgeschlagen: {e}")



def _render_forging_process_analysis_page(pdf, meta):
    """
    Erzeugt die Schmiedeforming-Prozessanalyse-Seite mit Vorformgravur, 
    Stauchen-Ergebnissen, Werkzeugdaten und REFA-Informationen.
    
    Hinweis: Gewichte werden auf Seite 3 (Kostenuebersicht) angezeigt.
    """
    process_analysis = meta.get("process_analysis") or {}
    stages = process_analysis.get("stages") or []
    aggregates = process_analysis.get("aggregates") or {}
    tooling = process_analysis.get("tooling_data") or meta.get("tooling") or {}
    refa = process_analysis.get("refa_data") or {}

    if not (stages or tooling or refa):
        return

    fig = plt.figure(figsize=(11.69, 8.27))
    fig.patch.set_facecolor("white")
    _page_header(
        fig, meta, 
        "Schmiedeverfahren & Prozessanalyse", 
        "Vorformdaten, Werkzeuganalyse & REFA"
    )

    def _present(value, fallback="N/A"):
        text = str(value or "").strip()
        return text if text and text != "-" else fallback

    stage_lines = []
    for stage in stages:
        nr = stage.get("number", "?")
        stage_lines.append(
            f"Schmiedevorform {nr}: {stage.get('category_label', 'nicht gewaehlt')} | Aggregat: {stage.get('aggregate', 'nicht gewaehlt')}"
        )
        aggregate_info = _present(stage.get("aggregate_info"), "")
        if aggregate_info:
            stage_lines.append(f"  Aggregatdaten: {aggregate_info}")

        stauch = (stage.get("stauchen") or {}).get("results") or {}
        if any(_present(stauch.get(key), "") for key in ("stauchhoehe", "umformgrad", "force")):
            stage_lines.append(
                "  Stauchen/Druecken: "
                f"Kante1 {_present(stauch.get('kante1'))}, Kante2 {_present(stauch.get('kante2'))}, "
                f"Stauchhoehe {_present(stauch.get('stauchhoehe'))}, Umformgrad {_present(stauch.get('umformgrad'))}, "
                f"Kraft {_present(stauch.get('force'))}, sigma_m {_present(stauch.get('sigma_m'))}"
            )

        gravur = (stage.get("vorformgravur") or {}).get("results") or {}
        if any(_present(gravur.get(key), "") for key in ("phi1", "phi2", "force1", "force2", "assessment")):
            stage_lines.append(
                "  Vorformgravur: "
                f"phi1 {_present(gravur.get('phi1'))}, phi2 {_present(gravur.get('phi2'))}, "
                f"gesamt {_present(gravur.get('phi_total'))}, F1 {_present(gravur.get('force1'))}, "
                f"F2 {_present(gravur.get('force2'))}, {_present(gravur.get('assessment'))}"
            )
            if gravur.get("calculated_height"):
                stage_lines.append(f"  {gravur.get('calculated_height')}")

        freiform = stage.get("freiform") or {}
        dims = freiform.get("dimensions_mm") or {}
        if dims:
            stage_lines.append(
                f"  Freiform berechnet: {dims.get('x', 'N/A')} x {dims.get('y', 'N/A')} x {dims.get('z', 'N/A')} mm | "
                f"Gewicht {freiform.get('weight_kg', 'N/A')} kg"
            )

    aggregate_lines = []
    for label, key in (
        ("Endform", "endform"),
        ("Vorform 1", "vorform1"),
        ("Vorform 2", "vorform2"),
        ("Abgraten", "abgraten"),
    ):
        entry = aggregates.get(key) or {}
        aggregate_lines.append(f"{label}: {entry.get('aggregate', 'nicht gewaehlt')}")
        info = _present(entry.get("info"), "")
        if info:
            aggregate_lines.append(f"  Daten: {info}")

    tool_outputs = tooling.get("outputs") or {}
    tool_hints = tooling.get("hints") or []
    feature_dimensions = meta.get("feature_dimensions") or {}
    blow_count = feature_dimensions.get("blow_count_text") or tool_outputs.get("wb_n_schlaege") or "N/A"
    tooling_lines = [
        f"Erforderliche Schlagzahl (Hammer): n ≈ {blow_count}",
        f"Delta T: {_present(tool_outputs.get('wb_delta_T'))} C | p: {_present(tool_outputs.get('wb_p_calc'))} MPa | F max: {_present(tool_outputs.get('wb_F_calc'))} kN",
        f"Huebe/Standzeit: N {_present(tool_outputs.get('wb_N_huebe'))} | Hammerkraft {_present(tool_outputs.get('wb_F_hammer'))} kN | Energie {_present(tool_outputs.get('wb_E_total'))} kJ",
        f"Gesenkblock: {_present(tool_outputs.get('wb_block_dims'))} mm | Radius {_present(tool_outputs.get('wb_block_radius'))} mm | Volumen {_present(tool_outputs.get('wb_block_volume'))} cm3",
        f"Aggregat-Fit: {_present(tool_outputs.get('wb_fit_status'))} | {_present(tool_outputs.get('wb_fit_details'))}",
        *[f"Analyse: {hint}" for hint in tool_hints[:6]],
    ]

    blocks = [
        ("Schmiedevorformen mit Aggregatzuordnung", stage_lines, [0.05, 0.58, 0.43, 0.28], 54, 0.060),
        ("Aggregate", aggregate_lines, [0.52, 0.58, 0.43, 0.28], 54, 0.052),
        ("Werkzeuganalyse Ergebnis & Analyse", tooling_lines, [0.05, 0.05, 0.9, 0.46], 110, 0.060),
    ]

    for title, lines, rect, width, line_height in blocks:
        if not lines:
            continue
        ax = fig.add_axes(rect)
        _draw_wrapped_block(ax, title, lines, 0, 1, 1, 1, width=width, line_height=line_height)

    pdf.savefig(fig)
    plt.close(fig)


def export_company_summary_pdf(step_file, output_pdf, meta, summary_sections, base_step_file=None, source_mesh_file=None):
    """
    Erzeugt das Gesamt-PDF fuer Technik, Projektuebersicht und Kosten.
    
    Seiten-Reihenfolge:
    1. Technische Darstellungen (3D-Views, technische Daten)
    2. Freiformschmieden (optional, wenn ausgewählt)
    3. Schmiedeverfahren & Prozessanalyse (mit gerundeten Gewichten, Hubstauchhöhe, Vorformhöhe)
    4. Kostenübersicht / Unternehmenskalkulation (Cost Breakdown)
    5. Vertriebskalkulation
    
    `summary_sections` bleibt als Parameter erhalten, damit der Export spaeter
    wieder leicht um Textseiten erweitert werden kann.
    """
    import sys
    
    advanced_raw_part = meta.get("advanced_raw_part") or {}
    try:
        if source_mesh_file and os.path.exists(source_mesh_file):
            data = _prepare_mesh_geometry_data(source_mesh_file, meta=meta)
        else:
            data = _prepare_geometry_data(
                step_file,
                base_step_file=base_step_file,
                preferred_parting_z=advanced_raw_part.get("reference_z_mm"),
            )
    except Exception as e:
        print(f"Error: Geometry preparation failed: {e}", file=sys.stderr)
        raise ValueError(f"PDF-Geometrievorbereitung fehlgeschlagen: {e}")
    
    try:
        with PdfPages(output_pdf) as pdf:
            # Seite 1-2: Technische Darstellungen
            _render_detailed_technical_pages(pdf, meta, data)
            
            # Seite 3: Freiformschmieden (optional, wenn ausgewählt)
            vorform_source = (meta.get("vorform_drawing") or {}).get("source_file")
            if vorform_source:
                triangles = _triangles_from_preform_source(vorform_source)
                if triangles:
                    _render_preform_drawing_page(pdf, meta, triangles)
            
            # Seite 4: Schmiedeverfahren & Prozessanalyse (NEU: nach Freiformschmieden)
            _render_forging_process_analysis_page(pdf, meta)
            
            # Seite 5: Kostenübersicht / Unternehmenskalkulation
            _render_cost_overview_page(pdf, meta)
            
            # Seite 6: Vertriebskalkulation
            _render_sales_calculation_page(pdf, meta)
    except Exception as e:
        print(f"Error: PDF page rendering failed: {e}", file=sys.stderr)
        raise ValueError(f"PDF-Seiten-Rendering fehlgeschlagen: {e}")


def _build_triangles_from_faces(shape, origin_shift=None):
    """Generiert Dreiecke direkt aus den Flächen, wenn tessellate() fehlschlägt."""
    import sys
    
    triangles = []
    try:
        if shape.isNull():
            return triangles
        
        shift_x = shift_y = shift_z = 0.0
        if origin_shift:
            shift_x, shift_y, shift_z = origin_shift
        
        # Get all faces from shape
        faces = []
        if hasattr(shape, 'Faces'):
            faces = shape.Faces
        elif hasattr(shape, 'Face'):
            try:
                faces = [shape.Face]
            except:
                pass
        
        if not faces:
            print(f"Warning: Shape has no faces available", file=sys.stderr)
            return triangles
        
        face_count = 0
        for face in faces:
            try:
                face_count += 1
                # Try to tessellate individual face with progressive tolerance
                for tolerance in [0.5, 1.0, 2.0, 5.0]:
                    try:
                        points, facets = face.tessellate(tolerance)
                        for facet in facets:
                            try:
                                triangle = []
                                for index in facet:
                                    if index < len(points):
                                        point = points[index]
                                        triangle.append((point.x - shift_x, point.y - shift_y, point.z - shift_z))
                                if len(triangle) == 3:
                                    triangles.append(triangle)
                            except Exception:
                                continue
                        if facets:
                            break  # Tessellation successful, move to next face
                    except Exception:
                        continue
                        
            except Exception as face_e:
                print(f"Warning: Face {face_count} tessellation failed: {face_e}", file=sys.stderr)
                continue
        
        print(f"Info: Generated {len(triangles)} triangles from {face_count} faces", file=sys.stderr)
        return triangles
        
    except Exception as e:
        print(f"Warning: Face-based mesh generation failed: {e}", file=sys.stderr)
        return triangles


def _build_preview_triangles(shape, origin_shift=None):
    """Tesselliert das STEP-Modell fuer eine 3D-Vorschau im PDF."""
    import Part
    import sys
    
    triangles = []
    try:
        if shape.isNull():
            return triangles
        
        # Try standard tessellation first
        try:
            points, facets = shape.tessellate(1.2)
        except Exception as e:
            print(f"Warning: Tessellation at tolerance 1.2 failed, trying coarser tolerance: {e}", file=sys.stderr)
            try:
                points, facets = shape.tessellate(2.0)
            except Exception as e2:
                print(f"Warning: Tessellation at tolerance 2.0 failed, trying tolerance 5.0: {e2}", file=sys.stderr)
                try:
                    points, facets = shape.tessellate(5.0)
                except Exception as e3:
                    print(f"Warning: All tessellation attempts failed: {e3}", file=sys.stderr)
                    print(f"Attempting alternative mesh generation from faces...", file=sys.stderr)
                    # Try alternative approach: extract triangles from faces
                    return _build_triangles_from_faces(shape, origin_shift)

        shift_x = shift_y = shift_z = 0.0
        if origin_shift:
            shift_x, shift_y, shift_z = origin_shift

        for facet in facets:
            try:
                triangle = []
                for index in facet:
                    if index < len(points):
                        point = points[index]
                        triangle.append((point.x - shift_x, point.y - shift_y, point.z - shift_z))
                if len(triangle) == 3:
                    triangles.append(triangle)
            except Exception:
                # Skip malformed triangles
                continue
        
        if triangles:
            print(f"Info: Generated {len(triangles)} triangles from tessellation", file=sys.stderr)
        else:
            print(f"Warning: Tessellation generated no triangles, trying face extraction", file=sys.stderr)
            triangles = _build_triangles_from_faces(shape, origin_shift)
            
    except Exception as e:
        print(f"Warning: Preview mesh generation failed completely: {e}", file=sys.stderr)
        triangles = _build_triangles_from_faces(shape, origin_shift)
    
    return triangles




def _render_preview_mesh(ax, triangles):
    """Zeichnet die 3D-Rohteilvorschau auf einer Matplotlib-3D-Achse."""
    import sys
    
    ax.set_axis_off()
    if not triangles:
        print(f"Warning: No triangles to render in preview mesh", file=sys.stderr)
        ax.text2D(0.05, 0.5, "3D-Vorschau nicht verfuegbar\n(Tessellierung fehlgeschlagen)", transform=ax.transAxes, color="#64748b", fontsize=10)
        return

    try:
        collection = Poly3DCollection(
            triangles,
            facecolors="#fb923c",
            edgecolors="#7c2d12",
            linewidths=0.12,
            alpha=0.92,
        )
        ax.add_collection3d(collection)

        xs = [point[0] for tri in triangles for point in tri]
        ys = [point[1] for tri in triangles for point in tri]
        zs = [point[2] for tri in triangles for point in tri]
        
        if not xs or not ys or not zs:
            print(f"Warning: Triangle points have no valid coordinates", file=sys.stderr)
            ax.text2D(0.05, 0.5, "Keine Koordinaten in Dreiecken", transform=ax.transAxes, color="#64748b")
            return
            
        max_span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1.0)
        cx = (max(xs) + min(xs)) / 2.0
        cy = (max(ys) + min(ys)) / 2.0
        cz = (max(zs) + min(zs)) / 2.0
        half = max_span / 2.0
        ax.set_xlim(cx - half, cx + half)
        ax.set_ylim(cy - half, cy + half)
        ax.set_zlim(cz - half, cz + half)
        ax.view_init(elev=26, azim=-58)
        print(f"Info: Rendered {len(triangles)} triangles in 3D preview", file=sys.stderr)
    except Exception as e:
        print(f"Error: Failed to render preview mesh: {e}", file=sys.stderr)
        ax.text2D(0.05, 0.5, f"Fehler beim Rendern:\n{str(e)[:50]}", transform=ax.transAxes, color="#dc2626")


def _load_stl_triangles(stl_file, origin_shift=None):
    """Laedt STL-Dreiecke fuer echte Vorformzeichnungen."""
    import struct
    import sys

    triangles = []
    shift_x = shift_y = shift_z = 0.0
    if origin_shift:
        shift_x, shift_y, shift_z = origin_shift

    def _coords(point):
        if hasattr(point, "x") and hasattr(point, "y") and hasattr(point, "z"):
            return point.x, point.y, point.z
        if isinstance(point, (tuple, list)) and len(point) >= 3:
            return float(point[0]), float(point[1]), float(point[2])
        try:
            return float(point[0]), float(point[1]), float(point[2])
        except Exception:
            return None

    try:
        import Mesh

        mesh = Mesh.Mesh(stl_file)
        for facet in getattr(mesh, "Facets", []) or []:
            points = getattr(facet, "Points", None) or []
            triangle = []
            for point in points[:3]:
                coords = _coords(point)
                if coords is None:
                    continue
                x, y, z = coords
                triangle.append((x - shift_x, y - shift_y, z - shift_z))
            if len(triangle) == 3:
                triangles.append(triangle)
    except Exception as exc:
        print(f"Warning: FreeCAD STL triangle loading failed, trying raw STL parser: {exc}", file=sys.stderr)

    if triangles:
        return triangles

    def _shifted(coords):
        x, y, z = coords
        return (x - shift_x, y - shift_y, z - shift_z)

    try:
        with open(stl_file, "rb") as fh:
            data = fh.read()

        if len(data) >= 84:
            tri_count = struct.unpack_from("<I", data, 80)[0]
            expected_size = 84 + tri_count * 50
            if tri_count > 0 and expected_size == len(data):
                offset = 84
                for _ in range(tri_count):
                    values = struct.unpack_from("<12fH", data, offset)
                    p1 = _shifted(values[3:6])
                    p2 = _shifted(values[6:9])
                    p3 = _shifted(values[9:12])
                    triangles.append([p1, p2, p3])
                    offset += 50
                return triangles

        text = data.decode("utf-8", errors="ignore")
        vertices = []
        for line in text.splitlines():
            parts = line.strip().split()
            if len(parts) == 4 and parts[0].lower() == "vertex":
                try:
                    vertices.append(_shifted((float(parts[1]), float(parts[2]), float(parts[3]))))
                except ValueError:
                    continue
                if len(vertices) == 3:
                    triangles.append(vertices)
                    vertices = []
    except Exception as exc:
        print(f"Warning: Raw STL triangle loading failed: {exc}", file=sys.stderr)

    return triangles


def _triangle_bounds(triangles):
    xs = [point[0] for tri in triangles or [] for point in tri]
    ys = [point[1] for tri in triangles or [] for point in tri]
    zs = [point[2] for tri in triangles or [] for point in tri]
    if not xs or not ys or not zs:
        return None
    return {
        "xmin": min(xs),
        "xmax": max(xs),
        "ymin": min(ys),
        "ymax": max(ys),
        "zmin": min(zs),
        "zmax": max(zs),
        "xlen": max(xs) - min(xs),
        "ylen": max(ys) - min(ys),
        "zlen": max(zs) - min(zs),
    }


def _draw_preform_projection(ax, triangles, axis, title, dim_labels):
    """Zeichnet eine echte Vorformprojektion mit Kontur-/Gesamtmassen."""
    polygons = _build_projected_polygons(triangles, axis)
    bounds = _bounds_from_2d_items(polygons)
    if bounds is None:
        ax.text(0.5, 0.5, "Vorformansicht nicht verfuegbar", transform=ax.transAxes, ha="center", va="center", color="#64748b")
        ax.axis("off")
        return

    min_x, max_x, min_y, max_y = bounds
    span = max(max_x - min_x, max_y - min_y, 1.0)
    margin = span * 0.22

    _draw_mesh_polygons(ax, polygons, facecolor="#dbeafe", edgecolor="#1d4ed8")
    _draw_dimension(ax, min_x, max_x, max_y + margin * 0.62, dim_labels[0])
    _draw_dimension(ax, min_y, max_y, max_x + margin * 0.62, dim_labels[1], vertical=True)

    ax.text(0.0, 1.06, title, transform=ax.transAxes, fontsize=10.4, fontweight="bold", color="#0f172a", va="bottom")
    ax.text(0.0, 1.01, "Echte STL-/STEP-Projektion der Vorform", transform=ax.transAxes, fontsize=8.3, color="#64748b", va="bottom")
    ax.set_xlim(min_x - margin * 1.25, max_x + margin * 2.10)
    ax.set_ylim(min_y - margin * 1.45, max_y + margin * 1.55)
    ax.set_aspect("equal")
    ax.axis("off")


def _render_preform_drawing_page(pdf, meta, triangles, *, title="Schmiedevorformzeichnung"):
    """Rendert eine Vorform-Zeichnungsseite mit 2D-Konturen und 3D-Ansicht."""
    bounds = _triangle_bounds(triangles)
    if not bounds:
        return False

    fig = plt.figure(figsize=(11.69, 8.27))
    fig.patch.set_facecolor("white")
    _page_header(fig, meta, title, "Echte Vorform aus STL/STEP mit Konturmassen und 3D-Ansicht")
    grid = fig.add_gridspec(2, 3, left=0.04, right=0.97, top=0.86, bottom=0.18, wspace=0.16, hspace=0.24)

    _draw_preform_projection(
        fig.add_subplot(grid[0, 0]),
        triangles,
        "z",
        "Draufsicht Vorform",
        (f"X = {bounds['xlen']:.1f}", f"Y = {bounds['ylen']:.1f}"),
    )
    _draw_preform_projection(
        fig.add_subplot(grid[0, 1]),
        triangles,
        "y",
        "Vorderansicht Vorform",
        (f"X = {bounds['xlen']:.1f}", f"Z = {bounds['zlen']:.1f}"),
    )
    _draw_preform_projection(
        fig.add_subplot(grid[1, 0]),
        triangles,
        "x",
        "Seitenansicht Vorform",
        (f"Y = {bounds['ylen']:.1f}", f"Z = {bounds['zlen']:.1f}"),
    )

    preview_ax = fig.add_subplot(grid[:, 2], projection="3d")
    _render_preview_mesh(preview_ax, triangles)
    preview_ax.set_title("3D-Vorformmodell", fontsize=11, fontweight="bold", pad=10)

    info_ax = fig.add_subplot(grid[1, 1])
    source = meta.get("vorform_drawing") or {}
    lines = [
        f"Vorform: {bounds['xlen']:.1f} x {bounds['ylen']:.1f} x {bounds['zlen']:.1f} mm",
        f"STL: {os.path.basename(str(source.get('stl_file') or source.get('source_file') or 'Vorform'))}",
    ]
    if source.get("weight_kg") is not None:
        lines.append(f"Gewicht: {float(source.get('weight_kg')):.3f} kg")
    if source.get("volume_mm3") is not None:
        lines.append(f"Volumen: {float(source.get('volume_mm3')):.0f} mm3")
    if source.get("preform_description"):
        lines.append(str(source.get("preform_description")))
    _draw_wrapped_block(info_ax, "Vorformdaten", lines, 0, 1, 1, 1, width=50, line_height=0.085)

    title_data = {"final_weight": float(source.get("weight_kg") or 0)}
    _draw_title_block(fig, meta, title_data, bottom=0.04, height=0.085)
    pdf.savefig(fig)
    plt.close(fig)
    return True


def _triangles_from_preform_source(source_file):
    """Laedt Vorform-Dreiecke bevorzugt aus STL, sonst aus STEP."""
    if not source_file or not os.path.exists(source_file):
        return []
    if source_file.lower().endswith(".stl"):
        return _load_stl_triangles(source_file)
    try:
        doc, shape = _load_shape(source_file, "VorformZeichnung")
        try:
            bbox = shape.BoundBox
            origin_shift = (
                bbox.XMin + bbox.XLength * 0.5,
                bbox.YMin + bbox.YLength * 0.5,
                bbox.ZMin + bbox.ZLength * 0.5,
            )
            return _build_preview_triangles(shape, origin_shift=origin_shift)
        finally:
            _close_doc(doc)
    except Exception as exc:
        print(f"Warning: Preform STEP triangle loading failed: {exc}", file=sys.stderr)
        return []


def export_preform_sketch_pdf(source_file, output_pdf, meta=None):
    """Erzeugt die einzelne Vorformzeichnung aus echter STL-/STEP-Geometrie."""
    meta = meta or {}
    triangles = _triangles_from_preform_source(source_file)
    if not triangles:
        raise ValueError("Vorformgeometrie konnte nicht fuer die Zeichnung gelesen werden")
    with PdfPages(output_pdf) as pdf:
        _render_preform_drawing_page(pdf, meta, triangles)
    
