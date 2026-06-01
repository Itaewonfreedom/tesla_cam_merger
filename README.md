# Tesla Dashcam Merger

테슬라 블랙박스(Dashcam / Sentry) 영상을 **4분할 화면 하나로 합쳐주는** macOS용 데스크톱 앱입니다.
테슬라는 한 시점의 영상을 카메라(전방/후방/좌우 리피터 등)별로 따로 저장하는데, 이 앱은 같은 시각의 클립들을 모아 하나의 영상으로 합성하고, 화면 하단에 촬영 시각 타임스탬프를 새겨 넣습니다.

PyQt6로 만든 GUI에서 폴더를 고르면 이벤트 목록이 뜨고, 선택한 이벤트들을 **개별 변환**하거나 **하나의 긴 영상으로 병합**할 수 있습니다. 실제 영상 처리는 시스템에 설치된 **FFmpeg**가 담당합니다.

---

## 빠른 시작

### 1. 사전 요구사항
- **Python 3.x** (개발/검증은 3.14에서 진행)
- **FFmpeg / ffprobe** — Homebrew로 설치:
  ```bash
  brew install ffmpeg
  ```

### 2. 셋업 & 실행
```bash
# 한 번에 실행 (가상환경 없으면 자동 생성 + 설치)
./run.sh
```

또는 수동으로:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

### 3. 사용법
1. **Select Folder** — 테슬라 블랙박스 영상이 들어있는 폴더 선택
2. 같은 시각의 4개 카메라가 모두 있는 이벤트만 목록에 표시됨
3. 변환할 이벤트 선택 (여러 개 다중 선택 가능)
4. **Encoding** / **Bitrate** 옵션 선택
   - `hevc_videotoolbox (GPU)` — Mac 하드웨어 가속 (빠름)
   - `libx264 (CPU)` — 호환성 높음 (느림)
5. **Process Selected** 클릭
   - 1개 선택 → 단일 파일로 저장
   - 여러 개 선택 → **병합(하나로 이어붙이기)** 또는 **일괄 변환(각각 저장)** 선택

---

## 파일 구조와 역할

| 파일 / 폴더 | 역할 |
|---|---|
| **`main.py`** | 앱 진입점. `QApplication`을 만들고 `gui.py`의 `MainWindow`를 띄웁니다. 실행은 항상 이 파일로 시작합니다. |
| **`gui.py`** | PyQt6 GUI 본체 (`MainWindow`). 폴더 선택, 이벤트 목록 표시, 인코딩/비트레이트 옵션, 진행률 표시줄, 로그 창을 담당합니다. FFmpeg는 `QProcess`로 비동기 실행하며, 여러 이벤트를 큐(queue)로 순차 처리합니다. 병합 모드에서는 임시 폴더에 세그먼트를 만든 뒤 마지막에 이어붙입니다(concat). |
| **`processor.py`** | 영상 처리 **핵심 로직** (`TeslaDashcamProcessor`). GUI가 없어도 동작합니다. 주요 메서드: <br>• `find_events()` — 폴더를 스캔해 파일명(`날짜시각-카메라.mp4`)을 정규식으로 파싱하고, 같은 시각끼리 묶어 4개 카메라가 모두 있는 이벤트만 반환<br>• `get_video_info()` — `ffprobe`로 해상도/길이 조회<br>• `generate_ffmpeg_command()` — 4분할(xstack) 합성 + 타임스탬프 자막을 그리는 FFmpeg 명령 생성<br>• `generate_concat_command()` — 여러 영상을 이어붙이는 명령 생성 |
| **`verify_cli.py`** | GUI 없이 터미널에서 동작을 검증하는 테스트 스크립트. 샘플 폴더를 스캔해 첫 이벤트를 실제로 합성해 봅니다. |
| **`requirements.txt`** | Python 의존성 목록 (`PyQt6`). |
| **`run.sh`** | 가상환경 자동 셋업 + 앱 실행 스크립트. |
| **`settings.json`** | 앱이 자동 생성/관리하는 로컬 설정 (마지막 저장 폴더 등). git 추적 제외. |
| **`.venv/`** | Python 가상환경 (git 추적 제외). |
| **`__pycache__/`** | Python 바이트코드 캐시 (git 추적 제외). |
| **`2025-11-29_*/`** | 테슬라 블랙박스 **원본 영상 샘플 폴더**. 카메라별 `.mp4`, `event.json`(이벤트 메타데이터), `thumb.png`(썸네일) 포함. 용량이 크므로 git 추적 제외. |

---

## 동작 원리 (영상 합성)

테슬라 파일명 규칙: `2025-11-29_16-42-04-front.mp4`
→ `(타임스탬프)-(카메라)` 형태로 파싱합니다.

합성 시:
- **전방(front)** 카메라를 위쪽 전체에 크게 배치
- **좌측 리피터 / 후방 / 우측 리피터**를 아래쪽에 1/3 크기로 가로 배치 (`xstack`)
- 화면 하단 중앙에 **촬영 시각 타임스탬프**를 자막으로 표시 (`drawtext` + `gmtime`)
- 파일명 시각을 클립의 **종료 시각**으로 보고, 영상 길이만큼 빼서 시작 시각을 계산
- 재생 호환성을 위해 **30fps CFR**, `yuv420p`로 정규화

> 참고: `processor.py`는 필수 카메라를 `front / left_repeater / back / right_repeater` 4종으로 가정합니다. 샘플 폴더에는 `left_pillar / right_pillar`(B필러 카메라, 최신 모델) 영상도 함께 들어있지만 현재 합성에는 사용하지 않습니다.

---

## 트러블슈팅

### `No such filter: 'drawtext'` 오류로 합성 실패
하단 타임스탬프 자막은 FFmpeg의 `drawtext` 필터를 쓰는데, 이 필터는 FFmpeg가 **libfreetype을 포함해 빌드**되어 있어야 동작합니다. Homebrew 빌드에 따라 빠져 있을 수 있습니다. 확인:
```bash
ffmpeg -filters | grep drawtext   # 아무것도 안 나오면 빠진 것
```
해결: freetype 포함 빌드로 재설치
```bash
brew reinstall ffmpeg            # 보통 freetype 포함됨
# 또는
brew install homebrew-ffmpeg/ffmpeg/ffmpeg
```
(빌드를 못 바꾸는 경우, `processor.py`의 `filter_complex`에서 `drawtext` 부분을 빼면 자막 없이 합성은 됩니다.)

## 개발 메모
- 영상 처리 로직(`processor.py`)과 UI(`gui.py`)가 분리되어 있어, 로직은 `verify_cli.py`로 GUI 없이 단독 테스트 가능합니다.
- FFmpeg 명령을 직접 실행하므로, 동작이 이상하면 로그 창에 출력되는 `Command:` 줄을 복사해 터미널에서 직접 실행해보면 디버깅에 도움이 됩니다.
