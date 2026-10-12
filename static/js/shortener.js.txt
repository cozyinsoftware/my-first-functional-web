const longUrl = document.getElementById("long-url");
const shortenButton = document.getElementById("shorten-link");
const shortenStatus = document.getElementById("shorten-status");
const shortResultWrap = document.getElementById("short-result-wrap");
const shortResult = document.getElementById("short-result");

shortenButton.addEventListener("click", async () => {
    const value = longUrl.value.trim();

    if (!value) {
        shortenStatus.textContent = "Please enter a link.";
        return;
    }

    shortenButton.disabled = true;
    shortenStatus.textContent = "Creating short link...";

    try {
        const response = await fetch("/api/shorten", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: value })
        });

        const data = await response.json();

        if (!response.ok) {
            shortenStatus.textContent = data.error || "Could not create the short link.";
            return;
        }

        shortResult.value = data.short_url;
        shortResultWrap.classList.remove("hidden");
        shortenStatus.textContent = "Short link created.";
    } catch {
        shortenStatus.textContent = "Network error. Please try again.";
    } finally {
        shortenButton.disabled = false;
    }
});

document.getElementById("copy-short-link").addEventListener("click", () => {
    copyText(shortResult.value);
});