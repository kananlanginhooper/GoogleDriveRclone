#!/usr/bin/env python3
"""gdrive-rclone: copy a list of files to Google Drive with rclone, and prove each one arrived.

  gdrive-rclone push   --remote gdrive: --dest backups --list files.jsonl [--manifest out.jsonl]
  gdrive-rclone pull   --remote gdrive: --remote-path backups/a/b.pdf --to ./restored
  gdrive-rclone remove --remote gdrive: --remote-path backups/a/b.pdf [--quarantine _removed]

push reads one JSON object per line: {"local_path": ..., "remote_path": ..., "sha256": optional},
where remote_path is relative to --dest. Each file is hashed first (SHA-256 and MD5 in one read).
A file whose SHA-256 no longer matches the one given is not uploaded, so a damaged local copy never
replaces a good offsite one. Files already on Drive with the same MD5 are skipped. The rest go up in
chunks with `rclone copy`, and each file is then checked against Drive's own MD5. One JSON line per
file goes to the manifest as soon as its result is known.

pull copies one file down into a folder and never overwrites anything there. remove never deletes:
it moves the file into a dated quarantine folder on the same Drive, which you empty yourself.

Exit codes: 0 every file copied or skipped, 7 Google's daily upload cap was hit (run the same list
again tomorrow; the manifest says which files were left), 1 some files failed, 2 bad arguments.
Needs Python 3.9+ and rclone with a Google Drive remote. Nothing else.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import posixpath
import subprocess
import sys
import tempfile

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CAP = 0, 1, 2, 7
HASH_TYPE = "md5"
FAILED = {"missing", "local_mismatch", "verify_failed"}
RCLONE = os.environ.get("RCLONE", "rclone")
COPY_FLAGS = ["--drive-stop-on-upload-limit", "--transfers", "4", "--checkers", "8",
              "--drive-chunk-size", "128M", "--stats", "60s", "--stats-one-line"]


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now()} {msg}", file=sys.stderr, flush=True)


def _run(cmd):
    """Run rclone and capture its output (listings)."""
    return subprocess.run(cmd, capture_output=True, text=True)


def _stream(cmd) -> int:
    """Run rclone with its progress going straight to our stderr (transfers)."""
    return subprocess.run(cmd, stdout=sys.stderr).returncode


def remote_root(remote: str, path: str) -> str:
    """'gdrive' or 'gdrive:' plus 'backups/x' -> 'gdrive:backups/x'."""
    return remote.rstrip(":") + ":" + path.strip("/")


def read_list(path: str) -> list:
    items = []
    f = sys.stdin if path == "-" else open(path, encoding="utf-8")
    try:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                it = json.loads(line)
            except ValueError:
                raise SystemExit(f"--list line {n}: not JSON")
            if not it.get("local_path") or not it.get("remote_path"):
                raise SystemExit(f"--list line {n}: needs local_path and remote_path")
            it["remote_path"] = it["remote_path"].replace("\\", "/").strip("/")
            items.append(it)
    finally:
        if f is not sys.stdin:
            f.close()
    return items


def hash_file(path: str):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as f:
        while block := f.read(8 << 20):
            sha.update(block)
            md5.update(block)
    return sha.hexdigest(), md5.hexdigest()


def source_root(it):
    """The local folder that local_path sits in at remote_path, so a whole chunk goes up in one
    `rclone copy`. None when the two paths don't line up; that file goes up on its own."""
    local = os.path.normpath(it["local_path"])
    rel = os.path.normpath(it["remote_path"])
    if local.endswith(os.sep + rel):
        return local[: -len(rel)]
    return None


def plan_chunks(items, max_files: int, max_bytes: float):
    """[(root or None, [item, ...]), ...]: at most max_files files or max_bytes bytes per chunk."""
    groups = {}
    for it in items:
        groups.setdefault(source_root(it), []).append(it)
    chunks = []
    for root, group in groups.items():
        chunk, size = [], 0
        for it in group:
            chunk.append(it)
            size += it.get("size") or 0
            if len(chunk) >= max_files or size >= max_bytes:
                chunks.append((root, chunk))
                chunk, size = [], 0
        if chunk:
            chunks.append((root, chunk))
    return chunks


def remote_md5s(dest: str, rels) -> dict:
    """{remote_path: md5} for the files that exist under dest."""
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".lst") as lst:
        lst.write("\n".join(rels) + "\n")
    try:
        out = _run([RCLONE, "md5sum", dest, "--files-from-raw", lst.name]).stdout
    finally:
        os.unlink(lst.name)
    found = {}
    for line in out.splitlines():
        if "  " in line:
            md5, rel = line.split("  ", 1)
            found[rel] = md5.lower()
    return found


class Manifest:
    def __init__(self, path: str, dest_path: str):
        self.f = sys.stdout if path == "-" else open(path, "a", encoding="utf-8")
        self.dest_path = dest_path
        self.counts = {}

    def emit(self, it, status: str, remote_hash=None, detail=None) -> None:
        rec = {"local_path": it["local_path"], "remote_path": posixpath.join(self.dest_path, it["remote_path"]),
               "size": it.get("size"), "sha256": it.get("sha256_local"), "remote_hash": remote_hash,
               "hash_type": HASH_TYPE, "status": status, "at": now()}
        if detail:
            rec["detail"] = detail
        self.f.write(json.dumps(rec) + "\n")
        self.f.flush()
        self.counts[status] = self.counts.get(status, 0) + 1

    def close(self) -> None:
        if self.f is not sys.stdout:
            self.f.close()


def push(a) -> int:
    items = read_list(a.list)
    dest = remote_root(a.remote, a.dest)
    out = Manifest(a.manifest, a.dest.strip("/"))
    for it in items:
        if os.path.isfile(it["local_path"]):
            it["size"] = os.path.getsize(it["local_path"])
    chunks = plan_chunks(items, a.max_files, a.max_gb * 1e9)
    log(f"{len(items)} files in {len(chunks)} chunks to {dest}")
    capped = False
    for n, (root, chunk) in enumerate(chunks, 1):
        if capped:
            for it in chunk:
                out.emit(it, "quota_stopped")
            continue
        if a.dry_run:
            for it in chunk:
                out.emit(it, "dry_run")
            continue

        ready = []
        for it in chunk:
            if "size" not in it:
                out.emit(it, "missing")
                continue
            it["sha256_local"], it["md5_local"] = hash_file(it["local_path"])
            if it.get("sha256") and it["sha256"].lower() != it["sha256_local"]:
                out.emit(it, "local_mismatch", detail="local file no longer matches the sha256 given")
                continue
            ready.append(it)

        already = remote_md5s(dest, [it["remote_path"] for it in ready])
        todo = []
        for it in ready:
            if already.get(it["remote_path"]) == it["md5_local"]:
                out.emit(it, "skipped", already[it["remote_path"]])
            else:
                todo.append(it)
        log(f"chunk {n}/{len(chunks)}: {len(chunk)} files, {len(chunk) - len(ready)} not sent,"
            f" {len(ready) - len(todo)} already there, {len(todo)} to copy")
        if not todo:
            continue

        bw = ["--bwlimit", a.bwlimit] if a.bwlimit else []
        rc = 0
        if root is not None:
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".lst") as lst:
                lst.write("\n".join(it["remote_path"] for it in todo) + "\n")
            try:
                rc = _stream([RCLONE, "copy", root, dest, "--files-from-raw", lst.name, *COPY_FLAGS, *bw])
            finally:
                os.unlink(lst.name)
        else:
            for it in todo:
                rc = _stream([RCLONE, "copyto", it["local_path"], f"{dest}/{it['remote_path']}", *COPY_FLAGS, *bw])
                if rc == EXIT_CAP:
                    break
        if rc:
            log(f"rclone exit {rc}; recording only what verifies")

        got = remote_md5s(dest, [it["remote_path"] for it in todo])
        for it in todo:
            remote = got.get(it["remote_path"])
            if remote == it["md5_local"]:
                out.emit(it, "copied", remote)
            elif rc == EXIT_CAP:
                out.emit(it, "quota_stopped")
            else:
                out.emit(it, "verify_failed", remote, f"local md5 {it['md5_local']}, remote {remote}")
        if rc == EXIT_CAP:
            log("Google's daily upload cap: stopping; run the same list again later")
            capped = True

    out.close()
    log("done: " + ", ".join(f"{k} {v}" for k, v in sorted(out.counts.items())))
    if capped:
        return EXIT_CAP
    return EXIT_FAILED if FAILED & out.counts.keys() else EXIT_OK


def pull(a) -> int:
    target = os.path.join(a.to, posixpath.basename(a.remote_path))
    if os.path.exists(target):
        log(f"{target} already exists; pull never overwrites")
        return EXIT_FAILED
    os.makedirs(a.to, exist_ok=True)
    src = remote_root(a.remote, a.remote_path)
    rc = _stream([RCLONE, "copyto", src, target])
    remote = remote_md5s(remote_root(a.remote, posixpath.dirname(a.remote_path)),
                         [posixpath.basename(a.remote_path)]).get(posixpath.basename(a.remote_path))
    local = hash_file(target)[1] if rc == 0 and os.path.isfile(target) else None
    status = "restored" if local and local == remote else "verify_failed"
    print(json.dumps({"remote_path": a.remote_path, "local_path": target, "remote_hash": remote,
                      "local_hash": local, "hash_type": HASH_TYPE, "status": status, "at": now()}), flush=True)
    return EXIT_OK if status == "restored" else EXIT_FAILED


def remove(a) -> int:
    paths = list(a.remote_path or [])
    if a.list:
        with open(a.list, encoding="utf-8") as f:
            paths += [line.strip() for line in f if line.strip()]
    day = dt.date.today().isoformat()
    failed = 0
    for rp in paths:
        rp = rp.replace("\\", "/").strip("/")
        to = posixpath.join(a.quarantine.strip("/"), day, rp)
        rc = _stream([RCLONE, "moveto", remote_root(a.remote, rp), remote_root(a.remote, to)])
        failed += rc != 0
        print(json.dumps({"remote_path": rp, "quarantine_path": to,
                          "status": "quarantined" if rc == 0 else "failed", "at": now()}), flush=True)
    return EXIT_FAILED if failed else EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gdrive-rclone", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("push", help="copy a list of files up and verify each by MD5")
    p.add_argument("--remote", required=True, help="rclone remote name, e.g. gdrive:")
    p.add_argument("--dest", required=True, help="folder on the remote that remote_path is relative to")
    p.add_argument("--list", required=True, help="JSONL file list, or - for stdin")
    p.add_argument("--manifest", default="-", help="JSONL results, appended (default: stdout)")
    p.add_argument("--max-files", type=int, default=300, help="files per rclone run (default 300)")
    p.add_argument("--max-gb", type=float, default=20, help="GB per rclone run (default 20)")
    p.add_argument("--bwlimit", help="passed to rclone, e.g. 40M")
    p.add_argument("--dry-run", action="store_true", help="plan only: no hashing, no rclone")
    p.set_defaults(fn=push)
    p = sub.add_parser("pull", help="copy one file down into a folder, never overwriting")
    p.add_argument("--remote", required=True)
    p.add_argument("--remote-path", required=True)
    p.add_argument("--to", required=True, help="local folder to restore into")
    p.set_defaults(fn=pull)
    p = sub.add_parser("remove", help="move files into a dated quarantine folder (never deletes)")
    p.add_argument("--remote", required=True)
    p.add_argument("--remote-path", action="append", help="file to quarantine; repeat for more")
    p.add_argument("--list", help="text file of remote paths, one per line")
    p.add_argument("--quarantine", default="_removed", help="quarantine folder on the remote (default _removed)")
    p.set_defaults(fn=remove)
    a = ap.parse_args(argv)
    if a.cmd == "remove" and not (a.remote_path or a.list):
        ap.error("remove needs --remote-path or --list")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
