#!/usr/bin/env python3
"""
Tesla Dashcam Merger — 커맨드라인 인터페이스.

GUI 없이 스크립트/자동화로 합성·머지할 수 있다. 명령 생성 로직은 processor.py를 공유한다.

예시:
  # 폴더의 이벤트 목록 보기 (길이/사유 포함)
  python cli.py /path/to/folder --list

  # 모든 이벤트를 6분할로 개별 변환, 1080p, GPU 인코딩
  python cli.py /path/to/folder --all --layout grid6 --width 1920

  # 특정 이벤트들을 하나로 머지, 4배속 타임랩스
  python cli.py /path/to/folder -s 2025-11-29_16-42-04 -s 2025-11-29_16-43-05 \\
      --merge -o drive.mp4 --speed 4
"""

import os
import sys
import argparse
import tempfile
import subprocess

from processor import (TeslaDashcamProcessor, MergeConfig,
                       LAYOUTS, RESOLUTION_PRESETS)


def build_config(args):
    return MergeConfig(
        encoding=args.encoding,
        quality_mode=args.quality,
        bitrate=args.bitrate,
        crf=args.crf,
        fps=args.fps,
        width=args.width,
        speed=args.speed,
        layout=args.layout,
        show_timestamp=not args.no_timestamp,
        timestamp_format=args.timestamp_format,
        show_labels=not args.no_labels,
        preset=args.preset,
    )


def run(cmd):
    """FFmpeg 실행. 진행 로그는 stderr로 흘려보낸다."""
    proc = subprocess.run(cmd)
    return proc.returncode


def cmd_list(proc, directory):
    events = proc.find_events(directory)
    meta = proc.read_event_meta(directory)
    if not events:
        print("이벤트 없음."); return 1
    reason = meta.get("reason", "") if meta else ""
    print(f"{len(events)}개 이벤트" + (f"  (event reason: {reason})" if reason else ""))
    for i, ts in enumerate(sorted(events)):
        cams = sorted(events[ts])
        dur = proc.event_duration(ts)
        layouts = ",".join(proc.available_layouts(ts))
        print(f"  [{i:2}] {ts}  {dur:5.1f}s  cams={len(cams)}  layouts=[{layouts}]")
    return 0


def resolve_selection(proc, events, args):
    keys = sorted(events)
    if args.all:
        return keys
    chosen = []
    for s in args.select:
        if s.isdigit():
            idx = int(s)
            if 0 <= idx < len(keys):
                chosen.append(keys[idx])
        elif s in events:
            chosen.append(s)
        else:
            print(f"경고: '{s}' 이벤트를 찾을 수 없음", file=sys.stderr)
    return chosen


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tesla Dashcam Merger CLI")
    ap.add_argument("directory", help="블랙박스 클립이 있는 폴더")
    ap.add_argument("--list", action="store_true", help="이벤트 목록만 출력")
    ap.add_argument("-s", "--select", action="append", default=[],
                    help="처리할 이벤트(타임스탬프 또는 인덱스). 여러 번 지정 가능")
    ap.add_argument("--all", action="store_true", help="모든 이벤트 처리")
    ap.add_argument("--merge", action="store_true", help="선택한 이벤트를 하나로 머지")
    ap.add_argument("-o", "--output", help="출력 파일(머지) 또는 폴더(배치)")

    ap.add_argument("--layout", choices=list(LAYOUTS), default="classic")
    ap.add_argument("--encoding", choices=["hevc_videotoolbox", "libx264"],
                    default="hevc_videotoolbox")
    ap.add_argument("--quality", choices=["bitrate", "crf"], default="bitrate")
    ap.add_argument("--bitrate", default="20M")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--width", type=int, default=1920,
                    help="출력 가로폭(px). 0=원본")
    ap.add_argument("--speed", type=float, default=1.0, help="재생 배속(타임랩스)")
    ap.add_argument("--preset", default="medium", help="libx264 preset")
    ap.add_argument("--no-timestamp", action="store_true")
    ap.add_argument("--no-labels", action="store_true")
    ap.add_argument("--timestamp-format", default="%Y-%m-%d %H:%M:%S")
    args = ap.parse_args(argv)

    proc = TeslaDashcamProcessor()

    if args.list:
        return cmd_list(proc, args.directory)

    events = proc.find_events(args.directory)
    if not events:
        print("이벤트 없음.", file=sys.stderr); return 1

    selection = resolve_selection(proc, events, args)
    if not selection:
        print("처리할 이벤트가 없습니다. --all 또는 -s 로 선택하세요.", file=sys.stderr)
        return 1

    config = build_config(args)
    print(f"레이아웃={config.layout} 인코딩={config.encoding} "
          f"폭={config.width or '원본'} fps={config.fps} 배속={config.speed}x "
          f"이벤트={len(selection)}개 머지={args.merge}")

    try:
        if args.merge:
            return do_merge(proc, selection, config, args)
        return do_batch(proc, selection, config, args)
    finally:
        proc.cleanup_temp()


def do_batch(proc, selection, config, args):
    out_dir = args.output or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    rc = 0
    for i, ts in enumerate(selection, 1):
        out = os.path.join(out_dir, f"merged_{ts}.mp4")
        print(f"[{i}/{len(selection)}] {ts} -> {out}")
        cmd = proc.build_command(ts, out, config, container="mp4")
        if run(cmd) != 0:
            print(f"  실패: {ts}", file=sys.stderr); rc = 1
    return rc


def do_merge(proc, selection, config, args):
    out = args.output or "merged_video.mp4"
    tmp = tempfile.mkdtemp(prefix="tesla_merge_")
    segs = []
    try:
        for i, ts in enumerate(selection, 1):
            seg = os.path.join(tmp, f"seg_{i:03}.ts")
            print(f"[{i}/{len(selection)}] 세그먼트 생성 {ts}")
            cmd = proc.build_command(ts, seg, config, container="mpegts")
            if run(cmd) != 0:
                print(f"  세그먼트 실패: {ts}", file=sys.stderr); return 1
            segs.append(seg)
        print("세그먼트 이어붙이는 중...")
        ccmd, listf = proc.build_concat_command(segs, out, config)
        if run(ccmd) != 0:
            print("concat 실패", file=sys.stderr); return 1
        if os.path.exists(listf):
            os.remove(listf)
        print(f"완료: {out}")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
