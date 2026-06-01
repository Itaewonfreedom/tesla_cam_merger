# Tesla Dashcam Merger

테슬라 블랙박스(Dashcam / Sentry) 영상을 **하나의 멀티뷰 영상으로 합쳐주는** macOS용 앱입니다.
테슬라는 한 시점의 영상을 카메라(전방/후방/좌·우 리피터/좌·우 B필러)별로 따로 저장하는데,
이 앱은 같은 시각의 클립들을 모아 한 화면으로 합성하고, 하단에 **매초 갱신되는 타임스탬프**와
각 화면의 **카메라 라벨**을 새겨 넣습니다.

**GUI(PyQt6)** 와 **CLI** 두 가지로 쓸 수 있고, 실제 영상 처리는 **FFmpeg**가 담당합니다.
GUI에서는 합성 전에 **원본 클립을 앵글별로 미리 재생**해 어떤 이벤트를 내보낼지 확인할 수 있습니다.

---

## 빠른 시작

### 사전 요구사항
- **Python 3.x**
- **FFmpeg / ffprobe** (`brew install ffmpeg`)

### 셋업 & 실행
```bash
./run.sh                      # 가상환경 자동 셋업 + GUI 실행
# 또는 수동
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py      # GUI
```

### CLI
```bash
# 이벤트 목록 (길이/카메라 수/가능한 레이아웃)
.venv/bin/python cli.py <폴더> --list

# 전체를 6분할 1080p로 개별 변환 (GPU 인코딩)
.venv/bin/python cli.py <폴더> --all --layout grid6 --width 1920

# 특정 이벤트들을 하나로 머지하면서 4배속 타임랩스
.venv/bin/python cli.py <폴더> -s 0 -s 1 -s 2 --merge -o drive.mp4 --speed 4

# CPU 인코딩 + 화질(CRF) 모드
.venv/bin/python cli.py <폴더> --all --encoding libx264 --quality crf --crf 20
```
전체 옵션은 `cli.py --help` 참고.

---

## 옵션

| 옵션 | 설명 |
|---|---|
| **Layout** | `classic`(전방+하단3분할), `grid6`(2×3 6분할, B필러 포함), `front`(전방만) |
| **Encoding** | `hevc_videotoolbox`(Mac GPU 가속) / `libx264`(CPU, 호환성↑) |
| **Quality** | `bitrate`(목표 비트레이트) / `crf`(화질 기준, 낮을수록 고화질) |
| **Resolution** | Original / 1440p / 1080p / 720p / 480p |
| **FPS** | 24 / 30 / 60 |
| **Speed** | 1×~16× 타임랩스 (긴 센트리 영상 빠르게 검토) |
| **Timestamp** | 하단 중앙 시각 자막 on/off, 포맷 지정 가능 |
| **Camera labels** | 각 화면 좌상단 카메라 이름(FRONT/BACK/…) on/off |

## 미리보기 (GUI)

이벤트를 클릭하면 우측 패널에서 **원본 클립이 첫 프레임으로 로드**되며(기본 **FRONT**),
**Angle** 드롭다운으로 카메라(전방/후방/좌·우 리피터/좌·우 B필러)를 바꿔 하나씩 볼 수 있습니다.
재생/일시정지 버튼과 탐색 슬라이더, 시간 표시를 제공합니다. 미리보기는 **합성 결과가 아니라
선택한 카메라의 원본 영상**을 그대로 재생하므로 인코딩 없이 즉시 확인됩니다.
(QtMultimedia의 FFmpeg 백엔드 사용)

---

## 파일 구조

| 파일 | 역할 |
|---|---|
| **`main.py`** | GUI 진입점 |
| **`gui.py`** | PyQt6 GUI (`MainWindow`). 폴더 선택·이벤트 목록·옵션 패널·진행률·로그. FFmpeg를 `QProcess`로 비동기 실행하며 머지 시 세그먼트 큐를 순차 처리 |
| **`processor.py`** | 핵심 로직. `MergeConfig`(옵션 묶음) + `TeslaDashcamProcessor`. 이벤트 탐색, 레이아웃 기하 계산, Pillow 오버레이(타임스탬프/라벨) 렌더링, FFmpeg 명령·concat 명령 생성 |
| **`cli.py`** | 커맨드라인 인터페이스 (목록/배치/머지). `processor`의 명령 생성을 그대로 공유 |
| **`verify_cli.py`** | GUI 없이 핵심 파이프라인을 빠르게 검증하는 스모크 테스트 |
| **`requirements.txt`** | `PyQt6`, `Pillow` |
| **`run.sh`** | 가상환경 자동 셋업 + 실행 |
| **`settings.json`** | 마지막 출력 폴더 등 로컬 설정(자동 생성, git 제외) |
| **`2025-11-29_*/`** | 테슬라 원본 영상 샘플 폴더. 카메라별 `.mp4` + `event.json`(GPS·사유 등 메타) + `thumb.png` |

---

## 동작 원리

### 합성
1. `find_events()`가 파일명(`날짜시각-카메라.mp4`)을 파싱해 같은 시각끼리 묶음
2. 선택 레이아웃의 타일 기하를 계산 → 각 카메라를 `scale` 후 `xstack`으로 배치
3. **카메라 라벨**(전체 캔버스 1장)과 **타임스탬프**(매초 PNG 시퀀스)를 `overlay`로 합성
4. 다운스케일 → 배속(`setpts`) → CFR(`fps`) → `yuv420p`로 정규화

### 타임스탬프/라벨이 freetype에 의존하지 않는 이유
Homebrew core의 FFmpeg는 라이선스 정책상 `libfreetype` 없이 빌드되어
`drawtext`·`subtitles`·`libass` 텍스트 필터가 **전부 없습니다**. 그래서 텍스트는
**Pillow로 PNG를 렌더링**(Pillow는 freetype를 자체 번들)한 뒤 FFmpeg 기본 `overlay`
필터로 얹습니다. 덕분에 FFmpeg 재빌드 없이 어디서나 동작합니다.
임시 PNG는 처리 후 `cleanup_temp()`으로 자동 삭제됩니다.

### 머지 시 재생 속도가 느려지던 문제 (해결됨)
예전에는 각 이벤트를 mp4로 인코딩한 뒤 `-c copy`로 이어붙였는데, mp4 세그먼트 경계에서
타임스탬프(PTS)가 어긋나 **재생 중 어느 순간부터 프레임이 느려지는** 현상이 있었습니다.

해결: 머지용 세그먼트를 **MPEG-TS 컨테이너 + 닫힌 GOP(`-g`/`-keyint_min`) + 강제 CFR**로
인코딩한 뒤 copy로 이어붙입니다. TS는 세그먼트 경계의 타임스탬프를 깔끔하게 리셋하므로
최종 영상의 프레임 간격이 전 구간 일정(`avg_frame_rate == fps`)해져 슬로우다운이 사라집니다.
(`verify_cli.py`가 머지 결과의 길이·CFR 연속성을 자동 검증합니다.)

---

## 검증
```bash
.venv/bin/python verify_cli.py          # 각 레이아웃 + 머지 연속성 스모크 테스트
```
저해상도/고속 설정으로 단일 합성(classic·grid6·front)과 3개 이벤트 머지를 돌려
명령 생성·실행과 머지 결과의 CFR 연속성을 확인합니다.
