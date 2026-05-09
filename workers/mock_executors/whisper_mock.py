#!/usr/bin/env python3
import sys
from pathlib import Path

# Usage: whisper_mock.py <input_file> <output_dir>
if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('usage: whisper_mock.py <input> <output_dir>')
        sys.exit(2)
    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    out_file = out / 'transcript.txt'
    out_file.write_text(f"mock transcript for {inp.name}\n")
    print('OK')
