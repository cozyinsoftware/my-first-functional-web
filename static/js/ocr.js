const ocrFile = document.getElementById("ocr-file");
const ocrDropZone = document.getElementById("ocr-drop-zone");
const languageSelect = document.getElementById("ocr-language");
const progressWrap = document.getElementById("ocr-progress-wrap");
const progress = document.getElementById("ocr-progress");
const ocrStatus = document.getElementById("ocr-status");
const ocrResult = document.getElementById("ocr-result");

async function readImage(file) {
    if (!file || !file.type.startsWith("image/")) {
        ocrStatus.textContent = "Please choose an image file.";
        return;
    }

    progressWrap.classList.remove("hidden");
    progress.style.width = "0%";
    ocrStatus.textContent = "Starting OCR...";

    try {
        const result = await Tesseract.recognize(file, languageSelect.value, {
            logger: (message) => {
                if (message.status) {
                    ocrStatus.textContent = message.status;
                }

                if (typeof message.progress === "number") {
                    progress.style.width = `${Math.round(message.progress * 100)}%`;
                }
            }
        });

        ocrResult.value = result.data.text.trim();
        ocrStatus.textContent = "Text reading finished.";
        progress.style.width = "100%";
    } catch {
        ocrStatus.textContent = "Could not read this image. Please try a clearer image.";
    }
}

ocrFile.addEventListener("change", () => {
    readImage(ocrFile.files[0]);
    ocrFile.value = "";
});

ocrDropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    ocrDropZone.classList.add("drag-over");
});

ocrDropZone.addEventListener("dragleave", () => {
    ocrDropZone.classList.remove("drag-over");
});

ocrDropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    ocrDropZone.classList.remove("drag-over");
    if (event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files.length > 0) {
        readImage(event.dataTransfer.files[0]);
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
            readImage(imageFile);
            return;
        }
    }

    if (!clipboard.items) {
        return;
    }

    const item = [...clipboard.items].find((entry) => entry.type && entry.type.startsWith("image/"));

    if (item) {
        event.preventDefault();
        readImage(item.getAsFile());
    }
});

document.getElementById("copy-ocr").addEventListener("click", () => {
    copyText(ocrResult.value);
});

document.getElementById("download-ocr").addEventListener("click", () => {
    const blob = new Blob([ocrResult.value], { type: "text/plain" });
    const link = document.createElement("a");

    link.href = URL.createObjectURL(blob);
    link.download = "image-text.txt";
    link.click();

    URL.revokeObjectURL(link.href);
});