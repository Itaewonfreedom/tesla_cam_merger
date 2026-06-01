import os
import re
import datetime
from pathlib import Path

class TeslaDashcamProcessor:
    def __init__(self):
        self.events = {}

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

        # Filter Complex
        # Scale bottom 3 videos to target_w
        
        filter_complex = (
            f"[1:v]scale=w={target_w}:h=-1[left];"
            f"[2:v]scale=w={target_w}:h=-1[back];"
            f"[3:v]scale=w={target_w}:h=-1[right];"
            f"[0:v][left][back][right]xstack=inputs=4:layout=0_0|0_{front_h}|w1_{front_h}|w1+w2_{front_h}[stacked];"
            f"[stacked]drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:text='%{{pts\\:gmtime\\:{start_epoch}}}':"
            f"fontsize=48:fontcolor=white:box=1:boxcolor=black@0.5:x=(w-text_w)/2:y={front_h}-text_h-10[outv]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", input_front,
            "-i", input_left,
            "-i", input_back,
            "-i", input_right,
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
