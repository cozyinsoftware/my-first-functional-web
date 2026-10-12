let html5QrCode = null;

const startCameraButton = document.getElementById("start-camera");
const stopCameraButton = document.getElementById("stop-camera");
const scanFile = document.getElementById("scan-file");
const dropZone = document.getElementById("scan-drop-zone");
const status = document.getElementById("scan-status");
const resultBox = document.getElementById("scan-result-box");
const resultText = document.getElementById("scan-result");
const copyButton = document.getElementById("copy-scan-result");
const openButton = document.getElementById("open-scan-result");

function showResult(value) {
    resultText.value = value;
    resultBox.classList.remove("hidden");

    if (typeof isHttpLink === "function" && isHttpLink(value)) {
        openButton.href = value;
        openButton.classList.remove("hidden");
    } else {
        openButton.classList.add("hidden");
    }
}

function decodeWithJsQR(image) {
    if (typeof jsQR === "undefined") {
        return null;
    }

    const maxSize = 1024;
    let width = image.naturalWidth || image.width;
    let height = image.naturalHeight || image.height;

    if (!width || !height) {
        return null;
    }

    const scale = Math.min(1, maxSize / Math.max(width, height));
    width = Math.round(width * scale);
    height = Math.round(height * scale);

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    context.drawImage(image, 0, 0, width, height);

    try {
        const imageData = context.getImageData(0, 0, width, height);
        const code = jsQR(imageData.data, imageData.width, imageData.height);
        return code ? code.data : null;
    } catch {
        return null;
    }
}

async function decodeWithHtml5Qrcode(file) {
    if (typeof Html5Qrcode === "undefined") {
        return null;
    }

    try {
        const tempScanner = new Html5Qrcode("camera-reader");
        const decodedText = await tempScanner.scanFileV2(file, true);
        if (tempScanner && typeof tempScanner.clear === "function") {
            try {
                await tempScanner.clear();
            } catch {
                // Ignore cleanup errors.
            }
        }
        return typeof decodedText === "string" ? decodedText : decodedText?.decodedText || null;
    } catch {
        return null;
    }
}

function loadImageFromFile(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            const image = new Image();
            image.onload = () => resolve(image);
            image.onerror = () => reject(new Error("Could not read this image file."));
            image.src = reader.result;
        };
        reader.onerror = () => reject(new Error("Could not read this image file."));
        reader.readAsDataURL(file);
    });
}

async function scanImage(file) {
    if (!file) {
        return;
    }

    if (!file.type || !file.type.startsWith("image/")) {
        status.textContent = "Please choose an image file.";
        return;
    }

    status.textContent = "Scanning image...";

    try {
        const image = await loadImageFromFile(file);
        const jsQrResult = decodeWithJsQR(image);

        if (jsQrResult) {
            status.textContent = "QR code found.";
            showResult(jsQrResult);
            return;
        }

        const fallbackResult = await decodeWithHtml5Qrcode(file);

        if (fallbackResult) {
            status.textContent = "QR code found.";
            showResult(fallbackResult);
        } else if (typeof jsQR === "undefined" && typeof Html5Qrcode === "undefined") {
            status.textContent = "Scanner libraries are still loading. Please wait a moment and try again.";
        } else {
            status.textContent = "No QR code was found in this image.";
        }
    } catch {
        status.textContent = "Could not read this image. Please try another image file.";
    } finally {
        // Allow selecting the same file again.
        if (scanFile) {
            scanFile.value = "";
        }
    }
}

async function startCamera() {
    if (typeof Html5Qrcode === "undefined") {
        status.textContent = "Camera library is still loading. Please wait a moment and try again.";
        return;
    }

    try {
        if (!html5QrCode) {
            html5QrCode = new Html5Qrcode("camera-reader");
        }

        await html5QrCode.start(
            { facingMode: "environment" },
            { fps: 10, qrbox: { width: 250, height: 250 } },
            (decodedText) => {
                status.textContent = "QR code found.";
                showResult(decodedText);
                stopCamera();
            }
        );

        status.textContent = "Camera is active. Point it at a QR code.";
    } catch {
        status.textContent = "Could not start the camera. Use HTTPS or localhost and allow camera access.";
    }
}

async function stopCamera() {
    if (!html5QrCode) return;

    try {
        const state = typeof html5QrCode.getState === "function" ? html5QrCode.getState() : null;
        // 2 = Html5QrcodeScannerState.SCANNING. Only stop when actually scanning.
        if (state === null || state === 2) {
            await html5QrCode.stop();
            status.textContent = "Camera stopped.";
        }
    } catch {
        // Camera may already be stopped.
    }
}

startCameraButton.addEventListener("click", startCamera);
stopCameraButton.addEventListener("click", stopCamera);

scanFile.addEventListener("change", () => scanImage(scanFile.files[0]));

copyButton.addEventListener("click", () => copyText(resultText.value));

dropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropZone.classList.add("drag-over");
});

dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("drag-over");
});

dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropZone.classList.remove("drag-over");
    if (event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files.length > 0) {
        scanImage(event.dataTransfer.files[0]);
    }
});

document.addEventListener("paste", (event) => {
    const clipboard = event.clipboardData;
    if (!clipboard) {
        return;
    }

    if (clipboard.files && clipboard.files.length > 0) {
        const imageFile = [...clipboard.files].find((file) => file.type.startsWith("image/"));
        if (imageFile) {
            event.preventDefault();
            scanImage(imageFile);
            return;
        }
    }

    if (!clipboard.items) {
        return;
    }

    const item = [...clipboard.items].find((entry) => entry.type && entry.type.startsWith("image/"));

    if (item) {
        event.preventDefault();
        scanImage(item.getAsFile());
    }
});
