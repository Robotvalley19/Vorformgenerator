#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT=5020

APPIMAGE="$PARENT_DIR/FreeCAD_1.0.0-conda-Linux-x86_64-py311.AppImage"
if [ ! -x "$APPIMAGE" ]; then
    APPIMAGE="$HOME/Downloads/FreeCAD_1.0.0-conda-Linux-x86_64-py311.AppImage"
fi

if [ ! -x "$APPIMAGE" ]; then
    echo "FreeCAD AppImage nicht gefunden."
    echo "Erwartet wurde:"
    echo "  $PARENT_DIR/FreeCAD_1.0.0-conda-Linux-x86_64-py311.AppImage"
    echo "oder:"
    echo "  $HOME/Downloads/FreeCAD_1.0.0-conda-Linux-x86_64-py311.AppImage"
    exit 1
fi

echo "Starte Vorschmiedefreiform Generator auf http://127.0.0.1:$PORT ..."

cd "$SCRIPT_DIR"

"$APPIMAGE" -c "
import os
import sys

PORT = $PORT
PROJECT_DIR = '$SCRIPT_DIR'

sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from app import app

app.run(host='0.0.0.0', port=PORT)
"
