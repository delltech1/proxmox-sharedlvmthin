"""Discover the complete local Lazy executor scope for package-only updates.

Discovery is NOT a graph-health or mutation grant. Candidate preinst and the
ordinary package gate must independently prove the held scope and live graph.
Remote active owners require package-only local nonownership evidence, never
a remote-runtime health or ownership-transfer grant.
"""
import re


def collect_snapshot(checker, node, boot):
    configs = checker.storage_configs()
    scopes = set()
    evidence = []
    seen = set()

    def observe(argv):
        result = checker.bounded_probe(argv)
        if result.get("status") != "PASS":
            raise RuntimeError("package scope observation is not proven: " + result.get("status", "UNKNOWN"))
        return result["out"]

    for cfg in configs:
        if cfg.get("type") != "sharedlvmthin":
            continue
        checker.config_boolean(cfg.get("disable"), "disable")
        nodes = checker.canonical_node_scope(cfg.get("nodes"))
        if nodes and node not in nodes:
            continue
        mode = cfg.get("slt-allocation-mode", "thin")
        if mode == "thin":
            continue
        if mode not in {"thick-generations", "thick-generations-lazy"}:
            raise RuntimeError("unsupported allocation mode in package scope")
        aliases = checker.thick_alias_group(configs, cfg["id"], cfg)
        vg = checker.clean(cfg.get("slt-vgname"))
        if vg in seen:
            continue
        seen.add(vg)
        expected_vg = checker.clean(cfg.get("slt-expected-vg-uuid"))
        expected_pv = checker.clean(cfg.get("slt-expected-pv-uuid"))
        wwid = checker.clean(cfg.get("slt-expected-wwid")).lower()
        if not expected_vg or not expected_pv or not re.fullmatch(r"[0-9a-f]+", wwid):
            raise RuntimeError("missing pinned physical package scope identity")
        devices = ["--devices", "/dev/mapper/" + wwid]
        vg_evidence = observe(["/sbin/vgs", "--readonly", *devices, "--noheadings",
                               "--separator", "|", "-o", "vg_uuid,vg_extent_size",
                               "--units", "b", "--nosuffix", vg])
        vg_lines = [row.strip().split("|") for row in vg_evidence.splitlines() if row.strip()]
        if len(vg_lines) != 1 or len(vg_lines[0]) != 2 \
                or vg_lines[0][0].strip().lower() != expected_vg.lower():
            raise RuntimeError("package scope VG identity mismatch")
        extent = int(vg_lines[0][1].strip())
        if extent < 512 or extent % 512:
            raise RuntimeError("package scope extent identity is invalid")
        pv_evidence = observe(["/sbin/pvs", "--readonly", *devices, "--noheadings",
                               "--separator", "|", "-o", "pv_uuid,pv_name",
                               "--select", "vg_name=" + vg])
        pv_lines = [tuple(part.strip() for part in row.split("|"))
                    for row in pv_evidence.splitlines() if row.strip()]
        if pv_lines != [(expected_pv, "/dev/mapper/" + wwid)]:
            raise RuntimeError("package scope PV/device identity mismatch")
        inventory = observe(["/sbin/lvs", *devices, "--noheadings", "--separator", "|",
                             "-o", "lv_name,lv_attr,lv_health_status,lv_check_needed,lv_tags,pool_lv,data_percent,lv_size,lv_uuid",
                             "--units", "b", "--nosuffix", vg])
        references = {sid: checker.pve_reference_inventory(sid) for sid in aliases}

        def discover_active(anchor, rows):
            if anchor["owner_node"] == node:
                if anchor["owner_boot"] != boot:
                    raise RuntimeError("stale local Lazy owner requires recovery")
            else:
                from sharedlvmthin_lazy_package_proof import audit_remote_local_absence
                owner = next(item for item in configs if item["id"] == anchor["sid"])
                audit_remote_local_absence(anchor, owner, rows, observe, node=node)
            scopes.update((sid, anchor["vol"]) for sid in aliases)
            return True  # inventory discovery only; actual graph proof follows under locks

        ok, _, failures = checker.thick_anchor_health(
            inventory, cfg["id"], expected_vg,
            lambda sid, vol: list(references[sid][0].get(vol, [])),
            lambda sid, vol: list(references[sid][1].get(vol, [])),
            aliases, extent, active_package_proof=discover_active)
        if not ok:
            raise RuntimeError("package scope anchor inventory refused: " + "; ".join(failures))
        # The canonical validator above has authenticated every anchor/object.
        # Fence dormant v6 volumes too: a preflight must not race their activation.
        for row in inventory.splitlines():
            fields = [part.strip() for part in row.split("|")]
            if len(fields) < 5 or not re.fullmatch(r"sltg-a-[0-9a-f]{24}", fields[0]):
                continue
            tags = dict(tag.split("=", 1) for tag in fields[4].split(",")
                        if tag.startswith("slt_tg_") and "=" in tag)
            if tags.get("slt_tg_v") == "6":
                scopes.update((sid, tags["slt_tg_vol"]) for sid in aliases)
        evidence.append((vg, vg_evidence, pv_evidence, inventory, references))
    return {"configs": configs, "evidence": evidence, "scopes": tuple(sorted(scopes)),
            "node": node, "boot": boot}


def require_unchanged(before, after):
    if before != after:
        raise RuntimeError("package scope/configuration/anchor/reference changed; no dispatch")
