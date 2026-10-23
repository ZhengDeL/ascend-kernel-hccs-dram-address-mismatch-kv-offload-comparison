#!/usr/bin/env python3
"""Reject private IPv4 addresses in Git-publishable text without printing values."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys

PATTERN = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
TEXT_SUFFIXES = {".py", ".sh", ".json", ".md", ".conf", ".txt", ".env", ".example", ".yaml", ".yml", ".csv"}


def private_address(value):
    try:
        parts = ipaddress.IPv4Address(value).packed
    except ipaddress.AddressValueError:
        return False
    return parts[0] == 10 or (parts[0] == 172 and 16 <= parts[1] <= 31) or (parts[0] == 192 and parts[1] == 168)


def findings(text):
    return sorted({text.count("\n", 0, match.start()) + 1 for match in PATTERN.finditer(text)
                   if private_address(match.group(0))})


def check(root):
    root = Path(root).resolve()
    selected = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    results = []
    for name in sorted(set(selected.decode("utf-8").split("\0")) - {""}):
        file = root / name
        if file.suffix not in TEXT_SUFFIXES or not file.is_file():
            continue
        try:
            lines = findings(file.read_text(encoding="utf-8"))
        except (UnicodeError, OSError) as error:
            results.append({"path": name, "error": type(error).__name__})
            continue
        if lines:
            results.append({"path": name, "lines": lines, "reason": "private IPv4 must be replaced before publication"})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    report = check(args.root)
    print(json.dumps({"status": "FAIL" if report else "PASS", "findings": report}, indent=2))
    sys.exit(1 if report else 0)
