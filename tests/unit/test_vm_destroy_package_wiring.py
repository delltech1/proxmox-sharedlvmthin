import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_GLOB = "SharedLvmThinVMDestroy*.pm"


def source_modules():
    base = ROOT / "usr/share/perl5/PVE"
    return sorted(path.relative_to(ROOT).as_posix() for path in base.glob(MODULE_GLOB))


class VMDestroyPackageSourceContractTests(unittest.TestCase):
    def test_internal_modules_are_common_payload_and_not_public_dispatch(self):
        modules = source_modules()
        self.assertEqual(
            modules,
            [
                "usr/share/perl5/PVE/SharedLvmThinVMDestroy.pm",
                "usr/share/perl5/PVE/SharedLvmThinVMDestroyDispatcher.pm",
                "usr/share/perl5/PVE/SharedLvmThinVMDestroyFinalizationObserver.pm",
                "usr/share/perl5/PVE/SharedLvmThinVMDestroyInventory.pm",
                "usr/share/perl5/PVE/SharedLvmThinVMDestroyJournal.pm",
                "usr/share/perl5/PVE/SharedLvmThinVMDestroyPlanAdapter.pm",
                "usr/share/perl5/PVE/SharedLvmThinVMDestroyPlanV3.pm",
                "usr/share/perl5/PVE/SharedLvmThinVMDestroyRecoveryDescriptor.pm",
                "usr/share/perl5/PVE/SharedLvmThinVMDestroyRuntime.pm",
                "usr/share/perl5/PVE/SharedLvmThinVMDestroyStoragePlan.pm",
            ],
        )
        excluded = (ROOT / "packaging/thick-only/excluded-paths.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        excluded = {line.strip() for line in excluded if line.strip() and not line.startswith("#")}
        self.assertTrue(set(modules).isdisjoint(excluded))

        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        self.assertIn('cp -a "$ROOT/DEBIAN" "$ROOT/etc" "$ROOT/lib" "$ROOT/usr" "$STAGE/"', build)
        self.assertIn('find "$STAGE/usr/share/perl5/PVE" -type f -name \'*.pm\'', build)
        self.assertIn('find "$STAGE" -type f -exec chmod 0644 {} +', build)
        self.assertIn("dpkg-deb -Zxz --root-owner-group --build", build)
        self.assertIn(">DEBIAN/md5sums", build)

        helper = (ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy").read_text(
            encoding="utf-8"
        )
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("journal substrate ONLY; never dispatches PVE/storage commands", helper)
        self.assertIn('choices=["observe", "_append", "_observe-durable"]', helper)
        self.assertIn("vm-destroy-observe)", cli)
        self.assertNotIn("vm-destroy-execute)", cli)

    def test_release_gate_privacy_scans_both_archives(self):
        gate = (ROOT / "scripts/check-release.sh").read_text(encoding="utf-8")
        self.assertIn('python3 "$ROOT/scripts/package-privacy-scan.py"', gate)
        self.assertIn('"$TMP/root" "$TMP/control"', gate)


@unittest.skipUnless(
    os.name == "posix" and all(shutil.which(tool) for tool in ("sh", "dpkg-deb", "tar")),
    "DEB payload contract requires a POSIX dpkg build environment",
)
class VMDestroyBuiltPackageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.packages = {}
        cls.roots = {}
        cls.controls = {}
        env = os.environ.copy()
        env["SOURCE_DATE_EPOCH"] = "1788231600"
        for profile in ("dual", "thick-only"):
            output = cls.base / profile
            output.mkdir()
            subprocess.run(
                ["sh", str(ROOT / "scripts/build.sh"), str(output), profile],
                cwd=ROOT,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            packages = list(output.glob("*.deb"))
            if len(packages) != 1:
                raise AssertionError(f"expected one {profile} DEB, got {packages}")
            package = packages[0]
            root = cls.base / f"root-{profile}"
            control = cls.base / f"control-{profile}"
            subprocess.run(["dpkg-deb", "--extract", str(package), str(root)], check=True)
            subprocess.run(["dpkg-deb", "--control", str(package), str(control)], check=True)
            cls.packages[profile] = package
            cls.roots[profile] = root
            cls.controls[profile] = control

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_both_profiles_ship_exact_root_owned_0644_modules_and_md5sums(self):
        expected = source_modules()
        for profile in ("dual", "thick-only"):
            with self.subTest(profile=profile):
                listing = subprocess.run(
                    ["dpkg-deb", "--contents", str(self.packages[profile])],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.splitlines()
                rows = {}
                for line in listing:
                    parts = line.split(maxsplit=5)
                    if len(parts) == 6:
                        rows[parts[5].removeprefix("./")] = (parts[0], parts[1])
                md5_lines = (self.controls[profile] / "md5sums").read_text(
                    encoding="ascii"
                ).splitlines()
                md5_paths = {line.split(maxsplit=1)[1] for line in md5_lines}
                for relative in expected:
                    self.assertEqual(rows.get(relative), ("-rw-r--r--", "root/root"))
                    self.assertIn(relative, md5_paths)
                    digest = hashlib.md5((self.roots[profile] / relative).read_bytes()).hexdigest()
                    self.assertIn(f"{digest}  {relative}", md5_lines)

    def test_common_destroy_module_bytes_are_identical_between_profiles(self):
        helpers = ["usr/libexec/pve-sharedlvmthin/" + name for name in (
            "sharedlvmthin-vm-destroy-dispatch", "sharedlvmthin-vm-destroy-state-bootstrap",
            "sharedlvmthin-vm-destroy", "sharedlvmthin-vm-destroy-recovery")]
        for relative in source_modules() + helpers:
            with self.subTest(module=relative):
                self.assertEqual(
                    (self.roots["dual"] / relative).read_bytes(),
                    (self.roots["thick-only"] / relative).read_bytes(),
                )

    def _runtime_id(self, root, embedded_marker, replacement=None):
        rows = []
        base = root / "usr/share/perl5/PVE"
        paths = sorted(base.rglob("*.pm"), key=lambda path: os.fsencode(str(path)))
        plugin_relative = "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        marker_bytes = embedded_marker.encode("ascii")
        template_bytes = b"__SLT_RUNTIME_BUILD_ID__"
        for path in paths:
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            if relative == plugin_relative:
                self.assertEqual(data.count(marker_bytes), 1)
                data = data.replace(marker_bytes, template_bytes, 1)
            if replacement and relative == replacement[0]:
                data = replacement[1]
            rows.append(f"{hashlib.sha256(data).hexdigest()}  {relative}\n".encode("ascii"))
        return hashlib.sha256(b"".join(rows)).hexdigest()

    def test_runtime_build_id_covers_destroy_modules_and_is_sensitive(self):
        target = source_modules()[0]
        for profile in ("dual", "thick-only"):
            with self.subTest(profile=profile):
                root = self.roots[profile]
                marker = (root / "usr/share/pve-sharedlvmthin/runtime-build-id").read_text(
                    encoding="ascii"
                ).strip()
                self.assertEqual(marker, self._runtime_id(root, marker))
                original = (root / target).read_bytes()
                self.assertNotEqual(
                    marker,
                    self._runtime_id(root, marker, (target, original + b"\n")),
                )

    def test_extracted_archives_pass_privacy_scan(self):
        for profile in ("dual", "thick-only"):
            with self.subTest(profile=profile):
                subprocess.run(
                    [
                        os.environ.get("PYTHON", "python3"),
                        str(ROOT / "scripts/package-privacy-scan.py"),
                        str(self.roots[profile]),
                        str(self.controls[profile]),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )


if __name__ == "__main__":
    unittest.main()
