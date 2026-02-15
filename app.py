#!/usr/bin/env python3
"""
Media Downloader - Web Application
Flask backend that downloads images and videos from any URL.
"""

import os
import re
import json
import time
import uuid
import hashlib
import zipfile
import threading
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, Response, send_file
from playwright.sync_api import sync_playwright

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

app = Flask(__name__, template_folder="templates", static_folder="static")

DOWNLOADS_DIR = Path(__file__).parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Store active job progress: { job_id: [list of SSE messages] }
jobs = {}
jobs_lock = threading.Lock()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mov", ".mkv", ".flv", ".m4v", ".ogv"}

# Domains where we skip HTML scraping and go straight to yt-dlp
YTDLP_DIRECT_DOMAINS = {
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com",
    "vm.tiktok.com",
    "facebook.com", "www.facebook.com",
    "m.facebook.com", "fb.watch",
    "web.facebook.com",
    "youtube.com", "www.youtube.com",
    "m.youtube.com", "youtu.be",
    "twitter.com", "www.twitter.com",
    "x.com", "www.x.com",
}


def is_ytdlp_direct_url(url: str) -> bool:
    """Check if URL belongs to a platform best handled directly by yt-dlp."""
    return urlparse(url).netloc.lower() in YTDLP_DIRECT_DOMAINS


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name: str, max_length: int = 200) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    if len(name) > max_length:
        stem, ext = os.path.splitext(name)
        name = stem[: max_length - len(ext)] + ext
    return name or "unnamed"


def get_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if not filename or "." not in filename:
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        filename = f"media_{url_hash}"
    return sanitize_filename(filename)


def push_event(job_id: str, event_type: str, data: dict):
    """Push a Server-Sent Event message to the job's queue."""
    with jobs_lock:
        if job_id in jobs:
            msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            jobs[job_id]["events"].append(msg)


# ---------------------------------------------------------------------------
# Media discovery
# ---------------------------------------------------------------------------

def find_image_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    image_urls = set()

    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "srcset"):
            val = img.get(attr)
            if val:
                if attr == "srcset":
                    for candidate in val.split(","):
                        parts = candidate.strip().split()
                        if parts:
                            image_urls.add(urljoin(base_url, parts[0]))
                else:
                    image_urls.add(urljoin(base_url, val))

    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            for candidate in srcset.split(","):
                parts = candidate.strip().split()
                if parts:
                    url = urljoin(base_url, parts[0])
                    ext = os.path.splitext(urlparse(url).path)[1].lower()
                    if ext in IMAGE_EXTENSIONS:
                        image_urls.add(url)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        ext = os.path.splitext(urlparse(href).path)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            image_urls.add(urljoin(base_url, href))

    for tag in soup.find_all(style=True):
        urls_found = re.findall(r'url\(["\']?(.*?)["\']?\)', tag["style"])
        for u in urls_found:
            full_url = urljoin(base_url, u)
            ext = os.path.splitext(urlparse(full_url).path)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                image_urls.add(full_url)

    filtered = set()
    for url in image_urls:
        lower = url.lower()
        if any(s in lower for s in ("1x1", "pixel", "spacer", "blank.gif", "data:image")):
            continue
        filtered.add(url)

    return sorted(filtered)


def _find_nearby_thumbnail(tag, base_url: str) -> str:
    """Walk up the DOM from a video/source tag to find a nearby <img> thumbnail."""
    # 1) Check poster attribute on <video>
    video_parent = tag if tag.name == "video" else tag.find_parent("video")
    if video_parent and video_parent.get("poster"):
        return urljoin(base_url, video_parent["poster"])

    # 2) Search parent containers (up to 5 levels) for an <img>
    current = tag
    for _ in range(5):
        parent = current.parent
        if not parent or parent.name in ("body", "html", "[document]"):
            break
        img = parent.find("img")
        if img:
            for attr in ("src", "data-src", "data-original", "data-lazy-src"):
                val = img.get(attr)
                if val and not val.startswith("data:"):
                    return urljoin(base_url, val)
            # Check srcset for the largest image
            srcset = img.get("srcset")
            if srcset:
                parts = srcset.split(",")[-1].strip().split()
                if parts:
                    return urljoin(base_url, parts[0])
        current = parent

    return ""


def find_video_urls(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Return list of dicts: {"url": ..., "poster": ...}"""
    seen = set()
    results = []

    def _add(url, poster=""):
        if url not in seen:
            seen.add(url)
            results.append({"url": url, "poster": poster})

    for video in soup.find_all("video"):
        poster = _find_nearby_thumbnail(video, base_url)
        src = video.get("src")
        if src:
            _add(urljoin(base_url, src), poster)
        for source in video.find_all("source"):
            src = source.get("src")
            if src:
                _add(urljoin(base_url, src), poster)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        ext = os.path.splitext(urlparse(href).path)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            poster = _find_nearby_thumbnail(a, base_url)
            _add(urljoin(base_url, href), poster)

    for tag in soup.find_all(["iframe", "embed"]):
        src = tag.get("src")
        if src:
            full_url = urljoin(base_url, src)
            ext = os.path.splitext(urlparse(full_url).path)[1].lower()
            if ext in VIDEO_EXTENSIONS:
                _add(full_url, _find_nearby_thumbnail(tag, base_url))

    results.sort(key=lambda d: d["url"])
    return results


def find_embedded_video_pages(soup: BeautifulSoup, base_url: str) -> list[str]:
    embedded = set()
    for tag in soup.find_all(["iframe", "embed"]):
        src = tag.get("src")
        if src:
            full_url = urljoin(base_url, src)
            if any(
                domain in full_url
                for domain in (
                    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
                    "twitch.tv", "streamable.com", "wistia.com",
                )
            ):
                embedded.add(full_url)
    return sorted(embedded)


# ---------------------------------------------------------------------------
# Download workers
# ---------------------------------------------------------------------------

def download_file(url: str, dest_path: str, session: requests.Session,
                  job_id: str, label: str) -> tuple[bool, int]:
    """Download a single file, pushing progress events. Returns (success, bytes)."""
    try:
        response = session.get(url, stream=True, timeout=60, headers=HEADERS)
        response.raise_for_status()
        total_size = int(response.headers.get("content-length", 0))
        downloaded = 0

        push_event(job_id, "file_start", {
            "filename": label,
            "total_bytes": total_size,
        })

        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        push_event(job_id, "file_progress", {
                            "filename": label,
                            "downloaded": downloaded,
                            "total_bytes": total_size,
                        })

        push_event(job_id, "file_done", {
            "filename": label,
            "bytes": downloaded,
            "success": True,
        })
        return True, downloaded

    except Exception as e:
        push_event(job_id, "file_done", {
            "filename": label,
            "success": False,
            "error": str(e),
        })
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False, 0


def download_with_ytdlp(url: str, output_dir: str, job_id: str) -> bool:
    if not YT_DLP_AVAILABLE:
        push_event(job_id, "log", {"message": "yt-dlp not available, skipping video"})
        return False

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            push_event(job_id, "ytdlp_progress", {
                "url": url[:80],
                "downloaded": downloaded,
                "total_bytes": total,
                "speed": speed,
                "eta": eta,
            })
        elif d["status"] == "finished":
            push_event(job_id, "log", {
                "message": f"Merging formats for: {d.get('filename', 'video')}",
            })

    ydl_opts = {
        # Prefer H.264 + AAC (QuickTime/macOS compatible), fall back to best available
        "format": (
            "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "bestvideo[vcodec^=avc1]+bestaudio/"
            "bestvideo+bestaudio/best"
        ),
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        # Re-encode to H.264/AAC if the source codec isn't compatible
        "postprocessor_args": {
            "merger": ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart"],
        },
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return True
    except Exception as e:
        push_event(job_id, "log", {"message": f"yt-dlp error: {e}"})
        return False


def run_download_job(job_id: str, url: str, mode: str, selected_urls: list[str] | None = None, folder_name: str = ""):
    """Main download worker that runs in a background thread."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        if folder_name:
            dir_name = sanitize_filename(folder_name)
        else:
            dir_name = f"{sanitize_filename(domain)}_{timestamp}"
        output_dir = DOWNLOADS_DIR / dir_name
        images_dir = output_dir / "images"
        videos_dir = output_dir / "videos"

        with jobs_lock:
            jobs[job_id]["output_dir"] = str(output_dir)

        session = requests.Session()
        session.headers.update(HEADERS)

        # If selected_urls is provided (from preview), skip the scan — we already
        # know what to download. Otherwise do a full scan.
        if selected_urls is not None:
            push_event(job_id, "status", {"step": "Preparing selected files...", "phase": "scan"})
            selection = set(selected_urls)
            # Classify URLs by type based on extension
            image_urls = [u for u in selected_urls if os.path.splitext(urlparse(u).path)[1].lower() in IMAGE_EXTENSIONS]
            direct_vid = [u for u in selected_urls if os.path.splitext(urlparse(u).path)[1].lower() in VIDEO_EXTENSIONS]
            # Everything else is assumed to be a yt-dlp / embedded video
            other_urls = [u for u in selected_urls if u not in set(image_urls) and u not in set(direct_vid)]

            video_urls = direct_vid
            embedded_urls = []
            ytdlp_urls = other_urls
        else:
            push_event(job_id, "status", {"step": "Fetching page with browser...", "phase": "fetch"})

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    viewport={"width": 1920, "height": 1080},
                )
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=60000)
                _dismiss_age_gate(page)
                _scroll_to_load_all(page)
                html_content = page.content()
                browser.close()

            soup = BeautifulSoup(html_content, "html.parser")
            push_event(job_id, "status", {"step": "Scanning for media...", "phase": "scan"})

            want_images = mode in ("all", "images")
            want_videos = mode in ("all", "videos")

            image_urls = find_image_urls(soup, url) if want_images else []
            video_urls_raw = find_video_urls(soup, url) if want_videos else []
            video_urls = [v["url"] for v in video_urls_raw]
            embedded_urls = find_embedded_video_pages(soup, url) if want_videos else []
            ytdlp_urls = []

            if want_videos and YT_DLP_AVAILABLE:
                ytdlp_info = _extract_ytdlp_info(url)
                ytdlp_urls = [v["url"] for v in ytdlp_info]

        total_media = len(image_urls) + len(video_urls) + len(embedded_urls) + len(ytdlp_urls)
        push_event(job_id, "scan_result", {
            "images": len(image_urls),
            "direct_videos": len(video_urls),
            "embedded_videos": len(embedded_urls),
            "ytdlp_videos": len(ytdlp_urls),
            "total": total_media,
        })

        if total_media == 0:
            push_event(job_id, "complete", {
                "images_downloaded": 0,
                "videos_downloaded": 0,
                "skipped": 0,
                "total_time": 0,
                "output_dir": str(output_dir),
            })
            return

        if image_urls:
            images_dir.mkdir(parents=True, exist_ok=True)
        if video_urls or embedded_urls or ytdlp_urls:
            videos_dir.mkdir(parents=True, exist_ok=True)

        overall_start = time.time()
        downloaded_images = 0
        downloaded_videos = 0
        skipped = 0
        total_bytes = 0

        # Download images
        if image_urls:
            push_event(job_id, "status", {
                "step": f"Downloading {len(image_urls)} images...",
                "phase": "images",
            })
            for i, img_url in enumerate(image_urls, 1):
                filename = get_filename_from_url(img_url)
                dest = str(images_dir / filename)
                if os.path.exists(dest):
                    stem, ext = os.path.splitext(filename)
                    dest = str(images_dir / f"{stem}_{i}{ext}")

                success, nbytes = download_file(img_url, dest, session, job_id, filename)
                if success:
                    downloaded_images += 1
                    total_bytes += nbytes
                else:
                    skipped += 1

                elapsed = time.time() - overall_start
                items_done = downloaded_images + skipped
                remaining_items = len(image_urls) - i
                avg_time = elapsed / items_done if items_done else 0
                eta = avg_time * remaining_items

                push_event(job_id, "overall_progress", {
                    "current": i,
                    "total_images": len(image_urls),
                    "downloaded_images": downloaded_images,
                    "downloaded_videos": downloaded_videos,
                    "skipped": skipped,
                    "elapsed": round(elapsed, 1),
                    "eta": round(eta, 1),
                    "total_bytes": total_bytes,
                    "phase": "images",
                })

        # Download direct videos
        if video_urls:
            push_event(job_id, "status", {
                "step": f"Downloading {len(video_urls)} direct videos...",
                "phase": "videos",
            })
            for i, vid_url in enumerate(video_urls, 1):
                filename = get_filename_from_url(vid_url)
                dest = str(videos_dir / filename)
                if os.path.exists(dest):
                    stem, ext = os.path.splitext(filename)
                    dest = str(videos_dir / f"{stem}_{i}{ext}")

                success, nbytes = download_file(vid_url, dest, session, job_id, filename)
                if success:
                    downloaded_videos += 1
                    total_bytes += nbytes
                else:
                    skipped += 1

        # Embedded videos
        for i, emb_url in enumerate(embedded_urls, 1):
            push_event(job_id, "status", {
                "step": f"Downloading embedded video {i}/{len(embedded_urls)}...",
                "phase": "videos",
            })
            if download_with_ytdlp(emb_url, str(videos_dir), job_id):
                downloaded_videos += 1
            else:
                skipped += 1

        # yt-dlp videos
        if ytdlp_urls:
            push_event(job_id, "status", {
                "step": f"Downloading {len(ytdlp_urls)} video(s) via yt-dlp...",
                "phase": "videos",
            })
            for yt_url in ytdlp_urls:
                if download_with_ytdlp(yt_url, str(videos_dir), job_id):
                    downloaded_videos += 1
                else:
                    skipped += 1

        total_elapsed = time.time() - overall_start

        push_event(job_id, "complete", {
            "images_downloaded": downloaded_images,
            "videos_downloaded": downloaded_videos,
            "skipped": skipped,
            "total_time": round(total_elapsed, 1),
            "total_bytes": total_bytes,
            "output_dir": str(output_dir),
        })

    except Exception as e:
        push_event(job_id, "error", {"message": str(e)})


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


def _infer_video_thumbnail(video_url: str) -> str:
    """Try to construct a thumbnail URL from a video URL using known CDN patterns."""
    url_lower = video_url.lower()

    # Pixabay: replace _tiny.mp4 / _small.mp4 / _large.mp4 etc. with _large.jpg
    if "cdn.pixabay.com/video/" in url_lower:
        thumb = re.sub(r'_(tiny|small|medium|large)\.\w+$', '_large.jpg', video_url)
        if thumb != video_url:
            return thumb

    # Pexels: similar pattern
    if "videos.pexels.com/" in url_lower:
        thumb = re.sub(r'\.\w+$', '.jpg', video_url)
        if thumb != video_url:
            return thumb

    # Generic: try replacing .mp4/.webm with .jpg
    for ext in (".mp4", ".webm", ".m4v", ".mov"):
        if url_lower.endswith(ext):
            candidate = video_url[: -len(ext)] + ".jpg"
            return candidate

    return ""


def _extract_ytdlp_info(url: str) -> list[dict]:
    """Use yt-dlp to extract video metadata (title, thumbnail, URL) without downloading."""
    results = []
    if not YT_DLP_AVAILABLE:
        return results

    ydl_opts = {
        "format": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=avc1]+bestaudio/bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return results

            def _pick_thumbnail(entry):
                if entry.get("thumbnail"):
                    return entry["thumbnail"]
                thumbs = entry.get("thumbnails") or []
                return thumbs[-1]["url"] if thumbs else ""

            if "entries" in info:
                for entry in info["entries"]:
                    if not entry:
                        continue
                    results.append({
                        "url": entry.get("webpage_url") or entry.get("url", ""),
                        "title": entry.get("title", "Unknown video"),
                        "thumbnail": _pick_thumbnail(entry),
                        "duration": entry.get("duration"),
                    })
            else:
                results.append({
                    "url": info.get("webpage_url") or info.get("url") or url,
                    "title": info.get("title", "Video"),
                    "thumbnail": _pick_thumbnail(info),
                    "duration": info.get("duration"),
                })
    except Exception:
        pass
    return results


def _dismiss_age_gate(page):
    """Detect and dismiss common age-verification / cookie / NSFW gates."""
    try:
        gate = page.evaluate("""
            () => {
                // Common age-gate selectors
                const sels = [
                    '.action.ok',                       // leakgallery
                    '.age-gate-yes', '.age-verify-yes',
                    '[class*="age-gate"] button:first-of-type',
                    '[class*="verification"] .ok',
                    '[class*="enter-site"]',
                ];
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el) { el.click(); return 'clicked:' + sel; }
                }
                // Generic: look for buttons with enter/agree/18+ text
                const keywords = ['enter', 'agree', 'i am 18', 'i am over',
                                  '进入', '确认', '同意', 'je suis majeur',
                                  'ich bin 18', 'tengo 18', '18+'];
                for (const el of document.querySelectorAll('button, div[class*="action"], a')) {
                    const txt = (el.textContent || '').toLowerCase().trim();
                    if (keywords.some(kw => txt.includes(kw))) {
                        el.click();
                        return 'clicked-text:' + txt.substring(0, 50);
                    }
                }
                return 'none';
            }
        """)
        if gate and gate.startswith("clicked"):
            page.wait_for_timeout(3000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
    except Exception:
        pass


def _find_scroll_container(page) -> str:
    """Find the actual scrollable element — some SPAs scroll inside
    <main> or another container instead of the body/window."""
    container = page.evaluate("""
        () => {
            // Check if body actually scrolls
            if (document.body.scrollHeight > window.innerHeight + 100) {
                return 'window';
            }
            // Look for inner scrollable containers
            const candidates = ['main', '[role="main"]', '.content', '#content',
                                '.main-content', '#main-content', '.page-content'];
            for (const sel of candidates) {
                const el = document.querySelector(sel);
                if (el && el.scrollHeight > el.clientHeight + 100) {
                    const style = getComputedStyle(el);
                    if (style.overflowY === 'auto' || style.overflowY === 'scroll' ||
                        style.overflow === 'auto' || style.overflow === 'scroll' ||
                        style.overflow === 'auto scroll' || style.overflow === 'hidden auto') {
                        return sel;
                    }
                }
            }
            // Brute-force: find the tallest scrollable element
            let best = null, bestH = 0;
            for (const el of document.querySelectorAll('*')) {
                if (el.scrollHeight > el.clientHeight + 200 && el.clientHeight > 200) {
                    const style = getComputedStyle(el);
                    if (style.overflowY === 'auto' || style.overflowY === 'scroll' ||
                        style.overflow.includes('auto') || style.overflow.includes('scroll')) {
                        if (el.scrollHeight > bestH) {
                            bestH = el.scrollHeight;
                            // Build a selector for this element
                            if (el.id) { best = '#' + el.id; }
                            else if (el.tagName) { best = el.tagName.toLowerCase(); }
                        }
                    }
                }
            }
            return best || 'window';
        }
    """)
    return container or "window"


def _scroll_to_load_all(page, max_rounds: int = 80):
    """Scroll the page (or inner container) incrementally to trigger lazy loaders."""
    container_sel = _find_scroll_container(page)
    is_window = container_sel == "window"

    def _scroll_js(y):
        if is_window:
            return f"window.scrollTo(0, {y})"
        return f"document.querySelector('{container_sel}').scrollTo(0, {y})"

    def _get_heights():
        if is_window:
            return page.evaluate("[document.body.scrollHeight, window.innerHeight]")
        return page.evaluate(f"""
            (() => {{
                const el = document.querySelector('{container_sel}');
                return [el.scrollHeight, el.clientHeight];
            }})()
        """)

    prev_count = 0
    stable_rounds = 0

    for _ in range(max_rounds):
        scroll_height, viewport_h = _get_heights()
        step = int(viewport_h * 0.7)

        # Scroll down incrementally
        y = 0
        while y < scroll_height:
            page.evaluate(_scroll_js(y))
            page.wait_for_timeout(500)
            y += step

        # Sit at the bottom
        page.evaluate(_scroll_js(scroll_height))
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            page.wait_for_timeout(2000)

        # Click any "Load More" buttons
        page.evaluate("""
            () => {
                const sels = [
                    'button', '[class*="load-more"]', '[class*="loadmore"]',
                    '[class*="show-more"]', '[class*="showmore"]',
                ];
                for (const el of document.querySelectorAll(sels.join(','))) {
                    const txt = (el.textContent || '').toLowerCase().trim();
                    if (txt.includes('load more') || txt.includes('show more') ||
                        txt.includes('view more') || txt.includes('see more') ||
                        txt === 'more' || txt === 'next') {
                        try { el.click(); } catch(e) {}
                    }
                }
            }
        """)
        page.wait_for_timeout(1000)

        # Check if new content appeared
        cur_count = page.evaluate("document.querySelectorAll('img, video, source').length")
        if cur_count == prev_count:
            stable_rounds += 1
            if stable_rounds >= 3:
                break
        else:
            stable_rounds = 0
        prev_count = cur_count

    # Force-load remaining lazy images
    page.evaluate("""
        () => {
            document.querySelectorAll('img[loading="lazy"]').forEach(img => {
                img.loading = 'eager';
            });
            document.querySelectorAll('img').forEach(img => {
                if (!img.src || img.src === '' || img.src.startsWith('data:')) {
                    const lazy = img.dataset.src || img.dataset.lazySrc ||
                                 img.dataset.original || img.dataset.lazysrc || '';
                    if (lazy) img.src = lazy;
                }
            });
        }
    """)
    page.wait_for_timeout(2000)

    # Scroll back to top
    page.evaluate(_scroll_js(0))
    page.wait_for_timeout(500)


def _deep_scan(url: str, mode: str, parsed) -> Response:
    """Deep scan: intercept ALL network-loaded media (images/videos) while
    scrolling the page, plus extract sub-page videos via yt-dlp."""
    media_items = []
    seen_urls = set()

    want_images = mode in ("all", "images")
    want_videos = mode in ("all", "videos")

    # Collect media URLs from actual network traffic
    network_images = set()
    network_videos = set()

    # Junk URL patterns to skip (icons, trackers, favicons, etc.)
    _JUNK_PATTERNS = ("favicon", "1x1", "pixel", "spacer", "blank.gif",
                      "data:image", "/analytics", "/tracking", "/beacon")

    def _on_response(response):
        try:
            resp_url = response.url
            if resp_url.startswith("data:"):
                return
            lower_url = resp_url.lower()

            content_type = response.headers.get("content-type", "")

            if "image/" in content_type:
                # Skip known junk by URL pattern
                if any(p in lower_url for p in _JUNK_PATTERNS):
                    return
                # Skip SVG (usually icons/logos)
                if "svg" in content_type:
                    return
                network_images.add(resp_url)
            elif "video/" in content_type:
                network_videos.add(resp_url)
        except Exception:
            pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = context.new_page()

        # Listen to ALL network responses to capture media URLs
        page.on("response", _on_response)

        page.goto(url, wait_until="networkidle", timeout=60000)
        _dismiss_age_gate(page)
        _scroll_to_load_all(page)

        # ---- Also collect from DOM for anything missed by network ----
        dom_images = page.evaluate("""
            () => {
                const results = [];
                const seen = new Set();
                document.querySelectorAll('img').forEach(img => {
                    const src = img.currentSrc || img.src ||
                                img.dataset.src || img.dataset.lazySrc ||
                                img.dataset.original || img.dataset.lazysrc ||
                                img.dataset.lazyload || '';
                    if (!src || src.startsWith('data:') || seen.has(src)) return;
                    seen.add(src);
                    results.push(src);
                });
                // srcset
                document.querySelectorAll('img[srcset], img[data-srcset], source[srcset]').forEach(el => {
                    const srcset = el.srcset || el.dataset.srcset || '';
                    for (const part of srcset.split(',')) {
                        const u = part.trim().split(' ')[0];
                        if (u && !u.startsWith('data:') && !seen.has(u)) {
                            seen.add(u);
                            results.push(u);
                        }
                    }
                });
                return results;
            }
        """)

        dom_videos = page.evaluate("""
            () => {
                const results = [];
                const seen = new Set();
                document.querySelectorAll('video').forEach(video => {
                    if (video.src && !seen.has(video.src)) {
                        seen.add(video.src);
                        results.push(video.src);
                    }
                    video.querySelectorAll('source').forEach(s => {
                        if (s.src && !seen.has(s.src)) {
                            seen.add(s.src);
                            results.push(s.src);
                        }
                    });
                });
                const videoExts = ['.mp4','.webm','.avi','.mov','.mkv','.flv','.m4v','.ogv'];
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.href;
                    const ext = '.' + href.split('?')[0].split('.').pop().toLowerCase();
                    if (videoExts.includes(ext) && !seen.has(href)) {
                        seen.add(href);
                        results.push(href);
                    }
                });
                return results;
            }
        """)

        # ---- Find content links for sub-page video extraction ----
        content_links = []
        if want_videos:
            content_links = page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();
                    const currentPath = location.pathname;

                    const navPatterns = [
                        '/login', '/register', '/signup', '/signin', '/auth',
                        '/search', '/discover', '/explore', '/trending',
                        '/about', '/contact', '/faq', '/help', '/terms',
                        '/privacy', '/welcome', '/settings', '/profile',
                        '/cart', '/checkout', '/pricing', '/comments',
                        '/random', '/most-liked', '/ranking', '/daily',
                    ];

                    function isNavLink(path) {
                        const p = path.toLowerCase();
                        return navPatterns.some(nav => p === nav || p.endsWith(nav));
                    }

                    function findThumb(el) {
                        const imgs = el.querySelectorAll('img');
                        for (const img of imgs) {
                            const src = img.currentSrc || img.src ||
                                        img.dataset.src || img.dataset.lazySrc ||
                                        img.dataset.original || '';
                            if (src && !src.startsWith('data:')) return src;
                        }
                        const checkBg = (el) => {
                            try {
                                const bg = getComputedStyle(el).backgroundImage;
                                if (bg && bg !== 'none') {
                                    const m = bg.match(/url\\(["']?(.*?)["']?\\)/);
                                    if (m && m[1]) return m[1];
                                }
                            } catch(e) {}
                            return '';
                        };
                        let bg = checkBg(el);
                        if (bg) return bg;
                        for (const child of el.querySelectorAll('*')) {
                            bg = checkBg(child);
                            if (bg) return bg;
                        }
                        return '';
                    }

                    function isInNavArea(el) {
                        let current = el;
                        while (current && current !== document.body) {
                            const tag = current.tagName.toLowerCase();
                            if (tag === 'nav' || tag === 'header' || tag === 'footer') return true;
                            const role = (current.getAttribute('role') || '').toLowerCase();
                            if (role === 'navigation' || role === 'banner' || role === 'contentinfo') return true;
                            const cls = (current.className || '').toLowerCase();
                            if (cls.includes('navbar') || cls.includes('header') || cls.includes('footer') ||
                                cls.includes('sidebar') || cls.includes('menu-')) return true;
                            current = current.parentElement;
                        }
                        return false;
                    }

                    document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.href;
                        if (!href || seen.has(href)) return;
                        let linkUrl;
                        try { linkUrl = new URL(href); } catch(e) { return; }

                        const baseHost = location.hostname;
                        if (linkUrl.hostname !== baseHost &&
                            !linkUrl.hostname.endsWith('.' + baseHost)) return;
                        if (href.startsWith('javascript:')) return;
                        if (linkUrl.pathname === currentPath) return;
                        if (isNavLink(linkUrl.pathname)) return;
                        if (isInNavArea(a)) return;

                        const rect = a.getBoundingClientRect();
                        if (rect.width < 40 || rect.height < 40) return;

                        const hasImg = a.querySelector('img') !== null;
                        const hasVideo = a.querySelector('video') !== null;
                        const hasPlayIcon = a.querySelector('[class*=play], [class*=Play]') !== null;
                        const isBigTile = rect.width > 80 && rect.height > 80;

                        if (hasImg || hasVideo || hasPlayIcon || isBigTile) {
                            seen.add(href);
                            results.push({
                                url: href,
                                thumbnail: findThumb(a),
                                text: (a.textContent || '').trim().substring(0, 100)
                            });
                        }
                    });

                    return results;
                }
            """)

        browser.close()

    # ---- Merge network-captured + DOM images (network first, more reliable) ----
    all_image_urls = list(dict.fromkeys(list(network_images) + dom_images))  # dedup, preserve order
    all_video_urls = list(dict.fromkeys(list(network_videos) + dom_videos))

    # ---- Build results: images ----
    if want_images:
        for img_url in all_image_urls:
            if img_url in seen_urls:
                continue
            # Skip obvious junk
            lower = img_url.lower()
            if any(s in lower for s in ("1x1", "pixel", "spacer", "blank.gif",
                                         "favicon", "data:image")):
                continue
            seen_urls.add(img_url)
            media_items.append({
                "type": "image",
                "url": img_url,
                "filename": get_filename_from_url(img_url),
                "preview_url": img_url,
            })

    # ---- Build results: direct videos from page ----
    if want_videos:
        for vid_url in all_video_urls:
            if vid_url in seen_urls:
                continue
            seen_urls.add(vid_url)
            thumb = _infer_video_thumbnail(vid_url)
            media_items.append({
                "type": "video",
                "url": vid_url,
                "filename": get_filename_from_url(vid_url),
                "preview_url": thumb,
            })

    # ---- Build results: sub-page videos via yt-dlp ----
    if want_videos and content_links:
        for link in content_links:
            page_url = link["url"]
            if page_url in seen_urls:
                continue
            page_thumb = link.get("thumbnail", "")

            yt_info = _extract_ytdlp_info(page_url)
            if yt_info:
                for vid in yt_info:
                    vid_key = vid.get("url", page_url)
                    if vid_key in seen_urls:
                        continue
                    seen_urls.add(vid_key)
                    thumb = vid.get("thumbnail") or page_thumb
                    duration = vid.get("duration")
                    dur_str = f" ({int(duration)}s)" if duration else ""
                    media_items.append({
                        "type": "ytdlp_video",
                        "url": page_url,
                        "filename": sanitize_filename(vid["title"]) + dur_str,
                        "preview_url": thumb,
                    })
            elif page_thumb:
                seen_urls.add(page_url)
                label = link.get("text", "") or get_filename_from_url(page_url)
                media_items.append({
                    "type": "ytdlp_video",
                    "url": page_url,
                    "filename": sanitize_filename(label) if label.strip() else get_filename_from_url(page_url),
                    "preview_url": page_thumb,
                })

    # ---- Also check yt-dlp on the main page ----
    if want_videos and YT_DLP_AVAILABLE:
        ytdlp_main = _extract_ytdlp_info(url)
        for yt_vid in ytdlp_main:
            vid_url = yt_vid.get("url", "")
            if vid_url in seen_urls:
                continue
            seen_urls.add(vid_url)
            media_items.append({
                "type": "ytdlp_video",
                "url": vid_url,
                "filename": sanitize_filename(yt_vid["title"]),
                "preview_url": yt_vid.get("thumbnail", ""),
            })

    return jsonify({"items": media_items, "scanned_url": url})


@app.route("/api/scan", methods=["POST"])
def scan_page():
    """Scan a URL and return lists of found media with preview URLs (no download yet)."""
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    mode = (data or {}).get("mode", "all")
    deep_scan = (data or {}).get("deep_scan", False)

    if not url:
        return jsonify({"error": "URL is required"}), 400

    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    if not parsed.netloc:
        return jsonify({"error": "Invalid URL"}), 400

    try:
        media_items = []

        # ----- Deep Scan: click into video links and extract via yt-dlp -----
        if deep_scan:
            return _deep_scan(url, mode, parsed)

        # ----- Social media URLs: redirect user to the Social Media tab -----
        if is_ytdlp_direct_url(url):
            host = parsed.netloc.lower()
            if "instagram" in host:
                platform = "Instagram"
            elif "tiktok" in host:
                platform = "TikTok"
            elif "facebook" in host or "fb.watch" in host:
                platform = "Facebook"
            elif "youtube" in host or "youtu.be" in host:
                platform = "YouTube"
            elif "twitter" in host or "x.com" in host:
                platform = "X/Twitter"
            else:
                platform = "this platform"
            return jsonify({
                "error": f"Use the Social Media tab to download from {platform}. "
                         f"Switch to the Social Media tab, paste your URL there, and click Download."
            }), 400

        # ----- Standard path: Playwright + in-browser JS extraction -----
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=60000)
            _dismiss_age_gate(page)
            _scroll_to_load_all(page)

            # Extract video+thumbnail pairs using JS in the live page
            browser_videos = page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();

                    // Helper: walk up the DOM to find the closest <img> thumbnail
                    function findThumbnail(el) {
                        let current = el;
                        for (let i = 0; i < 8; i++) {
                            const parent = current.parentElement;
                            if (!parent || parent === document.body) break;

                            // Look for <img> in this container
                            const imgs = parent.querySelectorAll('img');
                            for (const img of imgs) {
                                const src = img.currentSrc || img.src || img.dataset.src || img.dataset.lazySrc || '';
                                if (src && !src.startsWith('data:') && img.naturalWidth > 30) {
                                    return src;
                                }
                            }

                            // Check for background-image on parent or siblings
                            const children = parent.children;
                            for (const child of children) {
                                const bg = getComputedStyle(child).backgroundImage;
                                if (bg && bg !== 'none') {
                                    const match = bg.match(/url\\(["']?(.*?)["']?\\)/);
                                    if (match && match[1]) return match[1];
                                }
                            }

                            current = parent;
                        }
                        return '';
                    }

                    // Find all <video> elements and their sources
                    document.querySelectorAll('video').forEach(video => {
                        const poster = video.poster || '';
                        const sources = [];
                        if (video.src) sources.push(video.src);
                        video.querySelectorAll('source').forEach(s => {
                            if (s.src) sources.push(s.src);
                        });

                        const thumb = poster || findThumbnail(video);

                        sources.forEach(src => {
                            if (!seen.has(src)) {
                                seen.add(src);
                                results.push({ url: src, thumbnail: thumb, type: 'video' });
                            }
                        });
                    });

                    // Find <a> tags linking to video files
                    const videoExts = ['.mp4','.webm','.avi','.mov','.mkv','.flv','.m4v','.ogv'];
                    document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.href;
                        const ext = href.split('?')[0].split('.').pop().toLowerCase();
                        if (videoExts.includes('.' + ext) && !seen.has(href)) {
                            seen.add(href);
                            results.push({ url: href, thumbnail: findThumbnail(a), type: 'video' });
                        }
                    });

                    return results;
                }
            """)

            html_content = page.content()
            browser.close()

        soup = BeautifulSoup(html_content, "html.parser")
        want_images = mode in ("all", "images")
        want_videos = mode in ("all", "videos")

        image_urls = find_image_urls(soup, url) if want_images else []
        embedded_urls = find_embedded_video_pages(soup, url) if want_videos else []

        ytdlp_videos = []
        if want_videos and YT_DLP_AVAILABLE:
            ytdlp_videos = _extract_ytdlp_info(url)

        for img_url in image_urls:
            media_items.append({
                "type": "image",
                "url": img_url,
                "filename": get_filename_from_url(img_url),
                "preview_url": img_url,
            })
        if want_videos:
            for vid in browser_videos:
                thumb = vid.get("thumbnail", "") or _infer_video_thumbnail(vid["url"])
                media_items.append({
                    "type": "video",
                    "url": vid["url"],
                    "filename": get_filename_from_url(vid["url"]),
                    "preview_url": thumb,
                })
        for emb_url in embedded_urls:
            media_items.append({
                "type": "embedded_video",
                "url": emb_url,
                "filename": emb_url.split("/")[-1][:60],
                "preview_url": "",
            })
        for yt_vid in ytdlp_videos:
            media_items.append({
                "type": "ytdlp_video",
                "url": yt_vid["url"],
                "filename": sanitize_filename(yt_vid["title"]),
                "preview_url": yt_vid.get("thumbnail", ""),
            })

        return jsonify({"items": media_items, "scanned_url": url})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


SOCIAL_DOMAINS = {
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "fb.watch", "web.facebook.com",
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
}


def run_social_download_job(job_id: str, url: str, folder_name: str):
    """Download video/media from a social media URL using yt-dlp with max quality."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        timestamp = time.strftime("%Y%m%d_%H%M%S")

        if folder_name:
            dir_name = sanitize_filename(folder_name)
        else:
            domain = host.replace("www.", "")
            dir_name = f"{sanitize_filename(domain)}_{timestamp}"

        output_dir = DOWNLOADS_DIR / dir_name
        videos_dir = output_dir / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)

        with jobs_lock:
            jobs[job_id]["output_dir"] = str(output_dir)

        push_event(job_id, "status", {"step": "Extracting video info...", "phase": "fetch"})

        if not YT_DLP_AVAILABLE:
            push_event(job_id, "error", {"message": "yt-dlp is not installed."})
            return

        def progress_hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                speed = d.get("speed") or 0
                eta = d.get("eta") or 0
                push_event(job_id, "ytdlp_progress", {
                    "url": url[:80],
                    "downloaded": downloaded,
                    "total_bytes": total,
                    "speed": speed,
                    "eta": eta,
                })
            elif d["status"] == "finished":
                push_event(job_id, "log", {
                    "message": f"Downloaded: {d.get('filename', 'video')}",
                })

        def postprocessor_hook(d):
            if d["status"] == "started":
                push_event(job_id, "status", {
                    "step": "Re-encoding for macOS compatibility (this may take a moment)...",
                    "phase": "videos",
                })
            elif d["status"] == "finished":
                push_event(job_id, "log", {"message": "Re-encoding complete"})

        ydl_opts = {
            "format": (
                "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
                "bestvideo[vcodec^=avc1]+bestaudio/"
                "bestvideo+bestaudio/best"
            ),
            "merge_output_format": "mp4",
            "postprocessor_args": {
                "merger": ["-c:v", "libx264", "-preset", "veryfast",
                           "-c:a", "aac", "-movflags", "+faststart"],
            },
            "outtmpl": os.path.join(str(videos_dir), "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
        }

        push_event(job_id, "status", {"step": "Downloading at best quality...", "phase": "videos"})

        overall_start = time.time()
        downloaded_videos = 0
        skipped = 0

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    if "entries" in info:
                        downloaded_videos = len([e for e in info["entries"] if e])
                    else:
                        downloaded_videos = 1
        except Exception as e:
            push_event(job_id, "log", {"message": f"yt-dlp error: {e}"})
            skipped = 1

        total_elapsed = time.time() - overall_start

        push_event(job_id, "complete", {
            "images_downloaded": 0,
            "videos_downloaded": downloaded_videos,
            "skipped": skipped,
            "total_time": round(total_elapsed, 1),
            "total_bytes": 0,
            "output_dir": str(output_dir),
        })

    except Exception as e:
        push_event(job_id, "error", {"message": str(e)})


@app.route("/api/social-download", methods=["POST"])
def social_download():
    """Download video from a social media platform using yt-dlp at max quality."""
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    folder_name = (data or {}).get("folder_name", "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400

    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    if not parsed.netloc:
        return jsonify({"error": "Invalid URL"}), 400

    if not YT_DLP_AVAILABLE:
        return jsonify({"error": "yt-dlp is not installed on the server"}), 500

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"events": [], "output_dir": None}

    thread = threading.Thread(
        target=run_social_download_job, args=(job_id, url, folder_name), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    mode = (data or {}).get("mode", "all")  # all | images | videos
    selected_urls = (data or {}).get("selected_urls")  # list of URLs to download, or None for all
    folder_name = (data or {}).get("folder_name", "").strip()  # optional custom folder name

    if not url:
        return jsonify({"error": "URL is required"}), 400

    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    if not parsed.netloc:
        return jsonify({"error": "Invalid URL"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"events": [], "output_dir": None}

    thread = threading.Thread(
        target=run_download_job, args=(job_id, url, mode, selected_urls, folder_name), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def stream_progress(job_id):
    def generate():
        idx = 0
        done = False
        while not done:
            with jobs_lock:
                if job_id not in jobs:
                    yield f"event: error\ndata: {json.dumps({'message': 'Job not found'})}\n\n"
                    return
                events = jobs[job_id]["events"]
                new_events = events[idx:]
                idx = len(events)

            for ev in new_events:
                yield ev
                if "event: complete" in ev or "event: error" in ev:
                    done = True

            if not done:
                time.sleep(0.3)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/download-zip/<job_id>")
def download_zip(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or not job.get("output_dir"):
        return jsonify({"error": "Job not found"}), 404

    output_dir = Path(job["output_dir"])
    if not output_dir.exists():
        return jsonify({"error": "Download directory not found"}), 404

    zip_path = output_dir.parent / f"{output_dir.name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in output_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(output_dir))

    return send_file(str(zip_path), as_attachment=True,
                     download_name=f"{output_dir.name}.zip")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
