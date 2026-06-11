/*
 * Kleiner lokaler STL-Viewer ohne externe Bibliotheken.
 * Rendert ASCII- und Binary-STL per Canvas als interaktive Schraegansicht.
 */
(function () {
    const viewers = new Map();

    function parseBinaryStl(buffer) {
        const view = new DataView(buffer);
        if (buffer.byteLength < 84) return [];
        const triCount = view.getUint32(80, true);
        const expected = 84 + triCount * 50;
        if (expected > buffer.byteLength) return [];

        const triangles = [];
        let offset = 84;
        for (let i = 0; i < triCount; i++) {
            offset += 12;
            const tri = [];
            for (let v = 0; v < 3; v++) {
                tri.push({
                    x: view.getFloat32(offset, true),
                    y: view.getFloat32(offset + 4, true),
                    z: view.getFloat32(offset + 8, true),
                });
                offset += 12;
            }
            triangles.push(tri);
            offset += 2;
        }
        return triangles;
    }

    function parseAsciiStl(text) {
        const matches = [...text.matchAll(/vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)/g)];
        const triangles = [];
        for (let i = 0; i + 2 < matches.length; i += 3) {
            triangles.push([0, 1, 2].map(j => ({
                x: Number(matches[i + j][1]),
                y: Number(matches[i + j][2]),
                z: Number(matches[i + j][3]),
            })));
        }
        return triangles;
    }

    function parseStl(buffer) {
        const binary = parseBinaryStl(buffer);
        if (binary.length) return binary;
        const text = new TextDecoder("utf-8", {fatal: false}).decode(buffer);
        return parseAsciiStl(text);
    }

    function prepareGeometry(triangles) {
        const points = triangles.flat();
        const bounds = points.reduce((acc, p) => ({
            minX: Math.min(acc.minX, p.x),
            maxX: Math.max(acc.maxX, p.x),
            minY: Math.min(acc.minY, p.y),
            maxY: Math.max(acc.maxY, p.y),
            minZ: Math.min(acc.minZ, p.z),
            maxZ: Math.max(acc.maxZ, p.z),
        }), {
            minX: Infinity, maxX: -Infinity,
            minY: Infinity, maxY: -Infinity,
            minZ: Infinity, maxZ: -Infinity,
        });
        const center = {
            x: (bounds.minX + bounds.maxX) / 2,
            y: (bounds.minY + bounds.maxY) / 2,
            z: (bounds.minZ + bounds.maxZ) / 2,
        };
        const span = Math.max(
            bounds.maxX - bounds.minX,
            bounds.maxY - bounds.minY,
            bounds.maxZ - bounds.minZ,
            1
        );
        return {triangles, bounds, center, span};
    }

    function rotatePoint(point, state) {
        const x0 = (point.x - state.center.x) / state.span;
        const y0 = (point.y - state.center.y) / state.span;
        const z0 = (point.z - state.center.z) / state.span;

        const cy = Math.cos(state.rotY);
        const sy = Math.sin(state.rotY);
        const cx = Math.cos(state.rotX);
        const sx = Math.sin(state.rotX);

        const x1 = x0 * cy + z0 * sy;
        const z1 = -x0 * sy + z0 * cy;
        const y1 = y0 * cx - z1 * sx;
        const z2 = y0 * sx + z1 * cx;
        return {x: x1, y: y1, z: z2};
    }

    function draw(containerId) {
        const state = viewers.get(containerId);
        if (!state) return;

        const rect = state.canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        const width = Math.max(1, Math.floor(rect.width * dpr));
        const height = Math.max(1, Math.floor(rect.height * dpr));
        if (state.canvas.width !== width || state.canvas.height !== height) {
            state.canvas.width = width;
            state.canvas.height = height;
        }

        const ctx = state.canvas.getContext("2d");
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = "#0f172a";
        ctx.fillRect(0, 0, width, height);

        const scale = Math.min(width, height) * 0.74 * state.zoom;
        const projected = state.triangles.map(tri => {
            const pts = tri.map(p => {
                const r = rotatePoint(p, state);
                return {
                    x: width / 2 + r.x * scale,
                    y: height / 2 - r.y * scale,
                    z: r.z,
                };
            });
            return {
                pts,
                z: (pts[0].z + pts[1].z + pts[2].z) / 3,
            };
        }).sort((a, b) => a.z - b.z);

        for (const tri of projected) {
            const [a, b, c] = tri.pts;
            const light = Math.max(0.36, Math.min(0.98, 0.68 + tri.z * 0.42));
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.lineTo(c.x, c.y);
            ctx.closePath();
            ctx.fillStyle = shadeColor(state.color, light);
            ctx.fill();
            ctx.strokeStyle = "rgba(15, 23, 42, 0.24)";
            ctx.lineWidth = Math.max(0.35, dpr * 0.45);
            ctx.stroke();
        }

        ctx.fillStyle = "rgba(226, 232, 240, 0.9)";
        ctx.font = `${12 * dpr}px Arial, sans-serif`;
        const dims = state.bounds;
        const label = [
            `X ${(dims.maxX - dims.minX).toFixed(1)} mm`,
            `Y ${(dims.maxY - dims.minY).toFixed(1)} mm`,
            `Z ${(dims.maxZ - dims.minZ).toFixed(1)} mm`,
        ].join("   ");
        ctx.fillText(label, 14 * dpr, height - 14 * dpr);
    }

    function shadeColor(hex, factor) {
        const clean = hex.replace("#", "");
        const r = parseInt(clean.slice(0, 2), 16);
        const g = parseInt(clean.slice(2, 4), 16);
        const b = parseInt(clean.slice(4, 6), 16);
        const mix = value => Math.round(Math.max(0, Math.min(255, value * factor + 18)));
        return `rgb(${mix(r)}, ${mix(g)}, ${mix(b)})`;
    }

    function attachInteraction(containerId, canvas) {
        let dragging = false;
        let lastX = 0;
        let lastY = 0;

        canvas.addEventListener("pointerdown", event => {
            dragging = true;
            lastX = event.clientX;
            lastY = event.clientY;
            canvas.setPointerCapture(event.pointerId);
        });
        canvas.addEventListener("pointermove", event => {
            if (!dragging) return;
            const state = viewers.get(containerId);
            if (!state) return;
            state.rotY += (event.clientX - lastX) * 0.01;
            state.rotX += (event.clientY - lastY) * 0.01;
            lastX = event.clientX;
            lastY = event.clientY;
            draw(containerId);
        });
        canvas.addEventListener("pointerup", event => {
            dragging = false;
            try {
                canvas.releasePointerCapture(event.pointerId);
            } catch {
                // Ignore browsers that already released the pointer.
            }
        });
        canvas.addEventListener("wheel", event => {
            event.preventDefault();
            const state = viewers.get(containerId);
            if (!state) return;
            state.zoom = Math.max(0.35, Math.min(4, state.zoom * (event.deltaY > 0 ? 0.9 : 1.1)));
            draw(containerId);
        }, {passive: false});
    }

    window.renderStlViewer = async function renderStlViewer(url, containerId, options = {}) {
        const container = document.getElementById(containerId);
        if (!container || !url) return;
        container.innerHTML = "<div class=\"viewer-placeholder\">STL wird geladen...</div>";

        const response = await fetch(url);
        if (!response.ok) {
            container.innerHTML = "<div class=\"viewer-placeholder\">STL konnte nicht geladen werden.</div>";
            return;
        }
        const triangles = parseStl(await response.arrayBuffer());
        if (!triangles.length) {
            container.innerHTML = "<div class=\"viewer-placeholder\">Keine STL-Dreiecke gefunden.</div>";
            return;
        }

        const canvas = document.createElement("canvas");
        canvas.className = "local-stl-canvas";
        canvas.title = "Ziehen zum Drehen, Mausrad zum Zoomen";
        container.innerHTML = "";
        container.appendChild(canvas);

        const prepared = prepareGeometry(triangles);
        viewers.set(containerId, {
            ...prepared,
            canvas,
            color: options.color || "#60a5fa",
            rotX: -0.55,
            rotY: 0.72,
            zoom: 1,
        });
        attachInteraction(containerId, canvas);
        draw(containerId);
    };

    window.addEventListener("resize", () => {
        for (const id of viewers.keys()) draw(id);
    });

    window.clearStlViewer = function clearStlViewer(containerId) {
        viewers.delete(containerId);
    };
})();
