@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PORT=5020"
set "VORSCHMIEDE_DIR=%SCRIPT_DIR%"
set "VORSCHMIEDE_PORT=%PORT%"

if not defined FREECAD_PYTHON (
    if exist "C:\Program Files\FreeCAD 1.0\bin\python.exe" (
        set "FREECAD_PYTHON=C:\Program Files\FreeCAD 1.0\bin\python.exe"
    ) else if exist "C:\Program Files\FreeCAD 1.0\bin\FreeCADCmd.exe" (
        set "FREECAD_PYTHON=C:\Program Files\FreeCAD 1.0\bin\FreeCADCmd.exe"
    ) else if exist "C:\Program Files\FreeCAD 0.21\bin\python.exe" (
        set "FREECAD_PYTHON=C:\Program Files\FreeCAD 0.21\bin\python.exe"
    ) else if exist "C:\Program Files\FreeCAD 0.21\bin\FreeCADCmd.exe" (
        set "FREECAD_PYTHON=C:\Program Files\FreeCAD 0.21\bin\FreeCADCmd.exe"
    )
)

if not defined FREECAD_PYTHON (
    echo FreeCAD Python wurde nicht gefunden.
    echo Bitte FreeCAD installieren oder FREECAD_PYTHON auf die FreeCAD-python.exe setzen.
    echo Beispiel:
    echo   set FREECAD_PYTHON=C:\Program Files\FreeCAD 1.0\bin\python.exe
    pause
    exit /b 1
)

cd /d "%SCRIPT_DIR%"

echo Starte Vorschmiedefreiform Generator auf http://127.0.0.1:%PORT% ...

"%FREECAD_PYTHON%" -c "import os, sys; project_dir = os.environ['VORSCHMIEDE_DIR']; sys.path.insert(0, project_dir); os.chdir(project_dir); from app import app; app.run(host='0.0.0.0', port=int(os.environ.get('VORSCHMIEDE_PORT', '5020')), debug=True)"

pause
