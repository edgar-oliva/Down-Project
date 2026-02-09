#!/usr/bin/env python3
"""
Media Downloader - Download images and videos from any webpage URL.
Downloads all found media in the best quality available with progress and ETA.
"""

import os
import re
import sys
import time
import hashlib
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mov", ".mkv", ".flv", ".m4v", ".ogv"}


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """Remove invalid characters from a filename."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    if len(name) > max_length:
        stem, ext = os.path.splitext(name)
        name = stem[: max_length - len(ext)] + ext
    return name or "unnamed"


def get_filename_from_url(url: str) -> str:
    """Extract a filename from a URL."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if not filename or "." not in filename:
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        filename = f"media_{url_hash}"
    return sanitize_filename(filename)


def download_file(url: str, dest_path: str, session: requests.Session) -> bool:
    """Download a file with a progress bar showing ETA."""
    try:
        response = session.get(url, stream=True, timeout=30, headers=HEADERS)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))
        filename = os.path.basename(dest_path)
        display_name = filename[:40] + "..." if len(filename) > 40 else filename

        with open(dest_path, "wb") as f:
            with tqdm(
                total=total_size if total_size > 0 else None,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=f"  {display_name}",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
                ncols=90,
            ) as pbar:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        pbar.update(len(chunk))
        return True
    except Exception as e:
        print(f"  [ERROR] Failed to download {url}: {e}")
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


def find_image_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Extract all image URLs from the page."""
    image_urls = set()

    # <img> tags
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "srcset"):
            val = img.get(attr)
            if val:
                if attr == "srcset":
                    # srcset contains multiple URLs with size descriptors
                    candidates = val.split(",")
                    for candidate in candidates:
                        parts = candidate.strip().split()
                        if parts:
                            image_urls.add(urljoin(base_url, parts[0]))
                else:
                    image_urls.add(urljoin(base_url, val))

    # <picture> / <source> tags
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

    # <a> tags linking directly to images
    for a in soup.find_all("a", href=True):
        href = a["href"]
        ext = os.path.splitext(urlparse(href).path)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            image_urls.add(urljoin(base_url, href))

    # CSS background-image URLs
    for tag in soup.find_all(style=True):
        urls = re.findall(r'url\(["\']?(.*?)["\']?\)', tag["style"])
        for u in urls:
            full_url = urljoin(base_url, u)
            ext = os.path.splitext(urlparse(full_url).path)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                image_urls.add(full_url)

    # Filter out tiny icons / tracking pixels by excluding common patterns
    filtered = set()
    for url in image_urls:
        lower = url.lower()
        if any(skip in lower for skip in ("1x1", "pixel", "spacer", "blank.gif", "data:image")):
            continue
        filtered.add(url)

    return sorted(filtered)


def find_video_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Extract all video URLs from the page."""
    video_urls = set()

    # <video> tags
    for video in soup.find_all("video"):
        src = video.get("src")
        if src:
            video_urls.add(urljoin(base_url, src))
        for source in video.find_all("source"):
            src = source.get("src")
            if src:
                video_urls.add(urljoin(base_url, src))

    # <a> tags linking directly to video files
    for a in soup.find_all("a", href=True):
        href = a["href"]
        ext = os.path.splitext(urlparse(href).path)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            video_urls.add(urljoin(base_url, href))

    # <iframe> / <embed> tags (potential embedded videos)
    for tag in soup.find_all(["iframe", "embed"]):
        src = tag.get("src")
        if src:
            full_url = urljoin(base_url, src)
            ext = os.path.splitext(urlparse(full_url).path)[1].lower()
            if ext in VIDEO_EXTENSIONS:
                video_urls.add(full_url)

    return sorted(video_urls)


def find_embedded_video_pages(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Find embedded video player URLs (YouTube, Vimeo, etc.) for yt-dlp."""
    embedded = set()

    for tag in soup.find_all(["iframe", "embed"]):
        src = tag.get("src")
        if src:
            full_url = urljoin(base_url, src)
            # Check if it looks like a known video platform embed
            if any(
                domain in full_url
                for domain in (
                    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
                    "twitch.tv", "streamable.com", "wistia.com",
                )
            ):
                embedded.add(full_url)

    return sorted(embedded)


def download_with_ytdlp(url: str, output_dir: str) -> bool:
    """Download a video using yt-dlp for best quality with progress."""
    if not YT_DLP_AVAILABLE:
        print("  [SKIP] yt-dlp not installed. Install with: pip install yt-dlp")
        return False

    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "progress_hooks": [],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return True
    except Exception as e:
        print(f"  [ERROR] yt-dlp failed for {url}: {e}")
        return False


def try_ytdlp_page(page_url: str, output_dir: str) -> int:
    """Try to use yt-dlp to extract and download videos from the entire page."""
    if not YT_DLP_AVAILABLE:
        return 0

    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
            if info and "entries" in info:
                return len(info["entries"])
            elif info and info.get("url"):
                return 1
    except Exception:
        pass
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Download all images and videos from a webpage URL."
    )
    parser.add_argument("url", help="The webpage URL to download media from")
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output folder name (default: auto-generated from domain)"
    )
    parser.add_argument(
        "--images-only", action="store_true", help="Download only images"
    )
    parser.add_argument(
        "--videos-only", action="store_true", help="Download only videos"
    )
    parser.add_argument(
        "--skip-ytdlp", action="store_true",
        help="Skip yt-dlp extraction (only download direct video links)"
    )

    args = parser.parse_args()
    url = args.url

    # Validate URL
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    if not parsed.netloc:
        print("[ERROR] Invalid URL provided.")
        sys.exit(1)

    # Create output directory
    base_dir = Path(__file__).parent / "downloads"
    if args.output:
        output_dir = base_dir / sanitize_filename(args.output)
    else:
        domain = parsed.netloc.replace("www.", "")
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_dir = base_dir / f"{sanitize_filename(domain)}_{timestamp}"

    images_dir = output_dir / "images"
    videos_dir = output_dir / "videos"

    print(f"\n{'='*60}")
    print(f"  Media Downloader")
    print(f"{'='*60}")
    print(f"  URL:    {url}")
    print(f"  Output: {output_dir}")
    print(f"{'='*60}\n")

    # Fetch the page
    print("[1/4] Fetching page...")
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Could not fetch the page: {e}")
        sys.exit(1)

    soup = BeautifulSoup(response.text, "html.parser")
    print(f"  Page loaded successfully ({len(response.content)} bytes)\n")

    # Discover media
    print("[2/4] Scanning for media...")
    image_urls = [] if args.videos_only else find_image_urls(soup, url)
    video_urls = [] if args.images_only else find_video_urls(soup, url)
    embedded_urls = [] if args.images_only else find_embedded_video_pages(soup, url)

    # Check if yt-dlp can find additional videos on the page
    ytdlp_count = 0
    if not args.images_only and not args.skip_ytdlp and YT_DLP_AVAILABLE:
        print("  Checking for embedded/streaming videos via yt-dlp...")
        ytdlp_count = try_ytdlp_page(url, str(videos_dir))

    print(f"  Found: {len(image_urls)} images, {len(video_urls)} direct videos, "
          f"{len(embedded_urls)} embedded videos")
    if ytdlp_count > 0:
        print(f"  yt-dlp detected {ytdlp_count} additional downloadable video(s)")

    total_items = len(image_urls) + len(video_urls) + len(embedded_urls)
    if total_items == 0 and ytdlp_count == 0:
        print("\n[INFO] No downloadable media found on this page.")
        sys.exit(0)

    # Create directories
    if image_urls:
        images_dir.mkdir(parents=True, exist_ok=True)
    if video_urls or embedded_urls or ytdlp_count > 0:
        videos_dir.mkdir(parents=True, exist_ok=True)

    overall_start = time.time()
    downloaded_images = 0
    downloaded_videos = 0
    skipped = 0

    # Download images
    if image_urls:
        print(f"\n[3/4] Downloading {len(image_urls)} images...")
        for i, img_url in enumerate(image_urls, 1):
            filename = get_filename_from_url(img_url)
            dest = str(images_dir / filename)

            # Avoid overwriting — append a number if needed
            if os.path.exists(dest):
                stem, ext = os.path.splitext(filename)
                dest = str(images_dir / f"{stem}_{i}{ext}")

            if download_file(img_url, dest, session):
                downloaded_images += 1
            else:
                skipped += 1

            # Show overall ETA
            elapsed = time.time() - overall_start
            items_done = downloaded_images + skipped
            if items_done > 0:
                avg_time = elapsed / items_done
                remaining = avg_time * (len(image_urls) - i)
                print(f"  [{i}/{len(image_urls)}] "
                      f"Estimated time remaining for images: {remaining:.0f}s")
    else:
        print("\n[3/4] No images to download.")

    # Download videos
    if video_urls or embedded_urls or ytdlp_count > 0:
        total_vids = len(video_urls) + len(embedded_urls) + (1 if ytdlp_count > 0 else 0)
        print(f"\n[4/4] Downloading videos ({total_vids} sources)...")

        vid_start = time.time()

        # Direct video files
        for i, vid_url in enumerate(video_urls, 1):
            filename = get_filename_from_url(vid_url)
            dest = str(videos_dir / filename)
            if os.path.exists(dest):
                stem, ext = os.path.splitext(filename)
                dest = str(videos_dir / f"{stem}_{i}{ext}")

            print(f"\n  Direct video [{i}/{len(video_urls)}]: {vid_url[:80]}")
            if download_file(vid_url, dest, session):
                downloaded_videos += 1
            else:
                skipped += 1

        # Embedded videos via yt-dlp
        for i, emb_url in enumerate(embedded_urls, 1):
            print(f"\n  Embedded video [{i}/{len(embedded_urls)}]: {emb_url[:80]}")
            if download_with_ytdlp(emb_url, str(videos_dir)):
                downloaded_videos += 1
            else:
                skipped += 1

        # Full page yt-dlp extraction
        if ytdlp_count > 0 and not args.skip_ytdlp:
            print(f"\n  Downloading {ytdlp_count} video(s) via yt-dlp from page...")
            if download_with_ytdlp(url, str(videos_dir)):
                downloaded_videos += ytdlp_count
            else:
                skipped += 1

        vid_elapsed = time.time() - vid_start
        print(f"\n  Video downloads took {vid_elapsed:.1f}s")
    else:
        print("\n[4/4] No videos to download.")

    # Summary
    total_elapsed = time.time() - overall_start
    print(f"\n{'='*60}")
    print(f"  Download Complete!")
    print(f"{'='*60}")
    print(f"  Images downloaded:  {downloaded_images}")
    print(f"  Videos downloaded:  {downloaded_videos}")
    print(f"  Skipped/failed:    {skipped}")
    print(f"  Total time:        {total_elapsed:.1f}s")
    print(f"  Saved to:          {output_dir}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
