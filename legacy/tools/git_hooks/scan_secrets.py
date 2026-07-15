from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


MAX_TEXT_BYTES = 2_000_000
ALLOW_RE = re.compile(r"(?i)(your|example|placeholder|dummy|changeme|<.*>)")
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "password assignment",
        re.compile(
            r"(?i)\b(password|passwd|app_password|secret|api[_-]?key|token)\b"
            r"\s*[:=]\s*(?:['\"][^'\"]{12,}['\"]|[A-Za-z0-9_./+=-]{20,})"
            r"\s*(?:#|$|[,}])"
        ),
    ),
)


def run_gitleaks_if_available() -> int:
    exe = shutil.which("gitleaks")
    if not exe:
        return 0
    cmd = [exe, "protect", "--staged", "--redact", "--verbose"]
    if Path(".gitleaks.toml").exists():
        cmd.extend(["--config", ".gitleaks.toml"])
    proc = subprocess.run(cmd)
    return proc.returncode


def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    for encoding in ("utf-8", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def main(argv: list[str]) -> int:
    gitleaks_code = run_gitleaks_if_available()
    if gitleaks_code != 0:
        return gitleaks_code

    findings: list[str] = []
    for raw in argv:
        path = Path(raw)
        if not path.exists() or not path.is_file():
            continue
        text = read_text(path)
        if text is None:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if ALLOW_RE.search(line):
                continue
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(f"{path}:{line_no}: possible {label}")
                    break

    if not findings:
        return 0

    print("Potential secrets found in staged files:")
    for finding in findings:
        print(f"  - {finding}")
    print("Replace real credentials with placeholders and rotate any exposed secret.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
