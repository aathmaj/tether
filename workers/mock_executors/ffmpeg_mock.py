#!/usr/bin/env python3
import sys
from pathlib import Path

# Usage: ffmpeg_mock.py <input_file> <output_dir> [extra args]
if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('usage: ffmpeg_mock.py <input> <output_dir>')
        sys.exit(2)
    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    out_file = out / 'out.mp4'
    out_file.write_bytes(f"mock ffmpeg output from {inp.name}\n".encode())
    print('OK')
