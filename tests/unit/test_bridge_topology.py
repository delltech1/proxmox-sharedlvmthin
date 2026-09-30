import importlib.machinery
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-topology"


def load_helper():
    loader = importlib.machinery.SourceFileLoader("bridge_topology", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class BridgeTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = load_helper()

    def classify(self, text, allowed=("thin",)):
        return self.helper.classify(text, set(allowed))

    def refused(self, text, reason, allowed=("thin",)):
        with self.assertRaises(SystemExit) as caught:
            self.classify(text, allowed)
        self.assertEqual(caught.exception.code, 65)

    def test_one_and_multiple_plain_disks_are_canonical(self):
        self.assertEqual(self.classify(
            "scsi0: thin:vm-100-disk-0,size=1G\n"
            "virtio1: file=thin:vm-100-disk-1,size=2G\n"
            "ide2: none,media=cdrom\n"
        ), ["scsi0|thin:vm-100-disk-0", "virtio1|thin:vm-100-disk-1"])

    def test_cloudinit_or_backed_cd_is_refused(self):
        for line in (
            "ide2: thin:vm-100-cloudinit,media=cdrom\n",
            "sata5: local:iso/test.iso,media=cdrom\n",
        ):
            with self.subTest(line=line):
                self.refused("scsi0: thin:vm-100-disk-0,size=1G\n" + line,
                             "UNSUPPORTED_BACKED_CD")

    def test_efi_and_tpm_are_refused_on_every_storage(self):
        for key in ("efidisk0", "tpmstate0"):
            for sid in ("thin", "thick", "foreign"):
                with self.subTest(key=key, sid=sid):
                    self.refused(
                        f"scsi0: thin:vm-100-disk-0,size=1G\n{key}: {sid}:vm-100-disk-1,size=4M\n",
                        "UNSUPPORTED_SPECIAL_SLOT", ("thin", "thick"),
                    )

    def test_mixed_foreign_writable_and_managed_unused_are_refused(self):
        self.refused("scsi0: foreign:vm-100-disk-0,size=1G\n", "UNSUPPORTED_STORAGE")
        self.refused(
            "scsi0: thin:vm-100-disk-0,size=1G\nunused0: thin:vm-100-disk-9\n",
            "UNSUPPORTED_MANAGED_UNUSED",
        )

    def test_duplicate_malformed_and_unknown_managed_slots_are_refused(self):
        cases = (
            "scsi0: thin:vm-100-disk-0,size=1G\nscsi0: thin:vm-100-disk-1,size=1G\n",
            "scsi0: thin:vm-100-disk-0,,size=1G\n",
            "scsi0: thin:vm-100-disk-0,file=thin:vm-100-disk-1,size=1G\n",
            "future0: thin:vm-100-disk-0,size=1G\n",
        )
        for text in cases:
            with self.subTest(text=text):
                self.refused(text, "REFUSE")

    def test_snapshot_sections_do_not_expand_live_topology(self):
        self.assertEqual(self.classify(
            "scsi0: thin:vm-100-disk-0,size=1G\n"
            "[old]\nscsi1: foreign:vm-100-disk-1,size=1G\n"
        ), ["scsi0|thin:vm-100-disk-0"])


if __name__ == "__main__":
    unittest.main()
