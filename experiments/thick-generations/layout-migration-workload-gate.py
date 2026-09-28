#!/usr/bin/env python3
"""Read-only PREPARE gate for workloads using SharedLVM storage."""

from __future__ import annotations
import argparse, hashlib, json, os, re, stat, sys, time
from pathlib import Path

DISK = re.compile(r"^(?:scsi|virtio|sata|ide|efidisk|tpmstate)[0-9]+$")
LXC_DISK = re.compile(r"^(?:rootfs|mp[0-9]+)$")
VMCONF = re.compile(r"^[1-9][0-9]*\.conf$")

class Refusal(Exception): pass
def require(v, m):
    if not v: raise Refusal(m)
def canonical(v): return json.dumps(v, sort_keys=True, separators=(",", ":")).encode()
def sha(data): return hashlib.sha256(data).hexdigest()

def stable_read(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= maximum,
                f"unsafe file: {path}")
        chunks=[]; total=0
        while True:
            part=os.read(fd, min(65536, maximum + 1 - total))
            if not part: break
            chunks.append(part); total += len(part)
            require(total <= maximum, f"oversized file: {path}")
        after=os.fstat(fd); data=b"".join(chunks)
        require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
                (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
                and len(data)==before.st_size, f"file changed while read: {path}")
        return data
    finally: os.close(fd)

def storage_ids(raw: bytes):
    text=raw.decode("utf-8"); result=[]
    for line in text.splitlines():
        if line.startswith("sharedlvmthin:"):
            sid=line.split(":",1)[1].strip()
            require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",sid),
                    "unsafe storage id")
            require(sid not in result, "duplicate storage id")
            result.append(sid)
    require(result, "no SharedLVM storage definitions")
    return sorted(result)

def resource_map(raw: bytes, now: int, max_age: int):
    value=json.loads(raw)
    require(type(value) is dict and set(value)=={"schema","observed_at","resources"},
            "resource snapshot schema invalid")
    require(value["schema"]=="slt-pve-resources/v1" and type(value["observed_at"]) is int
            and 0 <= now-value["observed_at"] <= max_age,
            "resource snapshot stale or invalid")
    require(type(value["resources"]) is list, "resources must be a list")
    result={}
    for item in value["resources"]:
        require(type(item) is dict and set(item)=={"vmid","type","node","status","name"},
                "resource fields invalid")
        if item["type"] not in ("qemu", "lxc"): continue
        vmid=item["vmid"]
        require(type(vmid) is int and vmid>0 and vmid not in result,
                "resource VM identity invalid or duplicated")
        require(item["status"] in ("running","stopped"), "resource status is unknown")
        result[vmid]=item
    return result, value["observed_at"]

def current_disks(raw: bytes, managed: set[str], kind="qemu"):
    result=[]; keys=set()
    for line in raw.decode("utf-8").splitlines():
        if line.startswith("["): break
        key, sep, value=line.partition(":")
        matcher = DISK if kind == "qemu" else LXC_DISK
        if not sep or not matcher.fullmatch(key): continue
        require(key not in keys, "duplicate live disk key")
        keys.add(key)
        tokens=value.strip().split(",")
        identities=[]
        if tokens and "=" not in tokens[0]: identities.append(tokens[0])
        identities.extend(token[5:] for token in tokens if token.startswith("file="))
        require(len(identities) <= 1, "duplicate or conflicting disk identity")
        if not identities:
            require(not any(any((sid+":") in token for sid in managed)
                            for token in tokens),
                    "unrecognized managed disk identity")
            continue
        sid, sep, rest=identities[0].partition(":")
        if sep and sid in managed:
            volume=rest.split(",",1)[0]
            require(volume and "\x00" not in volume, "invalid volume identity")
            result.append({"key":key,"storage":sid,"volume":volume})
    return result

def evaluate(storage_path: Path, nodes_dir: Path, resources_path: Path,
             now: int, max_age: int):
    require(nodes_dir.is_dir() and not nodes_dir.is_symlink(),
            "nodes inventory root is absent or unsafe")
    storage_raw=stable_read(storage_path, 4*1024*1024)
    managed=set(storage_ids(storage_raw))
    resources_raw=stable_read(resources_path, 16*1024*1024)
    resources, observed_at=resource_map(resources_raw,now,max_age)
    consumers=[]; config_hashes={}; config_raw={}; seen=set()
    paths=sorted(list(nodes_dir.glob("*/qemu-server/*.conf"))
                 + list(nodes_dir.glob("*/lxc/*.conf")))
    require(paths, "guest configuration inventory is empty")
    for path in paths:
        require(VMCONF.fullmatch(path.name), f"unexpected VM config name: {path.name}")
        vmid=int(path.stem); require(vmid not in seen, "guest config exists more than once")
        seen.add(vmid); raw=stable_read(path,2*1024*1024); config_raw[str(path)]=raw
        kind="qemu" if path.parent.name == "qemu-server" else "lxc"
        require(vmid in resources, "guest config is absent from resource snapshot")
        r=resources[vmid]; node=path.parents[1].name
        require(r["node"]==node and r["type"]==kind,
                "guest type/node differs between config and resources")
        disks=current_disks(raw,managed,kind)
        if not disks: continue
        config_hashes[str(vmid)]=sha(raw)
        consumers.append({"vmid":vmid,"type":kind,"name":r["name"],"node":node,
                          "status":r["status"],"disks":disks})
    require(seen == set(resources), "guest resource and config coverage differs")
    require(sorted(str(x) for x in list(nodes_dir.glob("*/qemu-server/*.conf"))
                                   + list(nodes_dir.glob("*/lxc/*.conf")))
            == [str(x) for x in paths], "guest config path set changed during collection")
    require(stable_read(storage_path,4*1024*1024)==storage_raw
            and stable_read(resources_path,16*1024*1024)==resources_raw,
            "storage or resource snapshot changed during collection")
    for path in paths:
        require(stable_read(path,2*1024*1024)==config_raw[str(path)],
                "QEMU config changed during collection")
    running=[x for x in consumers if x["status"]=="running"]
    body={"schema":"slt-workload-prepare/v1","observed_at":observed_at,
          "managed_storages":sorted(managed),"consumers":consumers,
          "running_consumers":running,"storage_cfg_sha256":sha(storage_raw),
          "resources_sha256":sha(resources_raw),"config_sha256":config_hashes,
          "mutation_performed":False,"authorization":"NONE",
          "verdict":"SNAPSHOT_READY" if not running else "BLOCKED",
          "limitations":["snapshot evidence is not current quiescence; an external maintenance barrier is required"]}
    body["evidence_sha256"]=sha(canonical(body)); return body

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--storage-config",required=True); p.add_argument("--nodes-dir",required=True)
    p.add_argument("--resources",required=True); p.add_argument("--max-age-sec",type=int,default=120)
    p.add_argument("--now",type=int,help=argparse.SUPPRESS); a=p.parse_args()
    try:
        require(30 <= a.max_age_sec <= 600,"age bound unsafe")
        value=evaluate(Path(a.storage_config),Path(a.nodes_dir),Path(a.resources),
                       a.now if a.now is not None else int(time.time()),a.max_age_sec)
    except (OSError,UnicodeError,json.JSONDecodeError,Refusal) as e:
        print(json.dumps({"verdict":"REFUSED","authorization":"NONE",
                          "mutation_performed":False,"reason":str(e)},sort_keys=True)); return 2
    print(json.dumps(value,sort_keys=True,indent=2)); return 0 if value["verdict"]=="SNAPSHOT_READY" else 3
if __name__=="__main__": sys.exit(main())
