import hashlib
import json
import os
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "experiments/thick-generations/executor-admission-adapter.pl"
MODULE = ROOT / "usr/share/perl5/PVE/SharedLvmAdmission.pm"


class ExecutorAdmissionAdapterTests(unittest.TestCase):
    def invoke(self, action, payload, *, digest=None, extra_env=None):
        env = os.environ.copy()
        for name in ("PERL5OPT", "PERL5LIB", "PERLLIB", "PERL_USE_UNSAFE_INC"):
            env.pop(name, None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [
                "perl", "-T", str(ADAPTER), "--module", str(MODULE),
                "--module-sha256", digest or hashlib.sha256(MODULE.read_bytes()).hexdigest(),
                "--action", action,
            ],
            input=json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            text=True, capture_output=True, env=env,
        )

    @staticmethod
    def record():
        attempt = "1" * 32
        return {
            "schema": 1, "kind": "THICK_EXECUTOR_LAB",
            "enrollment_epoch": "e" * 64,
            "vg_uuid": "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF",
            "volume": "vm-900001-disk-0", "object_key": "c" * 24,
            "transaction": "d" * 32, "attempt": attempt,
            "node": "node-a", "boot_id": "11111111-2222-3333-4444-555555555555",
            "unit": f"slt-thick-lab-exec-{attempt}.service",
            "code_digest": "2" * 64, "policy_digest": "3" * 64,
            "state": "RESERVED",
        }

    def test_adapter_calls_exact_model_for_lab_reserve(self):
        result = self.invoke("reserve", {
            "requested": self.record(), "continuity_proven": 1,
            "attempt_fresh_proven": 1,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["action"], "RESERVE_ATOMIC")

    def test_adapter_rejects_boolean_reference_instead_of_truthiness(self):
        result = self.invoke("reserve", {
            "requested": self.record(), "continuity_proven": True,
            "attempt_fresh_proven": 1,
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reference-valued scalar", result.stderr)

    def test_adapter_rejects_wrong_module_digest(self):
        result = self.invoke("reserve", {}, digest="0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest mismatch", result.stderr)

    def test_adapter_rejects_perl_environment_injection(self):
        result = self.invoke("reserve", {}, extra_env={"PERL5OPT": "-Mstrict"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe Perl environment", result.stderr)

    def test_adapter_rejects_unknown_fields(self):
        result = self.invoke("reserve", {"future": 1})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown adapter input field", result.stderr)

    def test_adapter_rejects_production_domain(self):
        record = self.record()
        record["kind"] = "THICK_EXECUTOR"
        record["unit"] = "slt-thick-exec-" + record["attempt"] + ".service"
        result = self.invoke("reserve", {
            "requested": record, "continuity_proven": 1, "attempt_fresh_proven": 1,
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a lab executor record", result.stderr)

    def test_adapter_rejects_string_proof_and_schema(self):
        payload = {"requested": self.record(), "continuity_proven": "1",
                   "attempt_fresh_proven": 1}
        result = self.invoke("reserve", payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("JSON integer 0 or 1", result.stderr)
        payload["continuity_proven"] = 1
        payload["requested"]["schema"] = "1"
        result = self.invoke("reserve", payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema must be the JSON integer 1", result.stderr)

    def test_adapter_rejects_noncanonical_and_duplicate_json(self):
        env = os.environ.copy()
        for name in ("PERL5OPT", "PERL5LIB", "PERLLIB", "PERL_USE_UNSAFE_INC"):
            env.pop(name, None)
        command = ["perl", "-T", str(ADAPTER), "--module", str(MODULE),
                   "--module-sha256", hashlib.sha256(MODULE.read_bytes()).hexdigest(),
                   "--action", "reserve"]
        raw = '{"continuity_proven":0,"continuity_proven":1}\n'
        result = subprocess.run(command, input=raw, text=True, capture_output=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("canonical transport", result.stderr)


if __name__ == "__main__":
    unittest.main()
