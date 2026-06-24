let currentJobId = null;
let eventSource = null;
let scannedUrl = null;
let mediaItems = []; // items returned from /api/scan
let activeTab = "website";

function formatBytes(bytes) {
    if (!bytes || bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function formatTime(seconds) {
    if (!seconds || seconds <= 0) return "0s";
    if (seconds < 60) return Math.round(seconds) + "s";
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return m + "m " + s + "s";
}

function addLog(message, type = "") {
    const logContent = document.getElementById("logContent");
    const entry = document.createElement("div");
    entry.className = "log-entry" + (type ? " " + type : "");
    entry.textContent = message;
    logContent.appendChild(entry);
    const logContainer = document.getElementById("logContainer");
    logContainer.scrollTop = logContainer.scrollHeight;
}

// -----------------------------------------------------------------------
// Scan phase
// -----------------------------------------------------------------------

function scanPage() {
    const urlInput = document.getElementById("urlInput");
    const url = urlInput.value.trim();
    if (!url) {
        urlInput.focus();
        return;
    }

    const mode = document.querySelector('input[name="mode"]:checked').value;
    const deepScan = document.getElementById("deepScan").checked;

    document.getElementById("scanBtn").disabled = true;
    urlInput.disabled = true;
    document.getElementById("previewSection").classList.add("hidden");
    document.getElementById("progressSection").classList.add("hidden");
    document.getElementById("resultsSection").classList.add("hidden");
    document.getElementById("scanningSpinner").classList.remove("hidden");

    const spinnerText = document.querySelector("#scanningSpinner span");
    if (deepScan) {
        spinnerText.textContent = "Deep scanning — scrolling page to load all content, then extracting media (this may take a while)...";
    } else {
        spinnerText.textContent = "Scanning page for media...";
    }

    fetch("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url, mode: mode, deep_scan: deepScan }),
    })
        .then((r) => r.json())
        .then((data) => {
            document.getElementById("scanningSpinner").classList.add("hidden");
            document.getElementById("scanBtn").disabled = false;
            urlInput.disabled = false;

            if (data.error) {
                alert("Error: " + data.error);
                return;
            }

            scannedUrl = data.scanned_url;
            mediaItems = data.items;
            renderPreview();
        })
        .catch((err) => {
            document.getElementById("scanningSpinner").classList.add("hidden");
            document.getElementById("scanBtn").disabled = false;
            urlInput.disabled = false;
            alert("Scan failed: " + err);
        });
}

// -----------------------------------------------------------------------
// Preview grid
// -----------------------------------------------------------------------

function renderPreview() {
    const grid = document.getElementById("previewGrid");
    grid.innerHTML = "";

    if (mediaItems.length === 0) {
        grid.innerHTML =
            '<div style="grid-column:1/-1;text-align:center;color:#666;padding:40px 0;">No media found on this page.</div>';
        document.getElementById("previewSection").classList.remove("hidden");
        document.getElementById("previewCount").textContent = "0 items";
        return;
    }

    mediaItems.forEach((item, index) => {
        const card = document.createElement("div");
        card.className = "preview-card selected";
        card.dataset.index = index;
        card.onclick = () => toggleCard(card, index);

        const isVideo = item.type !== "image";
        const platform = item.platform || "";
        let badgeClass, badgeLabel;
        if (platform) {
            badgeClass = "platform";
            badgeLabel = platform;
        } else if (isVideo) {
            badgeClass = "video";
            badgeLabel = "VID";
        } else {
            badgeClass = "image";
            badgeLabel = "IMG";
        }

        let thumbHtml;
        if (item.preview_url) {
            thumbHtml = `<img class="preview-thumb" src="${escapeAttr(item.preview_url)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=\\'preview-placeholder\\'>${isVideo ? '&#127909;' : '&#128247;'}</div>'">`;
        } else {
            thumbHtml = `<div class="preview-placeholder">${isVideo ? "&#127909;" : "&#128247;"}</div>`;
        }

        card.innerHTML = `
            <div class="check-overlay">&#10003;</div>
            <div class="type-badge ${badgeClass}">${badgeLabel}</div>
            ${thumbHtml}
            <div class="filename" title="${escapeAttr(item.filename)}">${escapeHtml(item.filename)}</div>
        `;

        grid.appendChild(card);
    });

    document.getElementById("previewSection").classList.remove("hidden");
    updateSelectionCount();
    document.getElementById("previewCount").textContent = mediaItems.length + " items";
}

function escapeAttr(str) {
    return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeHtml(str) {
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function toggleCard(card) {
    card.classList.toggle("selected");
    updateSelectionCount();
}

function selectAll() {
    document.querySelectorAll(".preview-card").forEach((c) => c.classList.add("selected"));
    updateSelectionCount();
}

function deselectAll() {
    document.querySelectorAll(".preview-card").forEach((c) => c.classList.remove("selected"));
    updateSelectionCount();
}

function updateSelectionCount() {
    const count = document.querySelectorAll(".preview-card.selected").length;
    document.getElementById("selectedCount").textContent = count;
    document.getElementById("downloadSelectedBtn").disabled = count === 0;
}

// -----------------------------------------------------------------------
// Download phase
// -----------------------------------------------------------------------

function downloadSelected() {
    const selectedCards = document.querySelectorAll(".preview-card.selected");
    if (selectedCards.length === 0) return;

    // Generate a default folder name from the URL domain
    let defaultName = "";
    try {
        const host = new URL(scannedUrl).hostname.replace(/^www\./, "");
        defaultName = host;
    } catch (_) {}

    const input = document.getElementById("folderNameInput");
    input.value = defaultName;
    document.getElementById("folderModal").classList.remove("hidden");
    setTimeout(() => { input.focus(); input.select(); }, 50);
}

function closeFolderModal() {
    document.getElementById("folderModal").classList.add("hidden");
}

function confirmModalDownload() {
    if (activeTab === "social") {
        confirmSocialDownload();
    } else if (activeTab === "mainvideo") {
        confirmMainVideoDownload();
    } else {
        confirmDownload();
    }
}

function confirmDownload() {
    const folderName = document.getElementById("folderNameInput").value.trim();
    closeFolderModal();

    const selectedCards = document.querySelectorAll(".preview-card.selected");
    const selectedUrls = [];
    selectedCards.forEach((card) => {
        const idx = parseInt(card.dataset.index);
        selectedUrls.push(mediaItems[idx].url);
    });

    if (selectedUrls.length === 0) return;

    const mode = document.querySelector('input[name="mode"]:checked').value;

    // Hide preview, show progress
    document.getElementById("previewSection").classList.add("hidden");
    document.getElementById("progressSection").classList.remove("hidden");
    document.getElementById("resultsSection").classList.add("hidden");
    document.getElementById("logContent").innerHTML = "";
    document.getElementById("scanResults").classList.add("hidden");
    document.getElementById("currentFile").classList.add("hidden");
    document.getElementById("overallProgress").style.width = "0%";
    document.getElementById("statusText").textContent = "Starting download...";
    document.getElementById("etaText").textContent = "";
    document.getElementById("progressDetail").textContent = "";
    document.getElementById("bytesDownloaded").textContent = "";
    document.getElementById("scanBtn").disabled = true;
    document.getElementById("urlInput").disabled = true;
    const stopBtn = document.getElementById("stopBtn");
    stopBtn.disabled = false;
    stopBtn.textContent = "Stop";

    addLog("Downloading " + selectedUrls.length + " selected items...", "info");

    const payload = {
        url: scannedUrl,
        mode: mode,
        selected_urls: selectedUrls,
    };
    if (folderName) {
        payload.folder_name = folderName;
    }

    fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    })
        .then((r) => r.json())
        .then((data) => {
            if (data.error) {
                addLog("Error: " + data.error, "error");
                document.getElementById("scanBtn").disabled = false;
                document.getElementById("urlInput").disabled = false;
                return;
            }
            currentJobId = data.job_id;
            listenToProgress(data.job_id);
        })
        .catch((err) => {
            addLog("Request failed: " + err, "error");
            document.getElementById("scanBtn").disabled = false;
            document.getElementById("urlInput").disabled = false;
        });
}

function listenToProgress(jobId) {
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource("/api/progress/" + jobId);

    eventSource.addEventListener("status", function (e) {
        const data = JSON.parse(e.data);
        document.getElementById("statusText").textContent = data.step;
        addLog(data.step, "info");

        // Show encoding spinner when re-encoding, hide download bars
        if (data.step.includes("Re-encoding")) {
            document.getElementById("downloadProgress").classList.add("hidden");
            document.getElementById("currentFile").classList.add("hidden");
            document.getElementById("encodingSpinner").classList.remove("hidden");
            document.getElementById("encodingSpinner").querySelector("span").textContent = data.step;
        } else {
            document.getElementById("encodingSpinner").classList.add("hidden");
            document.getElementById("downloadProgress").classList.remove("hidden");
        }
    });

    eventSource.addEventListener("scan_result", function (e) {
        const data = JSON.parse(e.data);
        document.getElementById("scanResults").classList.remove("hidden");
        document.getElementById("scanImages").textContent = data.images;
        document.getElementById("scanVideos").textContent =
            data.direct_videos + data.embedded_videos + data.ytdlp_videos;
        addLog(
            "Downloading " + data.images + " images, " +
                (data.direct_videos + data.embedded_videos + data.ytdlp_videos) +
                " videos",
            "info"
        );

        if (data.total === 0) {
            document.getElementById("statusText").textContent = "No media found";
            addLog("No downloadable media found.", "error");
        }
    });

    eventSource.addEventListener("file_start", function (e) {
        const data = JSON.parse(e.data);
        const cf = document.getElementById("currentFile");
        cf.classList.remove("hidden");
        document.getElementById("currentFileName").textContent = data.filename;
        document.getElementById("fileProgress").style.width = "0%";
    });

    eventSource.addEventListener("file_progress", function (e) {
        const data = JSON.parse(e.data);
        if (data.total_bytes > 0) {
            const pct = Math.round((data.downloaded / data.total_bytes) * 100);
            document.getElementById("fileProgress").style.width = pct + "%";
        }
    });

    eventSource.addEventListener("file_done", function (e) {
        const data = JSON.parse(e.data);
        if (data.success) {
            addLog("Downloaded: " + data.filename + " (" + formatBytes(data.bytes) + ")", "success");
        } else {
            addLog("Failed: " + data.filename + " - " + (data.error || "unknown error"), "error");
        }
        document.getElementById("fileProgress").style.width = "100%";
    });

    eventSource.addEventListener("overall_progress", function (e) {
        const data = JSON.parse(e.data);
        let total = data.total_images || 1;
        let pct = Math.round((data.current / total) * 100);
        document.getElementById("overallProgress").style.width = pct + "%";
        document.getElementById("progressDetail").textContent =
            data.current + " / " + total + " files";
        document.getElementById("bytesDownloaded").textContent =
            formatBytes(data.total_bytes);

        if (data.eta > 0) {
            document.getElementById("etaText").textContent =
                "ETA: " + formatTime(data.eta);
        }
    });

    eventSource.addEventListener("ytdlp_progress", function (e) {
        const data = JSON.parse(e.data);
        if (data.total_bytes > 0) {
            const pct = Math.round((data.downloaded / data.total_bytes) * 100);
            document.getElementById("overallProgress").style.width = pct + "%";
            document.getElementById("fileProgress").style.width = pct + "%";
            document.getElementById("currentFileName").textContent =
                "Video: " + formatBytes(data.downloaded) + " / " + formatBytes(data.total_bytes);
        }
        if (data.eta > 0) {
            document.getElementById("etaText").textContent =
                "ETA: " + formatTime(data.eta);
        }
        if (data.speed > 0) {
            document.getElementById("bytesDownloaded").textContent =
                formatBytes(data.speed) + "/s";
        }
    });

    eventSource.addEventListener("log", function (e) {
        const data = JSON.parse(e.data);
        addLog(data.message, "info");
    });

    eventSource.addEventListener("complete", function (e) {
        const data = JSON.parse(e.data);
        eventSource.close();
        eventSource = null;

        document.getElementById("stopBtn").disabled = true;
        document.getElementById("overallProgress").style.width = "100%";
        document.getElementById("statusText").textContent = "Complete!";
        document.getElementById("etaText").textContent = "";
        document.getElementById("currentFile").classList.add("hidden");
        document.getElementById("encodingSpinner").classList.add("hidden");
        document.getElementById("downloadProgress").classList.remove("hidden");

        // Show results
        document.getElementById("progressSection").classList.add("hidden");
        document.getElementById("resultsSection").classList.remove("hidden");
        document.getElementById("resultImages").textContent = data.images_downloaded;
        document.getElementById("resultVideos").textContent = data.videos_downloaded;
        document.getElementById("resultSkipped").textContent = data.skipped;
        document.getElementById("resultTime").textContent = formatTime(data.total_time);
        if (data.total_bytes) {
            document.getElementById("resultBytes").textContent =
                "Total downloaded: " + formatBytes(data.total_bytes);
        }

        addLog(
            "Completed: " +
                data.images_downloaded +
                " images, " +
                data.videos_downloaded +
                " videos in " +
                formatTime(data.total_time),
            "success"
        );
    });

    eventSource.addEventListener("error", function (e) {
        let msg = "Connection error";
        try {
            const data = JSON.parse(e.data);
            msg = data.message || msg;
        } catch (_) {}
        addLog("Error: " + msg, "error");
        document.getElementById("statusText").textContent = "Error occurred";
        document.getElementById("stopBtn").disabled = true;
        document.getElementById("scanBtn").disabled = false;
        document.getElementById("urlInput").disabled = false;
        document.getElementById("socialDownloadBtn").disabled = false;
        document.getElementById("socialUrlInput").disabled = false;
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }
    });
}

function stopDownload() {
    if (!currentJobId) return;
    const btn = document.getElementById("stopBtn");
    btn.disabled = true;
    btn.textContent = "Stopping...";
    fetch("/api/cancel/" + currentJobId, { method: "POST" })
        .then(() => addLog("Stop requested — finishing current segment...", "error"))
        .catch(() => addLog("Stop request failed.", "error"));
}

function downloadZip() {
    if (currentJobId) {
        window.location.href = "/api/download-zip/" + currentJobId;
    }
}

function resetUI() {
    document.getElementById("progressSection").classList.add("hidden");
    document.getElementById("resultsSection").classList.add("hidden");
    document.getElementById("previewSection").classList.add("hidden");
    document.getElementById("scanningSpinner").classList.add("hidden");
    document.getElementById("previewModal").classList.add("hidden");
    document.getElementById("folderModal").classList.add("hidden");
    document.getElementById("urlInput").value = "";
    document.getElementById("urlInput").disabled = false;
    document.getElementById("scanBtn").disabled = false;
    document.getElementById("mainVideoUrlInput").value = "";
    document.getElementById("mainVideoUrlInput").disabled = false;
    document.getElementById("mainVideoDownloadBtn").disabled = false;
    document.getElementById("mainVideoPlatformBadge").classList.add("hidden");
    document.getElementById("socialUrlInput").value = "";
    document.getElementById("socialUrlInput").disabled = false;
    document.getElementById("socialDownloadBtn").disabled = false;
    document.getElementById("platformBadge").classList.add("hidden");
    currentJobId = null;
    scannedUrl = null;
    mediaItems = [];

    if (activeTab === "website") {
        document.getElementById("urlInput").focus();
    } else if (activeTab === "mainvideo") {
        document.getElementById("mainVideoUrlInput").focus();
    } else {
        document.getElementById("socialUrlInput").focus();
    }
}

// -----------------------------------------------------------------------
// Tab switching
// -----------------------------------------------------------------------

function switchTab(tab) {
    activeTab = tab;

    // Update tab buttons
    document.querySelectorAll(".tab-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.tab === tab);
    });

    // Show/hide tab content
    document.getElementById("websiteTab").classList.toggle("hidden", tab !== "website");
    document.getElementById("mainVideoTab").classList.toggle("hidden", tab !== "mainvideo");
    document.getElementById("socialTab").classList.toggle("hidden", tab !== "social");

    // Hide shared sections when switching
    document.getElementById("progressSection").classList.add("hidden");
    document.getElementById("resultsSection").classList.add("hidden");
}

// -----------------------------------------------------------------------
// Social media platform detection
// -----------------------------------------------------------------------

const SOCIAL_PLATFORMS = {
    "youtube.com": { name: "YouTube", css: "youtube" },
    "www.youtube.com": { name: "YouTube", css: "youtube" },
    "m.youtube.com": { name: "YouTube", css: "youtube" },
    "youtu.be": { name: "YouTube", css: "youtube" },
    "facebook.com": { name: "Facebook", css: "facebook" },
    "www.facebook.com": { name: "Facebook", css: "facebook" },
    "m.facebook.com": { name: "Facebook", css: "facebook" },
    "web.facebook.com": { name: "Facebook", css: "facebook" },
    "fb.watch": { name: "Facebook", css: "facebook" },
    "instagram.com": { name: "Instagram", css: "instagram" },
    "www.instagram.com": { name: "Instagram", css: "instagram" },
    "tiktok.com": { name: "TikTok", css: "tiktok" },
    "www.tiktok.com": { name: "TikTok", css: "tiktok" },
    "vm.tiktok.com": { name: "TikTok", css: "tiktok" },
    "twitter.com": { name: "X / Twitter", css: "twitter" },
    "www.twitter.com": { name: "X / Twitter", css: "twitter" },
    "x.com": { name: "X / Twitter", css: "twitter" },
    "www.x.com": { name: "X / Twitter", css: "twitter" },
};

function detectPlatform(url) {
    try {
        const host = new URL(url).hostname.toLowerCase();
        return SOCIAL_PLATFORMS[host] || null;
    } catch (_) {
        return null;
    }
}

function updatePlatformBadge() {
    const url = document.getElementById("socialUrlInput").value.trim();
    const badge = document.getElementById("platformBadge");

    if (!url) {
        badge.classList.add("hidden");
        return;
    }

    const platform = detectPlatform(url);
    if (platform) {
        badge.textContent = platform.name;
        badge.className = "platform-badge " + platform.css;
        badge.classList.remove("hidden");
    } else {
        badge.classList.add("hidden");
    }
}

// -----------------------------------------------------------------------
// Main Video extraction
// -----------------------------------------------------------------------

function mainVideoDownload() {
    const urlInput = document.getElementById("mainVideoUrlInput");
    const url = urlInput.value.trim();
    if (!url) {
        urlInput.focus();
        return;
    }

    // Disable button and show loading
    document.getElementById("mainVideoDownloadBtn").disabled = true;
    urlInput.disabled = true;
    
    addLog("Fetching video preview...", "info");

    fetch("/api/preview-main-video", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url }),
    })
        .then((r) => r.json())
        .then((data) => {
            document.getElementById("mainVideoDownloadBtn").disabled = false;
            urlInput.disabled = false;
            
            if (data.error) {
                addLog("Error: " + data.error, "error");
                return;
            }

            // Show preview modal
            document.getElementById("previewTitle").textContent = data.title;
            document.getElementById("previewPlatform").textContent = data.platform;
            
            const thumbnail = document.getElementById("previewThumbnail");
            if (data.thumbnail) {
                thumbnail.src = data.thumbnail;
                thumbnail.classList.remove("hidden");
            } else {
                thumbnail.classList.add("hidden");
            }
            
            const durationDiv = document.getElementById("previewDurationDiv");
            if (data.duration && data.duration > 0) {
                const mins = Math.floor(data.duration / 60);
                const secs = data.duration % 60;
                document.getElementById("previewDuration").textContent = mins + "m " + secs + "s";
                durationDiv.style.display = "flex";
            } else {
                durationDiv.style.display = "none";
            }
            
            document.getElementById("previewModal").classList.remove("hidden");
        })
        .catch((err) => {
            document.getElementById("mainVideoDownloadBtn").disabled = false;
            urlInput.disabled = false;
            addLog("Preview failed: " + err, "error");
        });
}

function closePreviewModal() {
    document.getElementById("previewModal").classList.add("hidden");
}

function proceedToDownload() {
    closePreviewModal();
    const url = document.getElementById("mainVideoUrlInput").value.trim();
    
    // Pre-fill with default name
    const parsed = new URL(url);
    const host = parsed.hostname.replace("www.", "");
    const defaultName = "main_video_" + host;
    
    const input = document.getElementById("folderNameInput");
    input.value = defaultName;
    document.getElementById("folderModal").classList.remove("hidden");
    setTimeout(() => { input.focus(); input.select(); }, 50);
}

function confirmMainVideoDownload() {
    const folderName = document.getElementById("folderNameInput").value.trim();
    closeFolderModal();

    const url = document.getElementById("mainVideoUrlInput").value.trim();
    if (!url) return;

    document.getElementById("progressSection").classList.remove("hidden");
    document.getElementById("resultsSection").classList.add("hidden");
    document.getElementById("logContent").innerHTML = "";
    document.getElementById("scanResults").classList.add("hidden");
    document.getElementById("currentFile").classList.remove("hidden");
    document.getElementById("overallProgress").style.width = "0%";
    document.getElementById("statusText").textContent = "Detecting main video...";
    document.getElementById("etaText").textContent = "";
    document.getElementById("progressDetail").textContent = "";
    document.getElementById("bytesDownloaded").textContent = "";
    document.getElementById("currentFileName").textContent = "";
    document.getElementById("mainVideoDownloadBtn").disabled = true;
    document.getElementById("mainVideoUrlInput").disabled = true;
    const stopBtnMV = document.getElementById("stopBtn");
    stopBtnMV.disabled = false;
    stopBtnMV.textContent = "Stop";

    const platform = detectPlatform(url);
    addLog("Extracting main video from " + (platform ? platform.name : url) + "...", "info");

    const payload = { url: url };
    if (folderName) payload.folder_name = folderName;

    fetch("/api/main-video-download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    })
        .then((r) => r.json())
        .then((data) => {
            if (data.error) {
                addLog("Error: " + data.error, "error");
                document.getElementById("statusText").textContent = "Error";
                document.getElementById("mainVideoDownloadBtn").disabled = false;
                document.getElementById("mainVideoUrlInput").disabled = false;
                return;
            }
            currentJobId = data.job_id;
            listenToProgress(data.job_id);
        })
        .catch((err) => {
            addLog("Request failed: " + err, "error");
            document.getElementById("mainVideoDownloadBtn").disabled = false;
            document.getElementById("mainVideoUrlInput").disabled = false;
        });
}

// -----------------------------------------------------------------------
// Social media download
// -----------------------------------------------------------------------

function socialDownload() {
    const urlInput = document.getElementById("socialUrlInput");
    const url = urlInput.value.trim();
    if (!url) {
        urlInput.focus();
        return;
    }

    const platform = detectPlatform(url);

    // Pre-fill folder name with platform name or domain
    let defaultName = "";
    if (platform) {
        defaultName = platform.name;
    } else {
        try {
            defaultName = new URL(url).hostname.replace(/^www\./, "");
        } catch (_) {}
    }

    const input = document.getElementById("folderNameInput");
    input.value = defaultName;
    document.getElementById("folderModal").classList.remove("hidden");
    setTimeout(() => { input.focus(); input.select(); }, 50);
}

function confirmSocialDownload() {
    const folderName = document.getElementById("folderNameInput").value.trim();
    closeFolderModal();

    const url = document.getElementById("socialUrlInput").value.trim();
    if (!url) return;

    // Hide social tab input section visually, show progress
    document.getElementById("progressSection").classList.remove("hidden");
    document.getElementById("resultsSection").classList.add("hidden");
    document.getElementById("logContent").innerHTML = "";
    document.getElementById("scanResults").classList.add("hidden");
    document.getElementById("currentFile").classList.remove("hidden");
    document.getElementById("overallProgress").style.width = "0%";
    document.getElementById("statusText").textContent = "Starting social media download...";
    document.getElementById("etaText").textContent = "";
    document.getElementById("progressDetail").textContent = "";
    document.getElementById("bytesDownloaded").textContent = "";
    document.getElementById("currentFileName").textContent = "";
    document.getElementById("socialDownloadBtn").disabled = true;
    document.getElementById("socialUrlInput").disabled = true;
    const stopBtnSocial = document.getElementById("stopBtn");
    stopBtnSocial.disabled = false;
    stopBtnSocial.textContent = "Stop";

    const platform = detectPlatform(url);
    addLog("Downloading from " + (platform ? platform.name : url) + "...", "info");

    const payload = { url: url };
    if (folderName) payload.folder_name = folderName;

    fetch("/api/social-download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    })
        .then((r) => r.json())
        .then((data) => {
            if (data.error) {
                addLog("Error: " + data.error, "error");
                document.getElementById("statusText").textContent = "Error";
                document.getElementById("socialDownloadBtn").disabled = false;
                document.getElementById("socialUrlInput").disabled = false;
                return;
            }
            currentJobId = data.job_id;
            listenToProgress(data.job_id);
        })
        .catch((err) => {
            addLog("Request failed: " + err, "error");
            document.getElementById("socialDownloadBtn").disabled = false;
            document.getElementById("socialUrlInput").disabled = false;
        });
}

// -----------------------------------------------------------------------
// Event listeners
// -----------------------------------------------------------------------

// Allow Enter key to trigger scan
document.getElementById("urlInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
        scanPage();
    }
});

// Social URL input: detect platform on input, Enter to download
document.getElementById("socialUrlInput").addEventListener("input", updatePlatformBadge);
document.getElementById("socialUrlInput").addEventListener("paste", function () {
    setTimeout(updatePlatformBadge, 0);
});
document.getElementById("socialUrlInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
        socialDownload();
    }
});

// Main Video URL input: detect platform on input, Enter to download
document.getElementById("mainVideoUrlInput").addEventListener("input", function () {
    const url = document.getElementById("mainVideoUrlInput").value.trim();
    const badge = document.getElementById("mainVideoPlatformBadge");
    const platform = detectPlatform(url);
    if (platform) {
        badge.textContent = platform.name;
        badge.className = "platform-badge " + platform.css;
        badge.classList.remove("hidden");
    } else {
        badge.classList.add("hidden");
    }
});
document.getElementById("mainVideoUrlInput").addEventListener("paste", function () {
    setTimeout(() => {
        const url = document.getElementById("mainVideoUrlInput").value.trim();
        const badge = document.getElementById("mainVideoPlatformBadge");
        const platform = detectPlatform(url);
        if (platform) {
            badge.textContent = platform.name;
            badge.className = "platform-badge " + platform.css;
            badge.classList.remove("hidden");
        } else {
            badge.classList.add("hidden");
        }
    }, 0);
});
document.getElementById("mainVideoUrlInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
        mainVideoDownload();
    }
});

// Allow Enter to confirm and Escape to cancel in folder modal
document.getElementById("folderNameInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
        if (activeTab === "social") {
            confirmSocialDownload();
        } else {
            confirmDownload();
        }
    } else if (e.key === "Escape") {
        closeFolderModal();
    }
});
