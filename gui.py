"""
Tesla Dashcam Merger — PyQt6 GUI.

다크 테마 기반 UI. 좌측 이벤트 목록, 중앙의 큼직한 16:9 미리보기 플레이어가 중심이고,
인코딩/해상도/배속 등 세부 export 옵션은 별도 설정 창(⚙ Export Settings)으로 분리했다.
메인에서는 "어떤 레이아웃으로 내보낼지"만 고르면 된다. 영상 처리 명령은 processor.py가
생성하고 실행은 QProcess로 비동기 수행한다.
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
                             QSplitter, QSlider, QStyle, QDialog, QDialogButtonBox,
                             QFormLayout, QSizePolicy)
from PyQt6.QtCore import QProcess, QUrl, Qt, QEvent, QTimer
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

from processor import (TeslaDashcamProcessor, MergeConfig, CAMERA_LABELS,
                       LAYOUTS, RESOLUTION_PRESETS)

# 미리보기 앵글 순서 (가진 카메라만 노출)
ANGLE_ORDER = ["front", "back", "left_repeater", "right_repeater",
               "left_pillar", "right_pillar"]

LAYOUT_LABELS = {
    "classic": "Classic — Front + 3",
    "grid6": "6-Camera Grid",
    "front": "Front Only",
}

DEFAULT_OPTS = dict(encoding="hevc_videotoolbox", quality="bitrate", bitrate="20M",
                    crf=20, fps=30, width=1920, speed=1.0, timestamp=True, labels=True)

# ----------------------------- 다크 테마 ----------------------------- #
DARK_QSS = """
QWidget { background:#15171c; color:#e6e8ec; font-size:13px; }
QMainWindow, QDialog { background:#15171c; }
#Header { background:#0e0f13; border-bottom:1px solid #2a2d36; }
QGroupBox { border:1px solid #2a2d36; border-radius:10px; margin-top:14px; padding:10px;
            background:#1a1d24; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 6px; color:#9aa0ad; }
QListWidget { background:#0f1115; border:1px solid #2a2d36; border-radius:10px; padding:4px; }
QListWidget::item { padding:7px 8px; border-radius:6px; }
QListWidget::item:selected { background:#2b6cff; color:#fff; }
QListWidget::item:hover:!selected { background:#20242d; }
QPushButton { background:#23262f; border:1px solid #313542; border-radius:8px;
              padding:7px 14px; color:#e6e8ec; }
QPushButton:hover { background:#2c3140; border-color:#3b4150; }
QPushButton:pressed { background:#1c1f27; }
QPushButton:disabled { color:#5b606c; background:#191b21; }
#Primary { background:#2b6cff; border:none; color:#fff; font-weight:600; padding:9px 18px; }
#Primary:hover { background:#3f7bff; }
#Primary:disabled { background:#28324a; color:#8a92a6; }
QComboBox { background:#0f1115; border:1px solid #313542; border-radius:8px; padding:5px 8px; }
QComboBox:hover { border-color:#3b4150; }
QComboBox QAbstractItemView { background:#1a1d24; selection-background-color:#2b6cff;
                              border:1px solid #313542; outline:none; }
QCheckBox { spacing:8px; }
QCheckBox::indicator { width:16px; height:16px; border-radius:4px; border:1px solid #3b4150;
                       background:#0f1115; }
QCheckBox::indicator:checked { background:#2b6cff; border-color:#2b6cff; }
QProgressBar { background:#0f1115; border:1px solid #2a2d36; border-radius:8px; height:14px;
               text-align:center; color:#cfd3db; }
QProgressBar::chunk { background:#2b6cff; border-radius:7px; }
QTextEdit { background:#0f1115; border:1px solid #2a2d36; border-radius:10px; color:#9aa0ad;
            font-family:Menlo,monospace; font-size:11px; }
QSlider::groove:horizontal { height:6px; background:#2a2d36; border-radius:3px; }
QSlider::sub-page:horizontal { background:#2b6cff; border-radius:3px; }
QSlider::handle:horizontal { background:#fff; width:14px; height:14px; margin:-5px 0;
                             border-radius:7px; }
QLabel { color:#cfd3db; }
QSplitter::handle { background:#2a2d36; width:2px; }
"""


def apply_dark_theme(app):
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#15171c"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#e6e8ec"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#0f1115"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#1a1d24"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#e6e8ec"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#23262f"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#e6e8ec"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#2b6cff"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#1a1d24"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#e6e8ec"))
    app.setPalette(pal)
    app.setStyleSheet(DARK_QSS)


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
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def resizeEvent(self, _e):
        w, h = self.width(), self.height()
        if h <= 0:
            return
        if w / h > self._ratio:
            iw, ih = int(h * self._ratio), h
        else:
            iw, ih = w, int(w / self._ratio)
        self._inner.setGeometry((w - iw) // 2, (h - ih) // 2, iw, ih)


# ----------------------------- 설정 다이얼로그 ----------------------------- #
class ExportSettingsDialog(QDialog):
    """인코딩/해상도/배속/오버레이 등 export 세부 옵션 창."""

    def __init__(self, opts, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Settings")
        self.setMinimumWidth(380)
        form = QFormLayout(self)

        self.combo_encoding = QComboBox()
        self.combo_encoding.addItem("hevc_videotoolbox (GPU)", "hevc_videotoolbox")
        self.combo_encoding.addItem("libx264 (CPU)", "libx264")
        self._select_data(self.combo_encoding, opts["encoding"])

        self.combo_resolution = QComboBox()
        self.combo_resolution.addItems(list(RESOLUTION_PRESETS))
        for name, w in RESOLUTION_PRESETS.items():
            if w == opts["width"]:
                self.combo_resolution.setCurrentText(name)

        self.combo_fps = QComboBox()
        self.combo_fps.addItems(["24", "30", "60"])
        self.combo_fps.setCurrentText(str(opts["fps"]))

        self.combo_speed = QComboBox()
        self.combo_speed.addItems(["1x", "2x", "4x", "8x", "16x"])
        self.combo_speed.setCurrentText(f"{int(opts['speed'])}x")

        self.combo_quality = QComboBox()
        self.combo_quality.addItem("Bitrate", "bitrate")
        self.combo_quality.addItem("Quality (CRF)", "crf")
        self._select_data(self.combo_quality, opts["quality"])
        self.combo_quality.currentIndexChanged.connect(self._sync_quality)

        self.combo_bitrate = QComboBox()
        self.combo_bitrate.addItems(["5M", "8M", "10M", "15M", "20M", "30M", "50M"])
        self.combo_bitrate.setCurrentText(opts["bitrate"])

        self.combo_crf = QComboBox()
        self.combo_crf.addItems([str(x) for x in range(16, 33, 2)])
        self.combo_crf.setCurrentText(str(opts["crf"]))

        self.chk_timestamp = QCheckBox("Burn-in timestamp")
        self.chk_timestamp.setChecked(opts["timestamp"])
        self.chk_labels = QCheckBox("Camera labels")
        self.chk_labels.setChecked(opts["labels"])

        form.addRow("Encoding", self.combo_encoding)
        form.addRow("Resolution", self.combo_resolution)
        form.addRow("FPS", self.combo_fps)
        form.addRow("Speed (timelapse)", self.combo_speed)
        form.addRow("Quality mode", self.combo_quality)
        form.addRow("Bitrate", self.combo_bitrate)
        form.addRow("CRF", self.combo_crf)
        form.addRow(self.chk_timestamp)
        form.addRow(self.chk_labels)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._sync_quality()

    @staticmethod
    def _select_data(combo, data):
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _sync_quality(self):
        crf = self.combo_quality.currentData() == "crf"
        self.combo_crf.setEnabled(crf)
        self.combo_bitrate.setEnabled(not crf)

    def get_opts(self):
        return dict(
            encoding=self.combo_encoding.currentData(),
            quality=self.combo_quality.currentData(),
            bitrate=self.combo_bitrate.currentText(),
            crf=int(self.combo_crf.currentText()),
            fps=int(self.combo_fps.currentText()),
            width=RESOLUTION_PRESETS[self.combo_resolution.currentText()],
            speed=float(self.combo_speed.currentText().rstrip("x")),
            timestamp=self.chk_timestamp.isChecked(),
            labels=self.chk_labels.isChecked(),
        )


# ----------------------------- 메인 윈도우 ----------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tesla Dashcam Merger")
        self.resize(1120, 760)

        self.processor = TeslaDashcamProcessor()
        self.current_directory = ""
        self.events = {}

        self.queue = []
        self.merge_mode = False
        self.temp_dir = None
        self.generated_segments = []
        self.final_output_file = ""
        self.current_process = None
        self.config = None

        self.total_output_secs = 0.0
        self.done_output_secs = 0.0
        self.current_event_secs = 0.0

        self.preview_event = None
        self._user_seeking = False
        self._pending_pos = 0
        self._want_play = False
        self._was_playing = False
        # 재생 의도. 이 백엔드는 setPosition(seek) 시 재생을 멈추므로, 의도가 살아있으면
        # playbackStateChanged에서 재생을 다시 걸어 "스크럽 후 자동 재생"을 보장한다.
        self._play_intent = False
        # 새 소스 로드 중인지 표시. seek는 mediaStatus를 다시 Buffered로 만드는데,
        # 그때 첫 프레임용 play();pause()가 돌면 스크럽 재생이 멈춘다. 이 플래그로 막는다.
        self._loading_source = False
        self._egg_buf = ""

        self.settings_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "settings.json")
        self.settings = self.load_settings()
        self.last_output_dir = self.settings.get("last_output_dir", os.getcwd())
        self.opts = {**DEFAULT_OPTS, **self.settings.get("opts", {})}

        self.init_ui()

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ------------------------- 전역 단축키 / 이스터에그 ------------------------- #
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.close()
                return True
            if key == Qt.Key.Key_Space and not event.isAutoRepeat():
                self.toggle_play()
                return True
            text = event.text().upper()
            if text.isalpha():
                self._egg_buf = (self._egg_buf + text)[-8:]
                if any(self._egg_buf.endswith(k) for k in ("ELON", "MARS", "DOGE")):
                    self._egg_buf = ""
                    self.show_elon_hype()
        return super().eventFilter(obj, event)

    def show_elon_hype(self):
        QMessageBox.information(self, " ", "일론 형, 도지 한 번만 띄워주세요 🐕")

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
        self.settings["opts"] = self.opts
        try:
            with open(self.settings_file, "w") as f:
                json.dump(self.settings, f, indent=2)
        except Exception as e:
            self.log(f"Failed to save settings: {e}")

    # ------------------------------ UI ------------------------------ #
    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(14, 12, 14, 12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_preview())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        bl.addWidget(splitter, stretch=1)

        bl.addWidget(self._build_actionbar())

        self.progress_bar = QProgressBar()
        bl.addWidget(self.progress_bar)
        self.lbl_status = QLabel("Ready — select a folder to begin.")
        bl.addWidget(self.lbl_status)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(110)
        bl.addWidget(self.log_text)

        root.addWidget(body, stretch=1)

    def _build_header(self):
        head = QWidget()
        head.setObjectName("Header")
        h = QHBoxLayout(head)
        h.setContentsMargins(16, 10, 12, 10)
        # 폴더 불러오기 버튼을 왼쪽에 배치 (상단 브랜딩/로켓은 제거)
        self.btn_folder = QPushButton("📁  Select Folder")
        self.btn_folder.clicked.connect(self.select_directory)
        h.addWidget(self.btn_folder)
        h.addStretch()
        return head

    def _build_left(self):
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.lbl_count = QLabel("0 events")
        bar.addWidget(self.lbl_count)
        bar.addStretch()
        self.btn_select_all = QPushButton("Select All")
        self.btn_select_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_select_all.clicked.connect(lambda: self.list_widget.selectAll())
        bar.addWidget(self.btn_select_all)
        lv.addLayout(bar)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list_widget.currentItemChanged.connect(self.on_current_changed)
        lv.addWidget(self.list_widget)
        return left

    def _build_preview(self):
        box = QGroupBox("Preview — original clip")
        v = QVBoxLayout(box)

        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background:#000;")
        self.video_frame = AspectRatioWidget(self.video_widget, 16 / 9)
        self.video_frame.setMinimumSize(480, 270)
        v.addWidget(self.video_frame, stretch=1)

        angle_row = QHBoxLayout()
        angle_row.addWidget(QLabel("Angle:"))
        self.combo_angle = QComboBox()
        self.combo_angle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.combo_angle.currentIndexChanged.connect(self.on_angle_changed)
        angle_row.addWidget(self.combo_angle, stretch=1)
        v.addLayout(angle_row)

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

    def _build_actionbar(self):
        box = QGroupBox("Export")
        h = QHBoxLayout(box)
        h.addWidget(QLabel("Layout:"))
        self.combo_layout = QComboBox()
        for key in LAYOUTS:
            self.combo_layout.addItem(LAYOUT_LABELS.get(key, key), key)
        h.addWidget(self.combo_layout)
        h.addStretch()
        self.btn_settings = QPushButton("⚙  Export Settings…")
        self.btn_settings.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_settings.clicked.connect(self.open_export_settings)
        h.addWidget(self.btn_settings)
        self.btn_process = QPushButton("▶  Export Selected")
        self.btn_process.setObjectName("Primary")
        self.btn_process.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_process.clicked.connect(self.start_processing)
        self.btn_process.setEnabled(False)
        h.addWidget(self.btn_process)
        return box

    def open_export_settings(self):
        dlg = ExportSettingsDialog(self.opts, self)
        if dlg.exec():
            self.opts = dlg.get_opts()
            self.save_settings()
            o = self.opts
            self.log(f"Export settings: {o['encoding']}, "
                     f"{o['width'] or 'orig'}px, {o['fps']}fps, {o['speed']}x, "
                     f"{'CRF '+str(o['crf']) if o['quality']=='crf' else o['bitrate']}")

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
            self.list_widget.addItem(f"{ts}   ({dur:.0f}s · {cams} cams)")

        self.lbl_count.setText(f"{len(self.events)} events"
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
        idx = self.combo_angle.findData("front")
        self.combo_angle.setCurrentIndex(max(0, idx))
        self.combo_angle.blockSignals(False)

    def on_angle_changed(self, _idx):
        self._load_preview(reset=False)

    def _load_preview(self, reset):
        if not self.preview_event:
            return
        cam = self.combo_angle.currentData()
        cams = self.events.get(self.preview_event, {})
        if not cam or cam not in cams:
            return
        self._want_play = (not reset) and self._play_intent
        self._pending_pos = 0 if reset else self.player.position()
        self._set_preview_enabled(True)
        self._loading_source = True
        self.player.setSource(QUrl.fromLocalFile(os.path.abspath(cams[cam])))

    def on_media_status(self, status):
        loaded = (QMediaPlayer.MediaStatus.LoadedMedia,
                  QMediaPlayer.MediaStatus.BufferedMedia)
        # seek도 mediaStatus를 loaded로 되돌리므로, 로드 처리는 '새 소스 로드 시'에만 한다.
        if status in loaded and self._loading_source:
            self._loading_source = False
            if self._pending_pos:
                self.player.setPosition(self._pending_pos)
                self._pending_pos = 0
            if self._want_play:
                self._play_intent = True
                QTimer.singleShot(160, self._resume_if_intended)
                QTimer.singleShot(450, self._resume_if_intended)
            else:
                # 일시정지 상태로 첫 프레임만 보여준다
                self._play_intent = False
                self.player.play()
                self.player.pause()
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._play_intent = False

    def _resume_if_intended(self):
        """재생 의도가 살아있고 드래그 중이 아니며 끝이 아니면 play()를 '한 번' 건다.
        이 백엔드는 seek가 비동기로 일시정지를 유발하므로, seek가 안착한 뒤(딜레이 후)
        호출해야 재생이 유지된다. 빠른 반복 호출은 진행 중 seek와 race가 나므로 피한다."""
        if not self._play_intent or self._user_seeking:
            return
        dur = self.player.duration()
        if dur and self.player.position() >= dur:
            return
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()

    def toggle_play(self):
        if not self.btn_play.isEnabled():
            return
        # 상태(seek로 요동침)가 아니라 "재생 의도"를 기준으로 토글한다.
        if self._play_intent:
            self._play_intent = False
            self.player.pause()
        else:
            self._play_intent = True
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
        # 드래그 시작. "재생 의도"를 기억한다(상태는 seek로 요동치므로 의도로 판단).
        self._user_seeking = True
        self._was_playing = self._play_intent

    def on_slider_moved(self, pos):
        # 드래그하는 동안 해당 프레임을 실시간 미리보기 (seek는 일시정지를 유발하지만
        # _user_seeking 동안에는 재시도하지 않아 프레임만 갱신된다)
        self.player.setPosition(pos)
        self.lbl_time.setText(f"{_fmt_time(pos)} / {_fmt_time(self.player.duration())}")

    def on_slider_released(self):
        self._user_seeking = False
        self.player.setPosition(self.slider.value())
        # 스크럽 전 재생 중이었으면, seek의 비동기 일시정지가 안착한 뒤 재생을 다시 건다.
        # 단발 + 안전망 1회 (rapid 반복은 진행 중 seek와 race가 나므로 사용하지 않음).
        if self._was_playing:
            self._play_intent = True
            QTimer.singleShot(160, self._resume_if_intended)
            QTimer.singleShot(450, self._resume_if_intended)
        self._was_playing = False

    # --------------------------- 설정 수집 --------------------------- #
    def build_config(self):
        o = self.opts
        return MergeConfig(
            encoding=o["encoding"], quality_mode=o["quality"],
            bitrate=o["bitrate"], crf=o["crf"], fps=o["fps"],
            width=o["width"], speed=o["speed"],
            layout=self.combo_layout.currentData(),
            show_timestamp=o["timestamp"], show_labels=o["labels"],
            preset="medium",
        )

    # --------------------------- 처리 시작 --------------------------- #
    def start_processing(self):
        items = self.list_widget.selectedItems()
        if not items:
            if self.list_widget.count() == 0:
                return
            reply = QMessageBox.question(self, "Export All?",
                                         "No events selected. Export ALL events?")
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
        self.btn_folder.setEnabled(False)
        self.log(f"Exporting {len(self.queue)} events (merge={self.merge_mode}, "
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
        self.lbl_status.setText("✅ Export 완료!")
        self.btn_process.setEnabled(True)
        self.btn_folder.setEnabled(True)
        QMessageBox.information(self, "Done", "Export 완료!")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
