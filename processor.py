"""
Tesla Dashcam Merger — core processing logic.

테슬라 블랙박스 클립(카메라별 mp4)을 하나의 멀티뷰 영상으로 합성하는 핵심 로직.
GUI(gui.py)와 CLI(cli.py) 양쪽에서 공용으로 사용한다. 외부 의존성은 FFmpeg/ffprobe와
Pillow 뿐이며, 텍스트 자막은 Pillow로 PNG를 렌더링해 overlay하므로 FFmpeg 빌드에
freetype(drawtext)가 없어도 동작한다.
"""

import os
import re
import json
import shutil
import datetime
import subprocess
from dataclasses import dataclass, field

# 자막/라벨 렌더링용 폰트 (Pillow가 직접 렌더링하므로 시스템 FFmpeg 빌드와 무관)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"

# 테슬라가 저장하는 카메라 종류와 표시용 라벨
CAMERA_LABELS = {
    "front": "FRONT",
    "back": "BACK",
    "left_repeater": "LEFT",
    "right_repeater": "RIGHT",
    "left_pillar": "L-PILLAR",
    "right_pillar": "R-PILLAR",
}

# 레이아웃별 필요한 카메라와 격자 배치
# rows: 각 행에 들어갈 카메라 이름 리스트. front는 classic에서 단독 전폭 행.
LAYOUTS = {
    # 클래식 테슬라 뷰: 위 전방(전폭), 아래 좌/후/우 1/3씩
    "classic": {"rows": [["front"], ["left_repeater", "back", "right_repeater"]]},
    # 6분할 2x3: 위 좌필러/전방/우필러, 아래 좌/후/우
    "grid6": {"rows": [["left_pillar", "front", "right_pillar"],
                       ["left_repeater", "back", "right_repeater"]]},
    # 전방만
    "front": {"rows": [["front"]]},
}

# 해상도 프리셋 (출력 가로폭). 0 = 원본 유지
RESOLUTION_PRESETS = {
    "Original": 0,
    "1440p (2560)": 2560,
    "1080p (1920)": 1920,
    "720p (1280)": 1280,
    "480p (854)": 854,
}


def _even(n):
    """짝수로 내림. yuv420p / 대부분 인코더는 짝수 해상도를 요구한다."""
    return int(n) // 2 * 2


@dataclass
class MergeConfig:
    """합성/인코딩 옵션 묶음. GUI·CLI 모두 이 객체를 만들어 processor에 넘긴다."""
    encoding: str = "hevc_videotoolbox"      # hevc_videotoolbox(GPU) | libx264(CPU)
    quality_mode: str = "bitrate"            # bitrate | crf
    bitrate: str = "20M"
    crf: int = 20
    fps: int = 30
    width: int = 1920                        # 출력 가로폭 (0=원본)
    speed: float = 1.0                       # 재생 배속 (타임랩스). 1.0=원본
    layout: str = "classic"                  # classic | grid6 | front
    show_timestamp: bool = True
    timestamp_format: str = "%Y-%m-%d %H:%M:%S"
    show_labels: bool = True                 # 각 화면에 카메라 라벨 표시
    preset: str = "medium"                   # libx264 preset (ultrafast..veryslow)


class TeslaDashcamProcessor:
    def __init__(self):
        self.events = {}
        # 자막/라벨 PNG를 만든 임시 폴더들. 처리 후 cleanup_temp()으로 정리한다.
        self.temp_dirs = []

    # ------------------------------------------------------------------ #
    # 이벤트 탐색
    # ------------------------------------------------------------------ #
    def find_events(self, directory):
        """
        디렉토리를 스캔해 같은 시각의 카메라 클립들을 이벤트로 묶는다.
        반환: {timestamp_str: {camera: filepath}}. front가 있는 이벤트만 유효로 본다.
        """
        self.events = {}
        pattern = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-([a-z_]+)\.mp4$")

        try:
            files = sorted(f for f in os.listdir(directory) if f.endswith(".mp4"))
        except FileNotFoundError:
            return {}

        grouped = {}
        for filename in files:
            m = pattern.match(filename)
            if not m:
                continue
            ts, cam = m.group(1), m.group(2)
            grouped.setdefault(ts, {})[cam] = os.path.join(directory, filename)

        # front가 있어야 합성의 기준이 되므로 최소 조건으로 둔다.
        self.events = {ts: cams for ts, cams in grouped.items() if "front" in cams}
        return self.events

    def available_layouts(self, timestamp):
        """해당 이벤트가 지원하는 레이아웃 목록(가진 카메라 기준)."""
        cams = set(self.events.get(timestamp, {}).keys())
        return [name for name, spec in LAYOUTS.items()
                if all(c in cams for row in spec["rows"] for c in row)]

    def read_event_meta(self, directory):
        """event.json이 있으면 (reason, timestamp, lat, lon)을 읽어 반환, 없으면 None."""
        path = os.path.join(directory, "event.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 영상 정보
    # ------------------------------------------------------------------ #
    def get_video_info(self, filepath):
        """(width, height, duration) 반환. ffprobe 실패 시 합리적 기본값."""
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", filepath,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            stream = data["streams"][0]
            width = int(stream["width"])
            height = int(stream["height"])
            duration = float(data.get("format", {}).get("duration", 60.0))
            return width, height, duration
        except Exception as e:
            print(f"Error probing {filepath}: {e}")
            return 2896, 1876, 60.0

    def event_duration(self, timestamp):
        """이벤트(front 기준) 길이 초. 진행률·합계 계산용."""
        cams = self.events.get(timestamp)
        if not cams:
            return 0.0
        return self.get_video_info(cams["front"])[2]

    # ------------------------------------------------------------------ #
    # 레이아웃 기하 계산
    # ------------------------------------------------------------------ #
    def _compute_layout(self, cams, layout_name, base_w, base_h):
        """
        레이아웃에 따른 타일 배치를 계산한다.
        반환: (ordered_cams, tiles, canvas_w, canvas_h)
          - ordered_cams: xstack 입력 순서대로의 카메라 이름
          - tiles: [(cam, x, y, w, h)] 네이티브 좌표 기준
        """
        spec = LAYOUTS[layout_name]
        rows = spec["rows"]

        ordered, tiles = [], []

        if layout_name == "classic":
            # 위: front 전폭 / 아래: 3분할
            tw = _even(base_w / 3)
            th = _even(base_h * tw / base_w)
            ordered = ["front", "left_repeater", "back", "right_repeater"]
            tiles.append(("front", 0, 0, base_w, base_h))
            tiles.append(("left_repeater", 0, base_h, tw, th))
            tiles.append(("back", tw, base_h, tw, th))
            tiles.append(("right_repeater", 2 * tw, base_h, tw, th))
            canvas_w = max(base_w, 3 * tw)
            canvas_h = base_h + th
        elif layout_name == "front":
            ordered = ["front"]
            tiles.append(("front", 0, 0, base_w, base_h))
            canvas_w, canvas_h = base_w, base_h
        else:  # grid6 등 균일 격자
            n_cols = max(len(r) for r in rows)
            tw = _even(base_w / n_cols)
            th = _even(base_h * tw / base_w)
            for r, row in enumerate(rows):
                for c, cam in enumerate(row):
                    ordered.append(cam)
                    tiles.append((cam, c * tw, r * th, tw, th))
            canvas_w = n_cols * tw
            canvas_h = len(rows) * th

        return ordered, tiles, canvas_w, canvas_h

    # ------------------------------------------------------------------ #
    # Pillow 오버레이 자산 (자막 시퀀스 + 라벨)
    # ------------------------------------------------------------------ #
    def _make_temp_dir(self, output_file, suffix):
        d = os.path.join(
            os.path.dirname(os.path.abspath(output_file)),
            "." + os.path.splitext(os.path.basename(output_file))[0] + suffix,
        )
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        self.temp_dirs.append(d)
        return d

    def _render_timestamp_frames(self, start_epoch, duration, canvas_w, canvas_h,
                                 fmt, output_file):
        """매 초 갱신되는 타임스탬프를 PNG 시퀀스로 렌더링. (폴더, 바높이) 반환."""
        from PIL import Image, ImageDraw, ImageFont

        font_size = max(20, int(canvas_h * 0.025))
        font = ImageFont.truetype(FONT_PATH, font_size)
        bar_h = font_size + 16

        d = self._make_temp_dir(output_file, "_ts")
        for i in range(int(duration) + 2):
            ts = datetime.datetime.fromtimestamp(start_epoch + i, tz=datetime.timezone.utc)
            text = ts.strftime(fmt)
            img = Image.new("RGBA", (canvas_w, bar_h), (0, 0, 0, 130))
            draw = ImageDraw.Draw(img)
            bb = draw.textbbox((0, 0), text, font=font)
            draw.text(((canvas_w - (bb[2] - bb[0])) / 2, (bar_h - (bb[3] - bb[1])) / 2 - bb[1]),
                      text, font=font, fill=(255, 255, 255, 255))
            img.save(os.path.join(d, f"f_{i:05d}.png"))
        return d, bar_h

    def _render_labels(self, tiles, canvas_w, canvas_h, output_file):
        """각 타일 좌상단에 카메라 라벨을 그린 전체 캔버스 투명 PNG 1장. 경로 반환."""
        from PIL import Image, ImageDraw, ImageFont

        font_size = max(16, int(canvas_h * 0.018))
        font = ImageFont.truetype(FONT_PATH, font_size)
        pad = max(6, font_size // 3)

        img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        for cam, x, y, w, h in tiles:
            label = CAMERA_LABELS.get(cam, cam.upper())
            bb = draw.textbbox((0, 0), label, font=font)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
            # 반투명 박스 + 흰 글자
            draw.rectangle([x + pad, y + pad, x + pad + tw + 2 * pad, y + pad + th + pad],
                           fill=(0, 0, 0, 110))
            draw.text((x + 2 * pad, y + pad + (pad // 2) - bb[1]), label,
                      font=font, fill=(255, 255, 255, 230))

        d = self._make_temp_dir(output_file, "_lbl")
        path = os.path.join(d, "labels.png")
        img.save(path)
        return path

    # ------------------------------------------------------------------ #
    # FFmpeg 명령 생성
    # ------------------------------------------------------------------ #
    def _encode_params(self, config, container):
        """인코더/품질/컨테이너에 따른 출력 파라미터."""
        params = []
        if config.encoding == "hevc_videotoolbox":
            params += ["-c:v", "hevc_videotoolbox", "-allow_sw", "1"]
            if config.quality_mode == "crf":
                # videotoolbox은 CRF 미지원 → q 스케일로 근사
                params += ["-q:v", str(max(1, min(100, 100 - config.crf * 2)))]
            else:
                params += ["-b:v", config.bitrate]
            if container == "mp4":
                params += ["-tag:v", "hvc1"]
        else:  # libx264
            params += ["-c:v", "libx264", "-preset", config.preset]
            if config.quality_mode == "crf":
                params += ["-crf", str(config.crf)]
            else:
                params += ["-b:v", config.bitrate]

        # CFR 강제 + 호환 픽셀 포맷 + 닫힌 GOP(세그먼트 단독 디코드 → 깔끔한 concat)
        params += [
            "-r", str(config.fps),
            "-fps_mode", "cfr",
            "-pix_fmt", "yuv420p",
            "-g", str(config.fps * 2),
            "-keyint_min", str(config.fps),
        ]
        if config.encoding == "libx264":
            params += ["-sc_threshold", "0"]
        return params

    def build_command(self, timestamp, output_file, config, container="mp4"):
        """
        단일 이벤트를 합성하는 FFmpeg 명령을 생성한다.
        container="mpegts"이면 머지용 중간 세그먼트(.ts)를 만든다.
        """
        cams = self.events.get(timestamp)
        if not cams:
            return None

        layout = config.layout
        # 이벤트가 선택 레이아웃을 지원하지 않으면 가능한 것으로 폴백
        if layout not in self.available_layouts(timestamp):
            avail = self.available_layouts(timestamp)
            layout = "classic" if "classic" in avail else (avail[0] if avail else "front")

        base_w, base_h, duration = self.get_video_info(cams["front"])
        ordered, tiles, canvas_w, canvas_h = self._compute_layout(
            cams, layout, base_w, base_h)

        # 타임스탬프 시작 epoch: 파일명 시각을 클립 종료(UTC)로 보고 길이만큼 뺀다
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S")
        dt = dt.replace(tzinfo=datetime.timezone.utc)
        start_epoch = int(dt.timestamp() - duration)

        # 입력 구성
        inputs = []
        for cam in ordered:
            inputs += ["-i", cams[cam]]
        n_cam = len(ordered)

        label_idx = ts_idx = None
        if config.show_labels:
            label_png = self._render_labels(tiles, canvas_w, canvas_h, output_file)
            inputs += ["-i", label_png]
            label_idx = n_cam
        if config.show_timestamp:
            ts_dir, _ = self._render_timestamp_frames(
                start_epoch, duration, canvas_w, canvas_h,
                config.timestamp_format, output_file)
            ts_idx = n_cam + (1 if config.show_labels else 0)
            inputs += ["-framerate", "1", "-i", os.path.join(ts_dir, "f_%05d.png")]

        # 필터그래프: 각 타일 스케일 → xstack → 라벨/타임스탬프 overlay → 스케일/배속/fps
        parts = []
        for i, (cam, x, y, w, h) in enumerate(tiles):
            parts.append(f"[{i}:v]scale={w}:{h}:flags=bicubic,setsar=1[c{i}]")

        if n_cam == 1:
            stage = "[c0]"
        else:
            layout_str = "|".join(f"{x}_{y}" for (_, x, y, _, _) in tiles)
            stage = "[stk]"
            parts.append(
                "".join(f"[c{i}]" for i in range(n_cam))
                + f"xstack=inputs={n_cam}:layout={layout_str}[stk]")

        if label_idx is not None:
            parts.append(f"{stage}[{label_idx}:v]overlay=0:0[lbl]")
            stage = "[lbl]"
        if ts_idx is not None:
            parts.append(
                f"{stage}[{ts_idx}:v]overlay=x=(W-w)/2:y=H-h-10:eof_action=repeat[ts]")
            stage = "[ts]"

        # 마무리 체인: 다운스케일 → 배속(setpts) → fps → 픽셀포맷
        chain = []
        if config.width and config.width < canvas_w:
            chain.append(f"scale={config.width}:-2:flags=bicubic")
        if abs(config.speed - 1.0) > 1e-6:
            chain.append(f"setpts=PTS/{config.speed}")
        chain.append(f"fps={config.fps}")
        chain.append("format=yuv420p")
        parts.append(f"{stage}{','.join(chain)}[outv]")

        filter_complex = ";".join(parts)

        enc = self._encode_params(config, container)
        cmd = ["ffmpeg", "-y", *inputs,
               "-filter_complex", filter_complex,
               "-map", "[outv]", "-an"]
        if container == "mpegts":
            cmd += enc + ["-f", "mpegts", output_file]
        else:
            cmd += enc + ["-movflags", "+faststart", output_file]
        return cmd

    def build_concat_command(self, ts_files, output_file, config):
        """MPEG-TS 세그먼트들을 무손실 copy로 이어붙여 최종 mp4 생성."""
        list_file = output_file + ".concat.txt"
        with open(list_file, "w") as f:
            for v in ts_files:
                f.write(f"file '{v.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")

        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
               "-c", "copy"]
        if config.encoding == "hevc_videotoolbox":
            cmd += ["-tag:v", "hvc1"]
        cmd += ["-movflags", "+faststart", output_file]
        return cmd, list_file

    # ------------------------------------------------------------------ #
    def cleanup_temp(self):
        """생성한 PNG 임시 폴더들 정리 (best-effort)."""
        for d in self.temp_dirs:
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        self.temp_dirs = []


if __name__ == "__main__":
    proc = TeslaDashcamProcessor()
    print("Layouts:", list(LAYOUTS))
