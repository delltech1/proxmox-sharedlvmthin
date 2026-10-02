import importlib.machinery
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "experiments/thick-generations/tg53-vg-wave-plan.py"


def load():
    loader = importlib.machinery.SourceFileLoader("tg53_vg_wave", str(PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class VGWavePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load()

    def test_disk_parser_covers_data_efi_tpm_and_skips_cdrom_cloudinit(self):
        self.assertEqual(
            self.module.disk_storage_ids({
                "scsi0": "lazy:vm-100-disk-0,size=1G",
                "scsi1": "eager:vm-100-disk-1,size=1G",
                "efidisk0": "efi:vm-100-disk-2,efitype=4m,size=4M",
                "tpmstate0": "tpm:vm-100-disk-3,size=4M,version=v2.0",
                "ide2": "local:cloudinit,media=cdrom",
                "sata5": "none,media=cdrom",
            }),
            {"lazy", "eager", "efi", "tpm"},
        )

    def test_aliases_with_same_exact_vg_identity_collide(self):
        a = self.module.canonical_resource("lazy", {
            "type": "sharedlvmthin", "slt-expected-vg-uuid": "vg",
            "slt-expected-wwid": "wwid", "slt-vgname": "name",
        })
        b = self.module.canonical_resource("eager", {
            "type": "sharedlvmthin", "slt-expected-vg-uuid": "vg",
            "slt-expected-wwid": "wwid", "slt-vgname": "name",
        })
        self.assertEqual(a, b)

    def test_greedy_plan_serializes_shared_resources_and_keeps_independent_parallel(self):
        plan = self.module.greedy_waves([
            ("100", {"vg:a"}),
            ("101", {"vg:b"}),
            ("102", {"vg:a", "vg:b"}),
            ("103", {"vg:c"}),
        ])
        self.assertEqual(plan["100"], 0)
        self.assertEqual(plan["101"], 0)
        self.assertEqual(plan["103"], 0)
        self.assertEqual(plan["102"], 1)


if __name__ == "__main__":
    unittest.main()
