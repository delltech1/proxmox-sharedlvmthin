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
    r"^((?:ide|sata|scsi|virtio)\d+|efidisk0|tpmstate0|unused\d+):\s*(.*)$"
)
_LXC_LINE = re.compile(r"^(rootfs|mp\d+|unused\d+):\s*(.*)$")
# Empty values still count as keys: a trailing empty duplicate must not make
# an earlier vmstate look like an unambiguous authoritative reference.
_VMSTATE_LINE = re.compile(r"^vmstate:\s*(.*)$")
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
    attached, efi, detached, auxiliary, or invalid. ``efi`` is granted only
    by the exact, unique top-level QEMU ``efidisk0`` key after the same strict
    identity/property validation as an attached data disk. No malformed
    identity is upgraded to SAFE.
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
        keys_with_identity = set()
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
            keys_with_identity.add(key)
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
                else "efi" if not is_lxc and key == "efidisk0"
                else "attached"
            )
            for volid in set(identities):
                current.setdefault(volid, []).append((path, size, role))
        # A duplicate key made solely of empty/unparseable values has no
        # volume identity to which an ``invalid`` row can be attached.  Keep
        # that ambiguity visible instead of silently proving absence.
        if any(count > 1 and key not in keys_with_identity
               for key, count in key_counts.items()):
            errors.append(path)

        # A VM-state volume is intentionally referenced from a QEMU snapshot
        # section rather than the live, top-level disk set.  It is still an
        # authoritative PVE reference to a managed LV and must participate in
        # Thick anchor ownership checks.  Parse only the exact vmstate key and
        # require the exact scalar pve-volume-id schema used by qemu-server.
        # A comma property list or file= alias is not valid for vmstate and
        # must never be upgraded to an authoritative auxiliary reference.
        # Bind the state-volume VMID to the config VMID as a second identity
        # proof; malformed or cross-VM references remain visible as invalid
        # so recovery fails closed instead of treating them as absent.
        if not is_lxc:
            vmid_match = re.search(r"/qemu-server/(\d+)\.conf$", path)
            config_vmid = vmid_match.group(1) if vmid_match else None
            vmstate_records = []
            vmstate_counts = {}
            section = ("current", "")
            for raw in content.splitlines():
                line = raw.strip()
                header = re.fullmatch(r"\[([^\]]+)\]", line)
                if header:
                    section = ("section", header.group(1))
                    continue
                match = _VMSTATE_LINE.fullmatch(line)
                if not match:
                    continue
                vmstate_counts[section] = vmstate_counts.get(section, 0) + 1
                value = match.group(1).strip()
                identities = set(_TOKEN.findall(value))
                if _VOLUME.fullmatch(value):
                    identities.add(value)
                exact = value if _VOLUME.fullmatch(value) else None
                vmstate_records.append((section, identities, exact))
            for section, identities, exact in vmstate_records:
                exact_name = exact.split(":", 1)[1] if exact else None
                expected = (
                    rf"vm-{re.escape(config_vmid)}-state-"
                    r"[A-Za-z0-9][A-Za-z0-9_.-]*"
                    if config_vmid else None
                )
                role = (
                    "auxiliary"
                    if exact is not None and len(identities) == 1
                    and vmstate_counts[section] == 1
                    and expected is not None
                    and re.fullmatch(expected, exact_name)
                    else "invalid"
                )
                for volid in identities:
                    current.setdefault(volid, []).append((path, None, role))
            # A duplicate with no parseable identity cannot be represented by
            # an invalid volume row. Keep that ambiguity visible to callers.
            if any(count > 1 and not any(ids for sec, ids, _ in vmstate_records if sec == section)
                   for section, count in vmstate_counts.items()):
                if path not in errors:
                    errors.append(path)
    return references, current, errors
