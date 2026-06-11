# Notice

Vorschmiedefreiform Generator

Copyright (c) 2026 Robotvalley19

This project is provided as standalone software for generating a forging preform from STEP/STP geometry.

## Third-party components

The project uses external open-source software at runtime, including:

- FreeCAD / OpenCASCADE through the FreeCAD Python modules
- Flask and Werkzeug for the web application
- matplotlib and NumPy for export and drawing functionality

Each third-party component remains subject to its own license terms.

## Content policy for this repository

This repository should not include:

- customer CAD files or confidential production data
- standards, norm PDFs, paid technical documents or copied reference literature
- third-party logos, trademarks or screenshots without permission
- generated uploads or output files

Runtime folders such as `uploads/` and `outputs/` are ignored by Git.

## Local-only browser policy

The web interface does not include external CDNs, Google Fonts or remote assets. The Flask application also sends a Content-Security-Policy that restricts scripts, styles, fonts, images and API requests to the local application origin.
