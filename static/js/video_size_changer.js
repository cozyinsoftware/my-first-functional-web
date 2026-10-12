(function () {
    var fileInput = document.getElementById("vc-file");
    var dropZone = document.getElementById("vc-drop-zone");
    var fileInfo = document.getElementById("vc-file-info");
    var modeRadios = document.querySelectorAll('input[name="vc-mode"]');
    var standardPane = document.getElementById("vc-standard");
    var proPane = document.getElementById("vc-pro");
    var convertBtn = document.getElementById("vc-convert");
    var resetBtn = document.getElementById("vc-reset");
    var progressWrap = document.getElementById("vc-progress-wrap");
    var progress = document.getElementById("vc-progress");
    var statusEl = document.getElementById("vc-status");
    var resultBox = document.getElementById("vc-result");
    var resultText = document.getElementById("vc-result-text");
    var resultWarnings = document.getElementById("vc-result-warnings");
    var downloadLink = document.getElementById("vc-download");
    var estimateText = document.getElementById("vc-estimate-text");

    var sourcePreviewWrap = document.getElementById("vc-source-preview-wrap");
    var sourcePreview = document.getElementById("vc-source-preview");
    var resultPreview = document.getElementById("vc-result-preview");
    var resultComparison = document.getElementById("vc-result-comparison");
    var convertAnotherBtn = document.getElementById("vc-convert-another");

    var taskEl = document.getElementById("vc-task");
    var resEl = document.getElementById("vc-resolution");
    var fpsEl = document.getElementById("vc-fps");
    var qualityEl = document.getElementById("vc-quality");

    var selectedFile = null;
    var previewUrl = null;
    var ALLOWED = ["mp4", "mov", "avi", "mkv", "webm", "mpeg", "mpg", "m4v"];
    var MAX_BYTES = 7 * 1024 * 1024 * 1024;

    function currentMode() {
        var checked = document.querySelector('input[name="vc-mode"]:checked');
        return checked ? checked.value : "standard";
    }

    function fmtBytes(n) {
        if (!n || n <= 0) return "unknown size";
        var units = ["B", "KB", "MB", "GB"];
        var i = 0;
        var v = n;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return v.toFixed(v >= 10 ? 0 : 1) + " " + units[i];
    }

    function setStatus(msg) {
        if (statusEl) {
            statusEl.textContent = msg || "";
            statusEl.style.color = "";
        }
    }

    function validateFile(file) {
        if (!file) return "Please choose a video file first.";
        var ext = (file.name.split(".").pop() || "").toLowerCase();
        if (ALLOWED.indexOf(ext) === -1) {
            return "Unsupported file type. Please upload MP4, MOV, AVI, MKV, WebM, or MPEG.";
        }
        if (file.size <= 0) return "The selected file looks empty.";
        if (file.size > MAX_BYTES) {
            return "File is too large (" + fmtBytes(file.size) + "). Limit is 7 GB.";
        }
        return null;
    }

    function pickFile(file) {
        var err = validateFile(file);
        if (err) {
            fileInfo.textContent = err;
            selectedFile = null;
            if (sourcePreviewWrap) sourcePreviewWrap.classList.add("hidden");
            updateEstimate();
            return;
        }
        selectedFile = file;
        fileInfo.textContent = "Selected: " + file.name + " (" + fmtBytes(file.size) + ")";
        resultBox.classList.add("hidden");

        // Preview selected video in browser
        if (sourcePreview && sourcePreviewWrap) {
            if (previewUrl) {
                try { URL.revokeObjectURL(previewUrl); } catch (e) {}
            }
            try {
                previewUrl = URL.createObjectURL(file);
                sourcePreview.src = previewUrl;
                sourcePreviewWrap.classList.remove("hidden");
            } catch (e) {
                sourcePreviewWrap.classList.add("hidden");
            }
        }

        updateEstimate();
    }

    function updateEstimate() {
        if (!estimateText) return;
        if (!selectedFile) {
            estimateText.textContent = "Choose a file to see an estimate.";
            return;
        }
        var params = new URLSearchParams({
            original_bytes: String(selectedFile.size),
            quality: qualityEl ? qualityEl.value : "medium",
            resolution: resEl ? resEl.value : "source"
        });
        fetch("/api/video/estimate?" + params.toString())
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.estimated_bytes) {
                    estimateText.textContent = "Rough estimate: ~" + fmtBytes(data.estimated_bytes) +
                        " (original " + fmtBytes(selectedFile.size) + "). Real size depends on video content and duration.";
                } else {
                    estimateText.textContent = "Estimate unavailable for this file.";
                }
            })
            .catch(function () {
                estimateText.textContent = "Estimate unavailable right now.";
            });
    }

    function proWarnings() {
        var box = document.getElementById("vc-pro-warnings");
        if (!box) return;
        var msgs = [];
        var w = parseInt(document.getElementById("vc-width").value || "0", 10);
        var h = parseInt(document.getElementById("vc-height").value || "0", 10);
        var crf = parseInt(document.getElementById("vc-crf").value || "99", 10);
        var vb = (document.getElementById("vc-vbitrate").value || "").toLowerCase();
        if (w >= 3840 || h >= 2160) msgs.push("Very high resolution: expect a large file and slow encoding.");
        if (!isNaN(crf) && crf <= 17) msgs.push("CRF 17 or lower is near-lossless and creates very large files.");
        if (vb && (vb.indexOf("20m") !== -1 || parseFloat(vb) >= 20)) msgs.push("High video bitrate may create a very large file.");
        var fmt = document.getElementById("vc-format").value;
        var vc = document.getElementById("vc-vcodec").value;
        var ac = document.getElementById("vc-acodec").value;
        if (fmt === "webm" && ["libvpx-vp9", "libvpx"].indexOf(vc) === -1) {
            msgs.push("Warning: WebM needs VP8/VP9 video. Pick libvpx or libvpx-vp9.");
        }
        if (fmt === "webm" && ["opus", "vorbis"].indexOf(ac) === -1) {
            msgs.push("Warning: WebM audio must be Opus or Vorbis.");
        }
        if (msgs.length) {
            box.classList.remove("hidden");
            box.innerHTML = "<h2>Warnings</h2><p class='status'>" + msgs.join("<br>") + "</p>";
        } else {
            box.classList.add("hidden");
            box.innerHTML = "";
        }
    }

    function buildFormData() {
        var fd = new FormData();
        fd.append("video", selectedFile);
        var mode = currentMode();
        fd.append("mode", mode);
        if (mode === "standard") {
            fd.append("task", taskEl.value);
            fd.append("resolution", resEl.value);
            fd.append("fps", fpsEl.value);
            fd.append("quality", qualityEl.value);
        } else {
            var ids = ["format", "vcodec", "acodec", "preset", "width", "height",
                "pro-fps", "vbitrate", "abitrate", "crf", "aspect", "rotation",
                "trim-start", "trim-end"];
            var map = {
                "format": "format", "vcodec": "video_codec", "acodec": "audio_codec",
                "preset": "preset", "width": "width", "height": "height",
                "pro-fps": "fps", "vbitrate": "video_bitrate", "abitrate": "audio_bitrate",
                "crf": "crf", "aspect": "aspect", "rotation": "rotation",
                "trim-start": "trim_start", "trim-end": "trim_end"
            };
            var elIds = {
                "format": "vc-format", "vcodec": "vc-vcodec", "acodec": "vc-acodec",
                "preset": "vc-preset", "width": "vc-width", "height": "vc-height",
                "pro-fps": "vc-pro-fps", "vbitrate": "vc-vbitrate", "abitrate": "vc-abitrate",
                "crf": "vc-crf", "aspect": "vc-aspect", "rotation": "vc-rotation",
                "trim-start": "vc-trim-start", "trim-end": "vc-trim-end"
            };
            ids.forEach(function (k) {
                var el = document.getElementById(elIds[k]);
                if (el) fd.append(map[k], el.value);
            });
            var removeAudio = document.getElementById("vc-remove-audio").checked;
            fd.append("remove_audio", removeAudio ? "1" : "0");
        }
        return fd;
    }

    function showDone(data) {
        progress.style.width = "100%";
        setStatus("Conversion complete!");
        resultBox.classList.remove("hidden");
        resultText.textContent = "Your video has been converted to ." + data.format + " format (" + fmtBytes(data.output_bytes) + ").";

        if (resultComparison && selectedFile) {
            var orig = selectedFile.size;
            var out = data.output_bytes || 0;
            var diff = orig - out;
            var pct = orig > 0 ? Math.round((Math.abs(diff) / orig) * 100) : 0;
            if (diff > 0) {
                resultComparison.innerHTML = "<span style='color:#16a34a; font-weight:600;'>Reduced by " + pct + "%</span> (" + fmtBytes(orig) + " &rarr; " + fmtBytes(out) + ")";
            } else if (diff < 0) {
                resultComparison.innerHTML = "<span style='color:#2563eb; font-weight:600;'>Resized: " + fmtBytes(orig) + " &rarr; " + fmtBytes(out) + "</span>";
            } else {
                resultComparison.innerHTML = "<span>Final size: " + fmtBytes(out) + "</span>";
            }
        }

        var resultPreviewWrap = document.getElementById("vc-result-preview-wrap");
        if (resultPreview) {
            if (resultPreviewWrap) resultPreviewWrap.classList.remove("hidden");
            var playUrl = data.stream_url || data.download_url;
            if (playUrl) {
                resultPreview.src = playUrl;
                resultPreview.load();
            }
        }

        if (downloadLink) {
            downloadLink.href = data.download_url;
            downloadLink.setAttribute("download", "converted." + data.format);
            var downloadText = document.getElementById("vc-download-text");
            if (downloadText) {
                downloadText.textContent = "Download Converted Video (" + fmtBytes(data.output_bytes) + ")";
            }
        }

        resultWarnings.textContent = (data.warnings || []).join(" ");
        convertBtn.disabled = false;

        try {
            resultBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
        } catch (e) {}
    }

    function showError(msg, detail) {
        progress.style.width = "0%";
        var full = msg || "Conversion failed. Please try again.";
        if (detail && full.indexOf(detail) === -1) {
            full += " (Details: " + detail + ")";
        }
        setStatus(full);
        if (statusEl) statusEl.style.color = "#dc2626";
        convertBtn.disabled = false;
    }

    function pollStatus(statusUrl, startedAt) {
        var TIMEOUT_MS = 2 * 60 * 60 * 1000;
        function elapsedText() {
            var s = Math.floor((Date.now() - startedAt) / 1000);
            var m = Math.floor(s / 60);
            if (m < 1) return s + "s";
            var h = Math.floor(m / 60);
            if (h < 1) return m + "m " + (s % 60) + "s";
            return h + "h " + (m % 60) + "m";
        }
        function tick() {
            if (Date.now() - startedAt > TIMEOUT_MS) {
                showError("Conversion timed out after 2 hours. Try a shorter video or lower settings.");
                return;
            }
            fetch(statusUrl, { cache: "no-store" })
                .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
                .then(function (res) {
                    var data = res.body || {};
                    if (!res.ok) {
                        showError(data.error || "Conversion failed. Please try again.");
                        return;
                    }
                    if (data.state === "done" && data.download_url) {
                        showDone(data);
                        return;
                    }
                    if (data.state === "error") {
                        showError(data.error || "Conversion failed. Please try again.", data.error_detail);
                        return;
                    }
                    var pct = 50 + Math.round((data.progress || 0) / 2);
                    if (pct > 97) pct = 97;
                    progress.style.width = pct + "%";
                    setStatus("Processing video on server… " + elapsedText() +
                        " elapsed. Please keep this tab open.");
                    setTimeout(tick, 2000);
                })
                .catch(function () {
                    setStatus("Processing video on server… " + elapsedText() +
                        " elapsed. Reconnecting to check status…");
                    setTimeout(tick, 3000);
                });
        }
        tick();
    }

    function convert() {
        var err = validateFile(selectedFile);
        if (err) {
            setStatus(err);
            progressWrap.classList.remove("hidden");
            return;
        }
        var fd = buildFormData();
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/video/convert");
        progressWrap.classList.remove("hidden");
        resultBox.classList.add("hidden");
        convertBtn.disabled = true;
        progress.style.width = "0%";
        setStatus("Uploading video… 0%");

        xhr.upload.onprogress = function (e) {
            if (e.lengthComputable) {
                var pct = Math.round((e.loaded / e.total) * 100);
                var shown = Math.round(pct / 2);
                progress.style.width = shown + "%";
                setStatus("Uploading video… " + pct + "%");
            } else {
                setStatus("Uploading video…");
            }
        };
        xhr.onload = function () {
            var data = {};
            try { data = JSON.parse(xhr.responseText); } catch (e) { data = {}; }
            if (xhr.status >= 200 && xhr.status < 300) {
                if (data.download_url) {
                    showDone(data);
                    return;
                }
                if (data.status_url || data.job_id) {
                    progress.style.width = "50%";
                    var statusUrl = data.status_url || ("/api/video/status/" + data.job_id);
                    pollStatus(statusUrl, Date.now());
                    return;
                }
            }
            showError((data && data.error) || ("Conversion failed (HTTP " + xhr.status + "). Please try again."));
        };
        xhr.onerror = function () {
            convertBtn.disabled = false;
            setStatus("Network error. Please check your connection and try again.");
        };
        xhr.ontimeout = function () {
            convertBtn.disabled = false;
            setStatus("Request timed out. Try a shorter video or lower settings.");
        };
        xhr.timeout = 0;
        xhr.send(fd);
    }

    function resetAll() {
        selectedFile = null;
        if (fileInput) fileInput.value = "";
        fileInfo.textContent = "";
        setStatus("");
        progress.style.width = "0%";
        progressWrap.classList.add("hidden");
        resultBox.classList.add("hidden");
        if (sourcePreviewWrap) sourcePreviewWrap.classList.add("hidden");
        if (sourcePreview) {
            sourcePreview.pause();
            sourcePreview.src = "";
        }
        if (resultPreview) {
            resultPreview.pause();
            resultPreview.src = "";
        }
        if (previewUrl) {
            try { URL.revokeObjectURL(previewUrl); } catch (e) {}
            previewUrl = null;
        }
        var downloadText = document.getElementById("vc-download-text");
        if (downloadText) {
            downloadText.textContent = "Download Converted Video";
        }
        convertBtn.disabled = false;
        updateEstimate();
        try {
            if (dropZone) dropZone.scrollIntoView({ behavior: "smooth", block: "nearest" });
        } catch (e) {}
    }

    modeRadios.forEach(function (r) {
        r.addEventListener("change", function () {
            var pro = currentMode() === "professional";
            standardPane.classList.toggle("hidden", pro);
            proPane.classList.toggle("hidden", !pro);
        });
    });

    fileInput.addEventListener("change", function () {
        if (fileInput.files && fileInput.files[0]) {
            pickFile(fileInput.files[0]);
        }
    });

    fileInput.addEventListener("click", function () {
        // Reset input value on click so selecting the same file triggers change event
        fileInput.value = "";
    });

    dropZone.addEventListener("dragover", function (e) {
        e.preventDefault();
        dropZone.classList.add("drag-over");
    });
    dropZone.addEventListener("dragleave", function () {
        dropZone.classList.remove("drag-over");
    });
    dropZone.addEventListener("drop", function (e) {
        e.preventDefault();
        dropZone.classList.remove("drag-over");
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
            pickFile(e.dataTransfer.files[0]);
        }
    });

    ["vc-task", "vc-resolution", "vc-fps", "vc-quality"].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) el.addEventListener("change", updateEstimate);
    });
    if (proPane) {
        proPane.addEventListener("input", proWarnings);
        proPane.addEventListener("change", proWarnings);
    }

    convertBtn.addEventListener("click", convert);
    resetBtn.addEventListener("click", resetAll);
    if (convertAnotherBtn) {
        convertAnotherBtn.addEventListener("click", resetAll);
    }

    updateEstimate();
})();
