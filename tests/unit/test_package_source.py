import ipaddress
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PackageSourceTests(unittest.TestCase):
    def test_publishable_sources_contain_no_lab_or_personal_identifiers(self):
        forbidden = (
            "192.168." + "50.",
            "10." + "240.",
            "stan" + "islav",
            "ba" + "ran",
            "dell" + "tech",
            "oke" + ".dev",
        )
        private_networks = tuple(
            ipaddress.ip_network(value)
            for value in (
                "10." + "0.0.0/8",
                "172." + "16.0.0/12",
                "192." + "168.0.0/16",
            )
        )
        ipv4 = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
        excluded_parts = {".git", "dist", "dist-lab", "outputs", "__pycache__"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or excluded_parts.intersection(path.parts):
                continue
            try:
                content = path.read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                continue
            for marker in forbidden:
                self.assertNotIn(marker, content, f"private marker in {path}")
            for candidate in ipv4.findall(content):
                try:
                    address = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                self.assertFalse(
                    any(address in network for network in private_networks),
                    f"private IPv4 address {candidate} in {path}",
                )

    def test_rc5_version(self):
        control = (ROOT / "DEBIAN/control").read_text(encoding="utf-8")
        self.assertRegex(
            control,
            r"(?m)^Version: 0\.9\.0~rc5(?:\.\d+)+(?:~tg\d+)?$",
        )

    def test_experimental_thick_build_has_distinct_package_version(self):
        control = (ROOT / "DEBIAN/control").read_text(encoding="utf-8")
        self.assertRegex(control, r"(?m)^Version: .*~tg\d+$")

    def test_combined_package_metadata_advertises_both_modes(self):
        control = (ROOT / "DEBIAN/control").read_text(encoding="utf-8")
        self.assertIn(
            "Description: Shared LVM Thin and Thick Generations storage for Proxmox VE",
            control,
        )
        self.assertIn("one LVM thin pool per VM", control)
        self.assertIn("fully allocated Thick Generations", control)

    def test_doctor_accepts_elastic_and_legacy_thresholds(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text()
        self.assertIn('pass "Thin autoextend threshold = 50% (elastic early-grow policy)"', doctor)
        self.assertIn('pass "Thin autoextend threshold = 80% (legacy fixed/proportional policy)"', doctor)

    def test_doctor_reports_two_node_and_forced_quorum_topologies(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("Two-node cluster is quorate (2/2) without qdevice", doctor)
        self.assertIn("forced single-node quorum without qdevice", doctor)
        self.assertIn("EXPECTED_VOTES", doctor)
        self.assertNotIn("pvecm expected", doctor)

    def test_doctor_requires_dmeventd_only_for_locally_active_pool(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text()
        self.assertIn("-o lv_name,segtype,lv_attr", doctor)
        self.assertIn('$3 ~ /^....a/', doctor)
        self.assertIn("no local per-VM thin pool currently requires monitoring", doctor)

    def test_doctor_skips_disabled_storage_in_all_operational_gates(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("storage_is_disabled", doctor)
        self.assertIn("storage is explicitly disabled; operational probes skipped", doctor)
        self.assertIn("PVE storage intentionally disabled", doctor)
        self.assertGreaterEqual(doctor.count('storage_is_disabled "$SID"'), 2)

    def test_doctor_skips_storage_outside_local_node_scope(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("storage_is_applicable_on_node", doctor)
        self.assertIn("storage is not assigned to this PVE node", doctor)
        self.assertGreaterEqual(
            doctor.count('storage_is_applicable_on_node "$SID"'), 2
        )

    def test_doctor_never_hides_inactive_pool_reservation(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("physical reservation=${POOL_GIB} GiB", doctor)
        self.assertIn("this reservation is unavailable to other VM pools", doctor)
        self.assertIn(
            "payload and reserved slack are unavailable while the pool is inactive",
            doctor,
        )

    def test_doctor_explains_same_vg_capacity_aliasing(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("_verify_same_vg_alias_configuration", doctor)
        self.assertIn("unsafe same-VG storage alias topology", doctor)
        self.assertIn("both aliases truthfully report the same physical VG capacity", doctor)
        self.assertIn("do not sum their PVE capacity values", doctor)

    def test_package_does_not_contain_private_keys(self):
        forbidden = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            if path.name.endswith(".key") or path.name in {"id_rsa", "id_ed25519"}:
                forbidden.append(path)
        self.assertEqual(forbidden, [])

    def test_build_writes_portable_checksum_manifest(self):
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        self.assertIn('(cd "$OUT" && sha256sum "$PACKAGE" >SHA256SUMS)', build)
        self.assertNotIn('sha256sum "$OUT/$PACKAGE" >"$OUT/SHA256SUMS"', build)

    def test_build_pins_cross_distribution_deb_compression(self):
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        self.assertIn('dpkg-deb -Zxz --root-owner-group --build', build)

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

    def test_installed_executable_sources_use_unix_line_endings(self):
        paths = list((ROOT / "usr/sbin").glob("*"))
        paths += list((ROOT / "usr/libexec/pve-sharedlvmthin").glob("*"))
        paths += list((ROOT / "DEBIAN").glob("*"))
        paths = [path for path in paths if path.is_file()]
        offenders = [str(path.relative_to(ROOT)) for path in paths if b"\r\n" in path.read_bytes()]
        self.assertEqual(offenders, [])

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

    def test_upgrade_check_is_packaged_and_exposed_by_cli(self):
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        checker = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check"

        self.assertTrue(checker.is_file())
        self.assertIn("sharedlvmthin-upgrade-check", build)
        self.assertIn("sharedlvmthin-upgrade-check", postinst)
        self.assertIn("upgrade-check)", cli)
        self.assertIn("sharedlvmthin upgrade-check", cli)

    def test_web_configurator_unit_detection_is_pipefail_safe(self):
        configurator = (
            ROOT / "usr/sbin/sharedlvmthin-web-configure"
        ).read_text(encoding="utf-8")
        self.assertIn('systemctl cat "$SERVICE"', configurator)
        self.assertNotIn("systemctl list-unit-files |", configurator)

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

    def test_doctor_classifies_thick_mode_and_interrupted_anchors(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn('inside && $1 == "slt-allocation-mode"', doctor)
        self.assertIn(
            "Thick Generations mode uses independent thick LVs; "
            "thin-pool headroom and autogrow policy do not apply",
            doctor,
        )
        self.assertIn("sharedlvmthin-health-json", doctor)
        self.assertIn("ANCHOR_STATE ANCHOR_WORKER ANCHOR_REASON", doctor)
        self.assertIn("new mutations must remain blocked", doctor)

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

    def test_thick_resume_derives_exact_persisted_transaction(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("thick-resume <storage-id> <volume>", cli)
        self.assertIn('--resume "$2" "$3"', cli)
        self.assertIn("_thick_read_anchor", worker)
        self.assertIn("has no resumable materialization transition", worker)
        self.assertIn("anchor-scoped materialization does not match transaction", worker)
        self.assertIn("a different VG intent targets this materialization anchor", worker)
        self.assertIn("$operation ne 'SNAPSHOT' && $operation ne 'ROLLBACK'", worker)
        self.assertIn("$operation eq 'ROLLBACK' ? 'DM_PIVOT' : 'DM_CUTOVER'", worker)
        self.assertNotIn("lvremove", worker)
        self.assertNotIn("pvcreate", worker)

    def test_snapshot_delete_recovery_is_an_explicit_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "thick-recover-delete <storage-id> <volume>",
            cli,
        )
        self.assertIn('--recover-delete "$2" "$3"', cli)
        self.assertIn("_thick_recover_snapshot_delete", worker)
        self.assertIn("SNAPSHOT_DELETE_RECOVERY_START", worker)

    def test_snapshot_delete_recovery_explains_stale_cluster_lock(self):
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("no recovery mutation was started", worker)
        self.assertIn("Do not remove or bypass the lock", worker)
        self.assertIn("Proxmox's 120-second", worker)
        self.assertIn("stale-lock window and re-run", worker)

    def test_empty_allocation_recovery_is_an_explicit_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("thick-recover-empty-alloc <storage-id> <volume>", cli)
        self.assertIn('--recover-empty-alloc "$2" "$3"', cli)
        self.assertIn("_thick_recover_empty_allocation", worker)
        self.assertIn("EMPTY_ALLOCATION_RECOVERY_START", worker)

    def test_partial_allocation_recovery_is_an_explicit_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("thick-recover-partial-alloc <storage-id> <volume>", cli)
        self.assertIn('--recover-partial-alloc "$2" "$3"', cli)
        self.assertIn("_thick_recover_partial_allocation", worker)
        self.assertIn("PARTIAL_ALLOCATION_RECOVERY_START", worker)

    def test_orphan_allocation_recovery_is_an_explicit_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("thick-recover-orphan-alloc <storage-id> <volume>", cli)
        self.assertIn("thick-recover-orphan-tree <storage-id> <volume>", cli)
        self.assertIn("thin-recover-orphan <storage-id> <volume>", cli)
        self.assertIn('--recover-orphan-alloc "$2" "$3"', cli)
        self.assertIn('--recover-orphan-tree "$2" "$3"', cli)
        self.assertIn("ORPHAN_ALLOCATION_RECOVERY_START", worker)
        self.assertIn("ORPHAN_TREE_RECOVERY_START", worker)
        self.assertIn("_thick_free_image($storeid, $scfg, $volname, 0, 1)", worker)
        self.assertIn("_thick_recover_orphan_tree($scfg, $storeid, $volname)", worker)
        self.assertIn("_thin_recover_orphan($scfg, $storeid, $volname)", worker)

    def test_snapshot_delete_crash_hooks_remain_production_inert(self):
        plugin = (
            ROOT
            / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        for point in range(5):
            self.assertEqual(
                plugin.count(f"_thick_fault_point('D{point}'"),
                1,
                f"D{point} must identify exactly one snapshot-delete boundary",
            )
        self.assertIn("sub _thick_fault_point {\n    return;\n}", plugin)

    def test_all_thin_metadata_mutations_enter_the_canonical_lock_helper(self):
        plugin = (
            ROOT
            / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        public_mutations = (
            "alloc_image",
            "free_image",
            "volume_resize",
            "volume_snapshot",
            "volume_snapshot_delete",
            "volume_snapshot_rollback",
        )
        for name in public_mutations:
            match = re.search(
                rf"sub {name} \{{(?P<body>.*?)(?=\nsub )",
                plugin,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"missing public mutation {name}")
            self.assertIn(
                "_with_mutation_lock",
                match.group("body"),
                f"{name} must serialize with same-VG thin and thick aliases",
            )

    def test_thick_lvm_commands_are_scoped_to_the_pinned_mapper(self):
        plugin = (
            ROOT
            / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        thick_lifecycle = (
            "_thick_alloc_image",
            "_thick_activate_volume",
            "_thick_deactivate_volume",
            "_thick_free_image",
            "_thick_volume_resize",
            "_thick_volume_snapshot",
            "_thick_volume_snapshot_delete",
            "_thick_recover_snapshot_delete",
        )
        checked = 0
        for name in thick_lifecycle:
            match = re.search(
                rf"sub {name} \{{(?P<body>.*?)(?=\nsub )",
                plugin,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"missing Thick Generations lifecycle {name}")
            for command in re.findall(r"\[(.*?)\]", match.group("body"), re.DOTALL):
                if not re.search(r"'/sbin/(?:lv|vg)(?:create|remove|extend|change|convert)'", command):
                    continue
                checked += 1
                self.assertIn(
                    "'--devices', $device",
                    command,
                    f"{name} contains a global LVM command: {command.strip()}",
                )

        self.assertGreater(checked, 0)

        scoped_inventory_paths = thick_lifecycle + (
            "_thick_read_anchor",
            "_thick_find_snapshot",
            "_thick_list_images",
            "_thick_verify_allocation_state",
            "_thick_resume_transition",
        )
        for name in scoped_inventory_paths:
            match = re.search(
                rf"sub {name} \{{(?P<body>.*?)(?=\nsub )",
                plugin,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"missing Thick Generations inventory path {name}")
            self.assertNotIn(
                "PVE::Storage::LVMPlugin::lvm_list_volumes",
                match.group("body"),
                f"{name} can still issue a global LVM inventory",
            )

        for name in ("_thick_activate_volume", "_thick_deactivate_volume"):
            match = re.search(
                rf"sub {name} \{{(?P<body>.*?)(?=\nsub )",
                plugin,
                re.DOTALL,
            )
            body = match.group("body")
            self.assertLess(
                body.index("_verify_storage_identity"),
                body.index("_thick_list_volumes_scoped"),
                f"{name} must prove storage identity before its first LVM inventory",
            )

    def test_fresh_lv_creation_is_noninteractive_about_stale_signatures(self):
        source = (ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm").read_text(
            encoding="utf-8"
        )
        commands = re.findall(
            r"\[(?:\s*)'/sbin/lvcreate'(?P<body>.*?)\]",
            source,
            flags=re.S,
        )
        fresh = [
            body for body in commands
            if ("'-L'" in body and "'-s'" not in body)
            or ("'-V'" in body and "'--thinpool'" in body)
        ]
        self.assertGreaterEqual(len(fresh), 6)
        for body in fresh:
            self.assertIn("'--yes'", body)
            self.assertIn("'--wipesignatures', 'y'", body)
            if "'--setactivationskip', 'y'" in body:
                self.assertIn("'--ignoreactivationskip'", body)

        snapshots = [body for body in commands if "'-s'" in body]
        self.assertGreaterEqual(len(snapshots), 2)
        for body in snapshots:
            self.assertNotIn("'--wipesignatures'", body)


if __name__ == "__main__":
    unittest.main()

