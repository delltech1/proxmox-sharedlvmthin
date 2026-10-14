"""Hold established local executor locks through a package transaction.

Not a cluster fence or standalone authorization. Caller must authenticate the
complete alias/volume inventory, close package admission with an old-runtime
compatible protocol, and retain this context through postqualification.
"""
from contextlib import contextmanager
import fcntl
import os
import re
import stat
import json


def create_sealed_context(identities, descriptors, boot):
    """Transport exact inherited scope through one immutable memfd, not argv."""
    data = json.dumps({"schema": 1, "scopes": list(identities),
                       "fds": list(descriptors), "boot": boot}, separators=(",", ":")).encode()
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("package executor context exceeds bounded transport")
    fd = os.memfd_create("sharedlvmthin-package-fence", os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
    try:
        os.fchmod(fd, 0o600)
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written < 1:
                raise OSError("incomplete executor context write")
            remaining = remaining[written:]
        seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_sealed_context(fd, boot, directory="/run/lock"):
    if type(fd) is not int or fd < 3:
        raise ValueError("invalid executor context descriptor")
    observed = os.fstat(fd)
    seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    if not stat.S_ISREG(observed.st_mode) or observed.st_uid != 0 \
            or stat.S_IMODE(observed.st_mode) != 0o600 \
            or not 0 < observed.st_size <= 8 * 1024 * 1024 \
            or fcntl.fcntl(fd, fcntl.F_GET_SEALS) & seals != seals:
        raise ValueError("executor context is not an immutable root-owned sealed object")
    raw = os.pread(fd, observed.st_size + 1, 0)
    if len(raw) != observed.st_size:
        raise ValueError("executor context read is incomplete")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate executor context key")
            result[key] = value
        return result
    context = json.loads(raw, object_pairs_hook=unique)
    if type(context) is not dict or set(context) != {"schema", "scopes", "fds", "boot"} \
            or type(context["schema"]) is not int or context["schema"] != 1 \
            or context["boot"] != boot or type(context["scopes"]) is not list \
            or type(context["fds"]) is not list:
        raise ValueError("executor context identity/schema mismatch")
    verify_inherited_executors(context["scopes"], context["fds"], directory)
    return context


def verify_inherited_executors(identities, descriptors, directory="/run/lock"):
    """Prove exact inherited OFDs own the established executor lock scope.

    Does not trust an environment boolean or a process-name scan. A free lock
    or a lock held through another open-file description is not proof.
    """
    names = set()
    for sid, volume in identities:
        if type(sid) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", sid) \
                or type(volume) is not str or not re.fullmatch(r"(?:vm|base)-[0-9]+-disk-[0-9]+", volume):
            raise ValueError("invalid inherited executor scope")
        names.update(f"pve-sharedlvmthin-{family}-{sid}-{volume}.lock"
                     for family in ("lazy", "thick-transition"))
    descriptors = tuple(descriptors)
    if not names or len(descriptors) != len(names) or len(set(descriptors)) != len(descriptors) \
            or any(type(fd) is not int or fd < 3 for fd in descriptors):
        raise ValueError("incomplete or ambiguous inherited executor descriptors")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        ds = os.fstat(directory_fd)
        if ds.st_uid != 0 or (ds.st_mode & 0o022 and not ds.st_mode & stat.S_ISVTX):
            raise ValueError("unsafe inherited executor directory")
        files = {}
        for name in names:
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(observed.st_mode) or observed.st_uid != 0 \
                    or stat.S_IMODE(observed.st_mode) != 0o600 or observed.st_nlink != 1:
                raise ValueError("unsafe inherited executor file")
            identity = (observed.st_dev, observed.st_ino)
            if identity in files:
                raise ValueError("executor files alias one inode")
            files[identity] = name
        seen = set()
        for fd in descriptors:
            observed = os.fstat(fd)
            identity = (observed.st_dev, observed.st_ino)
            if identity not in files or identity in seen:
                raise ValueError("inherited descriptor does not match exact lock file")
            seen.add(identity)
            probe = os.open(files[identity], os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                ps = os.fstat(probe)
                if (ps.st_dev, ps.st_ino) != identity:
                    raise ValueError("executor pathname changed during inherited proof")
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise ValueError("inherited executor lock was not held")
                # Same inherited OFD succeeds without releasing the lock; an
                # unrelated descriptor for someone else's lock must refuse.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
        return True
    finally:
        os.close(directory_fd)


@contextmanager
def hold_executors(identities, directory="/run/lock"):
    identities = list(identities)
    if not identities or len(identities) > 100_000:
        raise ValueError("missing or excessive executor scope")
    canonical = set()
    for row in identities:
        if type(row) is not tuple or len(row) != 2:
            raise ValueError("executor identity must be a storage/volume tuple")
        sid, volume = row
        if type(sid) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", sid) \
                or type(volume) is not str or not re.fullmatch(r"(?:vm|base)-[0-9]+-disk-[0-9]+", volume):
            raise ValueError("invalid executor identity")
        canonical.add(row)
    descriptors = []
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory_stat = os.fstat(directory_fd)
        if directory_stat.st_uid != 0 or (directory_stat.st_mode & 0o022
                                         and not directory_stat.st_mode & stat.S_ISVTX):
            raise ValueError("unsafe executor lock directory")
        for sid, volume in sorted(canonical):
            for family in ("lazy", "thick-transition"):
                filename = f"pve-sharedlvmthin-{family}-{sid}-{volume}.lock"
                fd = os.open(filename, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
                descriptors.append(fd)
                observed = os.fstat(fd)
                if not stat.S_ISREG(observed.st_mode) or observed.st_uid != 0 \
                        or stat.S_IMODE(observed.st_mode) != 0o600 or observed.st_nlink != 1:
                    raise ValueError("unsafe existing executor lock")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino):
                    raise ValueError("executor lock path changed during acquisition")
        # Caller must pass these exact descriptors through subprocess pass_fds
        # to keep locks alive if its wrapper dies while dpkg still runs.
        yield tuple(descriptors)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
        os.close(directory_fd)
