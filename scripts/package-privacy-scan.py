#!/usr/bin/env python3
"""Fail closed when an extracted Debian archive contains private identifiers."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import stat
import sys
from pathlib import Path


MAX_FILE = 32 * 1024 * 1024
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10." + "0.0.0/8", "172." + "16.0.0/12", "192." + "168.0.0/16",
))
IPV4 = re.compile(rb"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
EMAIL = re.compile(rb"(?i)(?<![a-z0-9._%+-])([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})(?![a-z0-9.-])")
CREDENTIAL = re.compile(
    rb"(?i)\b(password|passwd|passphrase|api[_-]?key|access[_-]?token|secret)"
    rb"\s*[:=]\s*['\"]?([^\s'\"]{4,})"
)
FORBIDDEN_BYTES = (
    b"-----begin private key-----", b"-----begin openssh private key-----",
    b"-----begin rsa private key-----", b"-----begin ec private key-----",
    b"ssh-rsa ", b"ssh-ed25519 ", b".codex", b"codex-remote-attachments",
    b"corosync-cluster-pg", b"dev-prx", b"oke-dev",
)
LOCAL_PATHS = (
    re.compile(rb"(?i)[a-z]:[\\/]+users[\\/]+"),
    re.compile(rb"(?i)(?:^|[^a-z0-9])/(?:home|root)/"),
    re.compile(rb"(?i)(?:^|[^a-z0-9])/(?:var/)?tmp/slt[-_/]"),
)
ALLOWED_EMAILS = {b"noreply@users.noreply.github.com"}


class PrivacyError(Exception):
    pass


def require(value, message):
    if not value:
        raise PrivacyError(message)


def scan_bytes(data: bytes, label: str) -> None:
    lowered = data.lower()
    for marker in FORBIDDEN_BYTES:
        require(marker not in lowered, f"forbidden private marker in {label}")
    for pattern in LOCAL_PATHS:
        require(pattern.search(data) is None, f"local operator path in {label}")
    for raw in IPV4.findall(data):
        try:
            address = ipaddress.ip_address(raw.decode("ascii"))
        except ValueError:
            continue
        require(not any(address in network for network in PRIVATE_NETWORKS),
                f"private IPv4 address in {label}")
    for match in EMAIL.finditer(data):
        address = match.group(1).lower()
        require(address in ALLOWED_EMAILS,
                f"non-project email address in {label}")
    require(CREDENTIAL.search(data) is None,
            f"credential-like assignment in {label}")


def scan_tree(root: Path, namespace: str) -> None:
    try:
        root_info = root.lstat()
    except OSError as error:
        raise PrivacyError(f"scan root is unavailable: {namespace}: {error}") from error
    require(stat.S_ISDIR(root_info.st_mode) and not root.is_symlink(),
            f"scan root is not a regular directory: {namespace}")
    for directory, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        base = Path(directory)
        for name in directories + files:
            relative = (base / name).relative_to(root).as_posix()
            scan_bytes(relative.encode("utf-8", "surrogateescape"),
                       f"{namespace} path")
        for name in files:
            path = base / name
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and not path.is_symlink(),
                    f"non-regular file in {namespace}: {path.relative_to(root)}")
            require(info.st_size <= MAX_FILE,
                    f"oversized file cannot be privacy-scanned: {namespace}/{path.relative_to(root)}")
            data = path.read_bytes()
            require(len(data) == info.st_size,
                    f"file changed during privacy scan: {namespace}/{path.relative_to(root)}")
            scan_bytes(data, f"{namespace}/{path.relative_to(root).as_posix()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tree", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        for index, tree in enumerate(args.tree, 1):
            scan_tree(tree, f"archive-{index}")
    except (OSError, PrivacyError) as error:
        print(f"package privacy scan: FAIL: {error}", file=sys.stderr)
        return 1
    print("package privacy scan: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
