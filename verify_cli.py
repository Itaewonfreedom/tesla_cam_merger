import os
import sys
from processor import TeslaDashcamProcessor

def verify():
    processor = TeslaDashcamProcessor()
    test_dir = "/Users/seunghyunjang/Movies/Work/tesla/2025-11-29_16-53-04"
    
    print(f"Scanning {test_dir}...")
    events = processor.find_events(test_dir)
    print(f"Found {len(events)} events.")
    
    if not events:
        print("No events found! Verification failed.")
        sys.exit(1)
        
    # Pick the first event
    timestamp = list(events.keys())[0]
    print(f"Testing event: {timestamp}")
    
    output_dir = os.path.join(test_dir, "merged_test")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"test_{timestamp}.mp4")
    
    # Generate command
    cmd = processor.generate_ffmpeg_command(timestamp, output_file, encoding="libx264")
    print("Generated Command:")
    print(" ".join(cmd))
    
    # Execute
    print("Running FFmpeg...")
    import subprocess
    ret = subprocess.call(cmd)
    
    if ret == 0:
        print(f"Success! Output saved to {output_file}")
        if os.path.exists(output_file):
            size = os.path.getsize(output_file)
            print(f"Output file size: {size / 1024 / 1024:.2f} MB")
    else:
        print("FFmpeg failed.")
        sys.exit(1)

if __name__ == "__main__":
    verify()
