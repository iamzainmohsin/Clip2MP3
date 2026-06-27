import sys
from collections import deque

import yt_dlp
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QRunnable, QThreadPool
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)

from downloader import (
    VideoItem,
    DownloadJob,
    run_batch_download,
    dedupe_urls,
    validate_url,
    ffmpeg_available,
    find_ffmpeg,
)

FFMPEG_LOCATION = find_ffmpeg()


class SignalEmitter(QObject):
    done = pyqtSignal(int, list)
    failed = pyqtSignal(int, str)


class MetaTask(QRunnable):
    def __init__(self, row, url, emitter):
        super().__init__()
        self.row = row
        self.url = url
        self.emitter = emitter

    def run(self):
        try:
            opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)

            formats = info.get("formats", []) or []
            heights = sorted(
                {
                    f.get("height")
                    for f in formats
                    if f.get("vcodec") != "none" and f.get("height")
                },
                reverse=True,
            )

            resolutions = ["Best"] + [f"{h}p" for h in heights]
            self.emitter.done.emit(self.row, resolutions)
        except Exception as e:
            self.emitter.failed.emit(self.row, str(e))


class DownloadWorker(QThread):
    progress = pyqtSignal(int, str)
    finished_all = pyqtSignal(list)
    row_state = pyqtSignal(int, str)

    def __init__(self, jobs, row_map):
        super().__init__()
        self.jobs = jobs
        self.row_map = row_map

    def run(self):
        all_results = []

        def cb(url, state, data):
            rows = self.row_map.get(url, [])
            if not rows:
                return

            row = rows[0]
            if len(rows) > 1:
                rows.popleft()

            if state == "downloading":
                msg = data.get("percent_str", "").strip()
                if data.get("speed"):
                    msg += f" | {data.get('speed')}"
                if data.get("eta"):
                    msg += f" | ETA {data.get('eta')}"
                msg = msg.strip() or "Downloading"
                self.progress.emit(row, msg)
                self.row_state.emit(row, msg)
            elif state == "finished":
                self.progress.emit(row, "Downloaded, post-processing...")
                self.row_state.emit(row, "Downloaded, post-processing...")
            elif state == "trimming":
                self.progress.emit(row, "Trimming...")
                self.row_state.emit(row, "Trimming...")
            elif state == "error":
                txt = f"Error: {data.get('message', '')}"
                self.progress.emit(row, txt)
                self.row_state.emit(row, txt)
            elif state == "cancelled":
                self.progress.emit(row, "Cancelled")
                self.row_state.emit(row, "Cancelled")

        for job in self.jobs:
            job.progress_callback = cb
            all_results.extend(run_batch_download(job))

        self.finished_all.emit(all_results)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube Batch Downloader")
        self.resize(1280, 720)

        self.thread_pool = QThreadPool.globalInstance()
        self.meta_emitter = SignalEmitter()
        self.meta_emitter.done.connect(self.fill_resolutions)
        self.meta_emitter.failed.connect(self.meta_failed)

        self.meta_workers = set()
        self.download_worker = None
        self.row_progress = {}
        self.pending_meta = deque()
        self.meta_active = 0
        self.meta_limit = 6
        self.row_cancel_buttons = {}

        self.init_ui()
        self.apply_dark_mode(True)
        self.set_status("Ready")

    def init_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)

        top = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Choose download folder...")
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.choose_folder)
        self.dark_toggle = QCheckBox("Dark mode")
        self.dark_toggle.setChecked(True)
        self.dark_toggle.toggled.connect(self.apply_dark_mode)
        top.addWidget(QLabel("Download folder:"))
        top.addWidget(self.folder_edit, 1)
        top.addWidget(browse_btn)
        top.addWidget(self.dark_toggle)
        main.addLayout(top)

        paste_row = QHBoxLayout()
        self.urls_box = QPlainTextEdit()
        self.urls_box.setPlaceholderText("Paste one YouTube link per line...")
        self.add_btn = QPushButton("Add Links")
        self.add_btn.clicked.connect(self.add_links)
        paste_row.addWidget(self.urls_box, 1)
        paste_row.addWidget(self.add_btn)
        main.addLayout(paste_row)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["URL", "Trim Start", "Trim End", "Resolution", "Format", "Status", "Action"]
        )
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        main.addWidget(self.table)

        controls = QHBoxLayout()
        self.download_btn = QPushButton("Download")
        self.download_btn.clicked.connect(self.start_download)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_all)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.status_lbl = QLabel("Ready")

        controls.addWidget(self.download_btn)
        controls.addWidget(self.clear_btn)
        controls.addWidget(self.status_lbl, 1)
        controls.addWidget(self.bar)
        main.addLayout(controls)

    def apply_dark_mode(self, enabled):
        if enabled:
            self.setStyleSheet("""
                QWidget {
                    background: #121212;
                    color: #e6e6e6;
                    font-size: 12px;
                }
                QLineEdit, QPlainTextEdit, QTableWidget, QComboBox {
                    background: #1e1e1e;
                    color: #e6e6e6;
                    border: 1px solid #3a3a3a;
                    selection-background-color: #2d6cdf;
                }
                QPushButton {
                    background: #2b2b2b;
                    color: #ffffff;
                    border: 1px solid #4a4a4a;
                    padding: 6px 10px;
                    border-radius: 6px;
                }
                QPushButton:hover {
                    background: #383838;
                }
                QPushButton:disabled {
                    color: #777777;
                    background: #222222;
                }
                QHeaderView::section {
                    background: #1f1f1f;
                    color: #e6e6e6;
                    border: 1px solid #333333;
                    padding: 6px;
                }
                QProgressBar {
                    border: 1px solid #3a3a3a;
                    background: #1e1e1e;
                    text-align: center;
                    color: #ffffff;
                    border-radius: 6px;
                }
                QProgressBar::chunk {
                    background: #2d6cdf;
                    border-radius: 6px;
                }
                QCheckBox {
                    spacing: 8px;
                }
            """)
        else:
            self.setStyleSheet("")

    def set_status(self, text):
        self.status_lbl.setText(text)

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select Download Folder")
        if path:
            self.folder_edit.setText(path)

    def add_links(self):
        text = self.urls_box.toPlainText().strip()
        if not text:
            return

        urls = dedupe_urls(text.splitlines())
        for url in urls:
            if validate_url(url):
                self.add_row(url)

        self.urls_box.clear()

    def add_row(self, url):
        row = self.table.rowCount()
        self.table.insertRow(row)

        url_item = QTableWidgetItem(url)
        url_item.setFlags(url_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 0, url_item)

        self.table.setItem(row, 1, QTableWidgetItem(""))
        self.table.setItem(row, 2, QTableWidgetItem(""))

        res_combo = QComboBox()
        res_combo.addItem("Best")
        self.table.setCellWidget(row, 3, res_combo)

        fmt_combo = QComboBox()
        fmt_combo.addItems(["mp4", "mp3"])
        self.table.setCellWidget(row, 4, fmt_combo)

        status_item = QTableWidgetItem("Waiting")
        status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 5, status_item)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(lambda _, r=row: self.cancel_row(r))
        self.table.setCellWidget(row, 6, cancel_btn)
        self.row_cancel_buttons[row] = cancel_btn

        self.queue_meta(row, url)

    def queue_meta(self, row, url):
        self.pending_meta.append((row, url))
        self.pump_meta()

    def pump_meta(self):
        while self.pending_meta and self.meta_active < self.meta_limit:
            row, url = self.pending_meta.popleft()
            self.meta_active += 1
            task = MetaTask(row, url, self.meta_emitter)
            self.meta_workers.add(task)
            self.thread_pool.start(task)

        self.cleanup_meta_workers()

    def cleanup_meta_workers(self):
        self.meta_workers = {
            w for w in self.meta_workers if not getattr(w, "isFinished", lambda: True)()
        }

    def fill_resolutions(self, row, items):
        combo = self.table.cellWidget(row, 3)
        if combo is not None:
            combo.clear()
            combo.addItems(items)
        self.meta_active = max(0, self.meta_active - 1)
        self.pump_meta()

    def meta_failed(self, row, err):
        combo = self.table.cellWidget(row, 3)
        if combo is not None:
            combo.clear()
            combo.addItem("Best")
        self.meta_active = max(0, self.meta_active - 1)
        self.pump_meta()
        self.set_row_status(row, "Metadata failed")

    def set_row_status(self, row, text):
        item = self.table.item(row, 5)
        if item is None:
            item = QTableWidgetItem("")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 5, item)
        item.setText(text)

    def clear_all(self):
        if self.download_worker and self.download_worker.isRunning():
            QMessageBox.warning(self, "Busy", "Stop downloads before clearing.")
            return

        self.table.setRowCount(0)
        self.pending_meta.clear()
        self.meta_active = 0
        self.row_progress.clear()
        self.row_cancel_buttons.clear()
        self.set_status("Cleared")
        self.bar.setValue(0)

    def cancel_row(self, row):
        if not self.download_worker or not self.download_worker.isRunning():
            self.set_row_status(row, "Cancelled")
            return

        url_item = self.table.item(row, 0)
        if not url_item:
            return

        url = url_item.text().strip()
        if not url:
            return

        for job in self.download_worker.jobs:
            for item in job.items:
                if item.url == url:
                    job.cancel(url)
                    self.set_row_status(row, "Cancelling...")
                    return

    def start_download(self):
        folder = self.folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "Missing folder", "Please choose a download folder.")
            return

        if self.table.rowCount() == 0:
            QMessageBox.warning(self, "No links", "Please add at least one video link.")
            return

        if self.download_worker and self.download_worker.isRunning():
            QMessageBox.warning(self, "Busy", "A download is already running.")
            return

        items_by_fmt = {"mp4": [], "mp3": []}
        row_map = {}
        self.row_progress = {}

        for row in range(self.table.rowCount()):
            url_item = self.table.item(row, 0)
            if not url_item:
                continue

            url = url_item.text().strip()
            if not url:
                continue

            start_item = self.table.item(row, 1)
            end_item = self.table.item(row, 2)
            res_combo = self.table.cellWidget(row, 3)
            fmt_combo = self.table.cellWidget(row, 4)

            start = start_item.text().strip() if start_item else ""
            end = end_item.text().strip() if end_item else ""
            resolution = res_combo.currentText() if res_combo else "Best"
            format_choice = fmt_combo.currentText() if fmt_combo else "mp4"

            item = VideoItem(
                url=url,
                start_time=start or None,
                end_time=end or None,
                resolution=resolution,
            )

            items_by_fmt[format_choice].append(item)
            row_map.setdefault(url, deque()).append(row)
            self.row_progress[row] = 0
            self.set_row_status(row, "Queued")

        jobs = []
        if items_by_fmt["mp4"]:
            jobs.append(
                DownloadJob(
                    items=items_by_fmt["mp4"],
                    download_path=folder,
                    format_choice="mp4",
                    max_workers=3,
                )
            )
        if items_by_fmt["mp3"]:
            jobs.append(
                DownloadJob(
                    items=items_by_fmt["mp3"],
                    download_path=folder,
                    format_choice="mp3",
                    max_workers=3,
                )
            )

        if not jobs:
            QMessageBox.warning(self, "No valid rows", "No valid items found to download.")
            return

        if any(job.format_choice == "mp3" for job in jobs) and not ffmpeg_available():
            QMessageBox.warning(
                self,
                "FFmpeg missing",
                "MP3 downloads need FFmpeg on PATH or bundled next to downloader.py.",
            )
            return

        self.download_btn.setEnabled(False)
        self.status_lbl.setText("Downloading...")
        self.bar.setRange(0, 100)
        self.bar.setValue(0)

        self.download_worker = DownloadWorker(jobs, row_map)
        self.download_worker.progress.connect(self.update_progress)
        self.download_worker.row_state.connect(self.set_row_status)
        self.download_worker.finished_all.connect(self.download_done)
        self.download_worker.start()

    def update_progress(self, row, text):
        self.set_row_status(row, text)
        if "%" in text:
            try:
                pct = float(text.split("%", 1)[0].split()[-1])
                self.row_progress[row] = max(0, min(100, int(pct)))
                avg = int(sum(self.row_progress.values()) / max(1, len(self.row_progress)))
                self.bar.setValue(avg)
            except Exception:
                pass

    def download_done(self, results):
        self.download_btn.setEnabled(True)
        self.bar.setValue(100)

        ok = sum(1 for r in results if r.success)
        fail = len(results) - ok
        self.set_status(f"Done: {ok} success, {fail} failed")

        lines = []
        for r in results[:20]:
            if r.success:
                lines.append(f"OK: {r.title or r.url}")
            else:
                lines.append(f"FAIL: {r.url} - {r.error}")

        QMessageBox.information(self, "Finished", "\n".join(lines) if lines else "Done.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())