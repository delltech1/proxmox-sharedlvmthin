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

    def test_empty_duplicate_disk_key_invalidates_valid_record(self):
        for path, valid, empty, volid in (
            (self.QEMU, "scsi0: thick:vm-100-disk-0,size=4G\n", "scsi0:\n",
             "thick:vm-100-disk-0"),
            (self.QEMU, "efidisk0: thick:vm-100-disk-1,size=4M\n", "efidisk0:   \n",
             "thick:vm-100-disk-1"),
            (self.QEMU, "tpmstate0: thick:vm-100-disk-2,size=4M\n", "tpmstate0:\n",
             "thick:vm-100-disk-2"),
            (self.QEMU, "unused0: thick:vm-100-disk-3\n", "unused0:\n",
             "thick:vm-100-disk-3"),
            (self.LXC, "mp0: thick:vm-101-disk-0,mp=/data,size=4G\n", "mp0:\n",
             "thick:vm-101-disk-0"),
            (self.LXC, "rootfs: thick:vm-101-disk-1,size=4G\n", "rootfs:\n",
             "thick:vm-101-disk-1"),
        ):
            for content in (valid + empty, empty + valid):
                with self.subTest(path=path, key=empty.split(":", 1)[0],
                                  empty_first=content.startswith(empty)):
                    _refs, current, errors = self.collect(path, content)
                    self.assertEqual(errors, [])
                    self.assertEqual(current[volid][0][2], "invalid")

    def test_identity_free_duplicate_disk_key_is_an_inventory_error(self):
        for path, content in (
            (self.QEMU, "scsi0:\nscsi0:   \n"),
            (self.QEMU, "efidisk0:\nefidi sk0:\nefidisk0:   \n"),
            (self.LXC, "mp0:\nmp0:   \n"),
        ):
            with self.subTest(path=path, content=content):
                _refs, current, errors = self.collect(path, content)
                self.assertEqual(current, {})
                self.assertEqual(errors, [path])

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

    def test_duplicate_vmstate_key_never_yields_authoritative_reference(self):
        for section in ("", "[ram-one]\n"):
            for tail in ("vmstate:\n", "vmstate:   \n",
                         "vmstate: thick:vm-100-state-other\n",
                         "vmstate: thick:vm-100-state-ram\n"):
                with self.subTest(section=section, tail=tail):
                    _refs, current, _errors = self.collect(
                        self.QEMU, section + "vmstate: thick:vm-100-state-ram\n" + tail)
                    self.assertTrue(current)
                    self.assertTrue(all(row[2] == "invalid" for rows in current.values() for row in rows))

    def test_empty_first_vmstate_and_repeated_snapshot_section_are_ambiguous(self):
        for content in (
            "[ram]\nvmstate:\nvmstate: thick:vm-100-state-ram\n",
            "[ram]\nvmstate: thick:vm-100-state-one\n[ram]\nvmstate: thick:vm-100-state-two\n",
        ):
            _refs, current, _errors = self.collect(self.QEMU, content)
            self.assertTrue(all(row[2] == "invalid" for rows in current.values() for row in rows))
        _refs, current, errors = self.collect(self.QEMU, "[ram]\nvmstate:\nvmstate:\n")
        self.assertEqual(current, {})
        self.assertEqual(errors, [self.QEMU], "identity-free duplicate cannot silently prove absence")

    def test_empty_mtu_preserves_distinct_snapshot_vmstates_and_current_auxiliary_disks(self):
        _refs, current, errors = self.collect(
            self.QEMU,
            "scsi0: thick:vm-100-disk-0,size=4G\n"
            "scsi1: thick:vm-100-disk-1,size=2G\n"
            "efidisk0: thick:vm-100-disk-2,size=4M\n"
            "tpmstate0: thick:vm-100-disk-3,size=4M\n"
            "ide2: thick:vm-100-cloudinit,media=cdrom,size=4M\n"
            "[ram-one]\nscsi0: thick:vm-100-disk-0,size=1G\n"
            "running-nets-host-mtu:  \nvmstate: thick:vm-100-state-ram-one\n"
            "[ram-two]\nrunning-nets-host-mtu: net0=0,net1=1500\n"
            "vmstate: thick:vm-100-state-ram-two\n"
            "description: vmstate: thick:vm-100-state-description-only\n",
        )
        self.assertEqual(errors, [])
        for state in ("ram-one", "ram-two"):
            self.assertEqual(current[f"thick:vm-100-state-{state}"], [(self.QEMU, None, "auxiliary")])
        self.assertEqual(current["thick:vm-100-disk-0"], [(self.QEMU, 4 * 2**30, "attached")])
        self.assertEqual(current["thick:vm-100-disk-2"], [(self.QEMU, 4 * 2**20, "efi")])
        self.assertEqual(current["thick:vm-100-disk-3"], [(self.QEMU, 4 * 2**20, "attached")])
        self.assertNotIn("thick:vm-100-state-description-only", current)

    def test_only_exact_unique_top_level_efidisk0_gets_efi_provenance(self):
        _refs, current, errors = self.collect(
            self.QEMU,
            "efidisk0: thick:vm-100-disk-0,size=528K\n"
            "scsi0: thick:vm-100-disk-1,size=528K\n"
            "tpmstate0: thick:vm-100-disk-2,size=528K\n",
        )
        self.assertEqual(errors, [])
        self.assertEqual(current["thick:vm-100-disk-0"][0][1:], (528 * 1024, "efi"))
        self.assertEqual(current["thick:vm-100-disk-1"][0][2], "attached")
        self.assertEqual(current["thick:vm-100-disk-2"][0][2], "attached")

    def test_duplicate_or_malformed_efidisk_never_gets_efi_provenance(self):
        _refs, current, _errors = self.collect(
            self.QEMU,
            "efidisk0: thick:vm-100-disk-0,size=528K\n"
            "efidisk0: thick:vm-100-disk-1,size=528K,size=4M\n",
        )
        self.assertTrue(all(
            row[2] == "invalid" for rows in current.values() for row in rows
        ))


if __name__ == "__main__":
    unittest.main()
