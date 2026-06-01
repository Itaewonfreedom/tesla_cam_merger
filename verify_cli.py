#!/usr/bin/env python3
"""
GUI 없이 핵심 파이프라인을 빠르게 검증하는 스모크 테스트.

각 레이아웃 단일 합성 + 다중 머지(연속 PTS)를 저해상도/고속 설정으로 돌려
명령 생성과 FFmpeg 실행, 그리고 머지 결과의 CFR 연속성을 확인한다.

  python verify_cli.py [샘플_폴더]
"""

import os
import sys
import json
import tempfile
import subprocess

from processor import TeslaDashcamProcessor, MergeConfig

FAST = dict(encoding="libx264", preset="ultrafast", quality_mode="crf", crf=32,
            width=640, fps=24)


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", path], capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def main():
    test_dir = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/seunghyunjang/Movies/Work/tesla/2025-11-29_16-53-04"
    proc = TeslaDashcamProcessor()
    events = proc.find_events(test_dir)
    print(f"Scanning {test_dir} ... {len(events)} events")
    if not events:
        print("No events found! Verification failed."); sys.exit(1)

    keys = sorted(events)
    ts = keys[0]
    print(f"Layouts available for {ts}: {proc.available_layouts(ts)}")

    out_dir = tempfile.mkdtemp(prefix="tcm_verify_")
    try:
        # 1) 각 레이아웃 단일 합성 (앞 6초만)
        for layout in ["classic", "grid6", "front"]:
            cfg = MergeConfig(layout=layout, **FAST)
            out = os.path.join(out_dir, f"{layout}.mp4")
            cmd = proc.build_command(ts, out, cfg, "mp4")
            cmd = cmd[:-1] + ["-t", "6", cmd[-1]]
            rc = subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ok = rc == 0 and os.path.exists(out)
            print(f"  [{'OK' if ok else 'FAIL'}] single layout={layout}")
            if not ok:
                sys.exit(1)

        # 2) 3개 이벤트 머지 → CFR 연속성 확인 (각 5초)
        cfg = MergeConfig(layout="classic", **FAST)
        segs = []
        for i, t in enumerate(keys[:3]):
            seg = os.path.join(out_dir, f"seg_{i}.ts")
            cmd = proc.build_command(t, seg, cfg, "mpegts")
            cmd = cmd[:-1] + ["-t", "5", cmd[-1]]
            if subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
                print("  [FAIL] segment encode"); sys.exit(1)
            segs.append(seg)
        final = os.path.join(out_dir, "merged.mp4")
        ccmd, listf = proc.build_concat_command(segs, final, cfg)
        if subprocess.call(ccmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
            print("  [FAIL] concat"); sys.exit(1)

        dur = probe_duration(final)
        expected = 5 * len(segs)
        ok = abs(dur - expected) < 0.3
        print(f"  [{'OK' if ok else 'FAIL'}] merge continuity: "
              f"duration={dur:.2f}s (expected ~{expected}s)")
        if not ok:
            sys.exit(1)

        print("\nAll checks passed ✅")
    finally:
        proc.cleanup_temp()
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
