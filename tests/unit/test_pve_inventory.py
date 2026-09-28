import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_pve_inventory.py"
SPEC = importlib.util.spec_from_file_location("shared_pve_inventory_test", MODULE)
INVENTORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INVENTORY)


class PveInventoryAdversarialTests(unittest.TestCase):
    QEMU = "/etc/pve/nodes/n/qemu-server/100.conf"
    LXC = "/etc/pve/nodes/n/lxc/101.conf"

    def collect(self, path, content):
        def globber(pattern):
            family = "/lxc/" if "/lxc/" in path else "/qemu-server/"
            return [path] if family in pattern and "/nodes/" in pattern else []

        return INVENTORY.collect(
            read_text=lambda candidate: content if candidate == path else None,
            globber=globber,
        )

    def test_non_volume_named_override_invalidates_bare_identity(self):
        _refs, current, errors = self.collect(
            self.QEMU,
            "scsi0: thick:vm-100-disk-0,file=/dev/other,size=4G\n",
        )
        self.assertEqual(errors, [])
        self.assertEqual(current["thick:vm-100-disk-0"][0][2], "invalid")

    def test_duplicate_disk_key_invalidates_every_identity(self):
        _refs, current, _errors = self.collect(
            self.QEMU,
            "scsi0: thick:vm-100-disk-0,size=4G\n"
            "scsi0: other:vm-100-disk-1,size=3G\n",
        )
        self.assertEqual(current["thick:vm-100-disk-0"][0][2], "invalid")
        self.assertEqual(current["other:vm-100-disk-1"][0][2], "invalid")

    def test_invalid_slot_cannot_hide_behind_detached_reference(self):
        _refs, current, _errors = self.collect(
            self.QEMU,
            "unused0: thick:vm-100-disk-0\n"
            "scsi99999: thick:vm-100-disk-0,size=3G\n",
        )
        self.assertEqual(
            [entry[2] for entry in current["thick:vm-100-disk-0"]],
            ["detached", "invalid"],
        )

    def test_malformed_second_positional_and_property_are_invalid(self):
        _refs, current, _errors = self.collect(
            self.QEMU,
            "scsi0: thick:vm-100-disk-0,other:vm-100-disk-1,size=4G\n"
            "scsi1: thick:vm-100-disk-2,size=4G,size\n",
        )
        self.assertEqual(current["thick:vm-100-disk-0"][0][2], "invalid")
        self.assertEqual(current["thick:vm-100-disk-2"][0][2], "invalid")

    def test_lxc_duplicate_key_and_malformed_volume_fail_closed(self):
        _refs, current, _errors = self.collect(
            self.LXC,
            "mp0: thick:vm-101-disk-0,mp=/a,size=4G\n"
            "mp0: mp=/b,volume=thick:vm-101-disk-1,size=4G\n"
            "rootfs: thick:vm-101-disk-2,volume=/dev/other,size=4G\n",
        )
        for volid in (
            "thick:vm-101-disk-0",
            "thick:vm-101-disk-1",
            "thick:vm-101-disk-2",
        ):
            self.assertEqual(current[volid][0][2], "invalid")

    def test_unparseable_duplicate_key_still_invalidates_valid_record(self):
        _refs, current, _errors = self.collect(
            self.QEMU,
            "scsi0: thick:vm-100-disk-0,size=4G\n"
            "scsi0: /dev/other,size=3G\n",
        )
        self.assertEqual(current["thick:vm-100-disk-0"][0][2], "invalid")

    def test_invalid_positional_plus_named_identity_is_invalid(self):
        for path, line, volid in (
            (self.QEMU,
             "scsi0: /dev/other,file=thick:vm-100-disk-0,size=4G\n",
             "thick:vm-100-disk-0"),
            (self.LXC,
             "mp0: /path,volume=thick:vm-101-disk-0,mp=/data,size=4G\n",
             "thick:vm-101-disk-0"),
        ):
            with self.subTest(path=path):
                _refs, current, _errors = self.collect(path, line)
                self.assertEqual(current[volid][0][2], "invalid")


if __name__ == "__main__":
    unittest.main()
