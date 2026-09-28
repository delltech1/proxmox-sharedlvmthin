#!/usr/bin/python3
"""Unpackaged read-only candidate serializer. No config/package/storage writes.

Creates a new private evidence directory under /run. Leaves all evidence intact.
Output is a proposal with authorization NONE, never a CAS serializer attestation.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile

MAX = 64 * 1024 * 1024
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C",
       "PERL_HASH_SEED": "0", "PERL_PERTURB_KEYS": "0"}
PLUGIN = "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
ARTIFACT = "usr/share/pve-sharedlvmthin/package-artifact-sha256"
FLAVOR = "usr/share/pve-sharedlvmthin/package-flavor"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_regular(path, limit=MAX):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o022:
            raise ValueError("unsafe input inode")
        raw = bytearray()
        while chunk := os.read(fd, 65536):
            raw.extend(chunk)
            if len(raw) > limit:
                raise ValueError("input exceeds bound")
        after = os.fstat(fd)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink,
                               st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if identity(before) != identity(after) or len(raw) != before.st_size:
            raise ValueError("input changed during read")
        return bytes(raw)
    finally:
        os.close(fd)


def create(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def select_payload(raw):
    """Inspect every entry before writing anything; never use tar extraction."""
    selected, seen = {}, set()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            name = member.name
            if name in (".", "./"):
                if not member.isdir() or name in seen:
                    raise ValueError("invalid root member")
                seen.add(name)
                continue
            if name.startswith("./"):
                name = name[2:]
            parts = name.rstrip("/").split("/")
            if not parts or any(p in ("", ".", "..") for p in parts) or name.startswith("/"):
                raise ValueError("unsafe archive member")
            name = "/".join(parts)
            if name in seen or len(seen) > 10000:
                raise ValueError("duplicate/excessive archive members")
            seen.add(name)
            if not (member.isdir() or member.isfile()) or member.uid != 0 or member.mode & 0o6022:
                raise ValueError("unsafe archive inode")
            relevant = name in (ARTIFACT, FLAVOR) or (name.startswith("usr/share/perl5/PVE/") and name.endswith(".pm"))
            if relevant:
                if not member.isfile() or member.size > 16 * 1024 * 1024:
                    raise ValueError("invalid candidate member")
                selected[name] = archive.extractfile(member).read()
    if not {PLUGIN, ARTIFACT, FLAVOR}.issubset(selected):
        raise ValueError("candidate payload incomplete")
    return selected


def run(argv):
    result = subprocess.run(argv, env=ENV, capture_output=True, timeout=60, check=False)
    if result.returncode or result.stderr or len(result.stdout) > MAX:
        raise ValueError("subprocess failed, warned, or exceeded output bound")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "baseline-sha256", "deb", "deb-sha256", "artifact-sha256", "version", "work-root"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or any(not re.fullmatch("[0-9a-f]{64}", value) for value in
                              (args.baseline_sha256, args.deb_sha256, args.artifact_sha256)):
        raise ValueError("root and exact hash pins required")
    if not re.fullmatch(r"/run/slt-candidate-render-[0-9a-f]{32}", args.work_root):
        raise ValueError("work root must be new /run/slt-candidate-render-<32 hex>")
    root = Path(args.work_root)
    for parent in (Path("/"), Path("/run")):
        st = parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError("unsafe work root ancestor")
    baseline, deb = read_regular(args.baseline, 1024 * 1024), read_regular(args.deb)
    if sha(baseline) != args.baseline_sha256 or sha(deb) != args.deb_sha256:
        raise ValueError("baseline/package pin differs")
    root.mkdir(mode=0o700)  # create-only; no existing directory may be reused
    create(root / "candidate.deb", deb)
    create(root / "baseline.cfg", baseline)
    control = run(["/usr/bin/dpkg-deb", "-f", str(root / "candidate.deb"), "Package", "Version", "Architecture"])
    expected = f"Package: pve-sharedlvmthin\nVersion: {args.version}\nArchitecture: all\n".encode()
    if control != expected:
        raise ValueError("candidate package/version/architecture differs")
    members = select_payload(run(["/usr/bin/dpkg-deb", "--fsys-tarfile", str(root / "candidate.deb")]))
    if members[ARTIFACT] != (args.artifact_sha256 + "\n").encode() or members[FLAVOR] != b"dual\n":
        raise ValueError("candidate artifact/flavor differs")
    for name, raw in members.items():
        path = root / "payload" / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        create(path, raw)
    helper = Path(__file__).with_name("layout-migration-candidate-render.pl")
    helper_raw = read_regular(helper)
    create(root / "renderer.pl", helper_raw)
    result = json.loads(run(["/usr/bin/perl", "-T", str(root / "renderer.pl"), str(root)]))
    if result.get("authorization") != "NONE" or result.get("classification") != "READ_ONLY_CANDIDATE_TARGET":
        raise ValueError("renderer did not return read-only result")
    if result.get("baseline_sha256") != args.baseline_sha256:
        raise ValueError("renderer baseline differs")
    inventory = {row["name"]: row for row in result["modules"]}
    for name, raw in members.items():
        if not name.startswith("usr/share/perl5/"):
            continue
        module = name.removeprefix("usr/share/perl5/")
        if module in inventory and inventory[module] != {
                "name": module, "path": str(root / "payload" / name), "sha256": sha(raw)}:
            raise ValueError("loaded candidate module differs from pinned package")
    if "PVE/Storage/Custom/SharedLvmThinPlugin.pm" not in inventory:
        raise ValueError("candidate plugin provenance missing")
    target = bytes.fromhex(result["target_hex"])
    if sha(target) != result["target_sha256"]:
        raise ValueError("renderer target digest differs")
    result["candidate"] = dict(package="pve-sharedlvmthin", version=args.version, flavor="dual",
                               deb_sha256=args.deb_sha256, artifact_sha256=args.artifact_sha256)
    result["collector_sha256"] = sha(read_regular(__file__))
    result["helper_sha256"] = sha(helper_raw)
    result["work_root"] = str(root)
    create(root / "target.cfg", target)
    create(root / "result.json", json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(json.dumps({"classification": "REFUSED", "authorization": "NONE", "reason": str(exc)}))
        raise SystemExit(2)
