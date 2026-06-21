"""
downloader.py
-------------
Core download engine for the YouTube batch downloader.

This module has NO input()/print()-driven CLI logic in it — it's meant to be
imported by either a CLI script or a GUI (Streamlit) front end. All progress
and status reporting happens through callbacks so any front end can hook in.

Fixes/improvements made vs. the original script:
  - Removed hardcoded test URL and dead/commented-out input loop.
  - URL validation regex is now actually used (validate_url()).
  - mp4 format string has a proper fallback chain instead of a single brittle option.
  - mp3 extraction now sets an explicit bitrate (192kbps) instead of relying on defaults.
  - Progress is reported via a callback instead of only being printed, so a GUI
    can show live per-file and overall progress.
  - max_workers is now a parameter instead of being hardcoded to 1.
  - ffmpeg check also looks for a local ./ffmpeg/bin/ffmpeg(.exe) next to this
    file as a fallback, matching the commented-out behavior in the original.
  - download_video() now returns the actual output filepath (when determinable)
    so a GUI can offer a direct download link.
  - Basic duplicate-URL removal and per-URL validation before submitting jobs.
"""

import os
import re
import shutil
import sys
import threading
import concurrent.futures
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import yt_dlp
from yt_dlp.utils import DownloadCancelled

YOUTUBE_REGEX = re.compile(
    r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/'
    r'(watch\?v=|embed/|v/|shorts/|.+\?v=)?([^&=%\?]{11})'
)


def validate_url(url: str) -> bool:
    """Return True if the given string looks like a YouTube URL."""
    return bool(YOUTUBE_REGEX.search(url.strip()))


def dedupe_urls(urls: List[str]) -> List[str]:
    """Remove duplicate URLs while preserving order."""
    seen = set()
    result = []
    for u in urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            result.append(u)
    return result


def parse_timecode(value: str) -> Optional[float]:
    """
    Parse a timecode string into seconds. Accepts:
      - plain seconds:        "90"
      - MM:SS:                "1:30"
      - HH:MM:SS:             "01:01:30"
    Returns None for empty/blank input. Raises ValueError for malformed input.
    """
    value = (value or "").strip()
    if not value:
        return None

    parts = value.split(":")
    if len(parts) > 3 or any(p == "" for p in parts):
        raise ValueError(f"Invalid timecode: {value!r}")

    try:
        parts_num = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"Invalid timecode: {value!r}")

    seconds = 0.0
    for p in parts_num:
        seconds = seconds * 60 + p
    return seconds


def format_timecode(seconds: Optional[float]) -> str:
    """Format a seconds value back into HH:MM:SS for display."""
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def find_ffmpeg() -> Optional[str]:
    """
    Locate ffmpeg: first on PATH, then in a local ./ffmpeg/bin folder next to
    this file (covers a bundled-ffmpeg setup on Windows, mirroring the
    commented-out path in the original script).
    Returns the ffmpeg directory to pass to yt-dlp, or None if not found.
    """
    if shutil.which("ffmpeg") is not None:
        return None  # on PATH already; yt-dlp will find it without help

    local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin")
    exe_name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    candidate = os.path.join(local_dir, exe_name)
    if os.path.isfile(candidate):
        return local_dir

    return "__NOT_FOUND__"


def ffmpeg_available() -> bool:
    location = find_ffmpeg()
    return location != "__NOT_FOUND__"


@dataclass
class VideoItem:
    """One video in a batch, with its own optional trim range."""
    url: str
    start_time: Optional[str] = None  # e.g. "1:30" or "00:01:30" or "" / None for no trim
    end_time: Optional[str] = None    # e.g. "2:45"; None / "" means "to the end"

    def trim_seconds(self):
        """Returns (start_seconds, end_seconds), either may be None."""
        start = parse_timecode(self.start_time) if self.start_time else None
        end = parse_timecode(self.end_time) if self.end_time else None
        if start is not None and end is not None and end <= start:
            raise ValueError("End time must be after start time")
        return start, end

    def has_trim(self) -> bool:
        return bool((self.start_time or "").strip() or (self.end_time or "").strip())


@dataclass
class DownloadResult:
    url: str
    success: bool
    filepath: Optional[str] = None
    title: Optional[str] = None
    error: Optional[str] = None
    cancelled: bool = False


@dataclass
class DownloadJob:
    """Represents one batch download request."""
    items: List[VideoItem]
    download_path: str
    format_choice: str  # 'mp3' or 'mp4'
    max_workers: int = 3
    audio_quality: str = "192"  # kbps, used only for mp3
    # Called as progress_callback(url, status, extra_dict)
    # status in {"starting", "downloading", "finished", "error", "cancelled"}
    progress_callback: Optional[Callable[[str, str, dict], None]] = field(default=None, repr=False)
    # url -> threading.Event; set the event to cancel that URL's download mid-flight.
    # If not provided, one is created per item automatically and exposed on the
    # job after construction via job.cancel_events.
    cancel_events: Dict[str, threading.Event] = field(default_factory=dict)

    def __post_init__(self):
        for item in self.items:
            self.cancel_events.setdefault(item.url, threading.Event())

    def cancel(self, url: str):
        """Signal that the download for this URL should stop as soon as possible."""
        ev = self.cancel_events.get(url)
        if ev:
            ev.set()


class _QuietLogger:
    """Swallow yt-dlp's debug/warning noise; surface errors through callback."""

    def __init__(self, url, progress_callback):
        self._url = url
        self._cb = progress_callback

    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        if self._cb:
            self._cb(self._url, "error", {"message": msg})


def _build_options(
    download_path, format_choice, audio_quality, url, progress_callback,
    ffmpeg_location, cancel_event, start_seconds, end_seconds,
):
    def hook(d):
        if cancel_event is not None and cancel_event.is_set():
            # yt-dlp's own DownloadCancelled, raised from inside a progress_hook,
            # is the documented/supported way to abort an in-progress download.
            raise DownloadCancelled("Cancelled by user")

        if d.get('status') == 'downloading' and progress_callback:
            percent_str = d.get('_percent_str', '').strip()
            progress_callback(url, "downloading", {
                "percent_str": percent_str,
                "speed": d.get('_speed_str', '').strip(),
                "eta": d.get('_eta_str', '').strip(),
            })
        elif d.get('status') == 'finished':
            title = d.get('info_dict', {}).get('title', 'Unknown Title')
            if progress_callback:
                progress_callback(url, "finished", {"title": title})

    if format_choice == 'mp4':
        # Fallback chain: best mp4 combo -> best combined mp4 -> any best format.
        format_str = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        postprocessors = []
    else:
        format_str = 'bestaudio/best'
        postprocessors = [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': audio_quality,
            }
        ]

    options = {
        'outtmpl': os.path.join(download_path, '%(title).150s.%(ext)s'),
        'format': format_str,
        'postprocessors': postprocessors,
        'logger': _QuietLogger(url, progress_callback),
        'progress_hooks': [hook],
        'compat_opts': ['no-youtube-skip-dash-manifest'],
        'no_warnings': True,
        'retries': 3,
        'fragment_retries': 3,
        'noplaylist': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36'
        },
    }

    if start_seconds is not None or end_seconds is not None:
        # download_ranges expects (start, end) in seconds; None end = to the end.
        range_start = start_seconds if start_seconds is not None else 0.0
        range_end = end_seconds  # yt-dlp accepts None to mean "to the end"

        def _ranges(info_dict, ydl_obj):
            return [{"start_time": range_start, "end_time": range_end}]

        options['download_ranges'] = _ranges
        options['force_keyframes_at_cuts'] = True  # more accurate cut points

    if ffmpeg_location and ffmpeg_location != "__NOT_FOUND__":
        options['ffmpeg_location'] = ffmpeg_location

    return options


def download_video(
    item: VideoItem,
    download_path: str,
    format_choice: str,
    audio_quality: str = "192",
    progress_callback: Optional[Callable[[str, str, dict], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> DownloadResult:
    """Download a single video/audio item (with its own optional trim range).
    Returns a DownloadResult."""
    url = item.url

    if not validate_url(url):
        err = "Invalid YouTube URL"
        if progress_callback:
            progress_callback(url, "error", {"message": err})
        return DownloadResult(url=url, success=False, error=err)

    try:
        start_seconds, end_seconds = item.trim_seconds()
    except ValueError as e:
        if progress_callback:
            progress_callback(url, "error", {"message": str(e)})
        return DownloadResult(url=url, success=False, error=str(e))

    if cancel_event is not None and cancel_event.is_set():
        if progress_callback:
            progress_callback(url, "cancelled", {})
        return DownloadResult(url=url, success=False, cancelled=True, error="Cancelled by user")

    if progress_callback:
        progress_callback(url, "starting", {})

    ffmpeg_location = find_ffmpeg()
    options = _build_options(
        download_path, format_choice, audio_quality, url, progress_callback,
        ffmpeg_location, cancel_event, start_seconds, end_seconds,
    )

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = None
            title = info.get('title')
            try:
                # requested_downloads gives the final, post-processed filepath
                if info.get('requested_downloads'):
                    filepath = info['requested_downloads'][0].get('filepath')
                if not filepath:
                    filepath = ydl.prepare_filename(info)
                    if format_choice == 'mp3':
                        filepath = os.path.splitext(filepath)[0] + '.mp3'
            except Exception:
                filepath = None

            return DownloadResult(url=url, success=True, filepath=filepath, title=title)
    except DownloadCancelled:
        if progress_callback:
            progress_callback(url, "cancelled", {})
        return DownloadResult(url=url, success=False, cancelled=True, error="Cancelled by user")
    except Exception as e:
        if progress_callback:
            progress_callback(url, "error", {"message": str(e)})
        return DownloadResult(url=url, success=False, error=str(e))


def run_batch_download(job: DownloadJob) -> List[DownloadResult]:
    """
    Run a batch of downloads concurrently (bounded by job.max_workers) and
    return a list of DownloadResult, one per item (input order not guaranteed
    since downloads complete as they finish).
    """
    os.makedirs(job.download_path, exist_ok=True)

    # De-dupe by URL while preserving each item's own trim settings.
    seen = set()
    items: List[VideoItem] = []
    for item in job.items:
        u = item.url.strip()
        if u and u not in seen:
            seen.add(u)
            items.append(item)

    results: List[DownloadResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, job.max_workers)) as executor:
        futures = {
            executor.submit(
                download_video,
                item,
                job.download_path,
                job.format_choice,
                job.audio_quality,
                job.progress_callback,
                job.cancel_events.get(item.url),
            ): item.url
            for item in items
        }
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                results.append(future.result())
            except Exception as e:
                results.append(DownloadResult(url=url, success=False, error=str(e)))

    return results