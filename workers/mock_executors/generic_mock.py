#!/usr/bin/env python3
import sys
from pathlib import Path

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('usage: generic_mock.py <input> <output_dir>')
        sys.exit(2)
    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    f = out / 'generic_result.txt'
    f.write_text(f'mock generic output for {inp.name}\n')
    print('OK')
