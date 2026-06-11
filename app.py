"""Standalone-Webapp fuer die Vorschmiedefreiform-Generierung aus STEP/STP."""

import json
import os
import sys
import time

os.environ.setdefault("OSD_PARALLEL", "0")
os.environ.setdefault("OCP_NUM_THREADS", "1")
os.environ.setdefault("FREECAD_DISABLE_THREADING", "1")

def _ensure_freecad_runtime():
    try:
        import FreeCAD  # noqa: F401
        import MeshPart  # noqa: F401
        import Part  # noqa: F401
        return
    except ModuleNotFoundError as exc:
        if exc.name != "FreeCAD":
            raise

    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    candidates = [
        os.path.join(parent_dir, "FreeCAD_1.0.0-conda-Linux-x86_64-py311.AppImage"),
        os.path.expanduser("~/Downloads/FreeCAD_1.0.0-conda-Linux-x86_64-py311.AppImage"),
    ]
    appimage = next((path for path in candidates if os.path.exists(path) and os.access(path, os.X_OK)), None)
    if not appimage:
        raise ModuleNotFoundError(
            "FreeCAD wurde in dieser Python-Umgebung nicht gefunden. "
            "Bitte mit ./run.sh starten oder die FreeCAD AppImage in das Hauptverzeichnis legen."
        )

    code = f"""
import os
import sys

PROJECT_DIR = {script_dir!r}
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from app import app
app.run(host='0.0.0.0', port=5020, debug=True)
"""
    os.execv(appimage, [appimage, "-c", code])


_ensure_freecad_runtime()

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import FreeCAD
import MeshPart
import Part

import schmiedevorform
import zeichnung_export

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "outputs")
STATE_FILE = os.path.join(OUTPUT_FOLDER, "state.json")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))


@app.after_request
def add_local_only_security_headers(response):
    """Erlaubt Browser-Ressourcen nur aus dieser lokalen Anwendung."""
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _parse_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}


def _output_path(filename):
    if not filename:
        return None
    basename = os.path.basename(filename)
    path = os.path.join(OUTPUT_FOLDER, basename)
    return path if os.path.exists(path) else None


def _load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_min_state(**values):
    state = _load_state()
    state.update(values)
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)


def _clear_runtime_folder(folder):
    """Loescht Laufzeitdateien, laesst aber .gitkeep fuer GitHub stehen."""
    os.makedirs(folder, exist_ok=True)
    for name in os.listdir(folder):
        if name == ".gitkeep":
            continue
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def _shape_info(shape, material="Stahl"):
    bbox = shape.BoundBox
    volume_mm3 = shape.Volume
    return {
        "material": material,
        "volume_mm3": volume_mm3,
        "weight_kg": volume_mm3 * 7.85e-6,
        "dimensions_mm": {
            "x": round(bbox.XLength, 2),
            "y": round(bbox.YLength, 2),
            "z": round(bbox.ZLength, 2),
            "max": round(max(bbox.XLength, bbox.YLength, bbox.ZLength), 2),
            "min": round(min(bbox.XLength, bbox.YLength, bbox.ZLength), 2),
        },
    }


def _export_shape(shape, basename):
    step_filename = f"{basename}.step"
    stl_filename = f"{basename}.stl"
    step_path = os.path.join(OUTPUT_FOLDER, step_filename)
    stl_path = os.path.join(OUTPUT_FOLDER, stl_filename)

    shape.exportStep(step_path)
    mesh = MeshPart.meshFromShape(
        Shape=shape,
        LinearDeflection=0.05,
        AngularDeflection=0.1,
        Relative=False,
    )
    if mesh is None:
        raise ValueError("Mesh-Erstellung fehlgeschlagen")
    mesh.write(stl_path)
    return step_filename, stl_filename


def _load_step_shape(step_path):
    shape = Part.Shape()
    shape.read(step_path)
    if shape.isNull():
        raise ValueError("STEP/STP-Datei konnte nicht gelesen werden")
    if shape.Volume <= 0:
        raise ValueError("STEP/STP-Datei enthaelt kein gueltiges Volumen")
    return shape


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/clear_steps", methods=["POST"])
def clear_steps():
    _clear_runtime_folder(UPLOAD_FOLDER)
    _clear_runtime_folder(OUTPUT_FOLDER)
    return jsonify({"status": "ok", "message": "Eingaben und erzeugte Dateien wurden geloescht"})


@app.route("/upload", methods=["POST"])
def upload_step():
    file = request.files.get("file")
    if not file:
        return jsonify({"status": "error", "message": "Keine STEP/STP-Datei hochgeladen"}), 400

    filename = secure_filename(file.filename or "")
    ext = os.path.splitext(filename)[1].lower()
    if not filename or ext not in {".step", ".stp"}:
        return jsonify({"status": "error", "message": "Bitte eine STEP- oder STP-Datei hochladen"}), 400

    upload_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(upload_path)

    try:
        shape = _load_step_shape(upload_path)
        prefix = f"referenz_{os.path.splitext(filename)[0]}_{int(time.time())}"
        step_filename, stl_filename = _export_shape(shape, prefix)
        part_info = _shape_info(shape, material="Stahl")
        part_info["source_file"] = filename
        debug_logs = [
            "STEP/STP-Upload erfolgreich.",
            f"Quelle: {filename}",
            f"Volumen: {part_info['volume_mm3']:.1f} mm3",
            "Referenzgeometrie als STEP und STL fuer den weiteren Workflow exportiert.",
        ]
        _save_min_state(
            step_file=step_filename,
            stl_file=stl_filename,
            part_info=part_info,
            debug_logs=debug_logs,
            source_step_file=filename,
            process_data={},
        )

        return jsonify({
            "status": "ok",
            "part_info": part_info,
            "stl_file": f"/download/{stl_filename}",
            "step_file": f"/download/{step_filename}",
            "debug_logs": debug_logs,
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/rotate", methods=["POST"])
def rotate_reference():
    state = _load_state()
    step_path = _output_path(state.get("step_file"))
    if not step_path:
        return jsonify({"status": "error", "message": "Bitte zuerst eine STEP/STP-Datei hochladen"}), 400

    data = request.get_json(silent=True) or {}
    angle_x = _parse_float(data.get("rotation_x_degrees"), 0) % 360
    angle_y = _parse_float(data.get("rotation_y_degrees"), 0) % 360
    if angle_x == 0 and angle_y == 0:
        return jsonify({"status": "error", "message": "Mindestens ein Winkel muss groesser 0 sein"}), 400

    try:
        shape = _load_step_shape(step_path)

        bbox = shape.BoundBox
        center = FreeCAD.Vector(
            (bbox.XMin + bbox.XMax) / 2.0,
            (bbox.YMin + bbox.YMax) / 2.0,
            (bbox.ZMin + bbox.ZMax) / 2.0,
        )
        if angle_x:
            shape.rotate(center, FreeCAD.Vector(1, 0, 0), angle_x)
        if angle_y:
            shape.rotate(center, FreeCAD.Vector(0, 1, 0), angle_y)

        basename = f"fertigteil_rotiert_{int(angle_x)}X_{int(angle_y)}Y_{int(time.time())}"
        step_filename, stl_filename = _export_shape(shape, basename)
        original_material = (state.get("part_info") or {}).get("material", "Stahl")
        part_info = _shape_info(shape, material=original_material)
        part_info["rotation_applied"] = {"x_degrees": angle_x, "y_degrees": angle_y}

        debug_logs = (state.get("debug_logs") or []) + [
            f"Rotation angewendet: X={angle_x:.1f} Grad, Y={angle_y:.1f} Grad"
        ]
        _save_min_state(
            step_file=step_filename,
            stl_file=stl_filename,
            part_info=part_info,
            debug_logs=debug_logs,
        )

        return jsonify({
            "status": "ok",
            "part_info": part_info,
            "stl_file": f"/download/{stl_filename}",
            "step_file": f"/download/{step_filename}",
            "debug_logs": debug_logs,
        })
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/generate_preform", methods=["POST"])
def generate_preform():
    state = _load_state()
    step_path = _output_path(state.get("step_file"))
    if not step_path:
        return jsonify({"status": "error", "message": "Bitte zuerst eine STEP/STP-Datei hochladen"}), 400

    data = request.get_json(silent=True) or {}
    coverage_target = _parse_float(data.get("coverage_target"), schmiedevorform.COVERAGE_PERCENTAGE)
    rotate_reference_90 = _parse_bool(data.get("rotate_reference_90"), False)
    debug_logs = ["Vorschmiedefreiform wird aus der STEP/STP-Referenz erzeugt."]

    try:
        basename = f"vorschmiedefreiform_{int(time.time())}"
        result = schmiedevorform.create_vorform_from_zuschnitt(
            step_path,
            step_path,
            coverage_target=coverage_target,
            rotate_reference_90=rotate_reference_90,
            output_folder=OUTPUT_FOLDER,
            output_basename=basename,
            debug_logs=debug_logs,
        )

        pdf_filename = f"{basename}_zeichnung.pdf"
        pdf_path = os.path.join(OUTPUT_FOLDER, pdf_filename)
        meta = {
            "project_name": "Vorschmiedefreiform",
            "part_name": "Vorschmiedefreiform fuer Gesenkschmieden",
            "material": "Stahl",
            "vorform_drawing": {
                "source_file": os.path.join(OUTPUT_FOLDER, result["stl_file"]),
                "stl_file": result["stl_file"],
                "step_file": result["step_file"],
                "weight_kg": result.get("weight_kg"),
                "volume_mm3": result.get("volume_mm3"),
                "preform_description": result.get("preform_description"),
            },
        }
        zeichnung_export.export_preform_sketch_pdf(
            os.path.join(OUTPUT_FOLDER, result["stl_file"]),
            pdf_path,
            meta,
        )
        result["debug_logs"].append(f"Vorformzeichnung exportiert: {pdf_filename}")

        process_data = state.get("process_data") or {}
        process_data["vorschmiedefreiform"] = {
            **result,
            "pdf_file": pdf_filename,
        }
        _save_min_state(process_data=process_data, debug_logs=result["debug_logs"])

        return jsonify({
            "status": "ok",
            "stl_file": f"/download/{result['stl_file']}",
            "step_file": f"/download/{result['step_file']}",
            "pdf_file": f"/download/{pdf_filename}",
            "volume_mm3": result.get("volume_mm3"),
            "weight_kg": result.get("weight_kg"),
            "dimensions_mm": result.get("dimensions_mm"),
            "coverage_xy": result.get("coverage_xy", {}),
            "preform_type": result.get("preform_type"),
            "preform_description": result.get("preform_description"),
            "sketch_group_hint": result.get("sketch_group_hint"),
            "debug_logs": result.get("debug_logs", []),
        })
    except Exception as exc:
        debug_logs.append(str(exc))
        return jsonify({"status": "error", "message": str(exc), "debug_logs": debug_logs}), 500


@app.route("/download/<filename>")
def download(filename):
    path = _output_path(filename)
    if path:
        return send_file(path, as_attachment=True, download_name=os.path.basename(path))
    return "Datei nicht gefunden", 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5020, debug=True)
