from __future__ import annotations

import sys
from pathlib import Path


MAX_BYTES = 1_000_000
TEXT_SAMPLE_BYTES = 8192


def is_binary(path: Path) -> bool:
    try:
        data = path.read_bytes()[:TEXT_SAMPLE_BYTES]
    except OSError:
        return False
    if not data:
        return False
    if b"\x00" in data:
        return True
    text_chars = bytes(range(32, 127)) + b"\n\r\t\b\f"
    non_text = data.translate(None, text_chars)
    return len(non_text) / len(data) > 0.30


def main(argv: list[str]) -> int:
    blocked: list[Path] = []
    for raw in argv:
        path = Path(raw)
        if not path.exists() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_BYTES and is_binary(path):
            blocked.append(path)

    if not blocked:
        return 0

    print("Large binary files over 1MB are blocked:")
    for path in blocked:
        print(f"  - {path} ({path.stat().st_size:,} bytes)")
    print("Move generated data/model artifacts outside git or add a narrow .gitignore rule.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
