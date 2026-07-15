from __future__ import annotations

import shutil
import subprocess
import sys


def main(argv: list[str]) -> int:
    files = [f for f in argv if f.endswith(".py")]
    if not files:
        return 0

    ruff = shutil.which("ruff")
    if not ruff:
        print("ruff is not installed. Run: pip install -r requirements-dev.txt")
        return 1

    return subprocess.run(
        [ruff, "check", "--select", "F401,F841", "--no-cache", "--", *files]
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
