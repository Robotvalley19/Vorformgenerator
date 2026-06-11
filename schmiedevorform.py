"""
Generiert Schmiedevorformen (Vorform 1 und 2) aus dem berechneten Zuschnitt.

Diese Datei:
- Nimmt das Halbzeug/Vormaterial (berechneten Zuschnitt)
- Skaliert es auf ca. 75% der X-Y Projektion des Fertigteils
- Berechnet die Z-Höhe durch Volumenerhaltungssatz
- Exportiert die Vorform als STEP und STL
- Generiert optional eine Skizzenzeichnung mit Bemaßung
"""

import math
import os
import FreeCAD
import MeshPart
import Part
import stl_quality

STEEL_DENSITY = 7.85e-6  # kg/mm^3
COVERAGE_PERCENTAGE = 0.75  # ca. 75% der Fertigteilgeometrie in X/Y
PROFILE_SAMPLE_COUNT = 13


def _safe_float(value, default=0.0):
    """Konvertiert beliebige Eingaben robust nach `float`."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _mesh_deflection_for_shape(shape, high_quality=False):
    """Leitet sinnvolle Mesh-Parameter aus der Bauteilgroesse ab."""
    bbox = shape.BoundBox
    max_dim = max(bbox.XLength, bbox.YLength, bbox.ZLength, 1.0)
    if high_quality:
        linear = max(0.03, min(0.35, max_dim / 2400.0))
        angular = 0.08 if max_dim > 800 else 0.06
    else:
        linear = max(0.08, min(0.8, max_dim / 1200.0))
        angular = 0.18 if max_dim > 800 else 0.12
    return linear, angular


def _bbox_dict(shape):
    bbox = shape.BoundBox
    return {"x": bbox.XLength, "y": bbox.YLength, "z": bbox.ZLength}


def _validate_exported_stl(stl_path, shape, debug_logs):
    """Prueft die gerade exportierte STL hart gegen das CAD-Solid."""
    report = stl_quality.validate_stl(
        stl_path,
        expected_volume=shape.Volume,
        expected_bbox=_bbox_dict(shape),
        tolerance=1.0e-4,
    )
    debug_logs.extend(stl_quality.format_quality_report(report))
    for warning in report.get("warnings", []):
        debug_logs.append(f"STL-Warnung: {warning}")
    if report.get("status") != "ok":
        for error in report.get("errors", []):
            debug_logs.append(f"STL-Fehler: {error}")
        raise ValueError("STL-Qualitaetspruefung fehlgeschlagen: " + "; ".join(report.get("errors", [])))
    return report


def _normalize_shape_coordinates(shape, debug_logs=None):
    """Zentriert verschobene STEP-Geometrien auf das globale Koordinatenursprung."""
    bbox = shape.BoundBox
    center_offset = FreeCAD.Vector(
        -(bbox.XMin + bbox.XMax) / 2.0,
        -(bbox.YMin + bbox.YMax) / 2.0,
        -(bbox.ZMin + bbox.ZMax) / 2.0,
    )
    if any(abs(v) > 1e-3 for v in (center_offset.x, center_offset.y, center_offset.z)):
        shape.translate(center_offset)
        if debug_logs is not None:
            debug_logs.append(
                f"Koordinatensystem kompensiert: Modell um {center_offset.x:.1f}, {center_offset.y:.1f}, {center_offset.z:.1f} mm zentriert"
            )
    return shape


def _get_xy_projection_dimensions(shape):
    """
    Ermittelt die Ausdehnung der Geometrie auf der X-Y Ebene.
    Gibt (width_x, depth_y) zurück.
    """
    bbox = shape.BoundBox
    width_x = bbox.XLength
    depth_y = bbox.YLength
    return width_x, depth_y


def _bbox_axis_value(bbox, axis, edge):
    return getattr(bbox, f"{axis.upper()}{edge}")


def _bbox_axis_length(bbox, axis):
    return getattr(bbox, f"{axis.upper()}Length")


def _vector_axis_value(vector, axis):
    return getattr(vector, axis)


def _transverse_axes(axis):
    return [candidate for candidate in ("x", "y", "z") if candidate != axis]


def _slice_profile_span(shape, axis, position):
    """Ermittelt Querschnittsabmessungen an einer Station ueber einen FreeCAD-Schnitt."""
    normal = {
        "x": FreeCAD.Vector(1, 0, 0),
        "y": FreeCAD.Vector(0, 1, 0),
        "z": FreeCAD.Vector(0, 0, 1),
    }[axis]
    transverse = _transverse_axes(axis)
    try:
        wires = shape.slice(normal, position) or []
    except Exception:
        return None

    mins = {name: None for name in transverse}
    maxs = {name: None for name in transverse}
    for wire in wires:
        bbox = wire.BoundBox
        spans = [_bbox_axis_length(bbox, name) for name in transverse]
        if max(spans) <= 1e-6:
            continue
        for name in transverse:
            low = _bbox_axis_value(bbox, name, "Min")
            high = _bbox_axis_value(bbox, name, "Max")
            mins[name] = low if mins[name] is None else min(mins[name], low)
            maxs[name] = high if maxs[name] is None else max(maxs[name], high)

    if any(mins[name] is None or maxs[name] is None for name in transverse):
        return None

    span_a = max(maxs[transverse[0]] - mins[transverse[0]], 0.0)
    span_b = max(maxs[transverse[1]] - mins[transverse[1]], 0.0)
    if span_a <= 1e-6 or span_b <= 1e-6:
        return None
    return span_a, span_b


def _fallback_profile_span_from_points(points, axis, position, half_window):
    transverse = _transverse_axes(axis)
    local = [
        point for point in points
        if abs(_vector_axis_value(point, axis) - position) <= half_window
    ]
    if len(local) < 3:
        return None
    span_a = max(_vector_axis_value(point, transverse[0]) for point in local) - min(
        _vector_axis_value(point, transverse[0]) for point in local
    )
    span_b = max(_vector_axis_value(point, transverse[1]) for point in local) - min(
        _vector_axis_value(point, transverse[1]) for point in local
    )
    if span_a <= 1e-6 or span_b <= 1e-6:
        return None
    return span_a, span_b


def _shape_sample_points(shape, max_dim):
    """Liefert robuste Stuetzpunkte fuer Profil-Fallbacks, ohne die STEP-Geometrie zu veraendern."""
    points = []
    try:
        tolerance = max(0.35, min(4.0, max_dim / 180.0))
        tess_points, _ = shape.tessellate(tolerance)
        points.extend(tess_points or [])
    except Exception:
        pass

    if not points:
        try:
            points.extend(vertex.Point for vertex in shape.Vertexes)
        except Exception:
            pass
    return points


def _fill_missing_profile_values(values):
    filled = list(values)
    known = [(idx, value) for idx, value in enumerate(filled) if value is not None and value > 0]
    if not known:
        return [1.0 for _ in filled]

    for idx, value in enumerate(filled):
        if value is not None and value > 0:
            continue
        left = next(((i, v) for i, v in reversed(known) if i < idx), None)
        right = next(((i, v) for i, v in known if i > idx), None)
        if left and right:
            t = (idx - left[0]) / max(right[0] - left[0], 1)
            filled[idx] = left[1] + (right[1] - left[1]) * t
        elif left:
            filled[idx] = left[1]
        else:
            filled[idx] = right[1]
    return filled


def _sample_longitudinal_profile(shape, axis="x", sample_count=PROFILE_SAMPLE_COUNT, debug_logs=None):
    """
    Erstellt ein Längsprofil aus Querschnitts-Schnitten.

    Die Skizzen in Vorschmiedeskizzen.pdf beschreiben im Kern Profilfolgen:
    einseitig/beseitig abgeschmiedet, abgesetzt, Kopfplatten, Stege usw.
    Diese Funktion reduziert das reale Rohteil deshalb auf eine normierte
    Querschnittskurve entlang der X-Achse.
    """
    bbox = shape.BoundBox
    length = max(_bbox_axis_length(bbox, axis), 1.0)
    axis_min = _bbox_axis_value(bbox, axis, "Min")
    max_dim = max(bbox.XLength, bbox.YLength, bbox.ZLength, 1.0)
    points = _shape_sample_points(shape, max_dim)
    raw_area_values = []
    raw_linear_values = []
    sample_rows = []

    for index in range(sample_count):
        t = index / max(sample_count - 1, 1)
        # Exakte Randebenen liefern bei manchen STEP-Solids leere Schnitte.
        sample_t = min(0.975, max(0.025, t))
        position = axis_min + length * sample_t
        span = _slice_profile_span(shape, axis, position)
        if span is None and points:
            span = _fallback_profile_span_from_points(points, axis, position, length / max(sample_count * 1.35, 1))
        if span is None:
            raw_area_values.append(None)
            raw_linear_values.append(None)
            sample_rows.append({"pos": t, "area_proxy": None, "linear_proxy": None})
            continue
        area_proxy = max(span[0] * span[1], 1e-6)
        linear_proxy = math.sqrt(area_proxy)
        raw_area_values.append(area_proxy)
        raw_linear_values.append(linear_proxy)
        sample_rows.append({"pos": t, "area_proxy": area_proxy, "linear_proxy": linear_proxy})

    area_values = _fill_missing_profile_values(raw_area_values)
    linear_values = _fill_missing_profile_values(raw_linear_values)
    max_area = max(area_values) if area_values else 1.0
    max_linear = max(linear_values) if linear_values else 1.0
    area_norm = [value / max_area for value in area_values]
    linear_norm = [value / max_linear for value in linear_values]

    for row, area_value, linear_value, area_n, linear_n in zip(sample_rows, area_values, linear_values, area_norm, linear_norm):
        row["area_proxy"] = area_value
        row["linear_proxy"] = linear_value
        row["area_norm"] = area_n
        row["linear_norm"] = linear_n

    middle = sample_count // 2
    left_mean = sum(linear_norm[:3]) / max(min(3, len(linear_norm)), 1)
    right_mean = sum(linear_norm[-3:]) / max(min(3, len(linear_norm)), 1)
    center_slice = linear_norm[max(0, middle - 1):min(len(linear_norm), middle + 2)]
    center_mean = sum(center_slice) / max(len(center_slice), 1)
    min_value = min(linear_norm) if linear_norm else 1.0
    max_value = max(linear_norm) if linear_norm else 1.0
    mean_value = sum(linear_norm) / max(len(linear_norm), 1)
    variance = sum((value - mean_value) ** 2 for value in linear_norm) / max(len(linear_norm), 1)
    variation = (max_value - min_value) / max(max_value, 1e-6)

    profile = {
        "axis": axis,
        "samples": sample_rows,
        "left_mean": left_mean,
        "right_mean": right_mean,
        "center_mean": center_mean,
        "min_value": min_value,
        "max_value": max_value,
        "mean_value": mean_value,
        "stddev": math.sqrt(variance),
        "variation": variation,
        "both_end_heavy": left_mean > center_mean * 1.16 and right_mean > center_mean * 1.16,
        "one_end_heavy": abs(left_mean - right_mean) > 0.18 and max(left_mean, right_mean) > center_mean * 1.10,
        "middle_heavy": center_mean > max(left_mean, right_mean) * 1.14,
        "left_heavier": left_mean > right_mean * 1.12,
        "right_heavier": right_mean > left_mean * 1.12,
        "waist_ratio": center_mean / max(left_mean, right_mean, 1e-6),
    }

    if debug_logs is not None:
        debug_logs.append(
            "Längsprofil X: "
            f"links {left_mean:.2f}, mitte {center_mean:.2f}, rechts {right_mean:.2f}, "
            f"Variation {variation:.2f}, Taille {profile['waist_ratio']:.2f}"
        )
    return profile


def _create_billet_shape_from_specs(billet_specs, debug_logs=None):
    """Erzeugt das Vormaterial aus Halbzeug-Form, Querschnitt und Zuschnittlaenge."""
    specs = billet_specs or {}
    form = str(specs.get("form") or "").strip().lower()
    cross_section_mm = _safe_float(specs.get("querschnitt_mm") or specs.get("diameter_mm") or specs.get("side_mm"), 0)
    length_mm = _safe_float(specs.get("length_mm") or specs.get("laenge_mm"), 0)
    if form not in {"vkt", "rund"} or cross_section_mm <= 0 or length_mm <= 0:
        return None

    if form == "rund":
        shape = Part.makeCylinder(cross_section_mm / 2.0, length_mm, FreeCAD.Vector(-length_mm / 2.0, 0, 0), FreeCAD.Vector(1, 0, 0))
        label = f"Rund Ø {cross_section_mm:.1f} x {length_mm:.1f} mm"
    else:
        shape = Part.makeBox(
            length_mm,
            cross_section_mm,
            cross_section_mm,
            FreeCAD.Vector(-length_mm / 2.0, -cross_section_mm / 2.0, -cross_section_mm / 2.0),
        )
        label = f"VKT {cross_section_mm:.1f} x {cross_section_mm:.1f} x {length_mm:.1f} mm"

    if debug_logs is not None:
        debug_logs.append(f"Halbzeug aus Vormaterial-Rechner aufgebaut: {label}, Vol={shape.Volume:.1f} mm³")
    return shape


def _rounded_octagon_wire_x(x_pos, width_y, height_z, corner_factor=0.18):
    """Erzeugt einen robusten oktogonalen Querschnitt in der Y/Z-Ebene."""
    half_y = max(width_y / 2.0, 0.5)
    half_z = max(height_z / 2.0, 0.5)
    cut_y = min(half_y * corner_factor, half_y * 0.42)
    cut_z = min(half_z * corner_factor, half_z * 0.42)
    pts = [
        FreeCAD.Vector(x_pos, -half_y + cut_y, -half_z),
        FreeCAD.Vector(x_pos, half_y - cut_y, -half_z),
        FreeCAD.Vector(x_pos, half_y, -half_z + cut_z),
        FreeCAD.Vector(x_pos, half_y, half_z - cut_z),
        FreeCAD.Vector(x_pos, half_y - cut_y, half_z),
        FreeCAD.Vector(x_pos, -half_y + cut_y, half_z),
        FreeCAD.Vector(x_pos, -half_y, half_z - cut_z),
        FreeCAD.Vector(x_pos, -half_y, -half_z + cut_z),
        FreeCAD.Vector(x_pos, -half_y + cut_y, -half_z),
    ]
    return Part.Wire(Part.makePolygon(pts).Edges)


def _analyze_part_geometry(fertigteil_shape, debug_logs=None):
    """
    Analysiert die Rohteil-Geometrie um den optimalen Vorform-Typ zu bestimmen.
    
    Gibt einen Dict mit Geometrie-Charakteristiken zurück:
    - part_type: 'rotational', 'shaft', 'bone', 'irregular'
    - aspect_ratio_xy: Verhältnis X/Y
    - aspect_ratio_xz: Verhältnis X/Z
    - aspect_ratio_yz: Verhältnis Y/Z
    - symmetry_score: Rotationssymmetrie-Wahrscheinlichkeit (0.0-1.0)
    - center_of_mass_deviation: Abweichung des Massenschwerpunkts (0.0-1.0)
    """
    bbox = fertigteil_shape.BoundBox
    x_len = max(bbox.XLength, 1.0)
    y_len = max(bbox.YLength, 1.0)
    z_len = max(bbox.ZLength, 1.0)
    yz_diff = abs(y_len - z_len) / max(y_len, z_len)
    xy_diff = abs(x_len - y_len) / max(x_len, y_len)
    aspect_xy = x_len / y_len if y_len > 0 else 1.0
    aspect_xz = x_len / z_len if z_len > 0 else 1.0
    aspect_yz = y_len / z_len if z_len > 0 else 1.0
    
    # Berechne Symmetrie-Score durch Analyse der Bounding-Box Verhältnisse
    symmetry_score = 0.0
    center_of_mass_deviation = 0.0
    
    try:
        # Wenn Y und Z ähnlich sind (rotationssymmetrisch um X-Achse)
        if yz_diff < 0.1:
            symmetry_score = 0.9
        elif yz_diff < 0.2:
            symmetry_score = 0.8
        elif yz_diff < 0.3:
            symmetry_score = 0.7
            
        # Wenn X und Y ähnlich sind (rotationssymmetrisch um Z-Achse)
        if xy_diff < 0.15:
            symmetry_score = max(symmetry_score, 0.8)
            
        # Versuche, Schwerpunkt-Abweichung zu detektieren (für Knochenform)
        try:
            center_of_mass = fertigteil_shape.CenterOfMass
            # Relative Abweichung vom geometrischen Mittelpunkt
            geom_center_x = (bbox.XMin + bbox.XMax) / 2.0
            geom_center_y = (bbox.YMin + bbox.YMax) / 2.0
            geom_center_z = (bbox.ZMin + bbox.ZMax) / 2.0
            
            max_dist = max(x_len, y_len, z_len)
            com_offset = (
                (geom_center_x - center_of_mass.x)**2 +
                (geom_center_y - center_of_mass.y)**2 +
                (geom_center_z - center_of_mass.z)**2
            ) ** 0.5
            center_of_mass_deviation = min(1.0, com_offset / max_dist)
        except:
            pass
    except:
        pass

    longitudinal_profile = _sample_longitudinal_profile(fertigteil_shape, axis="x", debug_logs=debug_logs)
    
    # Klassifikation basierend auf Aspekt-Verhältnisse und Symmetrie
    part_type = 'irregular'
    preform_description = "Standard-Form mit Zapfen und Abschmiedung"
    sketch_group_hint = "Vorschmiedeskizzen: allgemeine abgesetzte Vorform"
    
    profile_variation = longitudinal_profile.get("variation", 0.0)
    both_end_heavy = longitudinal_profile.get("both_end_heavy", False)
    one_end_heavy = longitudinal_profile.get("one_end_heavy", False)
    middle_heavy = longitudinal_profile.get("middle_heavy", False)
    waist_ratio = longitudinal_profile.get("waist_ratio", 1.0)

    if both_end_heavy and waist_ratio < 0.82:
        # Lenker/Knochen/Kopfplatten: an beiden Enden Material, schmaler Steg/Stil in der Mitte.
        part_type = 'bone'
        preform_description = "Knochen-/Lenkerform mit zentralem Stil und dickeren Enden"
        sketch_group_hint = "Vorschmiedeskizzen Gruppen 21/23/28-32"
    elif one_end_heavy and profile_variation > 0.18:
        # Ritzelwelle/Kopfplatte einseitig: ein dicker Kopf, ein langer Stil/Schaft.
        part_type = 'one_sided_shaft'
        side = "links" if longitudinal_profile.get("left_heavier") else "rechts"
        preform_description = f"einseitige Wellen-/Ritzelform mit dickem Ende {side}"
        sketch_group_hint = "Vorschmiedeskizzen Gruppen 1/5/9/13/20/22/27"
    elif middle_heavy and profile_variation > 0.16:
        # Klassische abgesetzte Welle: Mittelbereich dicker, beidseitig Zapfen/Stile.
        part_type = 'shaft'
        preform_description = "abgesetzte Welle mit Mittelbereich und Zapfen/Stil"
        sketch_group_hint = "Vorschmiedeskizzen Gruppen 2/6/10/14/16"
    elif profile_variation < 0.14 and symmetry_score > 0.70:
        # Einfach rotationssymmetrisch bzw. komplett ueberschmiedet: Blinz ohne Zapfen.
        part_type = 'rotational'
        preform_description = "Blinz mit gebrochenen Kanten ohne Zapfen"
        sketch_group_hint = "Vorschmiedeskizzen Gruppen 17-19"
    elif profile_variation > 0.16:
        # Wenn die Kurve klar profiliert ist, wird sie direkt als Vorlage genutzt.
        part_type = 'profiled'
        preform_description = "profilfolgende Vorform nach realem Rohteil-Längsprofil"
        sketch_group_hint = "Vorschmiedeskizzen: automatische Profilfolge"
    elif aspect_xz > 3.2 or aspect_xy > 3.2:
        part_type = 'shaft'
        preform_description = "schlanke Wellenform mit Stil/Zapfen"
        sketch_group_hint = "Vorschmiedeskizzen Gruppen 2/6/10/14"
    
    analysis = {
        'part_type': part_type,
        'preform_description': preform_description,
        'sketch_group_hint': sketch_group_hint,
        'aspect_ratio_xy': round(aspect_xy, 3),
        'aspect_ratio_xz': round(aspect_xz, 3),
        'aspect_ratio_yz': round(aspect_yz, 3),
        'symmetry_score': round(symmetry_score, 3),
        'center_of_mass_deviation': round(center_of_mass_deviation, 3),
        'dims': {'x': x_len, 'y': y_len, 'z': z_len},
        'longitudinal_profile': longitudinal_profile,
    }
    
    if debug_logs is not None:
        debug_logs.append(
            f"Geometrie-Analyse: Typ={part_type}, "
            f"Aspekt X/Y={aspect_xy:.2f}, X/Z={aspect_xz:.2f}, Y/Z={aspect_yz:.2f}, "
            f"Symmetrie={symmetry_score:.2f}, CoM-Abw={center_of_mass_deviation:.2f}"
        )
        debug_logs.append(f"✓ Aus Vorschmiedeskizzen abgeleitet: {preform_description} ({sketch_group_hint})")
    
    return analysis


def _normalize_station_widths(stations):
    max_width = max((width for _, width, _, _ in stations), default=1.0)
    if max_width <= 0:
        return stations
    return [
        (pos, width / max_width, height, label)
        for pos, width, height, label in stations
    ]


def _station_role(width_factor):
    if width_factor >= 0.80:
        return "Kopf/Abschmiedung"
    if width_factor <= 0.55:
        return "Stil/Steg"
    return "Übergang"


def _profile_based_stations(profile, debug_logs=None):
    samples = profile.get("samples") or []
    usable = [
        sample for sample in samples
        if sample.get("linear_norm") is not None
    ]
    if len(usable) < 5:
        return None

    stations = []
    for sample in usable:
        pos = max(0.0, min(1.0, float(sample.get("pos", 0.0))))
        linear = max(0.0, min(1.0, float(sample.get("linear_norm", 1.0))))
        # Der Faktor bleibt bewusst weich: die Vorform soll dem Rohteil folgen,
        # aber schmiedegerecht mit fliessfaehigen Uebergaengen bleiben.
        width_factor = max(0.34, min(1.0, 0.16 + 0.84 * linear))
        height_factor = max(0.42, min(1.10, 0.18 + 0.90 * linear))
        label = _station_role(width_factor)
        stations.append((pos, width_factor, height_factor, label))

    stations[0] = (0.0, stations[0][1], stations[0][2], stations[0][3])
    stations[-1] = (1.0, stations[-1][1], stations[-1][2], stations[-1][3])
    stations = _normalize_station_widths(stations)
    if debug_logs is not None:
        debug_logs.append("✓ Stationen direkt aus Rohteil-Längsprofil aufgebaut")
    return stations


def _features_from_stations(stations):
    """Verdichtet Loft-Stationen zu lesbaren Segmenten fuer UI/PDF-Skizzen."""
    features = []
    for left, right in zip(stations, stations[1:]):
        x_start = max(0.0, min(1.0, left[0]))
        x_end = max(x_start, min(1.0, right[0]))
        avg_width = (left[1] + right[1]) / 2.0
        avg_height = (left[2] + right[2]) / 2.0
        role = _station_role(avg_width)
        if features and features[-1]["name"] == role:
            features[-1]["x_end"] = x_end
            features[-1]["height_factor"] = max(features[-1]["height_factor"], avg_height)
        else:
            features.append({
                "name": role,
                "x_start": round(x_start, 3),
                "x_end": round(x_end, 3),
                "height_factor": round(avg_height, 3),
            })
    return features


def _get_adapted_stations(part_analysis, target_x, target_y, debug_logs=None):
    """
    Gibt adaptive Loft-Stationen basierend auf der Rohteil-Geometrie zurück.
    
    Unterschiedliche Profil-Sequenzen für verschiedene Geometrie-Typen:
    - rotational: Symmetrisch rund mit gebrochenen Kanten (Blinz ohne Zapfen)
    - shaft: Dünner Stil mit dickeren Enden (Ritzelwelle/Welle)
    - bone: Zentraler Stil mit dickeren Enden beider Seiten (Knochenform)
    - irregular: Standard mit Zapfen und Abschmiedung
    
    Stationsformat: (x_Normalisiert, width_Faktor, height_Faktor, Beschreibung)
    """
    part_type = part_analysis.get('part_type', 'irregular')
    symmetry = part_analysis.get('symmetry_score', 0.0)
    com_deviation = part_analysis.get('center_of_mass_deviation', 0.0)
    aspect_xz = part_analysis.get('aspect_ratio_xz', 1.0)
    profile = part_analysis.get('longitudinal_profile') or {}
    
    form_desc = "Standard"
    stations = None
    
    if part_type == 'rotational':
        # Rotationssymmetrisch: Blinz mit gebrochenen Kanten, KEINE Zapfen
        # "nur einen art blinz mit gebrochene kanten ohne zapfen"
        stations = [
            (0.00, 0.66, 0.70, "Blinz-Kante links"),
            (0.10, 0.76, 0.80, "Blinz-Anstieg links"),
            (0.25, 0.90, 0.92, "Blinz-Vorderwölbung"),
            (0.40, 0.98, 0.98, "Blinz-Übergang zur Mitte"),
            (0.50, 1.00, 1.00, "Blinz-Zentrum"),
            (0.60, 0.98, 0.98, "Blinz-Übergang von Mitte"),
            (0.75, 0.90, 0.92, "Blinz-Hinterwölbung"),
            (0.90, 0.76, 0.80, "Blinz-Anstieg rechts"),
            (1.00, 0.66, 0.70, "Blinz-Kante rechts"),
        ]
        form_desc = "Blinz mit gebrochenen Kanten (rotationssymmetrisch, kein Zapfen)"
    
    elif part_type in {'profiled', 'shaft', 'bone', 'one_sided_shaft'} and profile.get("variation", 0) > 0.12:
        stations = _profile_based_stations(profile, debug_logs=debug_logs)
        form_desc = part_analysis.get("preform_description", "profilfolgende Vorform")
        if stations is None:
            part_type = 'irregular'

    if part_type == 'one_sided_shaft' and not stations:
        # Ritzelwelle: ein dickeres Ende mit langem Stil/Schaft
        thick_left = profile.get("left_heavier", True)
        stations = [
            (0.00, 1.00, 1.04, "Kopfende dick"),
            (0.14, 0.95, 1.00, "Kopf-Ansatz"),
            (0.28, 0.70, 0.78, "Schulter"),
            (0.42, 0.48, 0.58, "Stil Eintritt"),
            (0.62, 0.40, 0.50, "langer Stil"),
            (0.80, 0.40, 0.50, "Stil"),
            (1.00, 0.44, 0.54, "Stilende"),
        ]
        if not thick_left:
            stations = [(1.0 - pos, width, height, label) for pos, width, height, label in reversed(stations)]
        form_desc = "Ritzelwelle / einseitiger Stil mit dickerem Ende"
    
    elif part_type == 'bone' and not stations:
        # Knochenform: Zentraler Stil mit dickeren Knollen an beiden Enden
        # "knochenform ein langer stil in der mitte vo an beiden enden ein dickeres ende dran ist"
        stations = [
            (0.00, 1.00, 1.05, "Endknollen links"),
            (0.12, 0.96, 1.00, "Knollen-Übergang links"),
            (0.28, 0.62, 0.70, "Stilbereich Eintritt"),
            (0.42, 0.42, 0.52, "Stil Eintritt"),
            (0.50, 0.38, 0.48, "langer Mittelstil"),
            (0.58, 0.42, 0.52, "Stil Austritt"),
            (0.72, 0.62, 0.70, "Stilbereich Austritt"),
            (0.88, 0.96, 1.00, "Knollen-Übergang rechts"),
            (1.00, 1.00, 1.05, "Endknollen rechts"),
        ]
        form_desc = "Knochenform mit zentralem Stil und Endknollen"
    
    elif part_type == 'shaft' and not stations:
        stations = [
            (0.00, 0.46, 0.58, "Zapfen links"),
            (0.14, 0.52, 0.64, "Zapfen-Übergang links"),
            (0.28, 0.82, 0.90, "Schulter links"),
            (0.42, 1.00, 1.04, "Mittelbereich"),
            (0.58, 1.00, 1.04, "Mittelbereich"),
            (0.72, 0.82, 0.90, "Schulter rechts"),
            (0.86, 0.52, 0.64, "Zapfen-Übergang rechts"),
            (1.00, 0.46, 0.58, "Zapfen rechts"),
        ]
        form_desc = "abgesetzte Welle mit Mittelbereich und Zapfen/Stil"

    if not stations:
        # Standard/Irregular: Klassische Form mit Zapfen und sanfteren Übergängen
        # "sollte sich automatishc dem rohteil anpassen aber beahcte in x und y achsen soll es 75% vom rohteil abdecken"
        stations = [
            (0.00, 0.62, 0.76, "Zapfen links Ansatz"),
            (0.12, 0.68, 0.82, "Zapfen links Übergang"),
            (0.24, 0.84, 0.92, "Übergangskurve links"),
            (0.36, 0.96, 1.02, "Abschmiedung links"),
            (0.50, 1.00, 1.04, "Abschmiedung Zentrum (Maximum)"),
            (0.64, 0.96, 1.02, "Abschmiedung rechts"),
            (0.76, 0.84, 0.92, "Übergangskurve rechts"),
            (0.88, 0.68, 0.82, "Zapfen rechts Übergang"),
            (1.00, 0.62, 0.76, "Zapfen rechts Ansatz"),
        ]
        form_desc = "Standard-Form mit Zapfen und Abschmiedung"

    stations = _normalize_station_widths(stations)
    
    if debug_logs is not None:
        debug_logs.append(f"✓ Adaptive Vorform-Strategie: {form_desc}")
        debug_logs.append(f"   Typ={part_type}, Symmetrie={symmetry:.2f}, CoM-Dev={com_deviation:.2f}, X/Z={aspect_xz:.2f}")
        debug_logs.append("   Y-Stationen auf maximale 75%-Rohteilabdeckung normalisiert")
    
    part_analysis["part_type"] = part_type
    part_analysis["preform_description"] = form_desc
    part_analysis["preform_features"] = _features_from_stations(stations)
    return stations


def _make_contour_near_preform(zuschnitt_shape, fertigteil_shape, coverage_target, debug_logs=None):
    """
    Baut eine industrielle, adaptive Freiform-Vorform als Loft.

    X/Y Abdeckung:
    - Deckt ca. coverage_target (75%) der Fertigteil-Gravur-Abmessungen in X/Y ab
    - Überschüssiges Volumen wird in Z-Richtung addiert (Volumenerhaltung)
    
    Adaptive Profilierung basierend auf Rohteil-Geometrie:
    - Rotationssymmetrische Teile: Blinz mit gebrochenen Kanten (keine Zapfen)
    - Ritzelwellen/Wellen: Dünner Stil mit dickeren Enden
    - Knochenform: Zentraler Stil mit dickeren Endknollen
    - Standard/Irregular: Klassische Form mit Zapfen und Abschmiedung
    
    Z-Berechnung:
    - Basis-Z wird rechnerisch so bestimmt, dass das Halbzeug-Volumen erhalten bleibt
    - Anschließend iterative Nachskalierung für präzise Volumenerhaltung
    """
    billet_volume = max(zuschnitt_shape.Volume, 1.0)
    fertigteil_bbox = fertigteil_shape.BoundBox
    target_x = max(fertigteil_bbox.XLength * coverage_target, 1.0)
    target_y = max(fertigteil_bbox.YLength * coverage_target, 1.0)

    if debug_logs is not None:
        debug_logs.append(f"Adaptive Konturennah-Vorformgenerierung gestartet")
        debug_logs.append(f"  Halbzeug-Volumen (Quelle): {billet_volume:.1f} mm³")
        debug_logs.append(f"  Zielabmessungen X/Y (75% Abdeckung):")
        debug_logs.append(f"    X: {target_x:.1f} mm (von {fertigteil_bbox.XLength:.1f} mm)")
        debug_logs.append(f"    Y: {target_y:.1f} mm (von {fertigteil_bbox.YLength:.1f} mm)")

    # Geometrie analysieren und adaptive Stationen auswählen
    part_analysis = _analyze_part_geometry(fertigteil_shape, debug_logs)
    stations = _get_adapted_stations(part_analysis, target_x, target_y, debug_logs)

    # Berechne effektive Querschnittsfläche über gewichtete Integration der Stationen
    weighted_area_factor = 0.0
    for (p0, wy0, hz0, _), (p1, wy1, hz1, _) in zip(stations, stations[1:]):
        segment_area = (wy0 * hz0 + wy1 * hz1) / 2.0
        weighted_area_factor += (p1 - p0) * segment_area
    weighted_area_factor = max(weighted_area_factor, 0.2)
    
    # Initiale Z-Höhe aus Volumenerhaltung
    base_z = billet_volume / (target_x * target_y * weighted_area_factor)
    
    if debug_logs is not None:
        debug_logs.append(f"  Loft-Profile mit {len(stations)} Stationen erzeugt")
        debug_logs.append(f"  Gewichtete Querschnittsfläche (normalisiert): {weighted_area_factor:.3f}")
        debug_logs.append(f"  Basis Z-Höhe (initial): {base_z:.1f} mm")

    feature_data = [{
        "name": label,
        "x_pos": round(pos, 3),

        "width_factor": round(width_factor, 3),
        "height_factor": round(height_factor, 3),
    } for pos, width_factor, height_factor, label in stations]

    def _build_loft(height_base):
        wires = []
        for pos, width_factor, height_factor, _ in stations:
            x_pos = (pos - 0.5) * target_x
            width_y = target_y * width_factor
            height_z = height_base * height_factor
            wires.append(_rounded_octagon_wire_x(x_pos, width_y, height_z))
        return Part.makeLoft(wires, True, True, False)

    # Erste Erzeugung mit initialer Z-Höhe
    vorform_shape = _build_loft(base_z)
    initial_volume = vorform_shape.Volume if vorform_shape.Volume > 0 else 1.0
    
    # Iterative Nachskalierung für präzise Volumenerhaltung
    if vorform_shape.Volume > 0 and abs(vorform_shape.Volume - billet_volume) > 0.1:
        volume_factor = billet_volume / vorform_shape.Volume
        base_z *= volume_factor
        vorform_shape = _build_loft(base_z)
        final_volume = vorform_shape.Volume
        
        if debug_logs is not None:
            debug_logs.append(f"  Volumen-Iteration für Erhaltung:")
            debug_logs.append(f"    Initial: {initial_volume:.1f} mm³ (Faktor: {volume_factor:.4f})")
            debug_logs.append(f"    Final:   {final_volume:.1f} mm³ (Diff: {abs(final_volume - billet_volume):.1f} mm³)")
    
    vorform_shape = _normalize_shape_coordinates(vorform_shape, debug_logs)

    if debug_logs is not None:
        bbox = vorform_shape.BoundBox
        analysis_type = part_analysis.get('part_type', 'unknown')
        final_volume = vorform_shape.Volume
        volume_diff_pct = abs(final_volume - billet_volume) / billet_volume * 100 if billet_volume > 0 else 0
        
        debug_logs.append(f"✓ Adaptive Vorform als Loft erzeugt")
        debug_logs.append(f"  Geometrie-Typ: {analysis_type}")
        debug_logs.append(f"  Abmessungen: {bbox.XLength:.1f} x {bbox.YLength:.1f} x {bbox.ZLength:.1f} mm")
        debug_logs.append(f"  Finales Volumen: {final_volume:.1f} mm³ (Ziel: {billet_volume:.1f} mm³, Abw: {volume_diff_pct:.2f}%)")
        debug_logs.append(f"  X/Y Abdeckung: {(bbox.XLength/fertigteil_bbox.XLength)*100:.1f}% x {(bbox.YLength/fertigteil_bbox.YLength)*100:.1f}%")

    return vorform_shape, feature_data, part_analysis


def create_vorform_from_zuschnitt(
    zuschnitt_step_file,
    fertigteil_step_file,
    billet_specs=None,
    coverage_target=COVERAGE_PERCENTAGE,
    rotate_reference_90=False,
    output_folder=".",
    output_basename="schmiedevorform",
    debug_logs=None,
):
    """
    Erstellt eine Schmiedevorform aus dem Zuschnitt mit Volumenerhaltung.
    
    Prozess:
    1. Lade Zuschnitt (Halbzeug) und Fertigteil
    2. Berechne X-Y Abmessungen des Fertigteils
    3. Skaliere Zuschnitt auf coverage_target (ca. 75%) der Fertigteilabmessungen
    4. Berechne Z-Höhe durch Volumenerhaltungssatz
    5. Exportiere als STEP und STL
    
    Args:
        zuschnitt_step_file: Pfad zum STEP des berechneten Zuschnitts (Halbzeug)
        fertigteil_step_file: Pfad zum STEP des Fertigteils (Gravur)
        coverage_target: Sollte ca. 0,75 (75%) sein
        output_folder: Zielordner für Exports
        output_basename: Basis-Name für Output-Dateien
        debug_logs: Optional Liste für Debug-Meldungen
        
    Returns:
        Dict mit Schlüsseln:
            stl_file: Pfad zur generierten STL
            step_file: Pfad zum generierten STEP
            volume_mm3: Berechnetes Volumen
            weight_kg: Berechnetes Gewicht (Stahl)
            dimensions_mm: Dict mit x, y, z Abmessungen
            debug_logs: Liste der Debug-Meldungen
    """
    if debug_logs is None:
        debug_logs = []
    
    doc = None
    try:
        debug_logs.append("=== Schmiedevorform-Generierung gestartet ===")
        
        # Lade Zuschnitt (Halbzeug)
        if not os.path.exists(zuschnitt_step_file):
            raise FileNotFoundError(f"Zuschnitt-STEP nicht gefunden: {zuschnitt_step_file}")
        
        if not os.path.exists(fertigteil_step_file):
            raise FileNotFoundError(f"Fertigteil-STEP nicht gefunden: {fertigteil_step_file}")
        
        doc = FreeCAD.newDocument("VorformGeneration")
        
        # Lade Zuschnitt oder baue ihn direkt aus dem Halbzeug-Rechner auf.
        zuschnitt_shape = _create_billet_shape_from_specs(billet_specs, debug_logs)
        if zuschnitt_shape is None:
            zuschnitt_shape = Part.Shape()
            zuschnitt_shape.read(zuschnitt_step_file)
            if zuschnitt_shape.isNull():
                raise ValueError("Zuschnitt-STEP konnte nicht gelesen werden")
        
        zuschnitt_shape = _normalize_shape_coordinates(zuschnitt_shape, debug_logs)
        zuschnitt_bbox = zuschnitt_shape.BoundBox
        zuschnitt_volume = zuschnitt_shape.Volume
        
        debug_logs.append(f"Zuschnitt geladen: Vol={zuschnitt_volume:.1f} mm³")
        debug_logs.append(
            f"  Zuschnitt BBox: {zuschnitt_bbox.XLength:.1f} x {zuschnitt_bbox.YLength:.1f} x {zuschnitt_bbox.ZLength:.1f} mm"
        )
        
        # Lade Fertigteil für Referenzdimensionen
        fertigteil_shape = Part.Shape()
        fertigteil_shape.read(fertigteil_step_file)
        if fertigteil_shape.isNull():
            raise ValueError("Fertigteil-STEP konnte nicht gelesen werden")
        
        fertigteil_shape = _normalize_shape_coordinates(fertigteil_shape, debug_logs)
        if rotate_reference_90:
            fertigteil_shape.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), 90)
            fertigteil_shape = _normalize_shape_coordinates(fertigteil_shape, debug_logs)
            debug_logs.append("Faserverlauf-Option aktiv: Fertigteil-/Gravurreferenz um 90 Grad zur Seite gedreht.")
        fertigteil_bbox = fertigteil_shape.BoundBox
        
        debug_logs.append(f"Fertigteil geladen (Referenzdimensionen)")
        debug_logs.append(
            f"  Fertigteil BBox: {fertigteil_bbox.XLength:.1f} x {fertigteil_bbox.YLength:.1f} x {fertigteil_bbox.ZLength:.1f} mm"
        )
        
        # Berechne Zielabmessungen: ca. 75% der Fertigteilabmessungen (X, Y)
        fertigteil_x = fertigteil_bbox.XLength
        fertigteil_y = fertigteil_bbox.YLength
        fertigteil_z = fertigteil_bbox.ZLength
        
        target_x = fertigteil_x * coverage_target
        target_y = fertigteil_y * coverage_target
        
        debug_logs.append(f"Zielabmessungen (X-Y Projektion mit {coverage_target*100:.1f}%):")
        debug_logs.append(f"  X: {target_x:.1f} mm (von {fertigteil_x:.1f} mm)")
        debug_logs.append(f"  Y: {target_y:.1f} mm (von {fertigteil_y:.1f} mm)")
        
        debug_logs.append("Volumenerhaltungssatz und rohteilkonturnahe Profilierung werden angewendet.")
        debug_logs.append(f"  Original-Volumen Halbzeug/Zuschnitt: {zuschnitt_volume:.1f} mm³")
        debug_logs.append(f"  Ziel X/Y aus Gravur: {target_x:.1f} x {target_y:.1f} mm")

        vorform_shape, feature_data, part_analysis = _make_contour_near_preform(
            zuschnitt_shape,
            fertigteil_shape,
            coverage_target,
            debug_logs=debug_logs,
        )
        
        vorform_bbox = vorform_shape.BoundBox
        vorform_volume = vorform_shape.Volume
        vorform_weight = vorform_volume * STEEL_DENSITY
        
        debug_logs.append(f"Finale Vorform generiert:")
        debug_logs.append(f"  Volumen: {vorform_volume:.1f} mm³")
        debug_logs.append(f"  Gewicht (Stahl): {vorform_weight:.3f} kg")
        debug_logs.append(
            f"  Abmessungen: {vorform_bbox.XLength:.1f} x {vorform_bbox.YLength:.1f} x {vorform_bbox.ZLength:.1f} mm"
        )
        
        # Exportiere als STEP
        step_filename = f"{output_basename}.step"
        step_path = os.path.join(output_folder, step_filename)
        
        os.makedirs(output_folder, exist_ok=True)
        vorform_shape.exportStep(step_path)
        
        if not os.path.exists(step_path):
            raise ValueError(f"STEP-Export fehlgeschlagen: {step_path}")
        
        debug_logs.append(f"✓ STEP exportiert: {step_filename}")
        
        # Exportiere als STL
        stl_filename = f"{output_basename}.stl"
        stl_path = os.path.join(output_folder, stl_filename)
        
        linear_deflect, angular_deflect = _mesh_deflection_for_shape(vorform_shape, high_quality=True)
        mesh = MeshPart.meshFromShape(
            Shape=vorform_shape,
            LinearDeflection=linear_deflect,
            AngularDeflection=angular_deflect,
            Relative=False,
        )
        
        if mesh is None:
            raise ValueError("Mesh-Erstellung fehlgeschlagen")
        
        mesh.write(stl_path)
        
        if not os.path.exists(stl_path):
            raise ValueError(f"STL-Export fehlgeschlagen: {stl_path}")

        stl_report = _validate_exported_stl(stl_path, vorform_shape, debug_logs)
        debug_logs.append(
            f"✓ STL exportiert und validiert: {stl_filename} "
            f"(LinearDeflection={linear_deflect:.3f}, AngularDeflection={angular_deflect:.3f})"
        )
        debug_logs.append("=== Schmiedevorform-Generierung ERFOLGREICH ===")
        
        return {
            "stl_file": stl_filename,
            "step_file": step_filename,
            "volume_mm3": vorform_volume,
            "weight_kg": vorform_weight,
            "dimensions_mm": {
                "x": round(vorform_bbox.XLength, 2),
                "y": round(vorform_bbox.YLength, 2),
                "z": round(vorform_bbox.ZLength, 2),
            },
            "debug_logs": debug_logs,
            "stl_quality": stl_report,
            "coverage_achieved": (vorform_bbox.XLength / fertigteil_x) if fertigteil_x > 0 else 0,
            "coverage_xy": {
                "x": round((vorform_bbox.XLength / fertigteil_x) if fertigteil_x > 0 else 0, 4),
                "y": round((vorform_bbox.YLength / fertigteil_y) if fertigteil_y > 0 else 0, 4),
            },
            "preform_type": part_analysis.get("part_type", "irregular"),
            "preform_description": part_analysis.get("preform_description", "Adaptive Schmiedevorform"),
            "sketch_group_hint": part_analysis.get("sketch_group_hint", ""),
            "sketch_data": {
                "vorform_dimensions_mm": {
                    "x": round(vorform_bbox.XLength, 2),
                    "y": round(vorform_bbox.YLength, 2),
                    "z": round(vorform_bbox.ZLength, 2),
                },
                "fertigteil_dimensions_mm": {
                    "x": round(fertigteil_bbox.XLength, 2),
                    "y": round(fertigteil_bbox.YLength, 2),
                    "z": round(fertigteil_bbox.ZLength, 2),
                },
                "coverage_target": round(coverage_target, 4),
                "volume_mm3": round(vorform_volume, 2),
                "weight_kg": round(vorform_weight, 4),
                "features": part_analysis.get("preform_features") or _features_from_stations([
                    (item["x_pos"], item["width_factor"], item["height_factor"], item["name"])
                    for item in feature_data
                ]),
                "loft_stations": feature_data,
                "billet_specs": billet_specs or {},
                "rotate_reference_90": bool(rotate_reference_90),
                "preform_type": part_analysis.get("part_type", "irregular"),
                "preform_description": part_analysis.get("preform_description", "Adaptive Schmiedevorform"),
                "sketch_group_hint": part_analysis.get("sketch_group_hint", ""),
            },
        }
        
    except Exception as e:
        error_msg = f"Fehler in Schmiedevorform-Generierung: {str(e)}"
        debug_logs.append(error_msg)
        import traceback
        debug_logs.append(f"Traceback: {traceback.format_exc()}")
        raise Exception(error_msg)
    finally:
        if doc is not None:
            try:
                FreeCAD.closeDocument(doc.Name)
            except Exception:
                pass


def create_sketch_drawing_with_dimensions(
    vorform_step_file,
    fertigteil_step_file,
    output_folder=".",
    output_basename="vorform_sketch",
    debug_logs=None,
):
    """
    Erstellt eine technische Skizzenzeichnung der Vorform mit Bemaßung.
    
    Args:
        vorform_step_file: Pfad zum generierten Vorform-STEP
        fertigteil_step_file: Pfad zum Fertigteil-STEP (für Vergleich)
        output_folder: Zielordner
        output_basename: Basis-Name für Output
        debug_logs: Optional Liste für Debug-Meldungen
        
    Returns:
        Dict mit:
            pdf_file: Pfad zur generierten PDF
            debug_logs: Liste der Debug-Meldungen
    """
    if debug_logs is None:
        debug_logs = []
    
    doc = None
    try:
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle, FancyBboxPatch
        
        debug_logs.append("=== Skizzenzeichnung mit Bemaßung gestartet ===")
        
        # Lade Vorform und Fertigteil
        vorform_shape = Part.Shape()
        vorform_shape.read(vorform_step_file)
        if vorform_shape.isNull():
            raise ValueError("Vorform-STEP konnte nicht gelesen werden")
        
        fertigteil_shape = Part.Shape()
        fertigteil_shape.read(fertigteil_step_file)
        if fertigteil_shape.isNull():
            raise ValueError("Fertigteil-STEP konnte nicht gelesen werden")
        
        vorform_bbox = vorform_shape.BoundBox
        fertigteil_bbox = fertigteil_shape.BoundBox
        
        debug_logs.append(f"Vorform-Dimensionen: {vorform_bbox.XLength:.1f} x {vorform_bbox.YLength:.1f} x {vorform_bbox.ZLength:.1f} mm")
        debug_logs.append(f"Fertigteil-Dimensionen: {fertigteil_bbox.XLength:.1f} x {fertigteil_bbox.YLength:.1f} x {fertigteil_bbox.ZLength:.1f} mm")
        
        fig = plt.figure(figsize=(11.69, 8.27))
        fig.patch.set_facecolor('white')
        fig.suptitle(f"Schmiedevorform - Technische Skizze\nFreiformschmieden", fontsize=16, fontweight='bold', y=0.98)

        grid = fig.add_gridspec(3, 4, left=0.04, right=0.98, top=0.92, bottom=0.06, hspace=0.22, wspace=0.20)

        # --- TOP VIEW (X-Y) ---
        ax_top = fig.add_subplot(grid[0:2, 0:2])
        fertig_rect = Rectangle(
            (fertigteil_bbox.XMin, fertigteil_bbox.YMin),
            fertigteil_bbox.XLength,
            fertigteil_bbox.YLength,
            linewidth=1.2,
            edgecolor='lightgray',
            facecolor='lightgray',
            alpha=0.35,
            label='Fertigteil (Gravur)'
        )
        ax_top.add_patch(fertig_rect)

        vorform_rect = Rectangle(
            (vorform_bbox.XMin, vorform_bbox.YMin),
            vorform_bbox.XLength,
            vorform_bbox.YLength,
            linewidth=2.2,
            edgecolor='#0f62fe',
            facecolor='#d5e8ff',
            alpha=0.55,
            label='Schmiedevorform'
        )
        ax_top.add_patch(vorform_rect)

        ax_top.annotate('', xy=(vorform_bbox.XMax, vorform_bbox.YMin - 15),
                       xytext=(vorform_bbox.XMin, vorform_bbox.YMin - 15),
                       arrowprops=dict(arrowstyle='<->', color='#b91c1c', lw=1.3))
        ax_top.text((vorform_bbox.XMin + vorform_bbox.XMax) / 2, vorform_bbox.YMin - 25,
                   f'X = {vorform_bbox.XLength:.1f} mm', ha='center', fontsize=10, color='#b91c1c', fontweight='bold')

        ax_top.annotate('', xy=(vorform_bbox.XMin - 15, vorform_bbox.YMax),
                       xytext=(vorform_bbox.XMin - 15, vorform_bbox.YMin),
                       arrowprops=dict(arrowstyle='<->', color='#b91c1c', lw=1.3))
        ax_top.text(vorform_bbox.XMin - 35, (vorform_bbox.YMin + vorform_bbox.YMax) / 2,
                   f'Y = {vorform_bbox.YLength:.1f} mm', ha='center', fontsize=10, color='#b91c1c', fontweight='bold', rotation=90)

        coverage_pct = (vorform_bbox.XLength / fertigteil_bbox.XLength) * 100 if fertigteil_bbox.XLength > 0 else 0
        ax_top.text(0.98, 0.98, f'Abdeckung X-Y: {coverage_pct:.1f}%\nZielwert: {COVERAGE_PERCENTAGE*100:.1f}%',
                   transform=ax_top.transAxes, fontsize=9, verticalalignment='top', horizontalalignment='right',
                   bbox=dict(boxstyle='round', facecolor='#f8e3c0', alpha=0.75))

        ax_top.set_xlim(fertigteil_bbox.XMin - 40, fertigteil_bbox.XMax + 40)
        ax_top.set_ylim(fertigteil_bbox.YMin - 40, fertigteil_bbox.YMax + 40)
        ax_top.set_aspect('equal')
        ax_top.set_title('Draufsicht (X-Y)', fontweight='bold')
        ax_top.set_xlabel('X [mm]')
        ax_top.set_ylabel('Y [mm]')
        ax_top.grid(True, alpha=0.25)
        ax_top.legend(loc='lower left', fontsize=9)

        # --- FRONT VIEW (X-Z) ---
        ax_front = fig.add_subplot(grid[0, 2:4])
        fertig_front = Rectangle(
            (fertigteil_bbox.XMin, fertigteil_bbox.ZMin),
            fertigteil_bbox.XLength,
            fertigteil_bbox.ZLength,
            linewidth=1.2,
            edgecolor='lightgray',
            facecolor='lightgray',
            alpha=0.35,
        )
        ax_front.add_patch(fertig_front)

        vorform_front = Rectangle(
            (vorform_bbox.XMin, vorform_bbox.ZMin),
            vorform_bbox.XLength,
            vorform_bbox.ZLength,
            linewidth=2.2,
            edgecolor='#0f62fe',
            facecolor='#d5e8ff',
            alpha=0.55,
        )
        ax_front.add_patch(vorform_front)

        ax_front.annotate('', xy=(vorform_bbox.XMax, vorform_bbox.ZMax),
                         xytext=(vorform_bbox.XMax, vorform_bbox.ZMin),
                         arrowprops=dict(arrowstyle='<->', color='#15803d', lw=1.3))
        ax_front.text(vorform_bbox.XMax + 18, (vorform_bbox.ZMin + vorform_bbox.ZMax) / 2,
                     f'Z = {vorform_bbox.ZLength:.1f} mm', ha='left', fontsize=10, color='#15803d', fontweight='bold')

        ax_front.set_xlim(fertigteil_bbox.XMin - 40, fertigteil_bbox.XMax + 40)
        ax_front.set_ylim(fertigteil_bbox.ZMin - 20, fertigteil_bbox.ZMax + 20)
        ax_front.set_aspect('equal')
        ax_front.set_title('Frontansicht (X-Z)', fontweight='bold')
        ax_front.set_xlabel('X [mm]')
        ax_front.set_ylabel('Z [mm]')
        ax_front.grid(True, alpha=0.25)

        # --- SIDE VIEW (Y-Z) ---
        ax_side = fig.add_subplot(grid[1, 2:4])
        fertig_side = Rectangle(
            (fertigteil_bbox.YMin, fertigteil_bbox.ZMin),
            fertigteil_bbox.YLength,
            fertigteil_bbox.ZLength,
            linewidth=1.2,
            edgecolor='lightgray',
            facecolor='lightgray',
            alpha=0.35,
        )
        ax_side.add_patch(fertig_side)

        vorform_side = Rectangle(
            (vorform_bbox.YMin, vorform_bbox.ZMin),
            vorform_bbox.YLength,
            vorform_bbox.ZLength,
            linewidth=2.2,
            edgecolor='#0f62fe',
            facecolor='#d5e8ff',
            alpha=0.55,
        )
        ax_side.add_patch(vorform_side)

        ax_side.annotate('', xy=(vorform_bbox.YMax, vorform_bbox.ZMin - 15),
                        xytext=(vorform_bbox.YMin, vorform_bbox.ZMin - 15),
                        arrowprops=dict(arrowstyle='<->', color='#b91c1c', lw=1.3))
        ax_side.text((vorform_bbox.YMin + vorform_bbox.YMax) / 2, vorform_bbox.ZMin - 25,
                    f'Y = {vorform_bbox.YLength:.1f} mm', ha='center', fontsize=10, color='#b91c1c', fontweight='bold')

        ax_side.set_xlim(fertigteil_bbox.YMin - 40, fertigteil_bbox.YMax + 40)
        ax_side.set_ylim(fertigteil_bbox.ZMin - 40, fertigteil_bbox.ZMax + 20)
        ax_side.set_aspect('equal')
        ax_side.set_title('Seitenansicht (Y-Z)', fontweight='bold')
        ax_side.set_xlabel('Y [mm]')
        ax_side.set_ylabel('Z [mm]')
        ax_side.grid(True, alpha=0.25)

        # --- INFO BOX ---
        ax_info = fig.add_subplot(grid[2, :])
        ax_info.axis('off')

        info_text = f"""
SCHMIEDEVORFORM - TECHNISCHE DATEN
{'='*44}

ABMESSUNGEN:
  Vorform: {vorform_bbox.XLength:.2f} x {vorform_bbox.YLength:.2f} x {vorform_bbox.ZLength:.2f} mm
  Fertigteil: {fertigteil_bbox.XLength:.2f} x {fertigteil_bbox.YLength:.2f} x {fertigteil_bbox.ZLength:.2f} mm

VOLUMENABDECKUNG (X-Y):
  Erreicht {coverage_pct:.1f} % von {COVERAGE_PERCENTAGE*100:.1f} %

VOLUMEN & GEWICHT:
  Vorform-Volumen: {vorform_shape.Volume:.0f} mm³
  Vorform-Gewicht: {vorform_shape.Volume * STEEL_DENSITY:.3f} kg (Stahl)

HERSTELLUNG:
  Freiformschmieden mit automatischer Generierung aus berechnetem Zuschnitt.
  Z-Höhe wird über Volumenerhaltung berechnet.
"""
        ax_info.text(0.01, 0.99, info_text, transform=ax_info.transAxes,
                    fontsize=9, verticalalignment='top', fontfamily='monospace',
                    bbox=dict(boxstyle='round', facecolor='#eff6ff', alpha=0.9))
        
        # Speichere als PDF
        pdf_filename = f"{output_basename}.pdf"
        pdf_path = os.path.join(output_folder, pdf_filename)
        
        os.makedirs(output_folder, exist_ok=True)
        fig.savefig(pdf_path, dpi=150, bbox_inches='tight', format='pdf')
        plt.close(fig)
        
        if not os.path.exists(pdf_path):
            raise ValueError(f"PDF-Export fehlgeschlagen: {pdf_path}")
        
        debug_logs.append(f"✓ Skizzenzeichnung exportiert: {pdf_filename}")
        debug_logs.append("=== Skizzenzeichnung ERFOLGREICH ===")
        
        return {
            "pdf_file": pdf_filename,
            "debug_logs": debug_logs,
        }
        
    except Exception as e:
        error_msg = f"Fehler bei Skizzenzeichnung: {str(e)}"
        debug_logs.append(error_msg)
        import traceback
        debug_logs.append(f"Traceback: {traceback.format_exc()}")
        raise Exception(error_msg)
    finally:
        if doc is not None:
            try:
                FreeCAD.closeDocument(doc.Name)
            except Exception:
                pass
