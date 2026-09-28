"""Strict, read-only PVE VM/CT volume-reference inventory.

This module is intentionally dependency-free so the package preinst copy can
use the exact same parser before the payload is unpacked.
"""

from decimal import Decimal, InvalidOperation
import glob
import re


SCHEMA_VERSION = 1
_VOLUME = re.compile(r"[A-Za-z0-9_.+-]+:[A-Za-z0-9_.+-]+")
_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.+-])"
    r"([A-Za-z0-9_.+-]+:[A-Za-z0-9_.+-]+)(?=[,\s]|$)"
)
_QEMU_LINE = re.compile(
    r"^((?:ide|sata|scsi|virtio)\d+|efidisk0|tpmstate0|unused\d+):\s*(.+)$"
)
_LXC_LINE = re.compile(r"^(rootfs|mp\d+|unused\d+):\s*(.+)$")
_SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)([KMGTPE]?)")
_SCALE = {
    "": 1, "K": 2**10, "M": 2**20, "G": 2**30,
    "T": 2**40, "P": 2**50, "E": 2**60,
}


def _valid_disk_key(key, is_lxc):
    if is_lxc and key == "rootfs":
        return True
    if not is_lxc and key in {"efidisk0", "tpmstate0"}:
        return True
    match = re.fullmatch(r"([a-z]+)(\d+)", key)
    if not match or str(int(match.group(2))) != match.group(2):
        return False
    limits = ({"mp": 256, "unused": 256} if is_lxc else {
        "ide": 4, "sata": 6, "scsi": 31, "virtio": 16, "unused": 256,
    })
    return match.group(1) in limits and int(match.group(2)) < limits[match.group(1)]


def _config_paths(globber):
    cluster = set()
    for pattern in (
        "/etc/pve/nodes/*/qemu-server/*.conf",
        "/etc/pve/nodes/*/lxc/*.conf",
    ):
        cluster.update(globber(pattern))
    if cluster:
        return sorted(cluster)
    local = set()
    for pattern in (
        "/etc/pve/qemu-server/*.conf",
        "/etc/pve/lxc/*.conf",
    ):
        local.update(globber(pattern))
    return sorted(local)


def collect(read_text=None, globber=None):
    """Return (all references, current disk references, read errors).

    Current entries are ``(path, size_bytes_or_none, role)`` where role is
    attached, detached, or invalid. No malformed identity is upgraded to SAFE.
    """
    globber = globber or glob.glob

    def default_read(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    read_text = read_text or default_read
    references = {}
    current = {}
    errors = []
    for path in _config_paths(globber):
        content = read_text(path)
        if content is None:
            errors.append(path)
            continue
        for volid in set(_TOKEN.findall(content)):
            references.setdefault(volid, []).append(path)
        is_lxc = "/lxc/" in path
        line_pattern = _LXC_LINE if is_lxc else _QEMU_LINE
        records = []
        key_counts = {}
        for raw in content.splitlines():
            line = raw.strip()
            if line.startswith("["):
                break
            match = line_pattern.search(line)
            if not match:
                continue
            key = match.group(1)
            key_counts[key] = key_counts.get(key, 0) + 1
            invalid_key = not _valid_disk_key(key, is_lxc)
            properties = [item.strip() for item in match.group(2).split(",")]
            malformed_property = any(
                not prop or "=" not in prop for prop in properties[1:]
            )
            identities = []
            identity_fields = 0
            if properties and "=" not in properties[0]:
                identity_fields += 1
                if _VOLUME.fullmatch(properties[0]):
                    identities.append(properties[0])
            named = "volume" if is_lxc else "file"
            prefix = named + "="
            for prop in properties:
                if prop.startswith(prefix):
                    identity_fields += 1
                    if _VOLUME.fullmatch(prop[len(prefix):]):
                        identities.append(prop[len(prefix):])
            if not identities:
                continue
            size_properties = [
                prop[5:] for prop in properties if prop.startswith("size=")
            ]
            size = None
            size_match = (
                _SIZE.fullmatch(size_properties[0])
                if len(size_properties) == 1 else None
            )
            if size_match:
                try:
                    exact = Decimal(size_match.group(1)) * _SCALE[size_match.group(2)]
                    size = int(exact) if exact == exact.to_integral_value() else None
                except (InvalidOperation, KeyError, ValueError):
                    size = None
            records.append((
                key, identities, identity_fields, size,
                invalid_key or malformed_property,
            ))
        for key, identities, identity_fields, size, forced_invalid in records:
            role = (
                "invalid" if forced_invalid
                or identity_fields != 1 or len(identities) != 1
                or key_counts[key] != 1
                else "detached" if key.startswith("unused")
                else "attached"
            )
            for volid in set(identities):
                current.setdefault(volid, []).append((path, size, role))
    return references, current, errors
