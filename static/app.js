const uploadForm = document.getElementById("uploadForm");
const logBox = document.getElementById("log");
const partInfo = document.getElementById("partInfo");
const preformInfo = document.getElementById("preformInfo");
const rotationInfo = document.getElementById("rotationInfo");
const referenceDownloads = document.getElementById("referenceDownloads");
const preformDownloads = document.getElementById("preformDownloads");
const clearButton = document.getElementById("clear");
const sidebarSteps = [...document.querySelectorAll(".sidebar-step-link[data-step-id]")];
const stepState = {
    uploaded: false,
    rotated: false,
    preform: false,
    log: false,
};

const stepLabels = {
    pending: "Offen",
    active: "Aktiv",
    complete: "Fertig",
};

function setSidebarStepStatus(stepId, status) {
    const link = document.querySelector(`.sidebar-step-link[data-step-id="${stepId}"]`);
    if (!link) return;
    link.dataset.stepStatus = status;
    const badge = link.querySelector(".sidebar-step-state");
    if (badge) badge.textContent = stepLabels[status] || status;
}

function setActiveSidebarStep(stepId) {
    sidebarSteps.forEach(link => link.classList.toggle("active", link.dataset.stepId === stepId));
}

function updateSidebarWorkflow(activeStep = null) {
    setSidebarStepStatus("upload", stepState.uploaded ? "complete" : "active");
    setSidebarStepStatus("reference", stepState.uploaded ? "complete" : "pending");
    setSidebarStepStatus("rotation", stepState.rotated ? "complete" : (stepState.uploaded ? "active" : "pending"));
    setSidebarStepStatus("preform", stepState.preform ? "complete" : (stepState.uploaded ? "active" : "pending"));
    setSidebarStepStatus("log", stepState.log ? "complete" : "pending");

    if (activeStep) {
        setActiveSidebarStep(activeStep);
    } else if (!stepState.uploaded) {
        setActiveSidebarStep("upload");
    } else if (!stepState.preform) {
        setActiveSidebarStep("preform");
    } else {
        setActiveSidebarStep("preform");
    }
}

function setLog(lines) {
    const content = Array.isArray(lines) ? lines.join("\n") : String(lines || "");
    logBox.textContent = content || "Bereit.";
    stepState.log = Boolean(content && content !== "Bereit.");
    setSidebarStepStatus("log", stepState.log ? "complete" : "pending");
}

function formatDims(dims) {
    if (!dims) return "N/A";
    return `${Number(dims.x || 0).toFixed(1)} x ${Number(dims.y || 0).toFixed(1)} x ${Number(dims.z || 0).toFixed(1)} mm`;
}

function formatInfo(info) {
    if (!info) return "N/A";
    return [
        `Material: ${info.material || "Stahl"}`,
        `Volumen: ${Number(info.volume_mm3 || 0).toFixed(1)} mm3`,
        `Gewicht: ${Number(info.weight_kg || 0).toFixed(3)} kg`,
        `Abmessungen: ${formatDims(info.dimensions_mm)}`
    ].join("\n");
}

function downloadLinks(target, links) {
    target.innerHTML = "";
    links.filter(item => item.href).forEach(item => {
        const a = document.createElement("a");
        a.href = item.href;
        a.textContent = item.label;
        a.className = "btn";
        target.appendChild(a);
    });
}

function resetViewer(containerId, message) {
    if (typeof clearStlViewer === "function") {
        clearStlViewer(containerId);
    }
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = `<div class="viewer-placeholder">${message}</div>`;
}

function resetUiState() {
    stepState.uploaded = false;
    stepState.rotated = false;
    stepState.preform = false;
    stepState.log = false;
    document.getElementById("file").value = "";
    document.getElementById("rotX").value = "0";
    document.getElementById("rotY").value = "0";
    document.getElementById("coverage").value = "75";
    document.getElementById("rotate90").value = "0";
    partInfo.textContent = "Noch keine STEP/STP-Datei geladen.";
    preformInfo.textContent = "Noch keine Vorschmiedefreiform erzeugt.";
    rotationInfo.textContent = "Rotation: nicht angewendet.";
    referenceDownloads.innerHTML = "";
    preformDownloads.innerHTML = "";
    resetViewer("viewer_reference", "Nach dem Upload erscheint hier die STL-Ansicht des Fertigteils.");
    resetViewer("viewer_preform", "Nach der Generierung erscheint hier die STL-Ansicht der Vorschmiedefreiform.");
    setLog("Eingaben geloescht. Bereit fuer ein neues Projekt.");
    updateSidebarWorkflow("upload");
}

async function readJsonResponse(res) {
    const text = await res.text();
    let data;
    try {
        data = JSON.parse(text);
    } catch {
        throw new Error(text || `Serverfehler ${res.status}`);
    }
    if (!res.ok || data.status !== "ok") {
        throw new Error(data.message || `Serverfehler ${res.status}`);
    }
    return data;
}

clearButton.addEventListener("click", async () => {
    try {
        const data = await readJsonResponse(await fetch("/clear_steps", {method: "POST"}));
        resetUiState();
        setLog(data.message || "Eingaben geloescht.");
    } catch (error) {
        setLog(error.message);
        alert(error.message);
    }
});

uploadForm.addEventListener("submit", async event => {
    event.preventDefault();
    const file = document.getElementById("file").files[0];
    if (!file) return;

    const fd = new FormData();
    fd.append("file", file);
    updateSidebarWorkflow("upload");
    setLog("STEP/STP wird analysiert...");

    try {
        const data = await readJsonResponse(await fetch("/upload", {method: "POST", body: fd}));
        stepState.uploaded = true;
        stepState.rotated = false;
        stepState.preform = false;
        partInfo.classList.remove("muted");
        partInfo.textContent = formatInfo(data.part_info);
        downloadLinks(referenceDownloads, [
            {label: "Referenz STL", href: data.stl_file},
            {label: "Referenz STEP", href: data.step_file}
        ]);
        renderStlViewer(data.stl_file, "viewer_reference", {color: "#60a5fa"});
        setLog(data.debug_logs);
        updateSidebarWorkflow("reference");
    } catch (error) {
        setLog(error.message);
        alert(error.message);
    }
});

document.getElementById("rotateBtn").addEventListener("click", async () => {
    const x = Number(document.getElementById("rotX").value || 0);
    const y = Number(document.getElementById("rotY").value || 0);
    updateSidebarWorkflow("rotation");
    setLog("Rotation wird angewendet...");

    try {
        const data = await readJsonResponse(await fetch("/rotate", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({rotation_x_degrees: x, rotation_y_degrees: y})
        }));
        stepState.uploaded = true;
        stepState.rotated = true;
        stepState.preform = false;
        partInfo.textContent = formatInfo(data.part_info);
        rotationInfo.classList.remove("muted");
        rotationInfo.textContent = `Rotation angewendet: X=${x} Grad, Y=${y} Grad`;
        downloadLinks(referenceDownloads, [
            {label: "Rotierte Referenz STL", href: data.stl_file},
            {label: "Rotierte Referenz STEP", href: data.step_file}
        ]);
        renderStlViewer(data.stl_file, "viewer_reference", {color: "#60a5fa"});
        setLog(data.debug_logs);
        updateSidebarWorkflow("rotation");
    } catch (error) {
        setLog(error.message);
        alert(error.message);
    }
});

document.getElementById("generateBtn").addEventListener("click", async () => {
    const coverage = Number(document.getElementById("coverage").value || 75) / 100;
    const rotateReference90 = document.getElementById("rotate90").value === "90";
    updateSidebarWorkflow("preform");
    setLog("Vorschmiedefreiform wird erzeugt...");

    try {
        const data = await readJsonResponse(await fetch("/generate_preform", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                coverage_target: coverage,
                rotate_reference_90: rotateReference90
            })
        }));
        stepState.uploaded = true;
        stepState.preform = true;
        preformInfo.classList.remove("muted");
        preformInfo.textContent = [
            `Typ: ${data.preform_description || "Adaptive Vorschmiedefreiform"}`,
            `Abmessungen: ${formatDims(data.dimensions_mm)}`,
            `Gewicht: ${Number(data.weight_kg || 0).toFixed(3)} kg`,
            `Abdeckung X/Y: ${Number((data.coverage_xy?.x || 0) * 100).toFixed(1)} % / ${Number((data.coverage_xy?.y || 0) * 100).toFixed(1)} %`,
            data.sketch_group_hint ? `Hinweis: ${data.sketch_group_hint}` : ""
        ].filter(Boolean).join("\n");
        downloadLinks(preformDownloads, [
            {label: "Vorschmiedefreiform STL", href: data.stl_file},
            {label: "Vorschmiedefreiform STEP", href: data.step_file},
            {label: "Zeichnung PDF", href: data.pdf_file}
        ]);
        renderStlViewer(data.stl_file, "viewer_preform", {color: "#f59e0b"});
        setLog(data.debug_logs);
        updateSidebarWorkflow("preform");
    } catch (error) {
        setLog(error.message);
        alert(error.message);
    }
});

updateSidebarWorkflow("upload");
