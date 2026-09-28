#!/usr/bin/python3
"""Derive a deterministic identity for one logical Debian package artifact."""

import hashlib
import json
import os
import pathlib
import stat
import sys


DATA_IDENTITY = pathlib.PurePosixPath(
    "usr/share/pve-sharedlvmthin/package-artifact-sha256"
)
CONTROL_EXCLUDED = {
    pathlib.PurePosixPath("md5sums"),
    pathlib.PurePosixPath("sharedlvmthin-candidate-artifact-sha256"),
}


def fail(message):
    raise SystemExit(f"package artifact identity: {message}")


def files_below(root, namespace, excluded, skip_debian=False):
    root = pathlib.Path(root)
    if not root.is_dir() or root.is_symlink():
        fail(f"{namespace} root is not a real directory: {root}")
    records = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = pathlib.Path(current)
        for name in list(directories):
            path = current_path / name
            if path.is_symlink():
                fail(f"symlink is not allowed: {path}")
        for name in files:
            path = current_path / name
            relative = pathlib.PurePosixPath(path.relative_to(root).as_posix())
            if skip_debian and relative.parts[:1] == ("DEBIAN",):
                continue
            if relative in excluded:
                continue
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                fail(f"non-regular artifact member is not allowed: {path}")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            records.append(
                (
                    f"{namespace}/{relative.as_posix()}",
                    format(stat.S_IMODE(metadata.st_mode), "04o"),
                    metadata.st_size,
                    digest.hexdigest(),
                )
            )
    return records


def main(argv):
    if len(argv) != 3:
        fail("usage: package-artifact-identity.py DATA_ROOT CONTROL_ROOT")
    data_root = pathlib.Path(argv[1]).resolve()
    control_root = pathlib.Path(argv[2]).resolve()
    # During build the control tree physically lives at DATA_ROOT/DEBIAN and
    # must not be counted twice. During release verification dpkg-deb extracts
    # data and control into independent roots; a crafted data member named
    # DEBIAN/... is then data and must remain covered rather than disappearing.
    staged_control = control_root == data_root / "DEBIAN"
    records = files_below(
        data_root, "data", {DATA_IDENTITY}, skip_debian=staged_control
    ) + files_below(control_root, "control", CONTROL_EXCLUDED)
    records.sort(key=lambda record: record[0].encode("utf-8"))
    digest = hashlib.sha256()
    for record in records:
        encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        digest.update(encoded.encode("ascii") + b"\n")
    print(digest.hexdigest())


if __name__ == "__main__":
    main(sys.argv)
