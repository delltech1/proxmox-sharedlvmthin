#!/usr/bin/env python3
"""Fail when the Git index contains likely private lab or credential material."""

from __future__ import annotations

import ipaddress
import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
TEXT_LIMIT = 8 * 1024 * 1024
EXAMPLE_NETS = tuple(
    ipaddress.ip_network(value)
    for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
FIXED = (
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I)),
    ("SSH public key", re.compile(rb"ssh-(?:rsa|ed25519)\s+[A-Za-z0-9+/]{40,}", re.I)),
    ("Windows user path", re.compile(rb"[A-Za-z]:\\Users\\[^\\\r\n]+", re.I)),
    ("Unix user home path", re.compile(rb"/home/[^/\s]+/", re.I)),
    ("private email", re.compile(rb"[A-Z0-9._%+-]+@(?!(?:example\.(?:com|org|net)|users\.noreply\.github\.com)\b)[A-Z0-9.-]+\.[A-Z]{2,}", re.I)),
    ("literal credential assignment", re.compile(rb"\b(?:password|passwd|secret|token)\s*[:=]\s*['\"][^'\"\r\n]{4,}['\"]", re.I)),
)
IPV4 = re.compile(rb"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
LAB_HOST = re.compile(rb"\b(?:PVE0[1-9]|DEV-PRX[A-Z0-9._-]*)\b", re.I)


def tracked_files() -> list[pathlib.Path]:
    try:
        raw = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT, stderr=subprocess.DEVNULL
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        # GitHub source archives and extracted release candidates have no
        # .git directory. Scan every regular file in that bounded tree.
        return [path for path in ROOT.rglob("*") if path.is_file()]
    return [ROOT / pathlib.Path(p.decode("utf-8")) for p in raw.split(b"\0") if p]


def forbidden_ip(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    if addr.is_loopback or any(addr in net for net in EXAMPLE_NETS):
        return False
    return addr.is_private or addr.is_link_local


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("tests/") or rel == "scripts/package-privacy-scan.py" or rel == "scripts/public-source-privacy-scan.py":
            continue
        if not path.is_file() or path.stat().st_size > TEXT_LIMIT:
            continue
        data = path.read_bytes()
        if b"\0" in data[:4096]:
            continue
        for label, pattern in FIXED:
            if pattern.search(data):
                findings.append(f"{rel}: {label}")
        if not rel.startswith("tests/") and LAB_HOST.search(data):
            findings.append(f"{rel}: lab hostname")
        for match in IPV4.finditer(data):
            value = match.group().decode("ascii")
            if forbidden_ip(value):
                findings.append(f"{rel}: private/link-local IPv4 address {value}")
    if findings:
        print("PUBLIC_SOURCE_PRIVACY_SCAN=FAIL", file=sys.stderr)
        for finding in sorted(set(findings)):
            print(finding, file=sys.stderr)
        return 1
    print("PUBLIC_SOURCE_PRIVACY_SCAN=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
