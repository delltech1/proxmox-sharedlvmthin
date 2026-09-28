#!/usr/bin/python3
"""Create-only exact journal file backend; never reopens, repairs or deletes."""

import hashlib
import copy
import os
from pathlib import Path
import re
import stat
import threading


PARENT = "/var/tmp"
PREFIX = "slt-prelive-journal-"
_CREATE_KEY = object()


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _hex32(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _write_all(fd, data):
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        require(type(written) is int and 0 < written <= len(data) - offset,
                "journal write made no strict progress")
        offset += written
    return offset


class ExactJournalFileBackend:
    """One fresh pinned directory and create-only records on the owner thread."""

    source_only = True

    def __init__(self, key, nonce):
        require(key is _CREATE_KEY and _hex32(nonce),
                "file backend construction is private")
        self.nonce = nonce
        self.root_name = PREFIX + nonce
        self.owner_pid = os.getpid()
        self.owner_thread = threading.current_thread()
        self.parent_fd = None
        self.root_fd = None
        self.parent_identity = None
        self.root_identity = None
        self.poisoned = False
        self.release_evidence = None
        self.release_started = False
        self.persisted_records = []
        self.state = "CREATING"
        try:
            self._create()
            require(self.state == "CREATING", "backend creation reentrancy")
            self.state = "READY"
        except BaseException:
            self.state = "UNKNOWN"
            self._best_effort_close()
            raise

    def __copy__(self):
        raise Refusal("file backend is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("file backend is noncopyable")

    def _poison(self):
        self.poisoned = True
        self.state = "UNKNOWN"

    def _assert_owner(self, expected="READY"):
        if (os.getpid() != self.owner_pid
                or threading.current_thread() is not self.owner_thread
                or self.state != expected or self.poisoned):
            self._poison()
            raise Refusal("file backend owner or phase changed")

    def _best_effort_close(self):
        for name in ("root_fd", "parent_fd"):
            fd = getattr(self, name, None)
            if fd is not None:
                setattr(self, name, None)
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _directory_identity(self, info):
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid(),
                "journal directory owner/type")
        return {"dev": info.st_dev, "inode": info.st_ino,
                "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)}

    def _create(self):
        for fd in (0, 1, 2):
            try:
                os.fstat(fd)
            except OSError as exc:
                raise Refusal("standard descriptors must already be open") from exc
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self.parent_fd = os.open(PARENT, flags)
        require(integer(self.parent_fd, 3), "unsafe parent descriptor")
        parent_fd = os.fstat(self.parent_fd)
        parent_name = os.stat(PARENT, follow_symlinks=False)
        self.parent_identity = self._directory_identity(parent_fd)
        require((parent_name.st_dev, parent_name.st_ino)
                == (parent_fd.st_dev, parent_fd.st_ino)
                and stat.S_ISDIR(parent_name.st_mode),
                "journal parent namespace identity")
        os.mkdir(self.root_name, 0o700, dir_fd=self.parent_fd)
        os.fsync(self.parent_fd)
        self.root_fd = os.open(self.root_name, flags, dir_fd=self.parent_fd)
        require(integer(self.root_fd, 3) and self.root_fd != self.parent_fd,
                "unsafe root descriptor")
        root_fd = os.fstat(self.root_fd)
        root_name = os.stat(self.root_name, dir_fd=self.parent_fd,
                            follow_symlinks=False)
        self.root_identity = self._directory_identity(root_fd)
        require((root_name.st_dev, root_name.st_ino)
                == (root_fd.st_dev, root_fd.st_ino)
                and root_fd.st_uid == os.geteuid()
                and root_name.st_uid == os.geteuid()
                and stat.S_IMODE(root_fd.st_mode) == 0o700
                and stat.S_IMODE(root_name.st_mode) == 0o700,
                "journal root namespace identity")

    def _verify_namespace(self):
        parent_fd = os.fstat(self.parent_fd)
        parent_name = os.stat(PARENT, follow_symlinks=False)
        root_fd = os.fstat(self.root_fd)
        root_name = os.stat(self.root_name, dir_fd=self.parent_fd,
                            follow_symlinks=False)
        require((parent_fd.st_dev, parent_fd.st_ino, parent_fd.st_uid,
                 stat.S_IMODE(parent_fd.st_mode))
                == (self.parent_identity["dev"], self.parent_identity["inode"],
                    self.parent_identity["uid"], self.parent_identity["mode"])
                and (parent_name.st_dev, parent_name.st_ino)
                == (parent_fd.st_dev, parent_fd.st_ino)
                and (root_fd.st_dev, root_fd.st_ino, root_fd.st_uid,
                     stat.S_IMODE(root_fd.st_mode))
                == (self.root_identity["dev"], self.root_identity["inode"],
                    self.root_identity["uid"], self.root_identity["mode"])
                and (root_name.st_dev, root_name.st_ino, root_name.st_uid,
                     stat.S_IMODE(root_name.st_mode))
                == (root_fd.st_dev, root_fd.st_ino, root_fd.st_uid,
                    stat.S_IMODE(root_fd.st_mode)),
                "journal namespace continuity")

    def persist_exact_file(self, name, raw):
        self._assert_owner()
        require(type(name) is str
                and re.fullmatch(r"exact-event-[0-9]{6}\.json", name)
                and type(raw) is bytes and 0 < len(raw) <= 65536,
                "exact record input")
        self.state = "PERSISTING"
        fd = None
        failure = None
        identity = None
        try:
            self._verify_namespace()
            flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                     | os.O_CLOEXEC)
            fd = os.open(name, flags, 0o600, dir_fd=self.root_fd)
            require(integer(fd, 3)
                    and fd not in (self.parent_fd, self.root_fd),
                    "unsafe record descriptor")
            first = os.fstat(fd)
            named = os.stat(name, dir_fd=self.root_fd, follow_symlinks=False)
            require(stat.S_ISREG(first.st_mode) and first.st_uid == os.geteuid()
                    and stat.S_IMODE(first.st_mode) == 0o600
                    and first.st_nlink == 1
                    and (first.st_dev, first.st_ino)
                    == (named.st_dev, named.st_ino),
                    "fresh record identity")
            written = _write_all(fd, raw)
            os.fsync(fd)
            os.fsync(self.root_fd)
            final = os.fstat(fd)
            final_name = os.stat(name, dir_fd=self.root_fd,
                                 follow_symlinks=False)
            require((final.st_dev, final.st_ino, final.st_uid,
                     stat.S_IMODE(final.st_mode), final.st_nlink, final.st_size)
                    == (first.st_dev, first.st_ino, first.st_uid, 0o600, 1,
                        len(raw))
                    and (final_name.st_dev, final_name.st_ino,
                         final_name.st_uid, stat.S_IMODE(final_name.st_mode),
                         final_name.st_nlink, final_name.st_size)
                    == (final.st_dev, final.st_ino, final.st_uid, 0o600, 1,
                        len(raw)), "final record identity")
            identity = {"dev": final.st_dev, "inode": final.st_ino,
                        "uid": final.st_uid, "mode": 0o600, "nlink": 1}
            require(self.state == "PERSISTING", "persistence reentrancy")
        except BaseException as exc:
            failure = exc
        if fd is not None:
            try:
                os.close(fd)
            except BaseException as exc:
                failure = failure or exc
        if failure is not None:
            self._poison()
            raise Refusal("exact record persistence is ambiguous") from failure
        try:
            self._verify_namespace()
            final_name = os.stat(name, dir_fd=self.root_fd,
                                 follow_symlinks=False)
            require((final_name.st_dev, final_name.st_ino,
                     final_name.st_uid, stat.S_IMODE(final_name.st_mode),
                     final_name.st_nlink, final_name.st_size)
                    == (identity["dev"], identity["inode"], identity["uid"],
                        0o600, 1, len(raw)),
                    "post-close named record identity")
            require(self.state == "PERSISTING", "post-close reentrancy")
        except BaseException:
            self._poison()
            raise
        self.state = "READY"
        ack = {"bytes_written": written, "file_synced": True,
                "dir_synced": True, "closed": True, "name": name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "record_identity": identity}
        self.persisted_records.append(copy.deepcopy(ack))
        return ack

    def qualification_evidence(self):
        self._assert_owner()
        return {"root_identity": copy.deepcopy(self.root_identity),
                "records": copy.deepcopy(self.persisted_records)}

    def close_preserving_artifacts(self):
        self._assert_owner()
        evidence = self.release_descriptors_preserving_state()
        require(evidence["prior_state"] == "READY"
                and evidence["close_ambiguous"] is False
                and self.state == "CLOSED_ARTIFACTS_PRESERVED"
                and self.poisoned is False, "clean backend close")

    def release_descriptors_preserving_state(self):
        if (os.getpid() != self.owner_pid
                or threading.current_thread() is not self.owner_thread
                or self.state in ("CLOSING", "CLOSED_ARTIFACTS_PRESERVED")
                or self.release_started or self.release_evidence is not None):
            self._poison()
            raise Refusal("descriptor release unavailable")
        prior = self.state
        if prior not in ("READY", "UNKNOWN"):
            self._poison()
            raise Refusal("descriptor release phase")
        self.release_started = True
        if prior == "READY":
            self.state = "CLOSING"
        failure = None
        for name in ("root_fd", "parent_fd"):
            fd = getattr(self, name)
            setattr(self, name, None)
            try:
                os.close(fd)
            except BaseException as exc:
                failure = failure or exc
        close_ambiguous = failure is not None
        if (prior == "READY"
                and (close_ambiguous or self.state != "CLOSING"
                     or self.poisoned)):
            self._poison()
        elif prior == "UNKNOWN":
            self._poison()
        else:
            self.state = "CLOSED_ARTIFACTS_PRESERVED"
        self.release_evidence = {
            "prior_state": prior, "root_fd_released": self.root_fd is None,
            "parent_fd_released": self.parent_fd is None,
            "close_ambiguous": close_ambiguous,
            "final_state": self.state}
        if close_ambiguous:
            raise Refusal("backend descriptor release is ambiguous") from failure
        return dict(self.release_evidence)

    def artifact_path(self):
        return str(Path(PARENT) / self.root_name)


def create_fresh_file_backend(nonce):
    return ExactJournalFileBackend(_CREATE_KEY, nonce)
