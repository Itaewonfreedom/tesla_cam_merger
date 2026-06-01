"""
Tesla Dashcam Merger — PyQt6 GUI.

폴더를 선택해 이벤트를 목록으로 보고, 레이아웃/인코딩/해상도/배속 등 옵션을 골라
개별 변환하거나 하나로 머지한다. 영상 처리 명령은 processor.py가 생성하고,
실행은 QProcess로 비동기 수행한다.
"""

import os
import re
import sys
import json
import shutil
import tempfile

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QPushButton, QFileDialog,
                             QListWidget, QLabel, QComboBox, QProgressBar,
                             QMessageBox, QTextEdit, QCheckBox, QGroupBox)
from PyQt6.QtCore import QProcess

from processor import (TeslaDashcamProcessor, MergeConfig,
                       LAYOUTS, RESOLUTION_PRESETS)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tesla Dashcam Merger")
        self.resize(900, 720)

        self.processor = TeslaDashcamProcessor()
        self.current_directory = ""
        self.events = {}

        # 처리 큐 / 상태
        self.queue = []                 # [(timestamp, output_path)]
        self.merge_mode = False
        self.temp_dir = None
        self.generated_segments = []
        self.final_output_file = ""
        self.current_process = None
        self.config = None

        # 진행률(실제 길이 기반)
        self.total_output_secs = 0.0
        self.done_output_secs = 0.0
        self.current_event_secs = 0.0

        self.settings_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "settings.json")
        self.settings = self.load_settings()
        self.last_output_dir = self.settings.get("last_output_dir", os.getcwd())

        self.init_ui()

    # ----------------------------- 설정 ----------------------------- #
    def load_settings(self):
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_settings(self):
        self.settings["last_output_dir"] = self.last_output_dir
        try:
            with open(self.settings_file, "w") as f:
                json.dump(self.settings, f)
        except Exception as e:
            self.log(f"Failed to save settings: {e}")

    # ------------------------------ UI ------------------------------ #
    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 상단: 폴더 선택
        top = QHBoxLayout()
        self.btn_select_dir = QPushButton("📁 Select Folder")
        self.btn_select_dir.clicked.connect(self.select_directory)
        top.addWidget(self.btn_select_dir)
        self.lbl_count = QLabel("Found: 0 events")
        top.addWidget(self.lbl_count)
        top.addStretch()
        self.btn_select_all = QPushButton("Select All")
        self.btn_select_all.clicked.connect(lambda: self.list_widget.selectAll())
        top.addWidget(self.btn_select_all)
        root.addLayout(top)

        # 이벤트 목록
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        root.addWidget(self.list_widget, stretch=2)

        # 옵션 그룹
        root.addWidget(self._build_options())

        # 처리 버튼 + 진행률
        self.btn_process = QPushButton("▶  Process Selected")
        self.btn_process.clicked.connect(self.start_processing)
        self.btn_process.setEnabled(False)
        root.addWidget(self.btn_process)

        self.progress_bar = QProgressBar()
        root.addWidget(self.progress_bar)
        self.lbl_status = QLabel("Ready")
        root.addWidget(self.lbl_status)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        root.addWidget(self.log_text, stretch=1)

    def _build_options(self):
        box = QGroupBox("Options")
        grid = QGridLayout(box)

        self.combo_layout = QComboBox()
        self.combo_layout.addItems(list(LAYOUTS))           # classic/grid6/front
        self.combo_encoding = QComboBox()
        self.combo_encoding.addItems(["hevc_videotoolbox (GPU)", "libx264 (CPU)"])
        self.combo_resolution = QComboBox()
        self.combo_resolution.addItems(list(RESOLUTION_PRESETS))
        self.combo_resolution.setCurrentText("1080p (1920)")
        self.combo_fps = QComboBox()
        self.combo_fps.addItems(["24", "30", "60"])
        self.combo_fps.setCurrentText("30")
        self.combo_speed = QComboBox()
        self.combo_speed.addItems(["1x", "2x", "4x", "8x", "16x"])
        self.combo_quality = QComboBox()
        self.combo_quality.addItems(["Bitrate", "Quality (CRF)"])
        self.combo_quality.currentTextChanged.connect(self._sync_quality)
        self.combo_bitrate = QComboBox()
        self.combo_bitrate.addItems(["5M", "8M", "10M", "15M", "20M", "30M", "50M"])
        self.combo_bitrate.setCurrentText("20M")
        self.combo_crf = QComboBox()
        self.combo_crf.addItems([str(x) for x in range(16, 33, 2)])
        self.combo_crf.setCurrentText("20")
        self.chk_timestamp = QCheckBox("Timestamp")
        self.chk_timestamp.setChecked(True)
        self.chk_labels = QCheckBox("Camera labels")
        self.chk_labels.setChecked(True)

        def row(r, *widgets):
            for c, w in enumerate(widgets):
                grid.addWidget(w, r, c)

        row(0, QLabel("Layout:"), self.combo_layout, QLabel("Encoding:"), self.combo_encoding)
        row(1, QLabel("Resolution:"), self.combo_resolution, QLabel("FPS:"), self.combo_fps)
        row(2, QLabel("Speed:"), self.combo_speed, QLabel("Quality:"), self.combo_quality)
        row(3, QLabel("Bitrate:"), self.combo_bitrate, QLabel("CRF:"), self.combo_crf)
        row(4, self.chk_timestamp, self.chk_labels)
        self._sync_quality(self.combo_quality.currentText())
        return box

    def _sync_quality(self, mode):
        crf = mode.startswith("Quality")
        self.combo_crf.setEnabled(crf)
        self.combo_bitrate.setEnabled(not crf)

    # ----------------------------- 로그 ----------------------------- #
    def log(self, msg):
        self.log_text.append(msg)
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    # --------------------------- 폴더 스캔 --------------------------- #
    def select_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Tesla Dashcam Folder")
        if directory:
            self.current_directory = directory
            self.scan_directory()

    def scan_directory(self):
        self.events = self.processor.find_events(self.current_directory)
        self.list_widget.clear()
        meta = self.processor.read_event_meta(self.current_directory)
        reason = (meta or {}).get("reason", "")

        for ts in sorted(self.events):
            cams = len(self.events[ts])
            dur = self.processor.event_duration(ts)
            self.list_widget.addItem(f"{ts}   ({dur:.0f}s, {cams} cams)")

        self.lbl_count.setText(f"Found: {len(self.events)} events"
                               + (f"  •  {reason}" if reason else ""))
        self.btn_process.setEnabled(len(self.events) > 0)
        self.lbl_status.setText(
            f"Loaded {len(self.events)} events from {os.path.basename(self.current_directory)}")
        self.log(f"Scanned {self.current_directory}: {len(self.events)} events.")

    # --------------------------- 설정 수집 --------------------------- #
    def _item_timestamp(self, item):
        # 표시 텍스트에서 타임스탬프 부분만 추출
        return item.text().split()[0]

    def build_config(self):
        quality = "crf" if self.combo_quality.currentText().startswith("Quality") else "bitrate"
        speed = float(self.combo_speed.currentText().rstrip("x"))
        return MergeConfig(
            encoding=self.combo_encoding.currentText().split(" ")[0],
            quality_mode=quality,
            bitrate=self.combo_bitrate.currentText(),
            crf=int(self.combo_crf.currentText()),
            fps=int(self.combo_fps.currentText()),
            width=RESOLUTION_PRESETS[self.combo_resolution.currentText()],
            speed=speed,
            layout=self.combo_layout.currentText(),
            show_timestamp=self.chk_timestamp.isChecked(),
            show_labels=self.chk_labels.isChecked(),
            preset="medium",
        )

    # --------------------------- 처리 시작 --------------------------- #
    def start_processing(self):
        items = self.list_widget.selectedItems()
        if not items:
            if self.list_widget.count() == 0:
                return
            reply = QMessageBox.question(self, "Process All?",
                                         "No events selected. Process ALL events?")
            if reply != QMessageBox.StandardButton.Yes:
                return
            items = [self.list_widget.item(i) for i in range(self.list_widget.count())]

        timestamps = [self._item_timestamp(i) for i in items]
        self.config = self.build_config()
        self.generated_segments = []
        self.merge_mode = False
        self.temp_dir = None

        if len(timestamps) > 1:
            reply = QMessageBox.question(
                self, "Merge?",
                "Multiple events selected.\nMERGE into a single continuous video?\n"
                "(No = save each event as a separate file)")
            self.merge_mode = (reply == QMessageBox.StandardButton.Yes)

        if self.merge_mode:
            default = os.path.join(self.last_output_dir, "merged_video.mp4")
            path, _ = QFileDialog.getSaveFileName(self, "Save Merged Video", default,
                                                  "MP4 Files (*.mp4)")
            if not path:
                return
            self.final_output_file = path
            self.last_output_dir = os.path.dirname(path)
            self.temp_dir = tempfile.mkdtemp(prefix="tesla_merge_")
            self.queue = [(ts, os.path.join(self.temp_dir, f"seg_{i:03}.ts"))
                          for i, ts in enumerate(timestamps)]
        elif len(timestamps) == 1:
            default = os.path.join(self.last_output_dir, f"merged_{timestamps[0]}.mp4")
            path, _ = QFileDialog.getSaveFileName(self, "Save Video", default,
                                                  "MP4 Files (*.mp4)")
            if not path:
                return
            self.last_output_dir = os.path.dirname(path)
            self.queue = [(timestamps[0], path)]
        else:
            out_dir = QFileDialog.getExistingDirectory(self, "Select Output Folder",
                                                       self.last_output_dir)
            if not out_dir:
                return
            self.last_output_dir = out_dir
            self.queue = [(ts, os.path.join(out_dir, f"merged_{ts}.mp4"))
                          for ts in timestamps]

        self.save_settings()

        # 진행률 합계(출력 길이 기준 = 원본/배속)
        self.total_output_secs = sum(
            self.processor.event_duration(ts) / self.config.speed for ts, _ in self.queue)
        self.done_output_secs = 0.0
        self.progress_bar.setValue(0)
        self.btn_process.setEnabled(False)
        self.btn_select_dir.setEnabled(False)
        self.log(f"Processing {len(self.queue)} events (merge={self.merge_mode}, "
                 f"layout={self.config.layout}, {self.config.width or 'orig'}px, "
                 f"{self.config.speed}x)...")
        self.process_next()

    def process_next(self):
        if not self.queue:
            if self.merge_mode:
                self.start_concat()
            else:
                self.finish_processing()
            return

        timestamp, output_file = self.queue.pop(0)
        self.current_event_secs = self.processor.event_duration(timestamp) / self.config.speed
        container = "mpegts" if self.merge_mode else "mp4"
        if self.merge_mode:
            self.generated_segments.append(output_file)

        self.lbl_status.setText(f"Processing {timestamp}...")
        try:
            cmd = self.processor.build_command(timestamp, output_file, self.config, container)
        except Exception as e:
            self.log(f"Error building command for {timestamp}: {e}")
            self.process_next()
            return
        if not cmd:
            self.log(f"Skip {timestamp} (no command)")
            self.process_next()
            return

        self.log(f"$ {' '.join(cmd[:6])} ... [{os.path.basename(output_file)}]")
        self._spawn(cmd, self.process_finished)

    def start_concat(self):
        self.lbl_status.setText("Merging segments...")
        self.log("Concatenating segments (lossless copy)...")
        cmd, list_file = self.processor.build_concat_command(
            self.generated_segments, self.final_output_file, self.config)
        self._concat_list = list_file
        self._spawn(cmd, self.concat_finished)

    def _spawn(self, cmd, on_finish):
        self.current_process = QProcess()
        self.current_process.finished.connect(on_finish)
        self.current_process.readyReadStandardError.connect(self.handle_stderr)
        self.current_process.errorOccurred.connect(
            lambda e: self.log(f"Process error: {e}"))
        self.current_process.start(cmd[0], cmd[1:])

    # --------------------------- 진행/완료 --------------------------- #
    def handle_stderr(self):
        data = bytes(self.current_process.readAllStandardError()).decode("utf8", "ignore")
        m = re.search(r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})", data)
        if m and self.total_output_secs > 0:
            h, mn, s = map(float, m.groups())
            cur = min(self.current_event_secs, h * 3600 + mn * 60 + s)
            pct = (self.done_output_secs + cur) / self.total_output_secs * 100
            self.progress_bar.setValue(int(min(99, pct)))

    def process_finished(self, exit_code, _status):
        if exit_code != 0:
            self.log(f"⚠️  FFmpeg exited {exit_code}")
        self.done_output_secs += self.current_event_secs
        self.progress_bar.setValue(
            int(min(99, self.done_output_secs / max(1e-6, self.total_output_secs) * 100)))
        self.process_next()

    def concat_finished(self, exit_code, _status):
        self.log(f"Concat finished (exit {exit_code})")
        if getattr(self, "_concat_list", None) and os.path.exists(self._concat_list):
            os.remove(self._concat_list)
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.log("Cleaned up temp segments.")
        self.finish_processing()

    def finish_processing(self):
        try:
            self.processor.cleanup_temp()
        except Exception as e:
            self.log(f"cleanup_temp failed: {e}")
        self.progress_bar.setValue(100)
        self.lbl_status.setText("✅ Processing Complete!")
        self.btn_process.setEnabled(True)
        self.btn_select_dir.setEnabled(True)
        QMessageBox.information(self, "Done", "Processing complete.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
