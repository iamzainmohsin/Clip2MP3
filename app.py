"""
app.py
------
Streamlit front end for the YouTube batch downloader.

Run with:
    streamlit run app.py

Requires downloader.py to be in the same folder.
"""

import os
import sys
import time
import threading
import queue
import zipfile
import subprocess
from io import BytesIO

import streamlit as st

from downloader import (
    DownloadJob,
    VideoItem,
    dedupe_urls,
    ffmpeg_available,
    format_timecode,
    parse_timecode,
    run_batch_download,
    validate_url,
)

FOLDER_PICKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "folder_picker.py")


def folder_picker_available() -> bool:
    """
    We can't reliably know in advance whether tkinter/a display is available
    on the machine running this app without actually trying it (and trying
    it safely means launching the subprocess). So this just checks the
    helper script exists; folder_picker.py itself reports the real failure
    reason if tkinter or a display isn't there when the button is clicked.
    """
    return os.path.isfile(FOLDER_PICKER_SCRIPT)

st.set_page_config(page_title="YouTube Batch Downloader", page_icon="🎬", layout="centered")

# ---------------------------------------------------------------------------
# Session state setup
# ---------------------------------------------------------------------------
defaults = {
    "progress_events": {},      # url -> {status, percent, title, error...}
    "results": None,
    "is_downloading": False,
    "download_path": os.path.join(os.path.expanduser("~"), "Downloads", "yt_batch"),
    "staged_urls": [],          # list[str] — URLs parsed from the textarea, awaiting per-video setup
    "trim_settings": {},        # url -> {"start": "", "end": ""}
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


def make_progress_callback(event_queue: "queue.Queue"):
    """Returns a callback that pushes events onto a thread-safe queue,
    since Streamlit session_state isn't safe to write to from worker threads."""

    def _callback(url, status, extra):
        event_queue.put((url, status, extra))

    return _callback


def run_job_in_thread(job: DownloadJob, event_queue: "queue.Queue", result_holder: dict):
    results = run_batch_download(job)
    result_holder["results"] = results
    event_queue.put(("__DONE__", "done", {}))


def pick_folder_dialog(start_dir: str):
    """
    Opens the native OS folder-browser dialog by launching folder_picker.py
    as a SEPARATE PROCESS (not a thread). This is required because tkinter's
    Tk() must run on a process's main thread, and Streamlit runs app.py on a
    worker thread — calling tkinter directly here would hang or crash the
    server. Running it as a subprocess gives the dialog its own real main
    thread, so it's safe, and a crash/hang there can't take this app down
    with it.

    Returns (path_or_none, error_message_or_none).
    """
    try:
        result = subprocess.run(
            [sys.executable, FOLDER_PICKER_SCRIPT, start_dir or ""],
            capture_output=True,
            text=True,
            timeout=120,  # generous, since the user may take a while browsing
        )
    except subprocess.TimeoutExpired:
        return None, "Folder picker timed out (took longer than 2 minutes). Please type the path manually."
    except Exception as e:
        return None, f"Couldn't launch the folder picker: {e}"

    if result.returncode == 0:
        path = result.stdout.strip()
        return (path or None), None

    # Non-zero exit: either the user cancelled the dialog (no error), or
    # tkinter/display genuinely isn't available on this machine (stderr set).
    stderr = (result.stderr or "").strip()
    if stderr:
        return None, f"Folder picker unavailable: {stderr.replace('ERROR: ', '')}"
    return None, None  # user simply cancelled — not an error


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("🎬 YouTube Batch Downloader")
st.caption("Paste in multiple YouTube links, trim each one if you like, pick MP3 or MP4, and download them all at once.")

if not ffmpeg_available():
    st.error(
        "**FFmpeg not found.** This app needs FFmpeg installed and available on your system "
        "PATH (or placed in a `ffmpeg/bin` folder next to `downloader.py`) for MP3 conversion, "
        "trimming, and some MP4 merges to work.\n\n"
        "Install it from [ffmpeg.org](https://ffmpeg.org/download.html), or via your package "
        "manager (`brew install ffmpeg`, `apt install ffmpeg`, `choco install ffmpeg`, etc.), "
        "then restart this app."
    )

# ---------------------------------------------------------------------------
# Download folder picker (native OS dialog via a separate subprocess, outside
# any form so the Browse button can update state immediately)
# ---------------------------------------------------------------------------
st.subheader("📁 Download folder")

folder_col1, folder_col2 = st.columns([4, 1])
with folder_col1:
    typed_path = st.text_input(
        "Download folder",
        value=st.session_state.download_path,
        label_visibility="collapsed",
        help="Type a path directly, or use Browse to open your system's folder picker.",
    )
    if typed_path != st.session_state.download_path:
        st.session_state.download_path = typed_path
with folder_col2:
    browse_clicked = st.button("Browse…", use_container_width=True)

if browse_clicked:
    with st.spinner("Opening folder picker… (check for a new window, possibly behind this one)"):
        chosen, error = pick_folder_dialog(st.session_state.download_path)
    if chosen:
        st.session_state.download_path = chosen
        st.rerun()
    elif error:
        st.warning(
            f"{error}\n\nThis can happen when the app is running on a remote/headless machine "
            "with no display. Just type the folder path directly in the box above instead."
        )
    # if chosen is None and error is None, the user simply cancelled the dialog — do nothing

# ---------------------------------------------------------------------------
# Stage 1: paste URLs
# ---------------------------------------------------------------------------
st.subheader("🔗 Video links")

urls_text = st.text_area(
    "YouTube URLs (one per line)",
    height=160,
    placeholder="https://youtu.be/dQw4w9WgXcQ\nhttps://www.youtube.com/watch?v=...\n...",
    help="Paste as many YouTube links as you like, one per line.",
    disabled=st.session_state.is_downloading,
)

load_clicked = st.button(
    "Load links ↓", disabled=st.session_state.is_downloading,
    help="Parses the links above so you can set an optional trim range for each one.",
)

if load_clicked:
    raw_urls = [u for u in urls_text.splitlines() if u.strip()]
    urls = dedupe_urls(raw_urls)
    invalid = [u for u in urls if not validate_url(u)]
    valid_urls = [u for u in urls if validate_url(u)]

    if not urls:
        st.warning("Please paste at least one YouTube URL first.")
    else:
        if invalid:
            st.warning(f"Skipping {len(invalid)} invalid URL(s): " + ", ".join(invalid))
        st.session_state.staged_urls = valid_urls
        # Keep existing trim settings for URLs that are still present; default new ones to blank.
        st.session_state.trim_settings = {
            u: st.session_state.trim_settings.get(u, {"start": "", "end": ""})
            for u in valid_urls
        }
        st.rerun()

# ---------------------------------------------------------------------------
# Stage 2: per-video trim editor + format/options + start button
# ---------------------------------------------------------------------------
if st.session_state.staged_urls and not st.session_state.is_downloading:
    st.subheader("✂️ Per-video trim (optional)")
    st.caption(
        "Leave both fields blank to download the full video. Use `MM:SS` or `HH:MM:SS` "
        "(e.g. `1:30` or `01:02:15`). Each video can have its own range."
    )

    for u in st.session_state.staged_urls:
        current = st.session_state.trim_settings.get(u, {"start": "", "end": ""})
        with st.expander(f"🎬 {u}", expanded=False):
            tcol1, tcol2, tcol3 = st.columns([2, 2, 1])
            with tcol1:
                start_val = st.text_input(
                    "Start time", value=current.get("start", ""), key=f"start_{u}",
                    placeholder="e.g. 0:30",
                )
            with tcol2:
                end_val = st.text_input(
                    "End time", value=current.get("end", ""), key=f"end_{u}",
                    placeholder="e.g. 2:00 (blank = to the end)",
                )
            with tcol3:
                st.write("")
                st.write("")
                if st.button("Clear", key=f"clear_{u}"):
                    st.session_state[f"start_{u}"] = ""
                    st.session_state[f"end_{u}"] = ""
                    st.session_state.trim_settings[u] = {"start": "", "end": ""}
                    st.rerun()

            # Validate as the user types so problems surface before download starts.
            trim_error = None
            try:
                s = parse_timecode(start_val)
                e = parse_timecode(end_val)
                if s is not None and e is not None and e <= s:
                    trim_error = "End time must be after start time."
            except ValueError:
                trim_error = "Use MM:SS or HH:MM:SS, e.g. 1:30 or 01:02:15."

            if trim_error:
                st.error(trim_error)
            elif start_val.strip() or end_val.strip():
                st.caption(
                    f"Will trim from **{format_timecode(parse_timecode(start_val)) or '0:00'}** "
                    f"to **{format_timecode(parse_timecode(end_val)) or 'end of video'}**."
                )

            st.session_state.trim_settings[u] = {"start": start_val, "end": end_val}

    st.divider()

    with st.form("download_options_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            format_choice = st.radio("Format", options=["mp3", "mp4"], horizontal=True)
        with col2:
            if format_choice == "mp3":
                audio_quality = st.selectbox("MP3 quality (kbps)", options=["128", "192", "256", "320"], index=1)
            else:
                audio_quality = "192"  # unused for mp4, kept for consistent job shape

        max_workers = st.slider(
            "Parallel downloads", min_value=1, max_value=6, value=3,
            help="How many videos to download at the same time. Higher = faster, but uses more bandwidth/CPU."
        )

        submitted = st.form_submit_button("Start batch download", type="primary")

    if submitted:
        download_path = st.session_state.download_path

        # Re-validate all trim fields right before launch in case something's still broken.
        bad = []
        items = []
        for u in st.session_state.staged_urls:
            t = st.session_state.trim_settings.get(u, {"start": "", "end": ""})
            try:
                item = VideoItem(url=u, start_time=t["start"], end_time=t["end"])
                item.trim_seconds()  # raises if malformed/inverted
                items.append(item)
            except ValueError as e:
                bad.append(f"{u}: {e}")

        if not download_path.strip():
            st.warning("Please choose a download folder.")
        elif bad:
            st.error("Fix these trim ranges before starting:\n\n" + "\n".join(f"- {b}" for b in bad))
        else:
            os.makedirs(download_path, exist_ok=True)

            st.session_state.progress_events = {item.url: {"status": "queued"} for item in items}
            st.session_state.results = None
            st.session_state.is_downloading = True

            event_queue: "queue.Queue" = queue.Queue()
            result_holder = {}

            job = DownloadJob(
                items=items,
                download_path=download_path,
                format_choice=format_choice,
                max_workers=max_workers,
                audio_quality=audio_quality,
                progress_callback=make_progress_callback(event_queue),
            )

            thread = threading.Thread(target=run_job_in_thread, args=(job, event_queue, result_holder), daemon=True)
            thread.start()

            st.session_state._event_queue = event_queue
            st.session_state._result_holder = result_holder
            st.session_state._job = job  # keep a handle so Cancel buttons can reach job.cancel_events
            st.session_state._valid_urls = [item.url for item in items]
            st.rerun()

# ---------------------------------------------------------------------------
# Live progress display (polling loop while a job is running), with a Cancel
# button per video so any single download can be stopped mid-flight without
# affecting the others.
# ---------------------------------------------------------------------------
if st.session_state.is_downloading:
    event_queue = st.session_state._event_queue
    result_holder = st.session_state._result_holder
    job = st.session_state._job

    st.subheader("⬇️ Downloading…")
    progress_bar = st.progress(0.0)

    done = False
    # Drain whatever events have arrived so far
    while True:
        try:
            url, status, extra = event_queue.get_nowait()
        except queue.Empty:
            break

        if url == "__DONE__":
            done = True
            break

        entry = st.session_state.progress_events.get(url, {})
        entry["status"] = status
        entry.update(extra)
        st.session_state.progress_events[url] = entry

    total = len(st.session_state._valid_urls)
    finished_count = sum(
        1 for v in st.session_state.progress_events.values()
        if v.get("status") in ("finished", "error", "cancelled")
    )
    progress_bar.progress(finished_count / total if total else 0)

    for u in st.session_state._valid_urls:
        info = st.session_state.progress_events.get(u, {})
        status = info.get("status", "queued")

        row_col1, row_col2 = st.columns([5, 1])
        with row_col1:
            if status == "downloading":
                pct = info.get("percent_str", "")
                speed = info.get("speed", "")
                st.markdown(f"⬇️ **Downloading** `{pct}` {speed} — {u}")
            elif status == "finished":
                title = info.get("title", u)
                st.markdown(f"✅ **Done** — {title}")
            elif status == "error":
                msg = info.get("message", "unknown error")
                st.markdown(f"❌ **Error** ({msg}) — {u}")
            elif status == "cancelled":
                st.markdown(f"🚫 **Cancelled** — {u}")
            elif status == "starting":
                st.markdown(f"🟡 **Starting** — {u}")
            else:
                st.markdown(f"⏳ Queued — {u}")
        with row_col2:
            already_cancelled = job.cancel_events.get(u) and job.cancel_events[u].is_set()
            can_cancel = status in ("queued", "starting", "downloading") and not already_cancelled
            if st.button("Cancel", key=f"cancel_{u}", disabled=not can_cancel, use_container_width=True):
                job.cancel(u)
                entry = st.session_state.progress_events.get(u, {})
                entry["status"] = "cancelled"
                st.session_state.progress_events[u] = entry
                st.rerun()

    if done:
        st.session_state.results = result_holder.get("results", [])
        st.session_state.is_downloading = False
        st.rerun()
    else:
        time.sleep(0.7)
        st.rerun()

# ---------------------------------------------------------------------------
# Final results + downloads
# ---------------------------------------------------------------------------
if st.session_state.results is not None and not st.session_state.is_downloading:
    results = st.session_state.results
    successes = [r for r in results if r.success]
    cancelled = [r for r in results if r.cancelled]
    failures = [r for r in results if not r.success and not r.cancelled]

    st.divider()
    st.subheader("Results")
    st.success(f"{len(successes)} succeeded, {len(failures)} failed, {len(cancelled)} cancelled.")
    st.caption(f"Files saved to: `{st.session_state.download_path}`")

    if cancelled:
        with st.expander(f"🚫 {len(cancelled)} cancelled download(s)"):
            for r in cancelled:
                st.write(f"- {r.url}")

    if failures:
        with st.expander(f"⚠️ {len(failures)} failed download(s)"):
            for r in failures:
                st.write(f"- {r.url} — {r.error}")

    existing_files = [r for r in successes if r.filepath and os.path.isfile(r.filepath)]

    if existing_files:
        # Offer a single zip download of everything
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in existing_files:
                zf.write(r.filepath, arcname=os.path.basename(r.filepath))
        zip_buffer.seek(0)

        st.download_button(
            "⬇️ Download all as ZIP",
            data=zip_buffer,
            file_name="youtube_batch_download.zip",
            mime="application/zip",
            type="primary",
        )

        st.write("Or download individually:")
        for r in existing_files:
            with open(r.filepath, "rb") as f:
                st.download_button(
                    f"⬇️ {os.path.basename(r.filepath)}",
                    data=f.read(),
                    file_name=os.path.basename(r.filepath),
                    key=f"dl_{r.filepath}",
                )

    if st.button("Start a new batch"):
        st.session_state.results = None
        st.session_state.progress_events = {}
        st.session_state.staged_urls = []
        st.session_state.trim_settings = {}
        st.rerun()