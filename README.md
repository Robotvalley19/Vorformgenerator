# Vorformgenerator

Standalone-Websoftware zur automatischen Erzeugung einer Vorschmiedefreiform aus STEP/STP-Geometrien fuer das Gesenkschmieden. Die Software ist fuer Linux und Windows geeignet.

Die Software erzeugt eine Vorform, die vor dem Fertigschmieden in das Gesenk eingelegt werden kann. Sie entstand aus der praktischen Problematik, Schmiedeprozesse schneller und nachvollziehbarer auszulegen: Aus einem vorhandenen 3D-Modell des Gesenkschmiedeteils wird eine freiformgeschmiedete Vorstufe abgeleitet, skaliert und als CAD-/Zeichnungsdaten ausgegeben.

## Einordnung in das Gesamtprojekt

Dieses Repository ist bewusst nur ein kleiner, veroeffentlichbarer Ausschnitt aus einem deutlich groesseren zusammenhaengenden Entwicklungsprojekt. Das Gesamtprojekt verbindet Konstruktion, technologische Entwicklung, Werkzeuganalyse und Vertrieb in einer gemeinsamen Website.

Im grossen System werden mehrere Arbeitsschritte zusammengefuehrt:

- CAD-/Konstruktionsdaten hochladen, ausrichten und geometrisch auswerten
- technologische Entwicklung des Schmiedeprozesses unterstuetzen
- Rohteil-, Vorform- und Fertigschmiedegeometrien ableiten
- Werkzeuganalyse und prozessrelevante Kennwerte sichtbar machen
- technische Zeichnungen, Auswertungen und Entscheidungsgrundlagen erzeugen
- vertriebliche Angebots- und Projektinformationen mit technischen Daten verbinden

Die hier ausgekoppelte Vorschmiedefreiform zeigt einen einzelnen Kernbaustein daraus: die automatische Ableitung einer Vorform, die als vorbereitende Freiformstufe in das Gesenk eingelegt und anschliessend fertiggeschmiedet werden kann.


## Funktionen

- STEP/STP-Modell eines Gesenkschmiedeteils hochladen
- Referenzgeometrie als STEP/STL pruefen und exportieren
- lokale STL-Ansicht des hochgeladenen Fertigteils anzeigen
- Bauteil optional um X- und Y-Achse ausrichten
- Vorschmiedefreiform automatisch aus der 3D-Geometrie ableiten
- lokale STL-Ansicht der erzeugten Vorschmiedefreiform anzeigen
- X/Y-Abdeckung einstellen, Standardwert 75 Prozent
- optionale 90-Grad-Lage fuer Faserverlauf/Freiformorientierung
- Export als STL, STEP und technische PDF-Zeichnung

## Fachlicher Zweck

Beim Gesenkschmieden ist die Vorform entscheidend fuer Materialfluss, Fuellverhalten und Prozesssicherheit. Dieses Projekt berechnet eine Vorschmiedefreiform als robuste Naeherung:

- Die STEP/STP-Geometrie dient als Referenz fuer die Fertigteilkontur.
- Die Vorschmiedefreiform wird in der X/Y-Projektion skaliert.
- Die Hoehe wird ueber Volumenerhaltung abgeleitet.
- Profilmerkmale entlang der Laengsachse werden fuer eine schmiedenahe Form beruecksichtigt.

Die Ausgabe ersetzt keine finale FEM-Simulation oder Werkzeugfreigabe, kann aber als schneller Ausgangspunkt fuer Prozessauslegung, Angebotsphase, Variantenvergleich und interne Abstimmung dienen.

## Voraussetzungen

- Python 3.11 oder neuer
- FreeCAD Python-Module: `FreeCAD`, `Part`, `MeshPart`
- Python-Pakete aus `requirements.txt`
- Linux oder Windows

FreeCAD muss so verfuegbar sein, dass die verwendete Python-Umgebung `FreeCAD`, `Part` und `MeshPart` importieren kann. Unter Linux kann dafuer die FreeCAD AppImage verwendet werden. Unter Windows wird die installierte FreeCAD-Python-Umgebung verwendet.

## Installation

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Start unter Linux mit FreeCAD AppImage

```bash
chmod +x run.sh
./run.sh
```

Die Anwendung laeuft danach standardmaessig auf:

```text
http://localhost:5020
```

`python app.py` funktioniert nur, wenn diese Python-Umgebung FreeCAD direkt importieren kann. Wenn `ModuleNotFoundError: No module named 'FreeCAD'` erscheint, bitte `./run.sh` verwenden oder den FreeCAD-Pythonpfad korrekt setzen.

## Start unter Windows mit FreeCAD

Per Doppelklick oder Eingabeaufforderung:

```bat
start.bat
```

Die Datei sucht FreeCAD standardmaessig unter `C:\Program Files\FreeCAD 1.0\bin\python.exe`. Wenn FreeCAD an einem anderen Ort installiert ist, kann vorher `FREECAD_PYTHON` gesetzt werden:

```bat
set "FREECAD_PYTHON=C:\Program Files\FreeCAD 1.0\bin\python.exe"
start.bat
```

Die Anwendung laeuft danach standardmaessig auf:

```text
http://localhost:5020
```

## Bedienung

1. STEP/STP-Datei des Gesenkschmiedeteils hochladen.
2. Falls noetig X-/Y-Drehung einstellen und anwenden.
3. Zielabdeckung fuer die X/Y-Projektion einstellen.
4. Vorschmiedefreiform erzeugen.
5. STL, STEP oder PDF-Zeichnung herunterladen.

## Projektstruktur

```text
Vorschmiedefreiform/
  app.py
  schmiedevorform.py
  stl_quality.py
  zeichnung_export.py
  static/
  templates/
  uploads/
  outputs/
  README.md
  LICENSE
  NOTICE.md
  requirements.txt
  run.sh
  start.bat
```

`uploads/` und `outputs/` sind Laufzeitordner. Sie bleiben fuer GitHub leer und werden per `.gitignore` geschuetzt.

## Lizenz

Dieses Projekt steht unter der MIT-Lizenz.

Copyright (c) 2026 Robotvalley19

Siehe [LICENSE](LICENSE).

## Rechtliche Hinweise

- Dieses Repository enthaelt keine Normtexte, Norm-PDFs, Kundendaten, fremde Logos oder proprietaere Zeichnungen.
- Hochgeladene STEP/STP-Dateien und generierte Ergebnisse werden durch `.gitignore` nicht fuer GitHub vorgemerkt.
- Die Weboberflaeche laedt keine externen Fonts, CDNs, Skripte oder Stylesheets.
- Die Flask-App setzt eine Content-Security-Policy, die Browser-Ressourcen und API-Verbindungen auf dieselbe lokale Anwendung beschraenkt.
- FreeCAD und weitere Abhaengigkeiten behalten ihre jeweiligen Lizenzen.
- Die Software wird ohne Gewaehr bereitgestellt. Fachliche Ergebnisse muessen vor Produktion, Werkzeugfreigabe oder Angebotsabgabe technisch geprueft werden.
- Diese Hinweise sind keine Rechtsberatung und koennen keine vollstaendige Abmahnsicherheit garantieren.
