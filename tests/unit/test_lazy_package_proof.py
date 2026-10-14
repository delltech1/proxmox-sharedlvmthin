import copy
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[2] / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_lazy_package_proof.py"
spec = importlib.util.spec_from_file_location("lazy_package_proof", path)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class LazyPackageProofTests(unittest.TestCase):
    def remote_fixture(self):
        anchor = dict(v="6", phase="LAZY_ACTIVE", sid="lazy", owner_node="peer",
                      vol="vm-100-disk-0", tx="b" * 32, head="head", metadata="meta",
                      data_uuid="c" * 32, metadata_uuid="d" * 32)
        cfg = {"id": "lazy", "slt-vgname": "vg", "slt-expected-vg-uuid": "a" * 32}
        rows = {lv: [lv, "-wi-------", "", "", "", "", "", "1048576", anchor[field]]
                for lv, field in (("head", "data_uuid"), ("meta", "metadata_uuid"))}
        return anchor, cfg, rows

    def test_remote_absence_is_only_nonownership_not_remote_health(self):
        anchor, cfg, rows = self.remote_fixture()
        calls = []
        def observe(argv):
            calls.append(argv)
            return "unrelated|other-uuid|253|1|Active\n"
        result = proof.audit_remote_local_absence(anchor, cfg, rows, observe, node="local")
        self.assertEqual(result, {"state": "REMOTE_OWNED_LOCAL_RUNTIME_ABSENT", "remote_runtime": "NOT_CERTIFIED"})
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(argv[:2] == ["/sbin/dmsetup", "info"] for argv in calls))

    def test_remote_absence_detects_names_and_renamed_uuid_maps(self):
        import hashlib
        anchor, cfg, rows = self.remote_fixture()
        key = hashlib.sha256(("a" * 32 + "\0vm-100-disk-0").encode()).hexdigest()[:24]
        for name, uuid in (("sltg-" + key, ""), ("sltg-c-" + key, ""),
                           ("sltg-z-" + key, ""), ("vg-head", ""), ("vg-meta", ""),
                           ("renamed", "SLT-TG2-" + key),
                           ("renamed", "SLT-TG6-CLONE-" + "b" * 32),
                           ("renamed", "LVM-" + "a" * 32 + "c" * 32),
                           ("renamed", "LVM-" + "a" * 32 + "d" * 32)):
            with self.subTest(name=name, uuid=uuid):
                with self.assertRaisesRegex(ValueError, "conflicting"):
                    proof.audit_remote_local_absence(anchor, cfg, rows,
                                                    lambda argv: f"{name}|{uuid}|253|1|Active", node="local")

    def test_remote_absence_refuses_active_backing_malformed_and_second_sample(self):
        anchor, cfg, rows = self.remote_fixture()
        for attr in ("-wi-a-----", "unknown", "-wi-?-----"):
            bad = copy.deepcopy(rows); bad["head"][1] = attr
            with self.assertRaises(ValueError):
                proof.audit_remote_local_absence(anchor, cfg, bad, lambda argv: "", node="local")
        for raw in ("truncated", "x||bad|1|Active", "x||253|1|UNKNOWN",
                    "x||253|1|Active\nx||253|2|Active", "x||253|1|Active\ny||253|1|Active"):
            with self.assertRaises(ValueError):
                proof.audit_remote_local_absence(anchor, cfg, rows, lambda argv: raw, node="local")
        samples = iter(["", "renamed|LVM-" + "a" * 32 + "c" * 32 + "|253|1|Active"])
        with self.assertRaisesRegex(ValueError, "conflicting"):
            proof.audit_remote_local_absence(anchor, cfg, rows, lambda argv: next(samples), node="local")
        with self.assertRaisesRegex(RuntimeError, "UNKNOWN"):
            proof.audit_remote_local_absence(anchor, cfg, rows,
                                            lambda argv: (_ for _ in ()).throw(RuntimeError("UNKNOWN")), node="local")

    def test_remote_branch_cannot_accept_local_owner_or_other_alias(self):
        anchor, cfg, rows = self.remote_fixture()
        for bad in (dict(anchor, owner_node="local"), dict(anchor, owner_node="none"),
                    dict(anchor, owner_node="../peer"), dict(anchor, phase="LAZY_CLAIMED")):
            with self.assertRaises(ValueError):
                proof.audit_remote_local_absence(bad, cfg, rows, lambda argv: "", node="local")
        with self.assertRaisesRegex(ValueError, "alias"):
            proof.audit_remote_local_absence(anchor, dict(cfg, id="eager"), rows, lambda argv: "", node="local")

    def test_remote_active_audit_refuses_before_any_probe(self):
        commands = []
        with self.assertRaisesRegex(ValueError, "remote or stale"):
            proof.audit_active_graph(dict(owner_node="remote", owner_boot="old"), {}, {},
                                     lambda argv: commands.append(argv), None, None,
                                     node="local", boot="new")
        self.assertEqual(commands, [])

    def test_fragmented_backing_is_derived_from_native_extent_evidence(self):
        kwargs = dict(lv_name="head", vg_uuid="v" * 32, lv_uuid="l" * 32,
                      pv_path="/dev/mapper/pinned", pv_devno="253:9", pe_start=8,
                      extent_sectors=8, pv_sectors=1024, expected_sectors=16)
        rows = "head|linear|0|8|/dev/mapper/pinned(2)\nhead|linear|8|8|/dev/mapper/pinned(10)"
        expected = proof.derive_backing_graph(rows, **kwargs)
        self.assertEqual(expected["table"], "0 8 linear 253:9 24\n8 8 linear 253:9 88")
        for bad in (rows.replace("(10)", "(999)"), rows.replace("8|8", "16|8"),
                    rows.replace("linear", "striped"), rows.replace("pinned(10)", "foreign(10)"),
                    rows.splitlines()[0], rows + "\n" + rows.splitlines()[0]):
            with self.subTest(rows=bad):
                with self.assertRaises(ValueError):
                    proof.derive_backing_graph(bad, **kwargs)

    def test_expected_graph_is_derived_from_owner_and_pinned_identity(self):
        anchor = dict(v="6", phase="LAZY_ACTIVE", owner_node="node", owner_boot="boot",
                      sid="lazy", tx="a" * 32, owner_epoch="b" * 32,
                      vol="vm-100-disk-0", bytes="1048576", region="2048",
                      publication="3", data_uuid="d" * 32, metadata_uuid="e" * 32)
        cfg = {"id": "lazy", "slt-expected-vg-uuid": "c" * 32}
        backing = {role: dict(uuid="LVM-" + "c" * 32 + char * 32,
                              readonly=False, table="approved physical segments", deps=("8:0",))
                   for role, char in (("data", "d"), ("metadata", "e"))}
        devnos = {role: f"253:{index}" for index, role in enumerate(("data", "metadata", "zero", "clone", "front"))}
        expected, names = proof.derive_custom_graph(anchor, cfg, backing, devnos, node="node", boot="boot")
        self.assertEqual(expected["zero"]["table"], "0 2048 zero")
        self.assertEqual(expected["front"]["table"], "0 2048 linear 253:3 0")
        self.assertEqual(expected["clone"]["hydration_threshold"], 4)
        self.assertEqual(expected["clone"]["hydration_batch_size"], 1)
        self.assertEqual(expected["clone"]["deps"], ("253:0", "253:1", "253:2"))
        self.assertTrue(names["front"].startswith("sltg-"))
        for field, value in (("owner_node", "other"), ("owner_boot", "stale"),
                             ("data_uuid", "wrong"), ("phase", "LAZY_CLAIMED"),
                             ("publication", "1")):
            with self.subTest(field=field):
                bad = dict(anchor, **{field: value})
                with self.assertRaises(ValueError):
                    proof.derive_custom_graph(bad, cfg, backing, devnos, node="node", boot="boot")
        with self.assertRaisesRegex(ValueError, "owner alias"):
            proof.derive_custom_graph(anchor, dict(cfg, id="eager"), backing, devnos, node="node", boot="boot")

    def test_collector_is_readonly_and_brackets_disk_sequence(self):
        commands = []
        def runner(argv):
            commands.append(argv)
            if argv[1] == "info":
                return "uuid|Writeable|253|1|Active\n"
            return {"table": "0 4096 linear 253:2 0\n",
                    "deps": "1 dependencies : (253, 2)\n"}[argv[1]]
        result = proof.observe_mapper("front", "front", runner,
                                      lambda devno, attr: 7 if attr == "diskseq" else 0)
        self.assertEqual(result["diskseq"], 7)
        self.assertEqual(result["deps"], ("253:2",))
        self.assertEqual([argv[1] for argv in commands], ["info", "table", "deps", "info"])
        sequences = iter((7, 8))
        with self.assertRaisesRegex(ValueError, "identity changed"):
            proof.observe_mapper("front", "front", runner,
                                 lambda devno, attr: next(sequences) if attr == "diskseq" else 0)

    def test_clone_collector_always_uses_noflush(self):
        commands = []
        def runner(argv):
            commands.append(argv)
            if argv[1] == "info":
                return "uuid|writeable|253|1|active\n"
            return {"table": "one approved clone table\n", "deps": "0 dependencies :\n",
                    "status": "one status\n"}[argv[1]]
        proof.observe_mapper("clone", "clone", runner,
                             lambda devno, attr: 7 if attr == "diskseq" else 0)
        self.assertIn(["/sbin/dmsetup", "status", "--noflush", "clone"], commands)

    def setUp(self):
        self.expected = {}
        self.graph = {}
        for index, name in enumerate(("data", "metadata", "zero", "clone", "front")):
            wanted = dict(uuid=f"uuid-{name}", table=f"approved-{name}",
                          deps=(), readonly=name == "zero")
            self.expected[name] = wanted
            self.graph[name] = dict(wanted, devno=f"253:{index}", diskseq=index + 1,
                                    suspended=False, discard_max_bytes=0,
                                    discard_max_hw_bytes=0, write_zeroes_max_bytes=0)
        self.expected["clone"].update(hydration_threshold=4, hydration_batch_size=1)
        self.suffix = "2 no_hydration no_discard_passdown 4 hydration_threshold 4 hydration_batch_size 1 rw"
        self.graph["clone"]["status"] = "0 4096 clone 8 1/100 2048 0/2 0 " + self.suffix

    def test_guest_hydration_progress_does_not_invalidate_stable_graph(self):
        after = copy.deepcopy(self.graph)
        after["clone"]["status"] = "0 4096 clone 8 2/100 2048 1/2 0 " + self.suffix
        self.assertTrue(proof.validate_graph(self.expected, self.graph, after, sectors=4096, region=2048))

    def test_identity_recreation_and_guard_drift_refuse(self):
        for name, field, value in (("data", "diskseq", 99), ("front", "devno", "253:99"),
                                   ("clone", "write_zeroes_max_bytes", 4096),
                                   ("front", "suspended", True), ("zero", "readonly", False),
                                   ("clone", "deps", ("253:99",)), ("front", "table", "wrong")):
            with self.subTest(name=name, field=field):
                after = copy.deepcopy(self.graph)
                after[name][field] = value
                with self.assertRaises(ValueError):
                    proof.validate_graph(self.expected, self.graph, after, sectors=4096, region=2048)

    def test_live_counter_samples_do_not_imply_an_atomic_sum(self):
        after = copy.deepcopy(self.graph)
        after["clone"]["status"] = "0 4096 clone 8 2/100 2048 2/2 1 " + self.suffix
        self.assertTrue(proof.validate_graph(self.expected, self.graph, after, sectors=4096, region=2048))

    def test_bad_kernel_status_refuses(self):
        for status in ("0 4096 clone Fail", "0 4096 clone 8 1/100 2048 0/2 0 ro",
                       "0 4096 clone 8 101/100 2048 0/2 0 rw",
                       "0 4096 clone 8 1/100 2048 1/2 3 " + self.suffix, "truncated",
                       "0 4096 clone 8 1/100 2048 0/2 0 1 no_hydration rw",
                       "0 4096 clone 8 1/100 2048 0/2 0 " + self.suffix.replace("threshold 4", "threshold 5")):
            with self.subTest(status=status):
                after = copy.deepcopy(self.graph)
                after["clone"]["status"] = status
                with self.assertRaises(ValueError):
                    proof.validate_graph(self.expected, self.graph, after, sectors=4096, region=2048)
