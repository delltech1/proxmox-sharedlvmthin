import copy
import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

runtime = Path(__file__).resolve().parents[2] / "usr/libexec/pve-sharedlvmthin"
sys.path.insert(0, str(runtime))
from sharedlvmthin_package_preservation import collect_snapshot, require_unchanged
loader = importlib.machinery.SourceFileLoader("scope_recovery", str(runtime / "sharedlvmthin-recovery-check"))
spec = importlib.util.spec_from_loader(loader.name, loader)
checker = importlib.util.module_from_spec(spec)
loader.exec_module(checker)


def inventory(sid, namespace, volume, phase="LAZY_ACTIVE", owner="local", boot="11111111-2222-3333-4444-555555555555"):
    key = hashlib.sha256((namespace.lower() + "\0" + volume).encode()).hexdigest()[:24]
    head, meta = "sltg-g-" + key + "-00000000", "sltg-m-" + key + "-00000000"
    values = dict(v="6", sid=sid, vol=volume, phase=phase, tx="b" * 32, op="ALLOC",
                  snapshot="none", source=head, old=head, new=head, head=head, generation="0",
                  region="2048", policy="lazy-zero", bytes="1048576", metadata=meta,
                  data_uuid="c" * 32, metadata_uuid="d" * 32, zero_source="dm-zero",
                  publication="3" if phase == "LAZY_ACTIVE" else "1",
                  owner_node=owner if phase == "LAZY_ACTIVE" else "none",
                  owner_boot=boot if phase == "LAZY_ACTIVE" else "none",
                  owner_epoch="e" * 32 if phase == "LAZY_ACTIVE" else "none")
    def tags(prefix, values):
        encoded = "|".join(f"{key}={value}" for key, value in values.items())
        return ",".join([*(prefix + key + "=" + value for key, value in values.items()),
                         prefix + "sha256=" + hashlib.sha256(encoded.encode()).hexdigest()[:32]])
    rows = [f"sltg-a-{key}|-wi-------|||{tags('slt_tg_', values)}|||8388608|" + "f" * 32]
    for name, kind, size, uuid in ((head, "data", "1048576", "c" * 32), (meta, "metadata", "20971520", "d" * 32)):
        obj = dict(v="1", sid=sid, vol=volume, tx="b" * 32, kind=kind, bytes=size, region="2048")
        rows.append(f"{name}|-wi-------|||{tags('slt_tgl_', obj)}|||{size}|{uuid}")
    return "\n".join(rows)


class PackageScopeTests(unittest.TestCase):
    def setUp(self):
        self.boot = "11111111-2222-3333-4444-555555555555"
        self.configs = []
        for sid, mode, vg, uuid, wwid in (("eager", "thick-generations", "vg-a", "a" * 32, "1111"),
                                         ("lazy", "thick-generations-lazy", "vg-a", "a" * 32, "1111"),
                                         ("other", "thick-generations-lazy", "vg-b", "2" * 32, "2222")):
            self.configs.append(dict(id=sid, type="sharedlvmthin", shared="1", disable="1",
                                     **{"slt-allocation-mode": mode, "slt-vgname": vg,
                                        "slt-expected-vg-uuid": uuid, "slt-expected-pv-uuid": "pv-uuid",
                                        "slt-expected-wwid": wwid}))
        self.inventories = {"vg-a": inventory("lazy", "a" * 32, "vm-100-disk-0") + "\n" +
                                      inventory("lazy", "a" * 32, "vm-101-disk-0", "LAZY_DORMANT"),
                            "vg-b": inventory("other", "2" * 32, "vm-102-disk-0")}
        self.commands = []

    def probe(self, argv):
        self.commands.append(argv)
        if argv[0] == "/sbin/dmsetup":
            return dict(status="PASS", out="unrelated|other|253|1|Active")
        vg = argv[-1]
        cfg = next(item for item in self.configs if item["slt-vgname"] == vg) if argv[0] != "/sbin/pvs" else None
        if argv[0] == "/sbin/vgs":
            out = cfg["slt-expected-vg-uuid"] + "|4194304"
        elif argv[0] == "/sbin/pvs":
            out = "pv-uuid|" + argv[argv.index("--devices") + 1]
        else:
            out = self.inventories[vg]
        return dict(status="PASS", out=out)

    def refs(self, sid):
        vols = {"lazy": ["vm-100-disk-0", "vm-101-disk-0"], "other": ["vm-102-disk-0"]}.get(sid, [])
        return ({vol: ["ref"] for vol in vols}, {vol: [("ref", 1048576, "attached")] for vol in vols})

    def collect(self):
        with patch.object(checker, "storage_configs", return_value=self.configs), \
                patch.object(checker, "bounded_probe", side_effect=self.probe), \
                patch.object(checker, "pve_reference_inventory", side_effect=self.refs):
            return collect_snapshot(checker, "local", self.boot)

    def test_multiple_active_and_dormant_volumes_all_aliases_disabled_included(self):
        result = self.collect()
        self.assertEqual(set(result["scopes"]), {("eager", "vm-100-disk-0"), ("lazy", "vm-100-disk-0"),
                                               ("eager", "vm-101-disk-0"), ("lazy", "vm-101-disk-0"),
                                               ("other", "vm-102-disk-0")})
        self.assertEqual(len(result["evidence"]), 2)

    def test_stale_local_owner_and_transitional_or_tampered_anchor_refuse(self):
        original = self.inventories["vg-b"]
        for raw in (inventory("other", "2" * 32, "vm-102-disk-0", boot="22222222-2222-3333-4444-555555555555"),
                    original.replace("LAZY_ACTIVE", "LAZY_CLAIMED"), original.replace("slt_tg_sha256=", "slt_tg_invalid=")):
            self.inventories["vg-b"] = raw
            with self.assertRaises(RuntimeError): self.collect()

    def test_remote_owner_requires_positive_local_absence_and_keeps_scope(self):
        self.inventories["vg-b"] = inventory("other", "2" * 32, "vm-102-disk-0", owner="peer")
        result = self.collect()
        self.assertIn(("other", "vm-102-disk-0"), result["scopes"])
        self.assertEqual(sum(argv[0] == "/sbin/dmsetup" for argv in self.commands), 2)

    def test_unknown_probe_cannot_be_empty_inventory(self):
        with patch.object(checker, "storage_configs", return_value=self.configs), \
                patch.object(checker, "bounded_probe", return_value=dict(status="TIMEOUT", out="")):
            with self.assertRaisesRegex(RuntimeError, "TIMEOUT"): collect_snapshot(checker, "local", self.boot)

    def test_rescan_changes_refuse_and_node_scope_is_exact(self):
        before = self.collect()
        after = copy.deepcopy(before); after["configs"][0]["disable"] = "0"
        with self.assertRaisesRegex(RuntimeError, "changed"): require_unchanged(before, after)
        after = copy.deepcopy(before); after["scopes"] += (("lazy", "vm-999-disk-0"),)
        with self.assertRaises(RuntimeError): require_unchanged(before, after)
        require_unchanged(before, copy.deepcopy(before))
        for cfg in self.configs: cfg["nodes"] = "peer"
        self.assertEqual(self.collect()["scopes"], ())

    def test_mismatched_alias_pin_and_foreign_same_vg_refuse(self):
        self.configs[1]["slt-expected-pv-uuid"] = "different"
        with self.assertRaises(RuntimeError): self.collect()
        self.configs[1]["slt-expected-pv-uuid"] = "pv-uuid"
        self.configs.append(dict(id="foreign", type="lvm", vgname="vg-a"))
        with self.assertRaises(RuntimeError): self.collect()
