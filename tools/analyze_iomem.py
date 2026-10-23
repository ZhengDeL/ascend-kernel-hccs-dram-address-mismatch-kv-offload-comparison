#!/usr/bin/env python3
"""Read /proc/iomem as evidence, never probe PFNs or physical memory."""
import argparse
import hashlib
import json
import re
from pathlib import Path

LINE = re.compile(r"^\s*([0-9a-fA-F]+)-([0-9a-fA-F]+)\s*:\s*(.*?)\s*$")


def number(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not an address")
    if isinstance(value, int):
        return value
    return int(str(value), 16) if str(value).lower().startswith("0x") else int(str(value))


def merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1] + 1:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def intersect(left, right):
    return merge([(max(a, c), min(b, d)) for a, b in left for c, d in right
                  if max(a, c) <= min(b, d)])


def size(intervals):
    return sum(end - start + 1 for start, end in merge(intervals))


def intervals_json(intervals):
    return [{"start": hex(a), "end_inclusive": hex(b), "bytes": b - a + 1}
            for a, b in merge(intervals)]


def load_windows(path):
    if path is None:
        return {"windows": [], "source_reference": None, "source_commit": None}
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("windows"), list):
        raise ValueError("windows JSON must be an object containing a windows array")
    result = dict(value)
    windows = []
    names = set()
    for item in value["windows"]:
        if not isinstance(item, dict):
            raise ValueError("each window must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("each window needs a unique non-empty name")
        start, end = number(item["start"]), number(item["end_inclusive"])
        if start < 0 or end < start:
            raise ValueError("window addresses must satisfy 0 <= start <= end_inclusive")
        names.add(name)
        windows.append({"name": name, "start": start, "end_inclusive": end})
    result["windows"] = windows
    return result


def analyze(text, config=None):
    config = config or {"windows": []}
    parsed, errors = [], []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = LINE.match(line)
        if not match:
            errors.append(line_no)
            continue
        start, end = int(match[1], 16), int(match[2], 16)
        if end < start:
            errors.append(line_no)
            continue
        parsed.append((start, end, match[3]))
    ram_entries = [(a, b) for a, b, name in parsed if name.lower() == "system ram"]
    hidden = (bool(parsed) and all(a == b == 0 for a, b, _ in parsed)) or (
        bool(ram_entries) and all(a == b == 0 for a, b in ram_entries))
    known = bool(ram_entries) and not hidden and not errors
    ram = merge(ram_entries) if known else []
    windows = []
    all_windows = []
    for item in config.get("windows", []):
        interval = [(number(item["start"]), number(item["end_inclusive"]))]
        all_windows.extend(interval)
        overlap = intersect(ram, interval)
        windows.append({"name": item["name"], "range": intervals_json(interval)[0],
                        "iomem_system_ram_intersection_bytes": size(overlap) if known else None,
                        "intersection_ranges": intervals_json(overlap) if known else None,
                        "numa_node": "unknown; correlate with separately collected NUMA evidence"})
    return {
        "schema_version": 1,
        "iomem_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "visibility": "addresses_hidden" if hidden else "known" if known else "unknown_or_incomplete",
        "parsed_resource_count": len(parsed), "malformed_line_numbers": errors,
        "iomem_system_ram_bytes": size(ram) if known else None,
        "iomem_system_ram_ranges": intervals_json(ram) if known else None,
        "windows_source_reference": config.get("source_reference"),
        "windows_source_commit": config.get("source_commit"),
        "windows_status": "supplied_by_operator" if windows else "not_supplied; no driver window assumption made",
        "windows": windows,
        "window_union_system_ram_intersection_bytes": size(intersect(ram, all_windows)) if known and windows else None,
        "interpretation": [
            "Inclusive physical address ranges; overlapping and nested System RAM entries counted once.",
            "System RAM capacity is iomem-described address coverage, not free, allocatable, pinnable or HCCS-accessible memory.",
            "Subresources such as Kernel code are not added again; allocator reservations are not inferred from iomem.",
            "Zero free memory never establishes zero physical capacity. Hidden addresses produce null capacity, not zero.",
            "Window intersections are version-specific evidence only and do not establish a hardware address contract.",
            "NUMA ownership and driver mem_scan acceptance require separate evidence; this tool does not scan PFNs."
        ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iomem", required=True, type=Path)
    parser.add_argument("--windows-json", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = analyze(args.iomem.read_text(encoding="utf-8", errors="replace"), load_windows(args.windows_json))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
