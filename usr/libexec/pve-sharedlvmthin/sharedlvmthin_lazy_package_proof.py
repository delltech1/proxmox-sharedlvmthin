"""Pure validation for a pinned Lazy graph; not upgrade authorization by itself.

The caller must derive expectations from authenticated anchors and hold the
old-runtime-compatible executor/package fence across audit and replacement.
No activation, queue repair, ownership claim or metadata write is performed.
"""
import re
import hashlib


def audit_remote_local_absence(anchor, cfg, rows, run_readonly, *, node):
    """Package-only nonownership proof, NOT remote runtime health/fencing.

    Caller authenticates canonical anchors/objects/references, holds all local
    alias executors and brackets this with unchanged inventory/configuration.
    Remote data-plane operations are neither issued nor authorized here.
    """
    owner = anchor.get("owner_node", "")
    if anchor.get("v") != "6" or anchor.get("phase") != "LAZY_ACTIVE" \
            or owner == node or owner == "none" \
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", owner):
        raise ValueError("remote package owner identity is not canonical")
    vg_uuid = cfg.get("slt-expected-vg-uuid", "")
    vg = cfg.get("slt-vgname", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+", vg_uuid) or len(vg_uuid.replace("-", "")) != 32 \
            or not re.fullmatch(r"[A-Za-z0-9+_.-]+", vg):
        raise ValueError("remote package scope lacks pinned VG identity")
    if cfg.get("id") != anchor.get("sid"):
        raise ValueError("remote owner alias disagrees with authenticated anchor")
    key = hashlib.sha256((vg_uuid.lower() + "\0" + anchor["vol"]).encode()).hexdigest()[:24]
    names = {"sltg-" + key, "sltg-c-" + key, "sltg-z-" + key}
    uuids = {"SLT-TG2-" + key, "SLT-TG6-CLONE-" + anchor["tx"],
             "SLT-TG6-ZERO-" + anchor["tx"]}
    for lv_field, uuid_field in (("head", "data_uuid"), ("metadata", "metadata_uuid")):
        lv, uuid = anchor[lv_field], anchor[uuid_field]
        row = rows.get(lv)
        if not row or len(row) < 9 or row[8] != uuid \
                or not re.fullmatch(r"-[wr]i-[a-][o-][-a-zA-Z]{4}", row[1]) \
                or row[1][4] != "-":
            raise ValueError("remote-owned backing LV is active or unproven on local node")
        if not re.fullmatch(r"[A-Za-z0-9-]+", uuid) or len(uuid.replace("-", "")) != 32:
            raise ValueError("remote backing UUID is not canonical")
        names.add(vg.replace("-", "--") + "-" + lv.replace("-", "--"))
        uuids.add("LVM-" + vg_uuid.replace("-", "") + uuid.replace("-", ""))
    command = ["/sbin/dmsetup", "info", "-c", "--noheadings", "--separator", "|",
               "-o", "name,uuid,major,minor,suspended"]
    for _ in range(2):
        output = run_readonly(command)
        seen_names, seen_devnos = set(), set()
        for line in output.splitlines():
            fields = [value.strip() for value in line.split("|")]
            if len(fields) != 5 or not fields[0] or not fields[2].isdigit() \
                    or not fields[3].isdigit() or fields[4].lower() not in {"active", "suspended"}:
                raise ValueError("local DM absence inventory is malformed")
            devno = (int(fields[2]), int(fields[3]))
            if fields[0] in seen_names or devno in seen_devnos:
                raise ValueError("local DM absence inventory is ambiguous")
            seen_names.add(fields[0]); seen_devnos.add(devno)
            if fields[0] in names or fields[1] in uuids:
                raise ValueError("remote-owned Lazy volume has a conflicting local mapper")
    return {"state": "REMOTE_OWNED_LOCAL_RUNTIME_ABSENT", "remote_runtime": "NOT_CERTIFIED"}


def audit_active_graph(anchor, cfg, rows, run_readonly, read_attribute, block_devno, *, node, boot):
    """Observe complete locally owned graph after common anchor validation.

    Caller must retain established executor fence and recheck authenticated
    anchor/config/intent after return, through package settlement. This function
    does not authorize installation or certify remote owners.
    """
    if anchor.get("owner_node") != node or anchor.get("owner_boot") != boot:
        raise ValueError("cannot audit a remote or stale Lazy owner")
    vg = cfg.get("slt-vgname", "")
    wwid = cfg.get("slt-expected-wwid", "")
    if not re.fullmatch(r"[A-Za-z0-9+_.-]+", vg) or not re.fullmatch(r"[0-9A-Fa-f]+", wwid):
        raise ValueError("missing pinned VG/SAN identity")
    device = "/dev/mapper/" + wwid
    pv_number = block_devno(device)
    pv_command = ["/sbin/pvs", "--readonly", "--devices", device, "--units", "s",
                  "--nosuffix", "--noheadings", "--separator", "|", "-o",
                  "pv_name,pv_uuid,pe_start,pv_size"]
    vg_command = ["/sbin/vgs", "--readonly", "--devices", device, "--units", "s",
                  "--nosuffix", "--noheadings", "--separator", "|", "-o",
                  "vg_name,vg_uuid,vg_extent_size"]
    pv_raw, vg_raw = run_readonly(pv_command), run_readonly(vg_command)
    def one_row(raw, count):
        lines = raw.strip().splitlines()
        fields = [value.strip() for value in lines[0].split("|")] if len(lines) == 1 else []
        if len(fields) != count:
            raise ValueError("ambiguous scoped LVM identity")
        return fields
    pv = one_row(pv_raw, 4)
    vg_row = one_row(vg_raw, 3)
    if pv[:2] != [device, cfg.get("slt-expected-pv-uuid")] \
            or vg_row[:2] != [vg, cfg.get("slt-expected-vg-uuid")]:
        raise ValueError("scoped PV/VG identity mismatch")
    if not all(value.isdigit() for value in (pv[2], pv[3], vg_row[2])):
        raise ValueError("scoped LVM geometry is not exact integer sectors")
    backing, commands, segment_samples, names = {}, {}, {}, {}
    for role, field, uuid_field in (("data", "head", "data_uuid"),
                                   ("metadata", "metadata", "metadata_uuid")):
        lv = anchor[field]
        if not re.fullmatch(r"[A-Za-z0-9+_.-]+", lv) or lv not in rows:
            raise ValueError("authenticated backing LV is missing")
        row = rows[lv]
        if len(row) < 9 or row[8] != anchor[uuid_field] or not row[7].isdigit() \
                or int(row[7]) % 512:
            raise ValueError("backing object UUID/size evidence changed")
        command = ["/sbin/lvs", "--readonly", "--devices", device, "--segments",
                   "--units", "s", "--nosuffix", "--noheadings", "--separator", "|",
                   "-o", "lv_name,segtype,seg_start,seg_size,devices", vg + "/" + lv]
        sample = run_readonly(command)
        backing[role] = derive_backing_graph(
            sample, lv_name=lv, vg_uuid=vg_row[1], lv_uuid=anchor[uuid_field],
            pv_path=device, pv_devno=pv_number, pe_start=int(pv[2]),
            extent_sectors=int(vg_row[2]), pv_sectors=int(pv[3]), expected_sectors=int(row[7]) // 512)
        commands[role], segment_samples[role] = command, sample
        names[role] = vg.replace("-", "--") + "-" + lv.replace("-", "--")
    key = hashlib.sha256((vg_row[1].lower() + "\0" + anchor["vol"]).encode()).hexdigest()[:24]
    names.update(zero="sltg-z-" + key, clone="sltg-c-" + key, front="sltg-" + key)
    def sample_graph():
        graph = {role: observe_mapper(name, role, run_readonly, read_attribute)
                 for role, name in names.items()}
        for role, name in names.items():
            if block_devno("/dev/mapper/" + name) != graph[role]["devno"]:
                raise ValueError("mapper device node disagrees with kernel identity")
        return graph
    before, after = sample_graph(), sample_graph()
    expected, derived = derive_custom_graph(anchor, cfg, backing,
                                           {role: row["devno"] for role, row in before.items()},
                                           node=node, boot=boot)
    if any(names[role] != name for role, name in derived.items()):
        raise ValueError("custom mapper namespace changed")
    validate_graph(expected, before, after, sectors=int(anchor["bytes"]) // 512, region=int(anchor["region"]))
    if run_readonly(pv_command) != pv_raw or run_readonly(vg_command) != vg_raw \
            or block_devno(device) != pv_number \
            or any(run_readonly(commands[role]) != segment_samples[role] for role in backing):
        raise ValueError("scoped backing identity changed during graph audit")
    return {"before": before, "after": after}


def derive_backing_graph(segment_output, *, lv_name, vg_uuid, lv_uuid, pv_path,
                         pv_devno, pe_start, extent_sectors, pv_sectors, expected_sectors):
    """Reconstruct linear LV table from scoped native LVM segment evidence.

    PV path/devno/UUID and VG UUID must be independently pinned by the caller.
    Accept fragmented linear extents, not stripe/foreign-PV/partial evidence.
    """
    numbers = (pe_start, extent_sectors, pv_sectors, expected_sectors)
    if any(type(value) is not int or value < 0 for value in numbers) \
            or not extent_sectors or not expected_sectors or pe_start >= pv_sectors:
        raise ValueError("invalid backing geometry")
    if not re.fullmatch(r"[0-9]+:[0-9]+", pv_devno):
        raise ValueError("invalid pinned PV device number")
    for uuid in (vg_uuid, lv_uuid):
        if not re.fullmatch(r"[A-Za-z0-9-]+", uuid) or len(uuid.replace("-", "")) != 32:
            raise ValueError("invalid scoped LVM UUID")
    rows = []
    for line in segment_output.splitlines():
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != 5 or fields[0] != lv_name or fields[1] != "linear" \
                or not fields[2].isdigit() or not fields[3].isdigit():
            raise ValueError("malformed or unsupported scoped LV segment")
        match = re.fullmatch(re.escape(pv_path) + r"\(([0-9]+)\)", fields[4])
        if not match:
            raise ValueError("LV segment does not use the pinned PV")
        start, length = int(fields[2]), int(fields[3])
        physical = pe_start + int(match.group(1)) * extent_sectors
        if not length or start % extent_sectors or length % extent_sectors \
                or physical + length > pv_sectors:
            raise ValueError("LV extent range is invalid")
        rows.append((start, length, physical))
    logical = 0
    table = []
    for start, length, physical in sorted(rows):
        if start != logical:
            raise ValueError("LV segments overlap or leave a gap")
        table.append(f"{start} {length} linear {pv_devno} {physical}")
        logical += length
    if logical != expected_sectors:
        raise ValueError("LV segment inventory does not cover authenticated size")
    return dict(uuid="LVM-" + vg_uuid.replace("-", "") + lv_uuid.replace("-", ""),
                readonly=False, table="\n".join(table), deps=(pv_devno,))


def derive_custom_graph(anchor, cfg, backing, devnos, *, node, boot):
    """Derive custom maps, never copy their expected tables from observations.

    Anchor must already pass the recovery canonical/object/reference checks.
    Backing rows must be independently qualified against scoped LVM segments
    and pinned PV/VG identity; this function does not certify those rows.
    """
    if anchor.get("v") != "6" or anchor.get("phase") != "LAZY_ACTIVE" \
            or anchor.get("owner_node") != node or anchor.get("owner_boot") != boot:
        raise ValueError("active Lazy owner is not local current boot")
    if cfg.get("id") != anchor.get("sid"):
        raise ValueError("configuration is not the authenticated anchor owner alias")
    vg_uuid = cfg.get("slt-expected-vg-uuid", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+", vg_uuid) or len(vg_uuid.replace("-", "")) != 32:
        raise ValueError("missing pinned VG UUID")
    if not re.fullmatch(r"[0-9a-f]{32}", anchor.get("tx", "")) \
            or not re.fullmatch(r"[0-9a-f]{32}", anchor.get("owner_epoch", "")):
        raise ValueError("invalid transaction or owner epoch")
    volume = anchor.get("vol", "")
    if not re.fullmatch(r"(?:vm|base)-[0-9]+-disk-[0-9]+", volume):
        raise ValueError("invalid authenticated volume")
    try:
        size, region, publication = (int(anchor[field]) for field in ("bytes", "region", "publication"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid anchor geometry") from exc
    if size < 512 or size % 512 or not 8 <= region <= 2_097_152 \
            or region & (region - 1) or publication < 2:
        raise ValueError("invalid active anchor geometry/publication")
    if set(backing) != {"data", "metadata"} or set(devnos) != {"data", "metadata", "zero", "clone", "front"}:
        raise ValueError("incomplete backing or device identity scope")
    if any(type(value) is not str or not re.fullmatch(r"[0-9]+:[0-9]+", value) for value in devnos.values()) \
            or len(set(devnos.values())) != 5:
        raise ValueError("ambiguous graph device numbers")
    expected = {}
    for role, field in (("data", "data_uuid"), ("metadata", "metadata_uuid")):
        lv_uuid = anchor.get(field, "")
        if not re.fullmatch(r"[A-Za-z0-9-]+", lv_uuid) or len(lv_uuid.replace("-", "")) != 32:
            raise ValueError("missing authenticated backing UUID")
        row = dict(backing[role])
        wanted = "LVM-" + vg_uuid.replace("-", "") + lv_uuid.replace("-", "")
        if row.get("uuid") != wanted or row.get("readonly") is not False:
            raise ValueError("backing LVM identity mismatch")
        expected[role] = row
    explicit = "slt-tg-hydration-threshold" in cfg or "slt-tg-hydration-batch-size" in cfg
    threshold = int(cfg.get("slt-tg-hydration-threshold", 32)) if explicit else (max(1, min(256, 8192 // region)) if region >= 128 else 32)
    batch = int(cfg.get("slt-tg-hydration-batch-size", 32)) if explicit else (max(1, min(256, 2048 // region)) if region >= 128 else 32)
    if not 1 <= batch <= threshold <= 256:
        raise ValueError("invalid approved hydration tuning")
    sectors = size // 512
    key = hashlib.sha256((vg_uuid.lower() + "\0" + volume).encode()).hexdigest()[:24]
    options = f"2 no_hydration no_discard_passdown 4 hydration_threshold {threshold} hydration_batch_size {batch}"
    expected["zero"] = dict(uuid="SLT-TG6-ZERO-" + anchor["tx"],
                            readonly=True, table=f"0 {sectors} zero", deps=())
    expected["clone"] = dict(uuid="SLT-TG6-CLONE-" + anchor["tx"], readonly=False,
                             table=f"0 {sectors} clone {devnos['metadata']} {devnos['data']} {devnos['zero']} {region} {options}",
                             deps=tuple(sorted(devnos[role] for role in ("data", "metadata", "zero"))),
                             hydration_threshold=threshold, hydration_batch_size=batch)
    expected["front"] = dict(uuid="SLT-TG2-" + key, readonly=False,
                             table=f"0 {sectors} linear {devnos['clone']} 0", deps=(devnos["clone"],))
    return expected, {"zero": "sltg-z-" + key, "clone": "sltg-c-" + key, "front": "sltg-" + key}


def observe_mapper(name, role, run_readonly, read_attribute):
    """Bracket one read-only observation by kernel identity and diskseq.

    Injected runner must enforce bounded subprocess observation without blind
    retry; attribute reader must reject missing/symlinked leaf/oversized values.
    This does not acquire a fence or authenticate the anchor/expected graph.
    """
    if type(name) is not str or not re.fullmatch(r"[A-Za-z0-9+_.-]+", name) \
            or name.startswith("-") or role not in {"data", "metadata", "zero", "clone", "front"}:
        raise ValueError("invalid mapper observation identity")

    def identity():
        output = run_readonly(["/sbin/dmsetup", "info", "-c", "--noheadings",
                               "--separator", "|", "-o",
                               "uuid,readonly,major,minor,suspended", name])
        lines = output.strip().splitlines()
        if len(lines) != 1:
            raise ValueError("ambiguous mapper identity")
        fields = [field.strip() for field in lines[0].split("|")]
        if len(fields) == 5:
            fields[1] = fields[1].lower()
            fields[4] = fields[4].lower()
        if len(fields) != 5 or not fields[0] or fields[1] not in {"read-only", "writeable"} \
                or not fields[2].isdigit() or not fields[3].isdigit() \
                or fields[4] not in {"active", "suspended"}:
            raise ValueError("malformed mapper identity")
        devno = fields[2] + ":" + fields[3]
        sequence = read_attribute(devno, "diskseq")
        if type(sequence) is not int or sequence < 1:
            raise ValueError("missing mapper diskseq")
        return dict(uuid=fields[0], readonly=fields[1] == "read-only", devno=devno,
                    diskseq=sequence, suspended=fields[4] != "active")

    first = identity()
    tables = run_readonly(["/sbin/dmsetup", "table", name]).strip().splitlines()
    if not tables or any(not line for line in tables) \
            or (role not in {"data", "metadata"} and len(tables) != 1):
        raise ValueError("ambiguous mapper table")
    deps = run_readonly(["/sbin/dmsetup", "deps", name]).strip()
    match = re.fullmatch(r"([0-9]+) dependencies\s*:\s*((?:\([0-9]+,\s*[0-9]+\)\s*)*)", deps)
    if not match:
        raise ValueError("malformed mapper dependencies")
    devices = tuple(sorted(f"{major}:{minor}" for major, minor in
                           re.findall(r"\(([0-9]+),\s*([0-9]+)\)", match.group(2))))
    if len(devices) != int(match.group(1)) or len(set(devices)) != len(devices):
        raise ValueError("ambiguous mapper dependencies")
    observation = dict(first, table="\n".join(tables), deps=devices)
    if role in {"clone", "front"}:
        guards = ("discard_max_bytes", "write_zeroes_max_bytes")
        if role == "front":
            guards += ("discard_max_hw_bytes",)
        for guard in guards:
            observation[guard] = read_attribute(first["devno"], "queue/" + guard)
    if role == "clone":
        statuses = run_readonly(["/sbin/dmsetup", "status", "--noflush", name]).strip().splitlines()
        if len(statuses) != 1:
            raise ValueError("ambiguous clone status")
        observation["status"] = statuses[0]
    if identity() != first:
        raise ValueError("mapper identity changed while collecting evidence")
    return observation


def validate_graph(expected, before, after, *, sectors, region):
    """Compare observations with caller-authenticated expected graph geometry.

    Does not derive graph edges or anchor identity: collector must prove those
    before passing expectations. Model tests do not qualify a kernel runtime.
    """
    if any(type(item) is not dict for item in (expected, before, after)):
        raise ValueError("graph evidence must be explicit mappings")
    if set(expected) != {"data", "metadata", "zero", "clone", "front"}:
        raise ValueError("incomplete expected Lazy graph")
    if set(before) != set(expected) or set(after) != set(expected):
        raise ValueError("incomplete observed Lazy graph")
    if type(sectors) is not int or sectors < 1 or type(region) is not int \
            or not 8 <= region <= 2_097_152 or region & (region - 1):
        raise ValueError("invalid signed Lazy geometry")
    stable = ("uuid", "devno", "diskseq", "table", "deps", "readonly", "suspended")
    for name, wanted in expected.items():
        first, last = before[name], after[name]
        if any(type(item) is not dict for item in (wanted, first, last)):
            raise ValueError(f"{name}: malformed graph row")
        for field in ("uuid", "table", "deps", "readonly"):
            if field not in wanted:
                raise ValueError(f"{name}: missing expected {field}")
        for observation in (first, last):
            for field in stable:
                if field not in observation:
                    raise ValueError(f"{name}: missing {field}")
            if type(observation["devno"]) is not str or not re.fullmatch(r"[0-9]+:[0-9]+", observation["devno"]):
                raise ValueError(f"{name}: malformed device number")
            if type(observation["diskseq"]) is not int or observation["diskseq"] < 1:
                raise ValueError(f"{name}: missing disk sequence")
            for field in ("uuid", "table", "deps", "readonly"):
                if observation[field] != wanted[field]:
                    raise ValueError(f"{name}: {field} mismatch")
            if type(observation["readonly"]) is not bool or type(wanted["readonly"]) is not bool:
                raise ValueError(f"{name}: ambiguous access mode")
            if type(observation["deps"]) is not tuple or any(
                    type(dev) is not str or not re.fullmatch(r"[0-9]+:[0-9]+", dev)
                    for dev in observation["deps"]):
                raise ValueError(f"{name}: malformed dependency set")
            if any(type(observation[field]) is not str or not observation[field]
                   for field in ("uuid", "table")):
                raise ValueError(f"{name}: missing mapper identity")
            if observation["suspended"] is not False:
                raise ValueError(f"{name}: suspended or ambiguous mapper")
            guards = ("discard_max_bytes", "write_zeroes_max_bytes") \
                if name == "clone" else ()
            if name == "front":
                guards = ("discard_max_bytes", "discard_max_hw_bytes", "write_zeroes_max_bytes")
            for field in guards:
                if type(observation.get(field)) is not int or observation[field] != 0:
                    raise ValueError(f"{name}: {field} is not proven zero")
        if any(first[field] != last[field] for field in stable):
            raise ValueError(f"{name}: graph identity changed during audit")
    for graph in (before, after):
        if len({row["devno"] for row in graph.values()}) != 5:
            raise ValueError("distinct Lazy roles share a device number")
    for sample in (before["clone"], after["clone"]):
        status = sample.get("status", "")
        if type(status) is not str:
            raise ValueError("clone: status is not text")
        match = re.fullmatch(
            r"0 (\d+) clone (\d+) (\d+)/(\d+) (\d+) (\d+)/(\d+) (\d+)"
            r" (.+)", status)
        if not match:
            raise ValueError("clone: malformed status")
        size, block, used, capacity, chunk, hydrated, total, hydrating = \
            map(int, match.groups()[:8])
        tail = match.group(9).split()
        # dm-clone reports counted feature tokens, counted core arguments,
        # then the metadata mode. Never accept an abbreviated status suffix.
        if len(tail) != 9 or tail[:4] != ["2", "no_hydration", "no_discard_passdown", "4"] \
                or tail[4] != "hydration_threshold" or tail[6] != "hydration_batch_size" \
                or not tail[5].isdigit() or not tail[7].isdigit() or tail[8] != "rw":
            raise ValueError("clone: unapproved or malformed counted status options")
        if int(tail[5]) != expected["clone"].get("hydration_threshold") \
                or int(tail[7]) != expected["clone"].get("hydration_batch_size"):
            raise ValueError("clone: hydration tuning differs from approved contract")
        if size != sectors or chunk != region or block != 8 or not capacity \
                or used > capacity or total != (sectors + region - 1) // region \
                or hydrated > total or hydrating > total:
            raise ValueError("clone: unhealthy or contradictory status")
    return True
