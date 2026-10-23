#!/usr/bin/env python3
"""Download the fixed community source set for local research, without submodules.

This is a workstation download utility, separate from all server launch scripts.
Archives are kept, regular source files are extracted with traversal checks, and
link entries are recorded without materializing filesystem links on Windows.
"""
import argparse
import concurrent.futures
import configparser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request


SOURCES = [
    {"name": "memcache", "url": "https://gitcode.com/Ascend/memcache.git", "kind": "gitcode",
     "reuse": "research-src-memcache", "commit": "98fb4c303caeeb294bdc2db84667a87e54833acf"},
    {"name": "memfabric", "url": "https://gitcode.com/Ascend/memfabric_hybrid.git", "kind": "gitcode",
     "reuse": "research-src-memfabric", "commit": "8df6c8f3e1ca1ddfce2dc903d0780300278c1b84"},
    {"name": "vllm-ascend-rc2", "repo": "vllm-project/vllm-ascend", "kind": "github",
     "ref": "v0.30.0rc2", "commit": "8fde6d4e335fe300e2b949aab0e31659cce0811d"},
    {"name": "vllm-ascend-main", "repo": "vllm-project/vllm-ascend", "kind": "github",
     "ref": "main", "commit": "4e6eac4e476d9be3476e99440f4cabed67b253ea"},
    {"name": "vllm-rc2", "repo": "vllm-project/vllm", "kind": "github", "ref": "v0.30.0rc2",
     "commit": "fa6ff060667f2cd96f142af021ba9e35165beb78"},
    {"name": "mooncake", "repo": "kvcache-ai/Mooncake", "kind": "github", "ref": "main",
     "commit": "dd1217b6c461491e7c73ff29b4ffad215e0a9ac0"},
    {"name": "lmcache-ascend", "repo": "LMCache/LMCache-Ascend", "kind": "github", "ref": "main"},
    {"name": "lmcache", "repo": "LMCache/LMCache", "kind": "github", "ref": "dev"},
    {"name": "yuanrong-datasystem", "repo": "openyuanrong/datasystem", "kind": "github", "ref": "master"},
    {"name": "hixl", "url": "https://gitcode.com/cann/hixl.git", "kind": "gitcode"},
    {"name": "hcomm", "url": "https://gitcode.com/cann/hcomm.git", "kind": "gitcode"},
    {"name": "umdk", "url": "https://gitcode.com/openeuler/umdk.git", "kind": "gitcode"},
    {"name": "ai-infra-summary", "repo": "MarsChan8293/ai_infra_summary", "kind": "github",
     "ref": "main", "commit": "5a1263fb5bfcc65e1504fa545486e7b76e1bcf52"},
]
PRINT_LOCK = threading.Lock()


def progress(message):
    with PRINT_LOCK:
        print(time.strftime("%H:%M:%S") + " " + message, flush=True)


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def request(url, destination=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    error = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ascend-hccs-source-research/1.0"})
            with opener.open(req, timeout=90) as response:
                if destination is None:
                    return response.read()
                with destination.open("wb") as output:
                    shutil.copyfileobj(response, output, length=4 * 1024 * 1024)
            return None
        except Exception as exc:
            error = exc
            if attempt == 0:
                progress("RETRY " + url + " " + str(exc))
                time.sleep(2)
    raise RuntimeError("Download failed after 2 attempts: {}: {}".format(url, error))


def git(arguments, cwd=None):
    env = {key: value for key, value in os.environ.items()
           if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}
    result = subprocess.run(["git", "-c", "http.proxy=", "-c", "https.proxy="] + arguments,
                            cwd=cwd, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=180)
    if result.returncode:
        raise RuntimeError("git failed {}: {}".format(arguments[:2], result.stderr[-3000:]))
    return result.stdout.strip()


def safe_extract(archive, destination):
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    skipped = []
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        tops = {PurePosixPath(member.name).parts[0] for member in members if PurePosixPath(member.name).parts}
        if len(tops) != 1:
            raise ValueError("Expected exactly one archive root")
        for member in members:
            posix = PurePosixPath(member.name)
            if posix.is_absolute() or ".." in posix.parts or "\\" in member.name or ":" in member.name:
                raise ValueError("Unsafe archive path: " + member.name)
            relative = posix.parts[1:]
            if not relative:
                continue
            target = destination.joinpath(*relative)
            if not target.resolve().is_relative_to(root):
                raise ValueError("Archive target escapes extraction root: " + member.name)
            if member.issym() or member.islnk():
                skipped.append({"path": "/".join(relative), "target": member.linkname,
                                "kind": "symlink" if member.issym() else "hardlink"})
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise ValueError("Duplicate archive member: " + member.name)
                with source.extractfile(member) as content, target.open("xb") as output:
                    shutil.copyfileobj(content, output, length=1024 * 1024)
            else:
                raise ValueError("Unsupported archive object: " + member.name)
    return skipped


def inventory(tree, output):
    count = 0
    total = 0
    with output.open("w", encoding="utf-8", newline="\n") as listing:
        for current, dirs, files in os.walk(tree, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name != ".git")
            for name in sorted(dirs + files):
                path = Path(current) / name
                if path.is_symlink():
                    raise ValueError("Unexpected materialized link in source snapshot: " + str(path))
            for name in sorted(files):
                path = Path(current) / name
                size = path.stat().st_size
                digest = sha256(path)
                listing.write("{}  {}  {}\n".format(digest, size, path.relative_to(tree).as_posix()))
                count += 1
                total += size
    modules = []
    module_file = tree / ".gitmodules"
    if module_file.exists():
        config = configparser.ConfigParser()
        config.read(module_file, encoding="utf-8")
        for section in config.sections():
            modules.append({"section": section, "path": config.get(section, "path", fallback=""),
                            "url": config.get(section, "url", fallback=""), "downloaded": False})
    return {"source_file_count": count, "source_bytes": total,
            "file_manifest": str(output), "file_manifest_sha256": sha256(output),
            "submodules": modules, "submodules_downloaded": False}


def download(spec, output, workspace):
    start = time.time()
    record = dict(spec)
    record["source_url"] = spec.get("url", "https://github.com/" + spec.get("repo", ""))
    progress("START " + spec["name"])
    try:
        if spec["kind"] == "github":
            commit = spec.get("commit")
            if commit is None:
                metadata = json.loads(request("https://api.github.com/repos/{}/commits/{}".format(
                    spec["repo"], spec["ref"])))
                commit = metadata["sha"]
            if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
                raise ValueError("Invalid resolved commit SHA")
            record["commit"] = commit
            archive = output / "archives" / (spec["name"] + "-" + commit[:12] + ".tar.gz")
            tree = output / (spec["name"] + "-" + commit[:12])
            record["download_url"] = "https://codeload.github.com/{}/tar.gz/{}".format(spec["repo"], commit)
            if not archive.exists():
                partial = archive.with_suffix(".partial")
                request(record["download_url"], partial)
                partial.replace(archive)
            record["archive_path"] = str(archive)
            record["archive_bytes"] = archive.stat().st_size
            record["archive_sha256"] = sha256(archive)
            marker = output / "manifests" / (spec["name"] + "-" + commit[:12] + ".complete.json")
            if tree.exists() and not marker.exists():
                raise RuntimeError("Incomplete existing tree preserved; move it aside before retry: " + str(tree))
            skipped = safe_extract(archive, tree) if not tree.exists() else json.loads(marker.read_text())["links_not_materialized"]
            record["snapshot_type"] = "github-commit-archive"
            record["links_not_materialized"] = skipped
        else:
            reuse = workspace / spec.get("reuse", "__absent__")
            commit = spec.get("commit")
            if reuse.is_dir():
                actual = git(["rev-parse", "HEAD"], cwd=reuse)
                if actual != commit:
                    raise RuntimeError("Local reuse commit mismatch")
                tree = output / (spec["name"] + "-" + actual[:12])
                if not tree.exists():
                    # Reuse the verified existing checkout without changing it.
                    shutil.copytree(reuse, tree, symlinks=True)
                record["snapshot_type"] = "verified-local-git-checkout-copy"
            else:
                actual = commit or git(["ls-remote", spec["url"], "HEAD"]).split()[0]
                tree = output / (spec["name"] + "-" + actual[:12])
                if not tree.exists():
                    git(["clone", "--depth", "1", spec["url"], str(tree)])
                if commit and git(["rev-parse", "HEAD"], cwd=tree) != commit:
                    git(["fetch", "--depth", "1", "origin", commit], cwd=tree)
                    git(["checkout", "--detach", commit], cwd=tree)
                record["snapshot_type"] = "shallow-git-checkout"
            record["commit"] = git(["rev-parse", "HEAD"], cwd=tree)
            if commit and record["commit"] != commit:
                raise RuntimeError("Copied checkout commit mismatch")
            if not commit and record["commit"] != actual:
                raise RuntimeError("Remote HEAD changed between resolution and clone; no pinned claim")
            record["git_worktree_status"] = git(["status", "--porcelain", "--untracked-files=no"], cwd=tree)
            record["links_not_materialized"] = []
            marker = output / "manifests" / (spec["name"] + "-" + record["commit"][:12] + ".complete.json")
        record["source_path"] = str(tree)
        record.update(inventory(tree, output / "manifests" / (spec["name"] + "-" + record["commit"][:12] + ".sha256")))
        record["status"] = "success"
        record["elapsed_seconds"] = round(time.time() - start, 2)
        marker.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        progress("DONE {} files={} bytes={} sha={}".format(spec["name"], record["source_file_count"],
                 record["source_bytes"], record["commit"][:12]))
    except Exception as exc:
        record.update(status="failed", error=str(exc), elapsed_seconds=round(time.time() - start, 2))
        progress("FAILED {} {}".format(spec["name"], exc))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("community-sources"))
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--only", nargs="+", choices=[item["name"] for item in SOURCES])
    parser.add_argument("--lock-file", type=Path,
                        help="Replay commit SHAs from download-report.json or COMMUNITY_SOURCES.json; URLs stay fixed")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for folder in ("archives", "manifests"):
        (output / folder).mkdir(exist_ok=True)
    sources = [dict(item) for item in SOURCES if not args.only or item["name"] in args.only]
    if args.lock_file:
        locked = {item["name"]: item["commit"] for item in json.loads(
            args.lock_file.read_text(encoding="utf-8"))["sources"]}
        for item in sources:
            commit = locked.get(item["name"], "")
            if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
                raise ValueError("Lock file has no valid commit for " + item["name"])
            item["commit"] = commit
    selected_names = {item["name"] for item in sources}
    previous_report = output / "download-report.json"
    previous = {}
    if args.only and previous_report.exists():
        previous = json.loads(previous_report.read_text(encoding="utf-8"))
        records = [item for item in previous.get("sources", []) if item["name"] not in selected_names]
    else:
        records = []
    requested_count = len(records) + len(sources)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(download, spec, output, args.workspace_root.resolve()) for spec in sources]
        for future in concurrent.futures.as_completed(futures):
            records.append(future.result())
            report = {"schema_version": 1, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "submodule_policy": "No recursive submodule download. Link entries recorded, not materialized.",
                      "requested_sources": requested_count, "finished_sources": len(records),
                      "success_count": sum(r["status"] == "success" for r in records),
                      "failure_count": sum(r["status"] != "success" for r in records),
                      "total_source_bytes": sum(r.get("source_bytes", 0) for r in records),
                      "total_source_files": sum(r.get("source_file_count", 0) for r in records),
                      "resolved_attempts": previous.get("resolved_attempts", []),
                      "sources": sorted(records, key=lambda r: r["name"])}
            pending = output / "download-report.json.tmp"
            pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            pending.replace(output / "download-report.json")
    progress("REPORT " + str(output / "download-report.json"))
    return 1 if any(r["status"] != "success" for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
