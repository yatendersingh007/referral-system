"""Thin wrapper around the real LibreOffice `soffice` binary so the rest of
this codebase can invoke a stable, repo-relative path
(`scripts/office/soffice.py --headless --convert-to pdf ...`) regardless of
where LibreOffice is actually installed on a given machine.

Usage: python scripts/office/soffice.py --headless --convert-to pdf --outdir <dir> <file.pptx>
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

_CANDIDATES = [
    "soffice",
    "soffice.exe",
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/opt/libreoffice/program/soffice",
]


def find_soffice() -> str | None:
    for c in _CANDIDATES:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
            continue
        found = shutil.which(c)
        if found:
            return found
    return None


def main() -> None:
    exe = find_soffice()
    if not exe:
        print("soffice (LibreOffice) not found on this machine.", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run([exe, *sys.argv[1:]])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
