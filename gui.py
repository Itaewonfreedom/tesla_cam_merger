import sys
import os
import json
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QFileDialog, QListWidget, 
                             QLabel, QComboBox, QProgressBar, QMessageBox, QTextEdit)
from PyQt6.QtCore import QProcess, Qt
from processor import TeslaDashcamProcessor

import shutil
import tempfile

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tesla Dashcam Merger")
        self.resize(800, 600)

        self.processor = TeslaDashcamProcessor()
        self.current_directory = ""
        self.events = {}
        self.queue = []
        self.total_queue = 0
        self.current_process = None
        
        # Merge Logic
        self.merge_mode = False
        self.temp_dir = None
        self.generated_segments = []
        self.final_output_file = ""
        
        # Settings
        self.settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        self.settings = self.load_settings()
        self.last_output_dir = self.settings.get("last_output_dir", os.getcwd())

        self.init_ui()

    def load_settings(self):
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r") as f:
                    return json.load(f)
            except:
                pass
        return {}

    def save_settings(self):
        self.settings["last_output_dir"] = self.last_output_dir
        try:
            with open(self.settings_file, "w") as f:
                json.dump(self.settings, f)
        except Exception as e:
            self.log(f"Failed to save settings: {e}")

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Top controls
        top_layout = QHBoxLayout()
        
        self.btn_select_dir = QPushButton("Select Folder")
        self.btn_select_dir.clicked.connect(self.select_directory)
        top_layout.addWidget(self.btn_select_dir)

        self.lbl_count = QLabel("Found: 0 events")
        top_layout.addWidget(self.lbl_count)
        
        layout.addLayout(top_layout)

        # Event List
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list_widget)

        # Encoding Options
        options_layout = QHBoxLayout()
        options_layout.addWidget(QLabel("Encoding:"))
        self.combo_encoding = QComboBox()
        self.combo_encoding.addItems(["hevc_videotoolbox (GPU)", "libx264 (CPU)"])
        options_layout.addWidget(self.combo_encoding)
        
        options_layout.addWidget(QLabel("Bitrate:"))
        self.combo_bitrate = QComboBox()
        self.combo_bitrate.addItems(["5M", "8M", "10M", "15M", "20M", "30M"])
        self.combo_bitrate.setCurrentText("20M")
        options_layout.addWidget(self.combo_bitrate)
        
        layout.addLayout(options_layout)

        # Process Controls
        self.btn_process = QPushButton("Process Selected")
        self.btn_process.clicked.connect(self.start_processing)
        self.btn_process.setEnabled(False)
        layout.addWidget(self.btn_process)

        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        
        self.lbl_status = QLabel("Ready")
        layout.addWidget(self.lbl_status)
        
        # Log Window
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)

    def log(self, message):
        self.log_text.append(message)
        # Scroll to bottom
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def select_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Tesla Dashcam Folder")
        if directory:
            self.current_directory = directory
            self.scan_directory()

    def scan_directory(self):
        self.events = self.processor.find_events(self.current_directory)
        self.list_widget.clear()
        
        sorted_timestamps = sorted(self.events.keys())
        for ts in sorted_timestamps:
            self.list_widget.addItem(ts)
            
        self.lbl_count.setText(f"Found: {len(self.events)} events")
        self.btn_process.setEnabled(len(self.events) > 0)
        self.lbl_status.setText(f"Loaded {len(self.events)} events from {os.path.basename(self.current_directory)}")
        self.log(f"Scanned {self.current_directory}. Found {len(self.events)} valid events.")

    def start_processing(self):
        selected_items = self.list_widget.selectedItems()
        if not selected_items:
            if self.list_widget.count() > 0:
                reply = QMessageBox.question(self, "Process All?", "No events selected. Process ALL events?", 
                                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply == QMessageBox.StandardButton.Yes:
                    selected_items = [self.list_widget.item(i) for i in range(self.list_widget.count())]
                else:
                    return
            else:
                return

        # Determine Output Location and Mode
        self.generated_segments = []
        self.merge_mode = False
        self.temp_dir = None
        
        if len(selected_items) > 1:
            # Multiple files -> Ask if merge or batch
            reply = QMessageBox.question(self, "Merge Files?", "Multiple events selected.\nDo you want to MERGE them into a single video file?", 
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            
            if reply == QMessageBox.StandardButton.Yes:
                self.merge_mode = True
                # Ask for single output file
                default_name = os.path.join(self.last_output_dir, f"merged_video.mp4")
                file_path, _ = QFileDialog.getSaveFileName(self, "Save Merged Video", default_name, "MP4 Files (*.mp4)")
                if not file_path:
                    return
                self.final_output_file = file_path
                self.last_output_dir = os.path.dirname(file_path)
                self.save_settings()
                
                # Create temp dir
                self.temp_dir = tempfile.mkdtemp(prefix="tesla_merge_")
                self.log(f"Created temp dir: {self.temp_dir}")
                
                self.queue = []
                for item in selected_items:
                    ts = item.text()
                    out_path = os.path.join(self.temp_dir, f"segment_{ts}.mp4")
                    self.queue.append((ts, out_path))
                    
            else:
                # Batch mode -> Ask for directory
                dir_path = QFileDialog.getExistingDirectory(self, "Select Output Folder", self.last_output_dir)
                if not dir_path:
                    return
                self.last_output_dir = dir_path
                self.save_settings()
                
                self.queue = []
                for item in selected_items:
                    ts = item.text()
                    out_path = os.path.join(dir_path, f"merged_{ts}.mp4")
                    self.queue.append((ts, out_path))
        else:
            # Single file
            ts = selected_items[0].text()
            default_name = os.path.join(self.last_output_dir, f"merged_{ts}.mp4")
            file_path, _ = QFileDialog.getSaveFileName(self, "Save Video", default_name, "MP4 Files (*.mp4)")
            if not file_path:
                return
            
            self.queue = [(ts, file_path)]
            self.last_output_dir = os.path.dirname(file_path)
            self.save_settings()

        self.total_queue = len(self.queue)
        if self.merge_mode:
            self.total_queue += 1 # Add concat step
            
        self.progress_bar.setValue(0)
        self.btn_process.setEnabled(False)
        self.btn_select_dir.setEnabled(False)
        
        self.log(f"Starting processing of {len(self.queue)} events (Merge: {self.merge_mode})...")
        self.process_next()

    def process_next(self):
        if not self.queue:
            if self.merge_mode:
                self.start_concat()
            else:
                self.finish_processing()
            return

        timestamp, output_file = self.queue.pop(0)
        self.lbl_status.setText(f"Processing {timestamp} ({self.total_queue - len(self.queue) - (1 if self.merge_mode else 0)}/{self.total_queue})")
        
        if self.merge_mode:
            self.generated_segments.append(output_file)
        
        encoding_choice = self.combo_encoding.currentText().split(" ")[0] # "libx264" or "hevc_videotoolbox"
        bitrate = self.combo_bitrate.currentText()
        
        self.log(f"Generating command for {timestamp} -> {os.path.basename(output_file)}...")
        try:
            cmd = self.processor.generate_ffmpeg_command(timestamp, output_file, encoding_choice, bitrate)
        except Exception as e:
            self.log(f"Error generating command: {e}")
            self.process_next()
            return
        
        if cmd:
            self.log(f"Command: {' '.join(cmd)}")
            self.current_process = QProcess()
            self.current_process.finished.connect(self.process_finished)
            self.current_process.readyReadStandardOutput.connect(self.handle_stdout)
            self.current_process.readyReadStandardError.connect(self.handle_stderr)
            self.current_process.errorOccurred.connect(self.handle_process_error)
            self.current_process.start(cmd[0], cmd[1:])
        else:
            self.log(f"Failed to generate command for {timestamp}")
            self.process_next()

    def start_concat(self):
        self.lbl_status.setText(f"Merging segments...")
        self.log("Starting concatenation...")
        
        cmd, list_file = self.processor.generate_concat_command(self.generated_segments, self.final_output_file)
        
        self.log(f"Concat Command: {' '.join(cmd)}")
        self.current_process = QProcess()
        self.current_process.finished.connect(self.concat_finished)
        self.current_process.readyReadStandardOutput.connect(self.handle_stdout)
        self.current_process.readyReadStandardError.connect(self.handle_stderr)
        self.current_process.start(cmd[0], cmd[1:])

    def concat_finished(self, exit_code, exit_status):
        self.log(f"Concat finished with exit code {exit_code}")
        
        # Cleanup temp dir
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                self.log(f"Cleaned up temp dir: {self.temp_dir}")
            except Exception as e:
                self.log(f"Failed to cleanup temp dir: {e}")
                
        self.finish_processing()

    def handle_stdout(self):
        data = self.current_process.readAllStandardOutput()
        stdout = bytes(data).decode("utf8")
        self.log(f"[FFmpeg OUT] {stdout}")

    def handle_stderr(self):
        data = self.current_process.readAllStandardError()
        stderr = bytes(data).decode("utf8")
        self.log(f"[FFmpeg ERR] {stderr}")
        
        # Parse progress
        import re
        match = re.search(r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})", stderr)
        if match:
            h, m, s = map(float, match.groups())
            seconds = h * 3600 + m * 60 + s
            # Assuming 60s per clip
            total_duration = 60.0
            percent_of_file = min(1.0, seconds / total_duration)
            
            # Global progress
            if self.merge_mode and not self.queue: # Concat phase
                # Concat is fast, just show 99% or something
                # Or we can track it if we know total duration.
                # Let's just assume it's the last step.
                files_done = len(self.generated_segments)
                total_progress = (files_done + percent_of_file) / self.total_queue * 100
            else:
                files_done = self.total_queue - len(self.queue) - (1 if self.merge_mode else 0) - 1
                total_progress = (files_done + percent_of_file) / self.total_queue * 100
            
            self.progress_bar.setValue(int(total_progress))

    def handle_process_error(self, error):
        self.log(f"Process Error: {error}")

    def process_finished(self, exit_code, exit_status):
        self.log(f"Process finished with exit code {exit_code}, status {exit_status}")
        # Update progress bar to next step
        if self.merge_mode:
             completed = len(self.generated_segments)
        else:
             completed = self.total_queue - len(self.queue)
             
        progress = int((completed / self.total_queue) * 100)
        self.progress_bar.setValue(progress)
        
        self.process_next()

    def finish_processing(self):
        # 자막 PNG 임시 폴더 정리
        try:
            self.processor.cleanup_temp()
        except Exception as e:
            self.log(f"Failed to cleanup timestamp frames: {e}")
        self.lbl_status.setText("Processing Complete!")
        self.btn_process.setEnabled(True)
        self.btn_select_dir.setEnabled(True)
        self.progress_bar.setValue(100)
        QMessageBox.information(self, "Done", "Processing complete.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
