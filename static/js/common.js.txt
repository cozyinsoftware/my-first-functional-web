function copyText(text) {
    navigator.clipboard.writeText(text)
        .then(() => alert("Copied to clipboard."))
        .catch(() => alert("Could not copy. Please copy it manually."));
}

function isHttpLink(value) {
    try {
        const url = new URL(value);
        return url.protocol === "http:" || url.protocol === "https:";
    } catch {
        return false;
    }
}