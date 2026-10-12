const qrUrl = document.getElementById("qr-url");
const generateButton = document.getElementById("generate-qr");
const qrStatus = document.getElementById("qr-status");
const qrResultWrap = document.getElementById("qr-result-wrap");
const qrImage = document.getElementById("qr-image");
const downloadQr = document.getElementById("download-qr");

generateButton.addEventListener("click", async () => {
    const value = qrUrl.value.trim();

    if (!value) {
        qrStatus.textContent = "Please enter a link.";
        return;
    }

    generateButton.disabled = true;
    qrStatus.textContent = "Generating QR code...";

    try {
        const response = await fetch("/api/generate-qr", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: value })
        });

        const data = await response.json();

        if (!response.ok) {
            qrStatus.textContent = data.error || "Could not generate QR code.";
            return;
        }

        qrUrl.value = data.url;
        qrImage.src = data.image;
        downloadQr.href = data.image;
        qrResultWrap.classList.remove("hidden");
        qrStatus.textContent = "QR code created.";
    } catch {
        qrStatus.textContent = "Network error. Please try again.";
    } finally {
        generateButton.disabled = false;
    }
});