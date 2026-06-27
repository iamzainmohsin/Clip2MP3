import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import yt_dlp
from yt_dlp.utils import DownloadCancelled


YOUTUBE_REGEX = re.compile(
    r"(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/"
    r"(watch\?v=|embed/|v/|shorts/|.+\?v=)?([^&=%\?]{11})"
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
    Locate FFmpeg.
    Returns None when ffmpeg is already on PATH.
    Otherwise returns the absolute path to ./ffmpeg/bin if a bundled copy exists.
    """
    if shutil.which("ffmpeg") is not None:
        return None

    local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin")
    exe_name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    candidate = os.path.join(local_dir, exe_name)
    if os.path.isfile(candidate):
        return os.path.abspath(local_dir)

    return None


def ffmpeg_available() -> bool:
    """
    Return True if FFmpeg is available either on PATH or in a local ./ffmpeg/bin
    bundle next to this file.
    """
    if shutil.which("ffmpeg") is not None:
        return True

    local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "bin")
    exe_name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    return os.path.isfile(os.path.join(local_dir, exe_name))


def get_ffmpeg_executable(ffmpeg_location):
    """
    Resolve the FFmpeg executable path.

    If ffmpeg_location is None, FFmpeg is expected to be found on PATH.
    Otherwise it points at a bundled ./ffmpeg/bin directory.
    """
    if not ffmpeg_location:
        return "ffmpeg"

    exe_name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    return os.path.join(ffmpeg_location, exe_name)


def trim_media(
    input_file: str,
    output_file: str,
    start_seconds: Optional[float],
    end_seconds: Optional[float],
    ffmpeg_location: Optional[str],
) -> None:
    """
    Trim a downloaded media file using FFmpeg after the full file has already
    been downloaded.

    A temporary output file is used so the original is only replaced after FFmpeg
    succeeds, which keeps the workflow safe and makes failed trims easy to clean up.
    """
    ffmpeg_exe = get_ffmpeg_executable(ffmpeg_location)
    cmd = [ffmpeg_exe, "-y"]

    if start_seconds is not None:
        cmd += ["-ss", format_timecode(start_seconds)]

    cmd += ["-i", input_file]

    if end_seconds is not None:
        cmd += ["-to", format_timecode(end_seconds)]

    cmd += ["-c", "copy", output_file]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(f"FFmpeg trimming failed:\n\n{stderr}") from e


@dataclass
class VideoItem:
    """One video in a batch, with its own optional trim range and resolution."""
    url: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    resolution: Optional[str] = None

    def trim_seconds(self):
        """Return (start_seconds, end_seconds), either may be None."""
        start = parse_timecode(self.start_time) if self.start_time else None
        end = parse_timecode(self.end_time) if self.end_time else None
        if start is not None and end is not None and end <= start:
            raise ValueError("End time must be after start time")
        return start, end

    def has_trim(self) -> bool:
        return bool((self.start_time or "").strip() or (self.end_time or "").strip())

    def resolution_height(self) -> Optional[int]:
        value = (self.resolution or "").strip().lower()
        if not value or value == "best":
            return None
        m = re.match(r"(\d+)\s*p?$", value)
        if not m:
            raise ValueError(f"Invalid resolution: {self.resolution!r}")
        return int(m.group(1))


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
    format_choice: str
    max_workers: int = 3
    audio_quality: str = "192"
    progress_callback: Optional[Callable[[str, str, dict], None]] = field(default=None, repr=False)
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
    download_path,
    format_choice,
    audio_quality,
    url,
    progress_callback,
    ffmpeg_location,
    cancel_event,
    resolution_height=None,
):
    """
    Build yt-dlp options for a full-file download.

    Trimming is deliberately excluded here so yt-dlp only downloads/merges media
    and FFmpeg handles any optional post-download trim afterwards.
    """
    def hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Cancelled by user")

        if d.get("status") == "downloading" and progress_callback:
            percent_str = d.get("_percent_str", "").strip()
            progress_callback(
                url,
                "downloading",
                {
                    "percent_str": percent_str,
                    "speed": d.get("_speed_str", "").strip(),
                    "eta": d.get("_eta_str", "").strip(),
                },
            )
        elif d.get("status") == "finished":
            title = d.get("info_dict", {}).get("title", "Unknown Title")
            if progress_callback:
                progress_callback(url, "finished", {"title": title})

    if format_choice == "mp4":
        if resolution_height is not None:
            format_str = (
                f"bestvideo[ext=mp4][height<={resolution_height}]+bestaudio[ext=m4a]/"
                f"best[ext=mp4][height<={resolution_height}]/"
                f"bestvideo[height<={resolution_height}]+bestaudio/best[height<={resolution_height}]/best"
            )
        else:
            format_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best"
        postprocessors = []
    else:
        format_str = "bestaudio/best"
        postprocessors = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": audio_quality,
            }
        ]

    options = {
        "outtmpl": os.path.join(download_path, "%(title).150s.%(ext)s"),
        "format": format_str,
        "postprocessors": postprocessors,
        "logger": _QuietLogger(url, progress_callback),
        "progress_hooks": [hook],
        "compat_opts": ["no-youtube-skip-dash-manifest"],
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }

    if ffmpeg_location:
        options["ffmpeg_location"] = ffmpeg_location

    return options


def _cleanup_path(path: Optional[str]) -> None:
    """
    Best-effort cleanup helper for temp or partial files.

    It exists so cancelled or failed jobs do not leave behind orphan files from
    the download or trim stages.
    """
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def download_video(
    item: VideoItem,
    download_path: str,
    format_choice: str,
    audio_quality: str = "192",
    progress_callback: Optional[Callable[[str, str, dict], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> DownloadResult:
    """
    Download one item, then optionally trim it with FFmpeg after the file exists.
    """
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

    try:
        resolution_height = item.resolution_height()
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
        download_path,
        format_choice,
        audio_quality,
        url,
        progress_callback,
        ffmpeg_location,
        cancel_event,
        resolution_height=resolution_height,
    )

    temp_trim_path = None

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = None
            title = info.get("title")

            try:
                if info.get("requested_downloads"):
                    filepath = info["requested_downloads"][0].get("filepath")
                if not filepath:
                    filepath = ydl.prepare_filename(info)
                    if format_choice == "mp3":
                        filepath = os.path.splitext(filepath)[0] + ".mp3"
                    elif format_choice == "mp4":
                        filepath = os.path.splitext(filepath)[0] + ".mp4"
            except Exception:
                filepath = None

            if filepath and format_choice == "mp4" and not filepath.lower().endswith(".mp4"):
                base, _ext = os.path.splitext(filepath)
                mp4_candidate = base + ".mp4"
                if os.path.isfile(mp4_candidate):
                    filepath = mp4_candidate

            if filepath and (start_seconds is not None or end_seconds is not None):
                if progress_callback:
                    progress_callback(url, "trimming", {})

                base, ext = os.path.splitext(filepath)
                temp_trim_path = f"{base}.tmp{ext}"

                trim_media(
                    input_file=filepath,
                    output_file=temp_trim_path,
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    ffmpeg_location=ffmpeg_location,
                )

                os.remove(filepath)
                os.replace(temp_trim_path, filepath)
                temp_trim_path = None

            return DownloadResult(url=url, success=True, filepath=filepath, title=title)

    except DownloadCancelled:
        _cleanup_path(temp_trim_path)
        if progress_callback:
            progress_callback(url, "cancelled", {})
        return DownloadResult(url=url, success=False, cancelled=True, error="Cancelled by user")
    except Exception as e:
        _cleanup_path(temp_trim_path)
        if progress_callback:
            progress_callback(url, "error", {"message": str(e)})
        traceback.print_exc()
        raise


def run_batch_download(job: DownloadJob) -> List[DownloadResult]:
    """
    Run a batch of downloads concurrently and return one DownloadResult per item.
    """
    os.makedirs(job.download_path, exist_ok=True)

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