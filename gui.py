"""
Tesla Dashcam Merger — PyQt6 GUI.

폴더를 선택해 이벤트를 목록으로 보고, 선택한 이벤트의 원본 클립을 앵글별로 미리 재생할 수
있다(기본 FRONT). 레이아웃/인코딩/해상도/배속 등 옵션을 골라 개별 변환하거나 하나로 머지한다.
영상 처리 명령은 processor.py가 생성하고, 실행은 QProcess로 비동기 수행한다.
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
                             QMessageBox, QTextEdit, QCheckBox, QGroupBox,
                             QSplitter, QSlider, QStyle)
from PyQt6.QtCore import QProcess, QUrl, Qt, QEvent
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

from processor import (TeslaDashcamProcessor, MergeConfig, CAMERA_LABELS,
                       LAYOUTS, RESOLUTION_PRESETS)

# 미리보기 앵글 선택 순서 (가진 카메라만 노출)
ANGLE_ORDER = ["front", "back", "left_repeater", "right_repeater",
               "left_pillar", "right_pillar"]


def _fmt_time(ms):
    s = max(0, ms) // 1000
    return f"{s // 60:02}:{s % 60:02}"


class AspectRatioWidget(QWidget):
    """단일 자식 위젯을 지정 비율(기본 16:9)로 중앙에 레터박스 배치한다."""

    def __init__(self, inner, ratio=16 / 9):
        super().__init__()
        self._ratio = ratio
        self._inner = inner
        inner.setParent(self)
        self.setStyleSheet("background:#000;")

    def resizeEvent(self, _e):
        w, h = self.width(), self.height()
        if h <= 0:
            return
        if w / h > self._ratio:          # 너무 넓음 → 폭을 비율에 맞춤
            iw, ih = int(h * self._ratio), h
        else:                            # 너무 높음 → 높이를 비율에 맞춤
            iw, ih = w, int(w / self._ratio)
        self._inner.setGeometry((w - iw) // 2, (h - ih) // 2, iw, ih)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tesla Dashcam Merger")
        self.resize(1080, 760)

        self.processor = TeslaDashcamProcessor()
        self.current_directory = ""
        self.events = {}

        # 처리 큐 / 상태
        self.queue = []
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

        # 미리보기 상태
        self.preview_event = None
        self._user_seeking = False
        self._pending_pos = 0
        self._want_play = False
        self._was_playing = False

        self.settings_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "settings.json")
        self.settings = self.load_settings()
        self.last_output_dir = self.settings.get("last_output_dir", os.getcwd())

        self.init_ui()

        # 전역 단축키: Esc=종료, Space=재생/일시정지 (포커스 위치와 무관하게 동작)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.close()
                return True
            if key == Qt.Key.Key_Space and not event.isAutoRepeat():
                self.toggle_play()
                return True
        return super().eventFilter(obj, event)

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

        # 가운데: [ 이벤트 목록 | 미리보기 ] 스플리터
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("Events"))
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list_widget.currentItemChanged.connect(self.on_current_changed)
        lv.addWidget(self.list_widget)
        splitter.addWidget(left)

        splitter.addWidget(self._build_preview())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, stretch=3)

        # 옵션
        root.addWidget(self._build_options())

        # 처리 버튼 + 진행률 + 로그
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
        self.log_text.setMaximumHeight(140)
        root.addWidget(self.log_text)

    def _build_preview(self):
        box = QGroupBox("Preview — original clip")
        v = QVBoxLayout(box)

        # 16:9 레터박스 컨테이너 안에 비디오 위젯 배치
        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background:#000;")
        self.video_frame = AspectRatioWidget(self.video_widget, 16 / 9)
        self.video_frame.setMinimumSize(480, 270)
        v.addWidget(self.video_frame, stretch=1)

        # 앵글 선택
        angle_row = QHBoxLayout()
        angle_row.addWidget(QLabel("Angle:"))
        self.combo_angle = QComboBox()
        self.combo_angle.currentIndexChanged.connect(self.on_angle_changed)
        angle_row.addWidget(self.combo_angle, stretch=1)
        v.addLayout(angle_row)

        # 트랜스포트
        trans = QHBoxLayout()
        self.btn_play = QPushButton()
        self.btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.btn_play.clicked.connect(self.toggle_play)
        trans.addWidget(self.btn_play)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(self.on_slider_pressed)
        self.slider.sliderMoved.connect(self.on_slider_moved)
        self.slider.sliderReleased.connect(self.on_slider_released)
        trans.addWidget(self.slider, stretch=1)
        self.lbl_time = QLabel("00:00 / 00:00")
        trans.addWidget(self.lbl_time)
        v.addLayout(trans)

        # 플레이어
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(self.on_duration)
        self.player.playbackStateChanged.connect(self.on_playback_state)
        self.player.mediaStatusChanged.connect(self.on_media_status)

        self._set_preview_enabled(False)
        return box

    def _build_options(self):
        box = QGroupBox("Output Options")
        grid = QGridLayout(box)

        self.combo_layout = QComboBox()
        self.combo_layout.addItems(list(LAYOUTS))
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
        self.player.stop()
        self.player.setSource(QUrl())
        self._set_preview_enabled(False)
        self.preview_event = None

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
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)

    # --------------------------- 미리보기 --------------------------- #
    def _set_preview_enabled(self, on):
        self.combo_angle.setEnabled(on)
        self.btn_play.setEnabled(on)
        self.slider.setEnabled(on)

    def _item_timestamp(self, item):
        return item.text().split()[0]

    def on_current_changed(self, current, _previous):
        if current is None:
            return
        ts = self._item_timestamp(current)
        if ts == self.preview_event:
            return
        self.preview_event = ts
        self._populate_angles(ts)
        self._load_preview(reset=True)

    def _populate_angles(self, ts):
        cams = self.events.get(ts, {})
        self.combo_angle.blockSignals(True)
        self.combo_angle.clear()
        for cam in ANGLE_ORDER:
            if cam in cams:
                self.combo_angle.addItem(CAMERA_LABELS.get(cam, cam.upper()), cam)
        # 기본값 FRONT
        idx = self.combo_angle.findData("front")
        self.combo_angle.setCurrentIndex(max(0, idx))
        self.combo_angle.blockSignals(False)

    def on_angle_changed(self, _idx):
        # 앵글만 바꿀 때는 현재 위치/재생상태 유지
        self._load_preview(reset=False)

    def _load_preview(self, reset):
        if not self.preview_event:
            return
        cam = self.combo_angle.currentData()
        cams = self.events.get(self.preview_event, {})
        if not cam or cam not in cams:
            return
        self._want_play = (not reset) and \
            (self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        self._pending_pos = 0 if reset else self.player.position()
        self._set_preview_enabled(True)
        self.player.setSource(QUrl.fromLocalFile(os.path.abspath(cams[cam])))

    def on_media_status(self, status):
        loaded = (QMediaPlayer.MediaStatus.LoadedMedia,
                  QMediaPlayer.MediaStatus.BufferedMedia)
        if status in loaded:
            if self._pending_pos:
                self.player.setPosition(self._pending_pos)
                self._pending_pos = 0
            if self._want_play:
                self.player.play()
            else:
                # 일시정지 상태로 첫 프레임을 보여주기
                self.player.play()
                self.player.pause()

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def on_playback_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        icon = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        self.btn_play.setIcon(self.style().standardIcon(icon))

    def on_duration(self, dur):
        self.slider.setRange(0, dur)
        self.lbl_time.setText(f"{_fmt_time(self.player.position())} / {_fmt_time(dur)}")

    def on_position(self, pos):
        if not self._user_seeking:
            self.slider.setValue(pos)
        self.lbl_time.setText(f"{_fmt_time(pos)} / {_fmt_time(self.player.duration())}")

    def on_slider_pressed(self):
        # 드래그 시작: 재생 중이었는지 기억하고, 매끄러운 탐색을 위해 잠시 일시정지
        self._user_seeking = True
        self._was_playing = (
            self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        if self._was_playing:
            self.player.pause()

    def on_slider_moved(self, pos):
        # 드래그하는 동안 해당 프레임을 실시간으로 미리보기
        self.player.setPosition(pos)
        self.lbl_time.setText(f"{_fmt_time(pos)} / {_fmt_time(self.player.duration())}")

    def on_slider_released(self):
        self._user_seeking = False
        self.player.setPosition(self.slider.value())
        # 재생 중에 긁었으면 놓은 뒤 자동으로 이어서 재생
        if getattr(self, "_was_playing", False):
            self.player.play()
        self._was_playing = False

    # --------------------------- 설정 수집 --------------------------- #
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
