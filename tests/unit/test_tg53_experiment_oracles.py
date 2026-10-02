import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments" / "thick-generations"
SPEC = importlib.util.spec_from_file_location(
    "tg53_move_receipt_oracle", EXP / "tg53-move-receipt-oracle.py"
)
MOVE_ORACLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOVE_ORACLE)


class Tg53ExperimentOracleTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Bash helper execution requires POSIX")
    def test_exact_task_waiter_requires_ok_unless_explicitly_observing_refusal(self):
        helper = EXP / "tg53-async-dispatch.sh"
        with tempfile.TemporaryDirectory() as directory:
            private_bin = Path(directory)
            pvesh = private_bin / "pvesh"
            pvesh.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  */status*) printf '%s\\n' \"$TASK_STATUS\" ;;\n"
                "  *) printf '%s\\n' \"$TASK_ROWS\" ;;\n"
                "esac\n",
                encoding="ascii",
            )
            pvesh.chmod(0o755)
            base = {
                "upid": "UPID:pve1:0001:0002:0003:qmsnapshot:100:root@pam:",
                "type": "qmsnapshot",
                "status": "stopped",
            }

            def invoke(exitstatus, policy=None):
                row = json.dumps([{**base, "status": exitstatus}])
                status = json.dumps({
                    "upid": base["upid"], "status": "stopped",
                    "exitstatus": exitstatus,
                })
                command = (
                    f"source {str(helper)!r}; "
                    "tg53_wait_exact_task pve1 100 qmsnapshot 0 9999999999"
                )
                if policy:
                    command += f" {policy}"
                return subprocess.run(
                    ["bash", "-c", command], text=True, capture_output=True,
                    env={**os.environ, "PATH": f"{private_bin}:{os.environ['PATH']}",
                         "TASK_ROWS": row, "TASK_STATUS": status}, check=False,
                )

            accepted = invoke("OK")
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            refused = invoke("storage migration failed")
            self.assertEqual(refused.returncode, 76, refused.stdout)
            observed = invoke("storage migration failed", "any-terminal")
            self.assertEqual(observed.returncode, 0, observed.stderr)
            self.assertIn("TASK_PINNED_UPID=", observed.stdout)

            # A task may already be terminal by the time a sequential wave
            # reaches it.  The deadline must not win before the final exact
            # status read.
            expired_status = json.dumps({
                "upid": base["upid"], "status": "stopped",
                "exitstatus": "storage migration failed",
            })
            expired = subprocess.run(
                ["bash", "-c", (
                    f"source {str(helper)!r}; "
                    "tg53_wait_exact_task pve1 100 qmsnapshot 0 0 any-terminal; "
                    "printf 'GLOBAL=%s|%s|%s\\n' \"$TG53_LAST_TASK_UPID\" "
                    "\"$TG53_LAST_TASK_STATE\" \"$TG53_LAST_TASK_EXITSTATUS\""
                )], text=True, capture_output=True,
                env={**os.environ, "PATH": f"{private_bin}:{os.environ['PATH']}",
                     "TASK_ROWS": json.dumps([base]), "TASK_STATUS": expired_status},
                check=False,
            )
            self.assertEqual(expired.returncode, 0, expired.stdout + expired.stderr)
            self.assertIn(f"GLOBAL={base['upid']}|stopped|storage migration failed", expired.stdout)

            running_status = json.dumps({
                "upid": base["upid"], "status": "running",
            })
            resume = subprocess.run(
                ["bash", "-c", (
                    f"source {str(helper)!r}; "
                    f"tg53_observe_exact_upid pve1 {base['upid']!r} 0 any-terminal"
                )], text=True, capture_output=True,
                env={**os.environ, "PATH": f"{private_bin}:{os.environ['PATH']}",
                     "TASK_ROWS": json.dumps([base]), "TASK_STATUS": running_status},
                check=False,
            )
            self.assertEqual(resume.returncode, 75, resume.stdout + resume.stderr)
            self.assertIn("RESULT=OBSERVATION_DEADLINE_RESUMABLE_UNKNOWN", resume.stdout)
            self.assertIn("RESUME_OBSERVATION=YES", resume.stdout)
            self.assertIn("MUTATION_REDISPATCH_AUTHORIZED=NO", resume.stdout)

            unknown = json.dumps({
                "upid": base["upid"], "status": "mystery", "exitstatus": "OK",
            })
            command = (
                f"source {str(helper)!r}; "
                "tg53_wait_exact_task pve1 100 qmsnapshot 0 9999999999"
            )
            result = subprocess.run(
                ["bash", "-c", command], text=True, capture_output=True,
                env={**os.environ, "PATH": f"{private_bin}:{os.environ['PATH']}",
                     "TASK_ROWS": json.dumps([base]), "TASK_STATUS": unknown},
                check=False,
            )
            self.assertEqual(result.returncode, 75, result.stdout)
            self.assertIn("RESULT=EXACT_TASK_TERMINAL_UNKNOWN", result.stdout)

            for invalid_exitstatus in (None, [], {}, 0, False):
                invalid = json.dumps({
                    "upid": base["upid"], "status": "stopped",
                    "exitstatus": invalid_exitstatus,
                })
                result = subprocess.run(
                    ["bash", "-c", (
                        f"source {str(helper)!r}; "
                        f"tg53_observe_exact_upid pve1 {base['upid']!r} "
                        "9999999999 any-terminal"
                    )], text=True, capture_output=True,
                    env={**os.environ,
                         "PATH": f"{private_bin}:{os.environ['PATH']}",
                         "TASK_STATUS": invalid}, check=False,
                )
                self.assertEqual(result.returncode, 75, result.stdout)
                self.assertIn("RESULT=EXACT_TASK_TERMINAL_UNKNOWN", result.stdout)

            missing_exitstatus = json.dumps({
                "upid": base["upid"], "status": "stopped",
            })
            wrong_identity = json.dumps({
                "upid": "UPID:pve2:0001:0002:0003:qmsnapshot:100:root@pam:",
                "status": "stopped", "exitstatus": "OK",
            })
            for status, expected in (
                (missing_exitstatus, "RESULT=EXACT_TASK_TERMINAL_UNKNOWN"),
                (wrong_identity, "RESULT=EXACT_TASK_IDENTITY_UNKNOWN"),
            ):
                result = subprocess.run(
                    ["bash", "-c", (
                        f"source {str(helper)!r}; "
                        f"tg53_observe_exact_upid pve1 {base['upid']!r} "
                        "9999999999 any-terminal"
                    )], text=True, capture_output=True,
                    env={**os.environ,
                         "PATH": f"{private_bin}:{os.environ['PATH']}",
                         "TASK_STATUS": status}, check=False,
                )
                self.assertEqual(result.returncode, 75, result.stdout)
                self.assertIn(expected, result.stdout)

    @unittest.skipUnless(os.name == "posix", "Bash helper execution requires POSIX")
    def test_exact_observer_bounds_stalled_probe_and_resumes_same_upid(self):
        helper = EXP / "tg53-async-dispatch.sh"
        upid = "UPID:pve1:0001:0002:0003:qmsnapshot:100:root@pam:"
        with tempfile.TemporaryDirectory() as directory:
            private_bin = Path(directory)
            pvesh = private_bin / "pvesh"
            pvesh.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  */status*)\n"
                "    if [ \"${STALL_PROBE:-0}\" = 1 ]; then\n"
                "      trap '' TERM; sleep 60\n"
                "    fi\n"
                "    printf '%s\\n' \"$TASK_STATUS\" ;;\n"
                "  *) echo 'task-list must not be read by exact resume' >&2; exit 97 ;;\n"
                "esac\n",
                encoding="ascii",
            )
            pvesh.chmod(0o755)
            command = (
                f"source {str(helper)!r}; "
                f"tg53_observe_exact_upid pve1 {upid!r} 9999999999"
            )
            stalled = subprocess.run(
                ["timeout", "15", "bash", "-c", command], text=True,
                capture_output=True, env={
                    **os.environ,
                    "PATH": f"{private_bin}:{os.environ['PATH']}",
                    "STALL_PROBE": "1", "TG53_OBSERVER_PROBE_SEC": "5",
                    "TASK_STATUS": "{}",
                }, check=False,
            )
            self.assertEqual(stalled.returncode, 75, stalled.stdout + stalled.stderr)
            self.assertIn("RESULT=EXACT_TASK_PROBE_UNKNOWN", stalled.stdout)
            self.assertIn("RESUME_OBSERVATION=YES", stalled.stdout)
            self.assertIn("MUTATION_REDISPATCH_AUTHORIZED=NO", stalled.stdout)

            completed = json.dumps({
                "upid": upid, "status": "stopped", "exitstatus": "OK",
            })
            resumed = subprocess.run(
                ["bash", "-c", command], text=True, capture_output=True,
                env={**os.environ,
                     "PATH": f"{private_bin}:{os.environ['PATH']}",
                     "TASK_STATUS": completed}, check=False,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertIn(f"TASK_UPID={upid}", resumed.stdout)
            self.assertIn("TASK_STATE=stopped", resumed.stdout)
            self.assertIn("TASK_EXITSTATUS=OK", resumed.stdout)

    def test_fiveway_does_not_call_mixed_terminal_receipts_pass(self):
        source = (EXP / "tg53-fiveway-mixed-wave.sh").read_text(encoding="utf-8")
        self.assertIn("TG53_FIVEWAY_RECEIPTS=COLLECTED", source)
        self.assertNotIn("TG53_FIVEWAY_RECEIPTS=PASS", source)

    def test_offline_canary_reader_refuses_running_vm(self):
        source = (EXP / "tg53-verify-canary-vm.sh").read_text(encoding="utf-8")
        self.assertIn('status="$(qm status "$VMID")"', source)
        self.assertIn("CANARY_VERIFY=REFUSED_VM_NOT_STOPPED", source)

    def test_volume_witness_samples_three_regions_and_proves_deactivation(self):
        source = (EXP / "tg53-hash-volume.sh").read_text(encoding="utf-8")
        self.assertIn("mktemp /run/tg53-hash-volume.XXXXXX", source)
        self.assertIn("VOLUME_DEACTIVATION=UNKNOWN", source)
        self.assertNotIn(">/dev/null 2>&1 || true", source)
        self.assertIn("SHA256_BEGIN=", source)
        self.assertIn("SHA256_MIDDLE=", source)
        self.assertIn("SHA256_END=", source)
        self.assertIn("VOLUME_READ_WITNESS=PASS", source)
        self.assertIn("iflag=skip_bytes,count_bytes", source)

    def move_fixture(self, *, expected="REFUSED", result="admission refused: busy",
                     after_disk="src:vm-100-disk-0", source_after=None,
                     target_after=None, refusal_regex="admission refused"):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        source_before = ["src:vm-100-disk-0"]
        target_before = []
        docs = {
            "config_before": {"scsi0": "src:vm-100-disk-0,size=1G"},
            "config_after": {"scsi0": after_disk + ",size=1G"},
            "source_before": [{"volid": item} for item in source_before],
            "source_after": [{"volid": item} for item in (
                source_before if source_after is None else source_after)],
            "target_before": [{"volid": item} for item in target_before],
            "target_after": [{"volid": item} for item in (
                target_before if target_after is None else target_after)],
            "task_status": {
                "upid": "UPID:pve1:0001:0002:0003:qmmove:100:root@pam:",
                "status": "stopped", "exitstatus": result,
            },
        }
        paths = {}
        for name, document in docs.items():
            path = root / f"{name}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            paths[name] = str(path)
        args = SimpleNamespace(
            expected=expected, refusal_regex=refusal_regex, disk="scsi0",
            source_volid="src:vm-100-disk-0", target_storage="dst", **paths,
        )
        return temporary, args

    def test_refusal_requires_exact_reason_and_unchanged_inventories(self):
        temporary, args = self.move_fixture()
        with temporary:
            _upid, _status, verdict = MOVE_ORACLE.evaluate(args)
        self.assertEqual(verdict, "REFUSED_BEFORE_EFFECT")

    def test_partial_target_error_is_not_a_refusal(self):
        temporary, args = self.move_fixture(target_after=["dst:vm-100-disk-0"])
        with temporary, self.assertRaisesRegex(ValueError, "target inventory changed"):
            MOVE_ORACLE.evaluate(args)

    def test_unexpected_worker_error_is_not_a_refusal(self):
        temporary, args = self.move_fixture(result="copy failed: I/O error")
        with temporary, self.assertRaisesRegex(ValueError, "not the expected admission"):
            MOVE_ORACLE.evaluate(args)

    def test_success_requires_config_inventory_and_source_settlement(self):
        temporary, args = self.move_fixture(
            expected="OK", result="OK", after_disk="dst:vm-100-disk-0",
            source_after=[], target_after=["dst:vm-100-disk-0"], refusal_regex="",
        )
        with temporary:
            _upid, _status, verdict = MOVE_ORACLE.evaluate(args)
        self.assertEqual(verdict, "OK_WITH_SETTLED_INVENTORY")

    def test_runner_bounds_client_and_tracks_original_receipt(self):
        source = (EXP / "tg53-one-move-with-receipt.sh").read_text(encoding="utf-8")
        self.assertIn("TG53_DISPATCH_DEADLINE_SEC", source)
        self.assertIn("TG53_RECEIPT_DEADLINE_SEC", source)
        self.assertIn("TIMED_OUT_UNKNOWN", source)
        self.assertIn('/tasks/$upid/status', source)
        self.assertNotIn("expected refusal but task succeeded", source)

    def test_snapshot_runners_detach_dispatch_from_observer(self):
        helper = (EXP / "tg53-async-dispatch.sh").read_text(encoding="utf-8")
        self.assertIn("systemd-run --quiet --collect", helper)
        self.assertIn("RESULT=OBSERVATION_DEADLINE_RESUMABLE_UNKNOWN", helper)
        self.assertIn("tg53_observe_exact_upid()", helper)
        self.assertIn("TASK_PINNED_UPID=", helper)
        self.assertIn("MUTATION_REDISPATCH_AUTHORIZED=NO", helper)
        self.assertIn('state == "running"', helper)
        self.assertIn('exitstatus != "OK"', helper)
        self.assertIn("require-ok|any-terminal", helper)
        self.assertIn("return 76", helper)
        self.assertIn("RESULT=AMBIGUOUS_TASK_UNKNOWN", helper)
        self.assertIn('/tasks/$upid/status', helper)
        self.assertIn("RESULT=EXACT_TASK_TERMINAL_UNKNOWN", helper)
        self.assertNotIn("--property=RuntimeMaxSec", helper)
        for name in ("tg53-cross-node-snapshot-worker.sh",
                     "tg53-cross-node-delete-worker.sh",
                     "tg53-mixed-three-disk-cycle.sh",
                     "tg53-nic-ram-matrix.sh",
                     "tg53-tenway-snapshot-batch.sh",
                     "tg53-concurrent-snapshot-move.sh",
                     "tg53-fiveway-mixed-wave.sh"):
            source = (EXP / name).read_text(encoding="utf-8")
            self.assertIn("tg53_dispatch_detached", source)
            self.assertIn("tg53_wait_exact_task", source)
            self.assertNotIn("timeout --foreground", source)

        materialize = (EXP / "tg53-materialize-volume.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("timeout --foreground", materialize)
        self.assertNotIn("trap deactivate", materialize)
        nic_ram = (EXP / "tg53-nic-ram-matrix.sh").read_text(encoding="utf-8")
        self.assertIn("CONFIRM_DISPOSABLE_VM", nic_ram)
        self.assertNotIn("VMID=${VMID:-", nic_ram)
        self.assertIn("PREFLIGHT_REFUSAL_PASS", nic_ram)

        concurrent = (EXP / "tg53-concurrent-snapshot-move.sh").read_text(
            encoding="utf-8"
        )
        fiveway = (EXP / "tg53-fiveway-mixed-wave.sh").read_text(encoding="utf-8")
        self.assertIn("qmmove \"$started\" \"$deadline\" any-terminal", concurrent)
        self.assertEqual(fiveway.count("qmmove \"$started\" \"$deadline\" any-terminal"), 3)

    def test_tenway_batch_preflights_every_vm_before_dispatch(self):
        source = (EXP / "tg53-tenway-snapshot-batch.sh").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            source.index('snapshot-preflight "$id"'),
            source.index("run_batch CREATE"),
        )
        self.assertIn('task_type=qmdelsnapshot', source)
        self.assertIn('VMIDS_CSV="${VMIDS_CSV:-', source)
        self.assertIn('[[ "${#VMIDS[@]}" -eq 10 ]]', source)
        self.assertIn('OWNER[$id]="$owner"', source)
        self.assertIn('tg53_wait_exact_task "${OWNER[$id]}"', source)
        self.assertNotIn('NODE="$(hostname)"', source)
        self.assertIn('OBSERVE_DEADLINE_SEC="${OBSERVE_DEADLINE_SEC:-7200}"', source)
        self.assertIn('"$((started + OBSERVE_DEADLINE_SEC))" any-terminal', source)
        self.assertIn("SAFE_REFUSAL='timed out waiting for an exact foreign Thick transition; no storage mutation was issued'", source)
        self.assertIn('status="$TG53_LAST_TASK_EXITSTATUS"', source)
        self.assertIn('${op}_VM_${id}_UPID=$TG53_LAST_TASK_UPID', source)
        self.assertNotIn('--source all --since "$started" --output-format json', source)
        self.assertIn('_VM_${id}=SAFE_REFUSAL', source)
        self.assertIn('refused VM $id retains transient config state', source)
        self.assertIn('tg53-vg-wave-plan.py', source)
        self.assertIn('VM_${id}_WAVE=', source)
        self.assertIn('[ "${WAVE[$id]}" = "$wave" ] || continue', source)
        self.assertIn('MIN_CREATE_SUCCESS="${MIN_CREATE_SUCCESS:-3}"', source)
        self.assertIn('CREATE_SUCCESS_COUNT=$create_success', source)
        self.assertIn('CREATE_SAFE_REFUSAL_COUNT=$create_refusal', source)
        self.assertIn('RESULT=INSUFFICIENT_SUCCESSFUL_SNAPSHOTS', source)
        self.assertIn('create_success + create_refusal != 10', source)
        self.assertIn('REQUIRED_SUCCESS_MODES="${REQUIRED_SUCCESS_MODES:-thin,eager,lazy}"', source)
        self.assertIn('VM_${id}_MODES=$modes', source)
        self.assertIn('CREATE_${mode^^}_SUCCESS_COUNT=', source)
        self.assertIn('RESULT=MISSING_REQUIRED_MODE_SUCCESS mode=$mode', source)
        self.assertGreaterEqual(source.count('snapshots="$(pvesh get'), 2)
        self.assertIn('cfg="$(pvesh get', source)
        self.assertGreaterEqual(source.count('inventory_state="$(python3'), 2)
        self.assertIn('config_state="$(python3', source)
        self.assertIn('PRESENT) echo "snapshot $SNAP remains', source)
        self.assertIn('TRANSIENT) echo "VM $id retains', source)
        self.assertNotIn('if pvesh get "/nodes/$owner/qemu/$id/snapshot"', source)
        self.assertNotIn('if python3 -c', source)
        self.assertIn('definition=stores.get(storage,{})', source)
        self.assertIn('raw=definition.get("slt-allocation-mode")', source)
        self.assertIn('raw in (None,"thin")', source)
        self.assertIn('if [[ "$owner" = "$(hostname)" ]]', source)

    def test_destructive_storage_move_runners_preflight_and_detach(self):
        for name in (
            "rc548-six-direction-cycle.sh",
            "evacuate-mixed-vg-storage.sh",
            "tg53-concurrent-snapshot-move.sh",
            "tg53-fiveway-mixed-wave.sh",
        ):
            source = (EXP / name).read_text(encoding="utf-8")
            self.assertIn("storage-move-preflight", source, name)
            self.assertIn("tg53_dispatch_detached", source, name)
            self.assertIn("tg53_wait_exact_task", source, name)
            self.assertNotIn('qm move_disk', source, name)

    def test_four_disk_composition_has_content_and_resize_oracles(self):
        source = (EXP / "tg53-four-disk-composition.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("DISKS=(scsi0 scsi1 scsi2 scsi3)", source)
        self.assertIn("PATTERN_A=(16 32 48 64)", source)
        self.assertIn("PATTERN_B=(80 96 112 128)", source)
        self.assertIn("PATTERN_C=(144 160 176 192)", source)
        self.assertIn("SAMPLE_OFFSETS=(8388608 536870912 1048576000)", source)
        self.assertIn("GROWN_TAIL_BYTES=67108864", source)
        self.assertIn("GROWN_FULL_TAIL_ZERO_SHA256=", source)
        self.assertIn("GROWN_TAIL_MARKER_SHA256=", source)
        self.assertIn("RESTORE_TAIL_MARKER_SHA256=", source)
        self.assertIn("SOURCE_AFTER_RESTORE=UNCHANGED", source)
        self.assertIn("snapshot inventory did not settle", source)
        self.assertIn("tg53_dispatch_detached", source)
        self.assertIn("tg53_wait_exact_task", source)
        self.assertNotIn("timeout qm ", source)
        self.assertNotIn("timeout qmrestore", source)
        self.assertNotIn("timeout vzdump", source)


if __name__ == "__main__":
    unittest.main()
