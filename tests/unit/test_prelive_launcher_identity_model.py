"""Tests for separated forked-Python launcher identity domains."""

import copy
import stat
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/thick-generations"))

import prelive_launcher_identity_model as MODEL


BOOT = "12345678-1234-1234-1234-123456789abc"
OWNER = {"pid": 100, "starttime": 200, "boot_id": BOOT}


def obj(path, pin, digest, inode):
    return {"path_hint": path, "dev": 20, "inode": inode, "size": 4096,
            "mode": stat.S_IFREG | 0o755, "uid": 0,
            "content_sha256": digest, "pin_kind": "MODEL_OPAQUE_FD_BINDING",
            "pin_id": pin}


def descriptor():
    return {
        "schema": 1, "kind": "FORKED_PYTHON_LAUNCHER_V1",
        "run_binding": {"request_id": "1" * 32, "boot_id": BOOT,
                        "supervisor": copy.deepcopy(OWNER),
                        "unarmed_deadline_ns": 1000,
                        "fd_graph_digest": "2" * 64},
        "interpreter": obj("/usr/bin/python3", "3" * 32, "4" * 64, 30),
        "launcher": {
            "entrypoint": "prelive_launcher:main",
            "source_manifest_sha256": "5" * 64,
            "loader_policy": "VERIFIED_BYTES_COMPILED_BEFORE_FORK",
            "loaded_code_provenance": {
                "kind": "MODEL_VERIFIED_BYTES_COMPILED",
                "manifest_sha256": "5" * 64,
                "frozen_before_fork": True}},
        "inherited": {"proc_cmdline_sha256": "6" * 64,
                      "launcher_environment_policy_sha256": "7" * 64},
        "payload": {"object": obj("/usr/bin/true", "8" * 32, "9" * 64, 31),
                    "argv": ["/usr/bin/true"],
                    "environment": {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}}}


def observation(desc=None):
    desc = descriptor() if desc is None else desc
    return {"child": {"pid": 300, "starttime": 400, "boot_id": BOOT,
                      "fork_token": "a" * 32, "pidfd": 50},
            "interpreter": copy.deepcopy(desc["interpreter"]),
            "payload": copy.deepcopy(desc["payload"]["object"]),
            "proc_cmdline_sha256": desc["inherited"]["proc_cmdline_sha256"],
            "launcher_environment_policy_sha256":
                desc["inherited"]["launcher_environment_policy_sha256"],
            "payload_argv_sha256": MODEL.digest(desc["payload"]["argv"]),
            "payload_environment_sha256": MODEL.digest(
                desc["payload"]["environment"]),
            "fd_graph_digest": desc["run_binding"]["fd_graph_digest"],
            "loaded_code_manifest_sha256":
                desc["launcher"]["source_manifest_sha256"],
            "status_state": "NO_EXEC_ERROR_REPORTED"}


class LauncherIdentityModelTests(unittest.TestCase):
    def test_separated_binding_and_preexec_never_claim_exec(self):
        desc = descriptor(); handle = MODEL.enroll_model(desc); obs = observation(desc)
        bound = MODEL.bind_unarmed_model(handle, obs, copy.deepcopy(obs))
        self.assertEqual(bound["classification"],
                         "MODEL_FORKED_LAUNCHER_BINDING_CONSISTENT")
        capability, result = MODEL.validate_preexec_model(handle, obs, 999)
        self.assertEqual(result["classification"],
                         "MODEL_PREEXEC_BINDING_CONSISTENT")
        self.assertFalse(result["exec_proven"])
        self.assertFalse(result["runtime_authorized"])
        self.assertIs(capability.handle, handle)

    def test_interpreter_source_cmdline_and_payload_domains_cannot_alias(self):
        attacks = []
        def interpreter_as_source(value):
            value["launcher"]["source_manifest_sha256"] = (
                value["interpreter"]["content_sha256"])
        attacks.append(interpreter_as_source)
        def source_as_cmdline(value):
            value["inherited"]["proc_cmdline_sha256"] = (
                value["launcher"]["source_manifest_sha256"])
        attacks.append(source_as_cmdline)
        def payload_pin_as_interpreter(value):
            value["payload"]["object"]["pin_id"] = value["interpreter"]["pin_id"]
        attacks.append(payload_pin_as_interpreter)
        for attack in attacks:
            desc = descriptor(); attack(desc)
            if attack is interpreter_as_source:
                desc["launcher"]["loaded_code_provenance"]["manifest_sha256"] = "5" * 64
            with self.assertRaises(MODEL.Refusal):
                MODEL.enroll_model(desc)

    def test_loaded_code_provenance_is_mandatory_and_exact(self):
        for mutate in (lambda d: d["launcher"].pop("loaded_code_provenance"),
                       lambda d: d["launcher"]["loaded_code_provenance"].update(
                           {"manifest_sha256": "f" * 64}),
                       lambda d: d["launcher"]["loaded_code_provenance"].update(
                           {"frozen_before_fork": 1})):
            desc = descriptor(); mutate(desc)
            with self.assertRaises(MODEL.Refusal): MODEL.enroll_model(desc)

    def test_two_unarmed_observations_must_match_exactly(self):
        fields = ("pid", "starttime", "boot_id", "pidfd")
        for field in fields:
            desc = descriptor(); first = observation(desc); second = copy.deepcopy(first)
            second["child"][field] = (float(second["child"][field])
                                      if field != "boot_id" else
                                      "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            handle = MODEL.enroll_model(desc)
            with self.assertRaises(MODEL.Refusal):
                MODEL.bind_unarmed_model(handle, first, second)
            self.assertEqual(handle.state, "UNKNOWN")

    def test_interpreter_payload_cmdline_graph_and_status_drift_refuse(self):
        for attack in ("interpreter", "interpreter_content", "payload",
                       "cmdline", "launcher_env", "payload_argv", "payload_env",
                       "graph", "loaded", "status"):
            desc = descriptor(); first = observation(desc); second = copy.deepcopy(first)
            if attack == "interpreter": second["interpreter"]["inode"] += 1
            elif attack == "interpreter_content":
                second["interpreter"]["content_sha256"] = "b" * 64
            elif attack == "payload": second["payload"]["content_sha256"] = "b" * 64
            elif attack == "cmdline": second["proc_cmdline_sha256"] = "b" * 64
            elif attack == "launcher_env":
                second["launcher_environment_policy_sha256"] = "b" * 64
            elif attack == "payload_argv":
                second["payload_argv_sha256"] = "b" * 64
            elif attack == "payload_env":
                second["payload_environment_sha256"] = "b" * 64
            elif attack == "graph": second["fd_graph_digest"] = "b" * 64
            elif attack == "loaded": second["loaded_code_manifest_sha256"] = "b" * 64
            else: second["status_state"] = "EOF"
            handle = MODEL.enroll_model(desc)
            with self.assertRaises(MODEL.Refusal):
                MODEL.bind_unarmed_model(handle, first, second)

    def test_descriptor_copy_mutation_and_replay_fail_closed(self):
        desc = descriptor(); obs = observation(desc); handle = MODEL.enroll_model(desc)
        MODEL.bind_unarmed_model(handle, obs, copy.deepcopy(obs))
        handle.descriptor["payload"]["object"]["inode"] += 1
        with self.assertRaises(MODEL.Refusal):
            MODEL.validate_preexec_model(handle, obs, 900)
        self.assertEqual(handle.state, "UNKNOWN")
        with self.assertRaises(MODEL.Refusal):
            MODEL.validate_preexec_model(handle, obs, 901)

    def test_deadline_is_strict_and_one_shot(self):
        desc = descriptor(); obs = observation(desc); handle = MODEL.enroll_model(desc)
        MODEL.bind_unarmed_model(handle, obs, copy.deepcopy(obs))
        with self.assertRaises(MODEL.Refusal):
            MODEL.validate_preexec_model(handle, obs, 1000)
        self.assertEqual(handle.state, "UNKNOWN")

    def test_mutated_preexec_observation_and_bool_alias_refuse(self):
        desc = descriptor(); obs = observation(desc); handle = MODEL.enroll_model(desc)
        MODEL.bind_unarmed_model(handle, obs, copy.deepcopy(obs))
        changed = copy.deepcopy(obs); changed["child"]["pidfd"] = True
        with self.assertRaises(MODEL.Refusal):
            MODEL.validate_preexec_model(handle, changed, 900)

    def test_mutated_stored_child_numeric_aliases_refuse(self):
        for field, value in (("pid", 300.0), ("starttime", 400.0),
                             ("pidfd", 50.0), ("pidfd", True)):
            desc = descriptor(); obs = observation(desc)
            handle = MODEL.enroll_model(desc)
            MODEL.bind_unarmed_model(handle, obs, copy.deepcopy(obs))
            handle.child[field] = value
            with self.assertRaises(MODEL.Refusal):
                MODEL.validate_preexec_model(handle, obs, 900)
            self.assertEqual(handle.state, "UNKNOWN")

    def test_scalar_subclass_is_rejected_without_invoking_custom_methods(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other):
                calls.append("eq")
                return super().__eq__(other)
            def __deepcopy__(self, memo):
                calls.append("deepcopy")
                return str(self)
        for field in ("status_state", "fd_graph_digest",
                      "loaded_code_manifest_sha256"):
            desc = descriptor(); obs = observation(desc)
            obs[field] = EvilStr(obs[field])
            handle = MODEL.enroll_model(desc)
            with self.assertRaises(MODEL.Refusal):
                MODEL.bind_unarmed_model(handle, obs, copy.deepcopy(observation(desc)))
            self.assertEqual(calls, [])

    def test_environment_subclass_is_rejected_before_deepcopy(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other):
                calls.append("eq")
                return super().__eq__(other)
            def __deepcopy__(self, memo):
                calls.append("deepcopy")
                return str(self)
        desc = descriptor()
        desc["payload"]["environment"]["LC_ALL"] = EvilStr("C")
        with self.assertRaises(MODEL.Refusal):
            MODEL.enroll_model(desc)
        self.assertEqual(calls, [])

        desc = descriptor(); obs = observation(desc)
        handle = MODEL.enroll_model(desc)
        MODEL.bind_unarmed_model(handle, obs, copy.deepcopy(obs))
        handle.descriptor["payload"]["environment"]["LC_ALL"] = EvilStr("C")
        with self.assertRaises(MODEL.Refusal):
            MODEL.validate_preexec_model(handle, obs, 900)
        self.assertEqual(calls, [])

    def test_stored_deadline_subclass_cannot_override_expiry(self):
        calls = []
        class EvilInt(int):
            def __gt__(self, other):
                calls.append("gt")
                return True
            def __deepcopy__(self, memo):
                calls.append("deepcopy")
                return int(self)
        desc = descriptor(); obs = observation(desc)
        handle = MODEL.enroll_model(desc)
        MODEL.bind_unarmed_model(handle, obs, copy.deepcopy(obs))
        handle.descriptor["run_binding"]["unarmed_deadline_ns"] = EvilInt(1000)
        with self.assertRaises(MODEL.Refusal):
            MODEL.validate_preexec_model(handle, obs, 2000)
        self.assertEqual(calls, [])

    def test_foreign_thread_cannot_bind(self):
        desc = descriptor(); obs = observation(desc); handle = MODEL.enroll_model(desc)
        errors = []
        thread = threading.Thread(target=lambda: self._foreign_bind(
            handle, obs, errors))
        thread.start(); thread.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(handle.state, "ENROLLED")

    @staticmethod
    def _foreign_bind(handle, obs, errors):
        try:
            MODEL.bind_unarmed_model(handle, obs, copy.deepcopy(obs))
        except BaseException as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
