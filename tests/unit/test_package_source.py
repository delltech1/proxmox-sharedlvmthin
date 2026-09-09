import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PackageSourceTests(unittest.TestCase):
    def test_rc5_version(self):
        control = (ROOT / "DEBIAN/control").read_text(encoding="utf-8")
        self.assertRegex(control, r"(?m)^Version: 0\.9\.0~rc5(?:\.\d+)+$")

    def test_doctor_accepts_elastic_and_legacy_thresholds(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text()
        self.assertIn('pass "Thin autoextend threshold = 50% (elastic early-grow policy)"', doctor)
        self.assertIn('pass "Thin autoextend threshold = 80% (legacy fixed/proportional policy)"', doctor)

    def test_package_does_not_contain_private_keys(self):
        forbidden = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            if path.name.endswith(".key") or path.name in {"id_rsa", "id_ed25519"}:
                forbidden.append(path)
        self.assertEqual(forbidden, [])

    def test_product_facing_sources_do_not_contain_slovak_or_czech_markers(self):
        product = [
            ROOT / "usr/share/pve-sharedlvmthin/web/index.html",
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-web",
            ROOT / "usr/sbin/sharedlvmthin",
            ROOT / "usr/sbin/sharedlvmthin-web-configure",
        ]
        marker = re.compile(r"\b(áno|chyba|úložisko|prihlás|zdravie|súbor)\b", re.I)
        hits = []
        for path in product:
            if marker.search(path.read_text(encoding="utf-8")):
                hits.append(path)
        self.assertEqual(hits, [])

    def test_postinst_does_not_claim_operational_after_failed_health_check(self):
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        self.assertNotIn("fully installed and operational", postinst)
        self.assertIn(
            "Operational status: NOT READY (health check or PVE service refresh failed)",
            postinst,
        )
        self.assertIn("must NOT be treated as operational", postinst)
        self.assertIn('DOCTOR_RC', postinst)
        self.assertIn('passed with diagnostic warnings', postinst)

    def test_postinst_refreshes_only_active_pve_storage_consumers(self):
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")

        for service in (
            "pvedaemon.service",
            "pvestatd.service",
            "pveproxy.service",
            "pve-ha-lrm.service",
        ):
            self.assertIn(service, postinst)
        self.assertIn('systemctl is-active --quiet "$PVE_SERVICE"', postinst)
        self.assertIn('systemctl try-restart "$PVE_SERVICE"', postinst)
        self.assertNotIn('systemctl restart "$PVE_SERVICE"', postinst)
        self.assertNotIn("pve-ha-crm.service", postinst)
        self.assertNotIn("corosync.service", postinst)
        self.assertIn("PVE_REFRESH_OK=0", postinst)
        self.assertIn(
            '[ "$HEALTH_OK" -eq 1 ] && [ "$PVE_REFRESH_OK" -eq 1 ]',
            postinst,
        )

    def test_lvm_autogrow_install_is_fail_closed_and_purge_is_scoped(self):
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        postrm = (ROOT / "DEBIAN/postrm").read_text(encoding="utf-8")
        self.assertIn("did not overwrite administrator-owned LVM settings", postinst)
        self.assertIn("BEGIN PVE-SHAREDLVMTHIN MANAGED AUTOGROW", postinst)
        self.assertNotIn("rm -f /etc/lvm/lvmlocal.conf", postrm)
        self.assertIn("BEGIN PVE-SHAREDLVMTHIN MANAGED AUTOGROW", postrm)
        self.assertIn("/usr/libexec/pve-sharedlvmthin/__pycache__", postrm)

    def test_shared_lvm_autoactivation_is_verified_not_best_effort(self):
        plugin = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        self.assertIn("_disable_and_verify_autoactivation", plugin)
        self.assertIn("lv_autoactivation", plugin)
        self.assertNotIn("could not disable autoactivation", plugin)
        self.assertIn("safe shared-storage autoactivation state is unconfirmed", plugin)

    def test_doctor_batches_per_vg_lvm_diagnostics(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn('LVS_REPORT="$(', doctor)
        self.assertIn("lv_autoactivation,pool_lv,lv_when_full", doctor)
        self.assertIn("no_space_timeout", doctor)
        self.assertNotIn(
            'lvs --readonly --binary --noheadings -o lv_autoactivation "$VG/$POOL"',
            doctor,
        )
        self.assertNotIn('--select "pool_lv=$POOL"', doctor)

    def test_recovery_check_is_read_only_and_fail_closed(self):
        checker = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check"
        ).read_text(encoding="utf-8")
        self.assertIn("SAFE_FOR_MUTATION", checker)
        self.assertIn("RECOVERY_REQUIRED", checker)
        self.assertIn("unscoped_dstate_tasks", checker)
        self.assertIn("one outstanding LVM/PVE probe maximum", checker)
        self.assertIn("--readonly suppresses that runtime query", checker)
        for command in ("pvcreate", "vgcreate", "lvcreate", "lvremove", "wipefs", "thin_repair"):
            self.assertNotIn(f'["/sbin/{command}"', checker)


if __name__ == "__main__":
    unittest.main()

