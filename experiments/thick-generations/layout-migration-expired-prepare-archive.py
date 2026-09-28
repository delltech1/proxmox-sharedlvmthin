#!/usr/bin/env python3
"""Archive an exact expired PREPARE tree, retaining every byte for recovery.

No CLI, package/service effect, release, retry or deletion operation exists.
The fixed-root entry point takes existing dpkg, CAS and maintenance locks and
rechecks current facts; caller assertions alone cannot authorize the rename.
An unpacked candidate with its exact PREINST receipt is supported, but an
already configured candidate, deferred receipt or any CAS reservation refuses.
The injectable file-lab primitive is solely for disposable local qualification.
"""
import copy
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import time

HERE = Path(__file__).resolve().parent
FIXED_ROOT = Path("/var/lib/pve-sharedlvmthin/maintenance")
CAS_ROOT = FIXED_ROOT.parent / "storage-config-cas"
DPKG_LOCKS = (Path("/var/lib/dpkg/lock-frontend"), Path("/var/lib/dpkg/lock"))


def load(name, path, extensionless=False):
    spec = (importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))
            if extensionless else importlib.util.spec_from_file_location(name, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FILES = load("expired_prepare_files", HERE / "local-hold-release-file-lab.py")
CHECK = load("expired_prepare_check", HERE.parents[1] / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-package-maintenance-check", True)
Refusal, require = FILES.Refusal, FILES.require
SHA = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,240}$")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields), label + " fields invalid")


def decode(raw):
    require(type(raw) is bytes and 0 < len(raw) <= 1024 * 1024, "JSON bytes invalid")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def inode(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _scan(root_fd, uid):
    """Bounded no-follow snapshot, including bytes and stable inode identity."""
    result, payloads = {}, {}
    root_device = os.fstat(root_fd).st_dev
    total = 0
    def walk(fd, prefix, depth):
        nonlocal total
        require(depth <= 8 and len(result) <= 1024, "evidence tree exceeds bound")
        before = os.fstat(fd)
        require(stat.S_ISDIR(before.st_mode) and before.st_uid == uid
                and stat.S_IMODE(before.st_mode) == 0o700 and before.st_dev == root_device,
                "evidence directory unsafe")
        names = sorted(os.listdir(fd))
        result[prefix] = {"kind": "directory", "dev": before.st_dev, "ino": before.st_ino,
                          "uid": uid, "mode": 0o700}
        for name in names:
            require(len(result) < 1024, "evidence tree exceeds bound")
            require(SAFE.fullmatch(name) and name not in (".", ".."), "evidence name unsafe")
            path = prefix + "/" + name if prefix else name
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            require(entry.st_uid == uid and entry.st_dev == root_device, "evidence owner/device differs")
            if stat.S_ISDIR(entry.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    require(inode(os.fstat(child)) == inode(entry), "evidence directory changed")
                    walk(child, path, depth + 1)
                    require(inode(os.fstat(child)) == inode(os.stat(name, dir_fd=fd, follow_symlinks=False)),
                            "evidence directory replaced")
                finally: os.close(child)
            else:
                require(stat.S_ISREG(entry.st_mode) and stat.S_IMODE(entry.st_mode) == 0o600
                        and entry.st_nlink == 1 and 0 <= entry.st_size <= 1024 * 1024,
                        "evidence file unsafe")
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    require(inode(os.fstat(file_fd)) == inode(entry), "evidence file replaced")
                    raw = bytearray()
                    while len(raw) <= entry.st_size:
                        chunk = os.read(file_fd, min(65536, entry.st_size + 1 - len(raw)))
                        if not chunk: break
                        raw.extend(chunk)
                    require(len(raw) == entry.st_size
                            and inode(entry) == inode(os.fstat(file_fd))
                            == inode(os.stat(name, dir_fd=fd, follow_symlinks=False)), "evidence file changed")
                    total += len(raw)
                    require(total <= 16 * 1024 * 1024, "evidence bytes exceed bound")
                    payloads[path] = bytes(raw)
                    result[path] = {"kind": "file", "identity": list(inode(entry)), "sha256": digest(raw)}
                finally: os.close(file_fd)
        require(names == sorted(os.listdir(fd)) and inode(before) == inode(os.fstat(fd)),
                "evidence directory changed during scan")
    walk(root_fd, "", 0)
    return result, payloads


def snapshot(root, uid=0):
    """Read-only inspection; returned digest binds namespace, inodes and bytes."""
    descriptors, _ = FILES.BASE._open_pinned_directory(Path(root), uid)
    try:
        tree, _ = _scan(descriptors[-1], uid)
        return {"tree": tree, "tree_sha256": digest(canonical(tree))}
    finally:
        for fd in reversed(descriptors): os.close(fd)


def validate(request, manifest_raw, facts, now):
    base_fields = {"schema", "operation_id", "node", "boot_id", "manifest_sha256",
                   "evidence_tree_sha256", "storage_cfg_sha256", "expected_package", "receipt_sha256"}
    if request.get("schema") == "slt-expired-prepare-archive/v2":
        exact(request, base_fields | {"preexisting_unpacked_proof"}, "abort request")
    else:
        exact(request, base_fields, "abort request")
    require(request["schema"] in {"slt-expired-prepare-archive/v1", "slt-expired-prepare-archive/v2"}
            and type(request["operation_id"]) is str and HEX32.fullmatch(request["operation_id"])
            and type(now) is int and now > 0, "abort identity/clock invalid")
    for key in ("manifest_sha256", "evidence_tree_sha256", "storage_cfg_sha256"):
        require(type(request[key]) is str and SHA.fullmatch(request[key]), "abort digest invalid")
    require(digest(manifest_raw) == request["manifest_sha256"], "expected manifest bytes differ")
    manifest = decode(manifest_raw)
    require(type(manifest) is dict and type(manifest.get("issued_at")) is int
            and type(manifest.get("expires_at")) is int and type(manifest.get("generation")) is int
            and manifest["generation"] > 0 and 0 < manifest["issued_at"] < manifest["expires_at"] < now,
            "PREPARE has not expired")
    exact(facts, {"node", "boot_id", "cluster_name", "corosync_conf_sha256",
                  "storage_cfg_sha256", "package", "blocking_processes", "payload_verified"}, "current facts")
    require(facts["node"] == request["node"] and facts["boot_id"] == request["boot_id"]
            and facts["storage_cfg_sha256"] == request["storage_cfg_sha256"]
            and type(facts["blocking_processes"]) is list and facts["blocking_processes"] == [],
            "local identity/configuration changed or blocking process is live")
    candidate = manifest["candidate"]
    # Historical structural validation only. Actual archive authorization above
    # requires NOW > expiry and can never admit an unexpired manifest.
    try:
        CHECK.validate_manifest(manifest, expected_phase="PREPARE_READY",
            package=candidate["package"], version=candidate["version"], flavor=candidate["flavor"],
            artifact_sha256=candidate["artifact_sha256"], storage_sha256=facts["storage_cfg_sha256"],
            hostname=facts["node"], boot_id=facts["boot_id"], cluster_name=facts["cluster_name"],
            corosync_sha256=facts["corosync_conf_sha256"], now=manifest["expires_at"])
    except CHECK.Refusal as error: raise Refusal(str(error)) from error
    package = request["expected_package"]
    exact(package, {"package", "version", "config_version", "flavor", "artifact_sha256", "dpkg_state"}, "expected package")
    require(package == facts["package"] and package["package"] == "pve-sharedlvmthin"
            and package["flavor"] == "dual" and type(package["artifact_sha256"]) is str
            and SHA.fullmatch(package["artifact_sha256"]) and type(package["version"]) is str
            and CHECK.SAFE_VERSION.fullmatch(package["version"])
            and type(package["config_version"]) is str, "installed package state differs")
    receipts = request["receipt_sha256"]
    require(type(receipts) is dict and set(receipts) in (set(), {manifest["tx"] + ".json"})
            and all(type(value) is str and SHA.fullmatch(value) for value in receipts.values()), "receipt coverage invalid")
    if package["dpkg_state"] == "unpacked":
        require(all(package[key] == candidate[key] for key in ("package", "version", "flavor", "artifact_sha256"))
                and CHECK.SAFE_VERSION.fullmatch(package["config_version"])
                and package["config_version"] != package["version"]
                and (len(receipts) == 1 or (len(receipts) == 0 and request["schema"] == "slt-expired-prepare-archive/v2")),
                "unpacked candidate or prior Config-Version/receipt differs")
    else:
        require(package["dpkg_state"] == "installed" and package["version"] != candidate["version"]
                and package["config_version"] in ("", package["version"]), "configured candidate cannot be aborted")
    return manifest


def validate_preexisting_unpacked(request, manifest, facts, now):
    """Prove zero-receipt PREPARE did not create the current unpacked state."""
    if request["receipt_sha256"]:
        require(request["schema"] == "slt-expired-prepare-archive/v1",
                "receipt-backed archive must use v1")
        return
    require(request["schema"] == "slt-expired-prepare-archive/v2",
            "zero-receipt archive requires predecessor proof")
    require(facts["payload_verified"] is True,
            "zero-receipt archive requires exact installed payload verification")
    proof = request["preexisting_unpacked_proof"]
    exact(proof, {"abort_request"}, "preexisting unpacked proof")
    predecessor = proof["abort_request"]
    require(type(predecessor) is dict
            and predecessor.get("schema") == "slt-expired-prepare-archive/v1",
            "predecessor abort request invalid")
    predecessor_receipts = predecessor.get("receipt_sha256")
    require(type(predecessor_receipts) is dict and len(predecessor_receipts) == 1,
            "predecessor requires one exact PREINST receipt")
    receipt_name = next(iter(predecessor_receipts))
    require(re.fullmatch(r"[0-9a-f]{32}\.json", receipt_name)
            and type(predecessor.get("operation_id")) is str
            and HEX32.fullmatch(predecessor["operation_id"]),
            "predecessor archive identity invalid")
    path = FIXED_ROOT.parent / (
        "maintenance.expired-" + receipt_name[:-5] + "-" + predecessor["operation_id"])
    descriptors, identities = FILES.BASE._open_pinned_directory(path, 0)
    try:
        tree, payloads = _scan(descriptors[-1], 0)
        predecessor_raw = payloads.get("active.json")
        require(type(predecessor_raw) is bytes, "predecessor manifest missing")
        predecessor_manifest = validate(predecessor, predecessor_raw, facts, now)
        validate_payloads(predecessor, predecessor_raw, predecessor_manifest, payloads)
        require(digest(canonical(tree)) == predecessor["evidence_tree_sha256"],
                "predecessor archive tree changed")
        require(predecessor_manifest["candidate"] == manifest["candidate"]
                and predecessor_manifest["expires_at"] < manifest["issued_at"]
                and predecessor_manifest["tx"] != manifest["tx"],
                "predecessor does not prove the unpacked candidate predates this PREPARE")
        FILES.BASE._require_directory_chain(descriptors, identities, path, 0)
        require(_scan(descriptors[-1], 0) == (tree, payloads),
                "predecessor archive changed during proof")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def validate_payloads(request, manifest_raw, manifest, payloads):
    require(payloads.get("active.json") == manifest_raw, "active manifest bytes differ")
    top = {name.split("/", 1)[0] for name in payloads}
    require(top <= {"active.json", ".maintenance.lock", "attempts", "sidecars", "receipts"},
            "unknown maintenance evidence")
    prefix = f"{manifest['tx']}-{manifest['generation']}-{request['node']}-create-prepare-"
    for path in payloads:
        if path.startswith(("attempts/", "sidecars/")):
            name = path.split("/", 1)[1]
            require(name.startswith(prefix) and "/" not in name, "non-PREPARE or foreign attempt evidence")
    actual_receipts = {name[len("receipts/"):]: digest(raw) for name, raw in payloads.items() if name.startswith("receipts/")}
    require(actual_receipts == request["receipt_sha256"], "exact PREINST receipt set differs")
    for path, raw in payloads.items():
        if not path.startswith("receipts/"): continue
        receipt = decode(raw)
        exact(receipt, {"schema", "tx", "generation", "phase", "node", "boot_id", "package", "version",
                        "flavor", "artifact_sha256", "manifest_sha256", "storage_cfg_sha256", "recorded_at"}, "PREINST receipt")
        require(receipt["schema"] == "slt-package-maintenance-receipt/v1"
                and receipt["phase"] == "PREINST_ACCEPTED"
                and receipt["tx"] == manifest["tx"] and type(receipt["generation"]) is int
                and receipt["generation"] == manifest["generation"]
                and all(receipt[key] == request[key] for key in ("node", "boot_id", "manifest_sha256", "storage_cfg_sha256"))
                and all(receipt[key] == manifest["candidate"][key] for key in ("package", "version", "flavor", "artifact_sha256"))
                and type(receipt["recorded_at"]) is int
                and manifest["issued_at"] <= receipt["recorded_at"] <= manifest["expires_at"],
                "PREINST receipt binding/phase differs")


class FileLab:
    """One-shot primitive. Test callbacks are not exposed by archive_once()."""
    def __init__(self, root, cas_root, dpkg_locks, *, uid=0):
        self.root, self.cas_root = Path(root), Path(cas_root)
        self.dpkg_locks = tuple(Path(path) for path in dpkg_locks)
        self.uid, self.used = uid, False

    def archive(self, request, manifest_raw, collect, *, clock=None, fault=None):
        require(self.used is False, "archive object already attempted; inspection required")
        self.used = True
        request = copy.deepcopy(request)
        clock = clock or (lambda: int(time.time()))
        descriptors, locks, package_locks = [], [], []
        renamed = False
        destination = None
        def hook(point):
            if fault: fault(point)
        try:
            # Existing package-manager record locks are acquired first; no
            # package command can cross the probe/rename boundary under dpkg.
            for path in self.dpkg_locks:
                chain, ids = FILES.BASE._open_pinned_directory(path.parent, self.uid)
                descriptors.extend(chain)
                fd = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=chain[-1])
                locks.append(fd)
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and info.st_nlink == 1
                        and not stat.S_IMODE(info.st_mode) & 0o022, "dpkg lock unsafe")
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                require(inode(info) == inode(os.stat(path.name, dir_fd=chain[-1], follow_symlinks=False)), "dpkg lock replaced")
                package_locks.append((path, chain, ids, fd, info))
            cas_parent_chain, cas_parent_ids = FILES.BASE._open_pinned_directory(self.cas_root.parent, self.uid)
            descriptors.extend(cas_parent_chain)
            cas_parent_fd = cas_parent_chain[-1]
            require(self.cas_root.name not in ("", ".", ".."), "CAS root name unsafe")
            try:
                cas_root_info = os.stat(self.cas_root.name, dir_fd=cas_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                cas_root_info = None
            created_cas = cas_root_info is None
            if created_cas:
                hook("before_cas_root_create")
                FILES.BASE._require_directory_chain(cas_parent_chain, cas_parent_ids, self.cas_root.parent, self.uid)
                try:
                    os.mkdir(self.cas_root.name, 0o700, dir_fd=cas_parent_fd)
                except FileExistsError as error:
                    raise Refusal("concurrent CAS root creator; explicit inspection required") from error
                cas_root_info = os.stat(self.cas_root.name, dir_fd=cas_parent_fd, follow_symlinks=False)
                os.fsync(cas_parent_fd)
                hook("after_cas_root_create")
            require(stat.S_ISDIR(cas_root_info.st_mode) and cas_root_info.st_uid == self.uid
                    and stat.S_IMODE(cas_root_info.st_mode) == 0o700, "CAS root unsafe")
            cas_fd = os.open(self.cas_root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cas_parent_fd)
            descriptors.append(cas_fd)
            require(inode(cas_root_info) == inode(os.fstat(cas_fd)), "CAS root changed before pinning")
            cas_chain, cas_ids = cas_parent_chain + [cas_fd], cas_parent_ids + [cas_root_info]
            FILES.BASE._require_directory_chain(cas_chain, cas_ids, self.cas_root, self.uid)
            require(not created_cas or os.listdir(cas_fd) == [], "new CAS root has concurrent evidence")
            cas_lock = os.open(".cas.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=cas_fd)
            locks.append(cas_lock); cas_info = os.fstat(cas_lock)
            require(stat.S_ISREG(cas_info.st_mode) and cas_info.st_uid == self.uid
                    and stat.S_IMODE(cas_info.st_mode) == 0o600 and cas_info.st_nlink == 1, "CAS lock unsafe")
            FILES._acquire_lock(cas_lock, 0.1)
            os.fsync(cas_lock)
            os.fsync(cas_fd)
            require(not created_cas or sorted(os.listdir(cas_fd)) == [".cas.lock"], "new CAS root changed during lock acquisition")
            chain, ids = FILES.BASE._open_pinned_directory(self.root, self.uid)
            descriptors.extend(chain); root_fd, parent_fd = chain[-1], chain[-2]
            root_info = os.fstat(root_fd)
            require(stat.S_IMODE(root_info.st_mode) == 0o700, "maintenance root unsafe")
            lock = os.open(".maintenance.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            locks.append(lock); lock_info = os.fstat(lock)
            require(stat.S_ISREG(lock_info.st_mode) and lock_info.st_uid == self.uid
                    and stat.S_IMODE(lock_info.st_mode) == 0o600 and lock_info.st_nlink == 1, "maintenance lock unsafe")
            FILES._acquire_lock(lock, 0.1)
            def namespace():
                for package_path, package_chain, package_ids, package_fd, package_info in package_locks:
                    FILES.BASE._require_directory_chain(package_chain, package_ids, package_path.parent, self.uid)
                    require(inode(package_info) == inode(os.fstat(package_fd))
                            == inode(os.stat(package_path.name, dir_fd=package_chain[-1], follow_symlinks=False)),
                            "dpkg lock namespace changed")
                FILES.BASE._require_directory_chain(chain, ids, self.root, self.uid)
                FILES.BASE._require_directory_chain(cas_chain, cas_ids, self.cas_root, self.uid)
                require(inode(lock_info) == inode(os.fstat(lock))
                        == inode(os.stat(".maintenance.lock", dir_fd=root_fd, follow_symlinks=False)), "maintenance lock replaced")
                require(inode(cas_info) == inode(os.fstat(cas_lock))
                        == inode(os.stat(".cas.lock", dir_fd=cas_fd, follow_symlinks=False)), "CAS lock replaced")
                require(not any("INTENT" in name.upper() or "OUTCOME" in name.upper() for name in os.listdir(cas_fd)),
                        "CAS intent/outcome or staged reservation exists")
                # All CAS objects must be ordinary safe files; symlinks or
                # subdirectories cannot hide reservations.
                for name in os.listdir(cas_fd):
                    entry = os.stat(name, dir_fd=cas_fd, follow_symlinks=False)
                    require(stat.S_ISREG(entry.st_mode) and entry.st_uid == self.uid
                            and stat.S_IMODE(entry.st_mode) == 0o600 and entry.st_nlink == 1,
                            "CAS evidence object unsafe")
            namespace()
            facts = collect()
            now = clock()
            manifest = validate(request, manifest_raw, facts, now)
            tree, payloads = _scan(root_fd, self.uid)
            require(digest(canonical(tree)) == request["evidence_tree_sha256"], "authorized evidence tree differs")
            require(set(os.listdir(root_fd)) == {"active.json", ".maintenance.lock", "attempts", "sidecars", "receipts"},
                    "maintenance root contains unknown or missing objects")
            validate_payloads(request, manifest_raw, manifest, payloads)
            validate_preexisting_unpacked(request, manifest, facts, now)
            destination = self.root.name + ".expired-" + manifest["tx"] + "-" + request["operation_id"]
            require(SAFE.fullmatch(destination), "archive name unsafe")
            hook("before_rename")
            namespace()
            require(_scan(root_fd, self.uid) == (tree, payloads), "evidence changed before archive")
            current_facts = collect(); current_now = clock()
            require(current_facts == facts and type(current_now) is int and current_now >= now, "facts/clock changed before archive")
            validate(request, manifest_raw, current_facts, current_now)
            validate_preexisting_unpacked(request, manifest, current_facts, current_now)
            namespace()
            FILES._rename_noreplace(parent_fd, self.root.name, parent_fd, destination)
            renamed = True
            hook("after_rename")
            os.fsync(parent_fd)
            hook("after_parent_fsync")
            archived = os.stat(destination, dir_fd=parent_fd, follow_symlinks=False)
            require((archived.st_dev, archived.st_ino) == (root_info.st_dev, root_info.st_ino), "archive inode differs")
            require(self.root.name not in os.listdir(parent_fd), "maintenance namespace recreated; inspect both trees")
            require(_scan(root_fd, self.uid) == (tree, payloads), "archived evidence changed")
            require(collect() == facts, "facts changed after archive; recovery required")
            return {"classification": "EXPIRED_PREPARE_ARCHIVED", "archive_path": str(self.root.parent / destination),
                    "manifest_sha256": request["manifest_sha256"], "evidence_tree_sha256": request["evidence_tree_sha256"],
                    "parent_synced": True, "authorization": "NONE", "runtime_authorized": False,
                    "rollout_authorized": False, "release_authorized": False, "retry_authorized": False}
        except Exception as error:
            location = str(self.root.parent / destination) if destination else str(self.root)
            raise Refusal(("archive outcome uncertain; inspect retained evidence at " if renamed else
                           "archive refused or outcome uncertain; inspect evidence at ") + location + ": " + str(error)) from error
        finally:
            for fd in reversed(locks): os.close(fd)
            for fd in reversed(descriptors): os.close(fd)


def blocking_processes():
    """Fail closed on unclassified /proc entries, except vanished processes."""
    result = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid(): continue
        try:
            first = (path / "stat").read_bytes().rsplit(b") ", 1)[1].split()
            raw = (path / "cmdline").read_bytes()
            require(len(raw) <= 1024 * 1024, "process argv too large")
            if not raw:
                require(int(first[6]) & 0x00200000 or first[0] == b"Z", "unclassified process")
                continue
            argv = [item for item in raw.split(b"\0") if item]
            names = [item.rsplit(b"/", 1)[-1].lower() for item in argv]
            blocked = any(name.startswith((b"dpkg", b"apt", b"layout-migration", b"pvesm")) for name in names)
            after = (path / "stat").read_bytes().rsplit(b") ", 1)[1].split()
            require(first[19] == after[19], "process identity reused")
            if blocked: result.append({"pid": int(path.name), "starttime": int(first[19]), "argv_sha256": digest(raw)})
        except (FileNotFoundError, ProcessLookupError): continue
    return sorted(result, key=lambda item: item["pid"])


def current_facts():
    # Reuse the existing bounded, stable regular-file collector for fixed paths.
    evidence = load("expired_prepare_evidence", HERE / "layout-migration-node-evidence.py")
    read = lambda path, maximum: evidence.regular_bytes(Path(path), "archive current facts", maximum)
    result = subprocess.run(["/usr/bin/dpkg-query", "-W", "-f=${Status}\n${Version}\n${Config-Version}\n", "pve-sharedlvmthin"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    require(result.returncode == 0 and len(result.stdout) <= 4096, "dpkg state unavailable")
    rows = result.stdout.decode("ascii").splitlines()
    require(len(rows) == 3 and rows[0] in ("install ok unpacked", "install ok installed"), "dpkg state is ambiguous")
    package = {"package": "pve-sharedlvmthin", "version": rows[1], "config_version": rows[2],
        "dpkg_state": "unpacked" if rows[0] == "install ok unpacked" else "installed",
        "flavor": read("/usr/share/pve-sharedlvmthin/package-flavor", 64).decode().strip(),
        "artifact_sha256": read("/usr/share/pve-sharedlvmthin/package-artifact-sha256", 128).decode().strip()}
    verify = subprocess.run(["/usr/bin/dpkg", "--verify", "pve-sharedlvmthin"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    require(len(verify.stdout) <= 1024 * 1024 and len(verify.stderr) <= 1024 * 1024,
            "dpkg payload verification output exceeds bound")
    payload_verified = verify.returncode == 0 and verify.stdout == b"" and verify.stderr == b""
    members = decode(read("/etc/pve/.members", 1024 * 1024))
    return {"node": socket.gethostname(), "boot_id": evidence.pseudo_bytes(Path("/proc/sys/kernel/random/boot_id"), "boot id", 128).decode().strip(),
            "cluster_name": members["cluster"]["name"], "corosync_conf_sha256": digest(read("/etc/pve/corosync.conf", 1024 * 1024)),
            "storage_cfg_sha256": digest(read("/etc/pve/storage.cfg", 1024 * 1024)),
            "package": package, "blocking_processes": blocking_processes(), "payload_verified": payload_verified}


def archive_once(request, manifest_raw):
    """Fixed paths only; no caller-supplied facts, clock, command or callbacks."""
    require(os.geteuid() == 0, "root required")
    return FileLab(FIXED_ROOT, CAS_ROOT, DPKG_LOCKS).archive(request, manifest_raw, current_facts)
