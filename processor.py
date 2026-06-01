import os
import re
import shutil
import datetime
from pathlib import Path

# 타임스탬프 자막에 사용할 폰트 (Pillow가 직접 렌더링)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
FONT_SIZE = 48

class TeslaDashcamProcessor:
    def __init__(self):
        self.events = {}
        # 자막 PNG 시퀀스를 만든 임시 폴더 목록. 처리 후 cleanup_temp()으로 정리.
        self.temp_frame_dirs = []

    def find_events(self, directory):
        """
        Scans the directory for Tesla dashcam files and groups them by timestamp.
        Returns a dictionary of events: {timestamp_str: {camera: filepath}}
        """
        self.events = {}
        # Regex to capture timestamp and camera angle
        # Example: 2025-11-29_16-42-04-front.mp4
        pattern = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-([a-z_]+)\.mp4")

        try:
            files = sorted([f for f in os.listdir(directory) if f.endswith(".mp4")])
        except FileNotFoundError:
            return {}

        for filename in files:
            match = pattern.match(filename)
            if match:
                timestamp = match.group(1)
                camera = match.group(2)
                
                # Normalize camera names if needed (though user specified standard ones)
                # -front, -left_repeater, -back, -right_repeater
                
                if timestamp not in self.events:
                    self.events[timestamp] = {}
                
                self.events[timestamp][camera] = os.path.join(directory, filename)

        # Filter out incomplete events (must have all 4 angles)
        valid_events = {}
        required_cameras = {"front", "left_repeater", "back", "right_repeater"}
        
        for timestamp, cameras in self.events.items():
            if required_cameras.issubset(cameras.keys()):
                valid_events[timestamp] = cameras
        
        self.events = valid_events
        return self.events

    def get_start_epoch(self, timestamp_str):
        """
        Converts timestamp string (YYYY-MM-DD_hh-mm-ss) to epoch.
        Subtracts 60 seconds as per requirement.
        """
        dt = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d_%H-%M-%S")
        # Subtract 1 minute
        dt = dt - datetime.timedelta(minutes=1)
        return int(dt.timestamp())

    def get_video_info(self, filepath):
        """
        Returns (width, height, duration) of the video using ffprobe.
        """
        import subprocess
        import json
        
        cmd = [
            "ffprobe", 
            "-v", "error", 
            "-select_streams", "v:0", 
            "-show_entries", "stream=width,height,duration", 
            "-of", "json", 
            filepath
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            stream = data["streams"][0]
            width = int(stream["width"])
            height = int(stream["height"])
            # Duration might be in stream or format. Stream duration is often present.
            # If not in stream, check format? But we selected stream.
            # Usually stream duration is accurate for video.
            duration = float(stream.get("duration", 60.0)) 
            return width, height, duration
        except Exception as e:
            print(f"Error probing {filepath}: {e}")
            return 1280, 960, 60.0 # Fallback

    def _render_timestamp_frames(self, start_epoch, duration, bar_width, output_file):
        """
        매 초 갱신되는 타임스탬프 자막을 PNG 시퀀스로 렌더링한다.
        ffmpeg의 drawtext(freetype 의존)를 쓸 수 없는 환경을 위해 Pillow로 직접 그린다.
        Pillow는 freetype를 자체 번들하므로 시스템 ffmpeg 빌드와 무관하게 동작한다.

        반환: (프레임 폴더 경로, 자막 바 높이). 이 폴더는 -framerate 1 입력으로 쓰인다.
        """
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
        bar_h = FONT_SIZE + 20  # 위아래 여백 10px

        # 출력 파일 옆 숨김 임시 폴더에 프레임 생성
        frame_dir = os.path.join(
            os.path.dirname(os.path.abspath(output_file)),
            "." + os.path.splitext(os.path.basename(output_file))[0] + "_ts",
        )
        if os.path.exists(frame_dir):
            shutil.rmtree(frame_dir)
        os.makedirs(frame_dir, exist_ok=True)
        self.temp_frame_dirs.append(frame_dir)

        # overlay eof 대비 약간 여유를 두고 생성 (duration 올림 + 2초)
        total_seconds = int(duration) + 2
        for i in range(total_seconds):
            ts = datetime.datetime.fromtimestamp(start_epoch + i, tz=datetime.timezone.utc)
            text = ts.strftime("%Y-%m-%d %H:%M:%S")

            img = Image.new("RGBA", (bar_width, bar_h), (0, 0, 0, 128))
            draw = ImageDraw.Draw(img)
            tb = draw.textbbox((0, 0), text, font=font)
            tw = tb[2] - tb[0]
            draw.text(((bar_width - tw) / 2, 6), text, font=font, fill=(255, 255, 255, 255))
            img.save(os.path.join(frame_dir, f"f_{i:05d}.png"))

        return frame_dir, bar_h

    def cleanup_temp(self):
        """생성된 자막 PNG 임시 폴더들을 정리한다 (best-effort)."""
        for d in self.temp_frame_dirs:
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        self.temp_frame_dirs = []

    def generate_ffmpeg_command(self, timestamp, output_file, encoding="libx264", bitrate="10M"):
        """
        Generates the FFmpeg command to merge the videos.
        """
        if timestamp not in self.events:
            return None

        cameras = self.events[timestamp]
        
        # Input files
        input_front = cameras["front"]
        input_left = cameras["left_repeater"]
        input_back = cameras["back"]
        input_right = cameras["right_repeater"]

        # Get dimensions and duration of front video
        front_w, front_h, duration = self.get_video_info(input_front)
        
        # Calculate start epoch
        # User requested: Filename Time - 1 minute (for 1 min video).
        # Generalized: Filename Time - Duration.
        # Filename timestamp is assumed to be the END time of the clip.
        # FIX: Treat parsed time as UTC to ensure gmtime displays the filename time exactly.
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S")
        dt = dt.replace(tzinfo=datetime.timezone.utc)
        end_epoch = dt.timestamp()
        start_epoch = int(end_epoch - duration)
        
        # Calculate target width for bottom videos (1/3 of front width)
        # Ensure even width
        target_w = int((front_w / 3) // 2 * 2)
        
        # Encoding options
        if encoding == "hevc_videotoolbox":
            # Mac Hardware Acceleration
            enc_params = ["-c:v", "hevc_videotoolbox", "-b:v", bitrate, "-tag:v", "hvc1", "-allow_sw", "1"]
        else:
            # CPU Encoding
            enc_params = ["-c:v", "libx264", "-b:v", bitrate, "-preset", "medium"]

        # Common options for stability
        # Tesla footage is VFR ~36fps. Force CFR 30fps for smooth playback and compatibility.
        # -pix_fmt yuv420p ensures compatibility with all players (QuickTime, etc).
        common_params = ["-r", "30", "-fps_mode", "cfr", "-pix_fmt", "yuv420p"]

        # 합성 결과 캔버스 폭: front 폭(위쪽) vs 하단 3분할 폭(target_w*3) 중 더 큰 값.
        # target_w = front_w/3을 짝수로 내림했으므로 보통 front_w와 같거나 1~2px 작다.
        canvas_w = max(front_w, target_w * 3)

        # 매 초 갱신되는 타임스탬프 자막을 Pillow로 PNG 시퀀스 렌더링 (drawtext 대체)
        frame_dir, bar_h = self._render_timestamp_frames(
            start_epoch, duration, canvas_w, output_file
        )
        ts_input = os.path.join(frame_dir, "f_%05d.png")

        # Filter Complex
        # - 하단 3개 영상을 target_w로 스케일
        # - xstack으로 2행 배치 (위: front / 아래: left|back|right)
        # - 자막 PNG 시퀀스(입력 4번)를 하단 중앙에 overlay
        filter_complex = (
            f"[1:v]scale=w={target_w}:h=-1[left];"
            f"[2:v]scale=w={target_w}:h=-1[back];"
            f"[3:v]scale=w={target_w}:h=-1[right];"
            f"[0:v][left][back][right]xstack=inputs=4:layout=0_0|0_{front_h}|w1_{front_h}|w1+w2_{front_h}[stacked];"
            f"[stacked][4:v]overlay=x=(W-w)/2:y=H-h-10[outv]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", input_front,
            "-i", input_left,
            "-i", input_back,
            "-i", input_right,
            "-framerate", "1", "-i", ts_input,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "0:a?",
        ] + enc_params + common_params + [output_file]

        return cmd

    def generate_concat_command(self, file_list, output_file):
        """
        Generates the FFmpeg command to concatenate a list of video files.
        Creates a temporary text file listing the inputs.
        """
        # Create list file
        list_file = output_file + ".txt"
        with open(list_file, "w") as f:
            for video in file_list:
                # Escape single quotes
                safe_path = video.replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")
        
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            output_file
        ]
        
        return cmd, list_file

if __name__ == "__main__":
    # Simple test
    proc = TeslaDashcamProcessor()
    # Assuming we run this where the folders are
    # events = proc.find_events("/Users/seunghyunjang/Movies/Work/tesla/2025-11-29_16-53-04")
    # print(f"Found {len(events)} events")
