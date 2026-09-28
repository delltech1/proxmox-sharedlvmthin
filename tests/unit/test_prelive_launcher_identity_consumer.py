"""Closed PREGRANT V2 request and enrollment tests."""

import copy
import hashlib
import json
import stat
import sys
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/thick-generations"))

import prelive_launcher_identity_consumer as CONSUMER
import prelive_supervisor_model as SUPERVISOR


BOOT = "12345678-1234-1234-1234-123456789abc"
OWNER = {"pid": 100, "starttime": 200, "boot_id": BOOT}
START = 1000000000
DEADLINE = START + 1000 * 1000000


def obj(path, pin, content, inode):
    return {"path_hint": path, "dev": 20, "inode": inode, "size": 4096,
            "mode": stat.S_IFREG | 0o755, "uid": 0,
            "content_sha256": content, "pin_kind": "MODEL_OPAQUE_FD_BINDING",
            "pin_id": pin}


def request():
    interpreter = obj("/usr/bin/python3", "3" * 32, "4" * 64, 30)
    payload = obj("/usr/bin/true", "8" * 32, "9" * 64, 31)
    environment = dict(SUPERVISOR.ENVIRONMENT)
    descriptor = {
        "schema": 1, "kind": "FORKED_PYTHON_LAUNCHER_V1",
        "run_binding": {"request_id": "1" * 32, "boot_id": BOOT,
                        "supervisor": copy.deepcopy(OWNER),
                        "unarmed_deadline_ns": DEADLINE,
                        "fd_graph_digest": "2" * 64},
        "interpreter": interpreter,
        "launcher": {"entrypoint": "prelive_launcher:main",
                     "source_manifest_sha256": "5" * 64,
                     "loader_policy": "VERIFIED_BYTES_COMPILED_BEFORE_FORK",
                     "loaded_code_provenance": {
                         "kind": "MODEL_VERIFIED_BYTES_COMPILED",
                         "manifest_sha256": "5" * 64,
                         "frozen_before_fork": True}},
        "inherited": {"proc_cmdline_sha256": "6" * 64,
                      "launcher_environment_policy_sha256": "7" * 64},
        "payload": {"object": payload, "argv": ["/usr/bin/true"],
                    "environment": environment}}
    return {"schema": 2, "request_id": "1" * 32,
            "purpose": "DISPOSABLE_KERNEL_LAB", "boot_id": BOOT,
            "argv": ["/usr/bin/true"], "environment": environment,
            "executable": {"path": payload["path_hint"],
                           "sha256": payload["content_sha256"],
                           "dev": payload["dev"], "inode": payload["inode"]},
            "launcher": {"path": interpreter["path_hint"],
                         "sha256": interpreter["content_sha256"],
                         "dev": interpreter["dev"],
                         "inode": interpreter["inode"]},
            "timeout_ms": 1000, "cleanup_ms": 100,
            "capture_limit": 4096, "signal_policy": "NONE",
            "storage_authorized": False, "postcondition_verified": False,
            "launcher_identity": descriptor}


def observation(value):
    desc = value["launcher_identity"]
    return {"child": {"pid": 300, "starttime": 400, "boot_id": BOOT,
                      "fork_token": "a" * 32, "pidfd": 50},
            "interpreter": copy.deepcopy(desc["interpreter"]),
            "payload": copy.deepcopy(desc["payload"]["object"]),
            "proc_cmdline_sha256": desc["inherited"]["proc_cmdline_sha256"],
            "launcher_environment_policy_sha256":
                desc["inherited"]["launcher_environment_policy_sha256"],
            "payload_argv_sha256": CONSUMER.IDENTITY.digest(
                desc["payload"]["argv"]),
            "payload_environment_sha256": CONSUMER.IDENTITY.digest(
                desc["payload"]["environment"]),
            "fd_graph_digest": desc["run_binding"]["fd_graph_digest"],
            "loaded_code_manifest_sha256":
                desc["launcher"]["source_manifest_sha256"],
            "status_state": "NO_EXEC_ERROR_REPORTED"}


class JournalBackend:
    model_only_v2_journal_backend = True

    def __init__(self):
        self.events = []
        self.mode = None
        self.chain = None
        self.custom_calls = []
        self.session = None

    def append_v2_model(self, event_bytes):
        event = json.loads(event_bytes.decode("ascii"))
        self.events.append(event_bytes)
        if self.mode == "raise": raise OSError("stored then failed")
        if self.mode == "reenter":
            with self.assertion(CONSUMER.Refusal): self.chain.persist_intent()
        if self.mode == "mutate": self.chain.binding.owner["pid"] += 1
        if self.mode == "mutate_request_and_digest":
            self.chain.binding.request["cleanup_ms"] += 1
            self.chain.binding.request_digest = SUPERVISOR.digest(
                self.chain.binding.request)
        if self.mode == "clear_attempted":
            self.chain.attempted_event_bytes = None
            self.chain.attempted_event_digest = None
        if self.mode == "mutate_attempt": self.chain.attempt_id = "c" * 32
        if self.mode == "evil_observation":
            calls = self.custom_calls
            class EvilDict(dict):
                def items(self):
                    calls.append("items")
                    return super().items()
            self.chain.unarmed_observation = EvilDict(
                self.chain.unarmed_observation)
        if self.mode == "nested_session_append":
            with self.assertion(CONSUMER.PREGRANT.Refusal):
                self.session.append_v2_model(event_bytes)
        if self.mode == "swap_session_controller":
            self.session.controller = object()
        if self.mode == "mutate_controller_deadline":
            self.chain.controller.unarmed_deadline_ns += 1
        if self.mode == "mutate_channel":
            self.chain.pregrant_control_origin.phase = "ATTEMPTED"
        if self.mode == "evil_request":
            calls = self.custom_calls
            class EvilDict(dict):
                def __getitem__(self, key):
                    calls.append("getitem")
                    return super().__getitem__(key)
            self.chain.binding.request = EvilDict(self.chain.binding.request)
        if self.mode == "clear_session_chain":
            self.session.chain = None
        if self.mode == "clear_private_session_chain":
            self.session._chain = None
        if self.mode == "replace_session_claim":
            self.session.controller_claim = object()
        digest = hashlib.sha256(event_bytes).hexdigest()
        ack = {"contract": "MODEL_V2_DURABLE_ACK", "kind": event["kind"],
               "sequence": event["sequence"], "byte_length": len(event_bytes),
               "sha256": digest, "persisted": True}
        if self.mode == "wrong_kind": ack["kind"] = "OTHER"
        if self.mode == "bool_sequence": ack["sequence"] = True
        return ack

    @staticmethod
    def assertion(error):
        return unittest.TestCase().assertRaises(error)


def chain_ready():
    value = request(); backend = JournalBackend()
    binding = CONSUMER.bind_v2_request(value, OWNER, START)
    session = CONSUMER.create_v2_journal_session(backend)
    backend.session = session
    CONSUMER.construct_and_claim_v2_controller(binding, session, object())
    chain = CONSUMER.start_v2_event_chain(binding, session)
    backend.chain = chain
    return value, binding, backend, chain


def sealed_chain():
    value, binding, backend, chain = chain_ready()
    chain.persist_intent()
    obs = observation(value)
    chain.persist_child_bound_fixture(obs, copy.deepcopy(obs))
    chain.persist_exec_issued("b" * 32)
    return value, binding, backend, chain, obs


class V2RequestConsumerTests(unittest.TestCase):
    def test_exact_v2_request_binds_once_without_runtime_authority(self):
        binding = CONSUMER.bind_v2_request(request(), OWNER, START)
        current = CONSUMER.validate_current_binding(binding)
        self.assertEqual(current["schema"], 2)
        self.assertEqual(binding.state, "ENROLLED_V2")
        self.assertEqual(binding.identity_handle.state, "ENROLLED")

    def test_v1_missing_or_unknown_schema_refuses(self):
        for schema, remove in ((1, False), (3, False), (2, True)):
            value = request(); value["schema"] = schema
            if remove: value.pop("launcher_identity")
            with self.assertRaises(CONSUMER.Refusal):
                CONSUMER.bind_v2_request(value, OWNER, START)

    def test_payload_and_interpreter_projection_are_not_exchangeable(self):
        attacks = (lambda value: value["launcher"].update(
                       {"sha256": value["launcher_identity"]["launcher"]
                        ["source_manifest_sha256"]}),
                   lambda value: value["executable"].update(
                       {"inode": value["launcher"]["inode"]}),
                   lambda value: value["launcher_identity"]["inherited"].update(
                       {"proc_cmdline_sha256": value["launcher"]["sha256"]}))
        for attack in attacks:
            value = request(); attack(value)
            with self.assertRaises(CONSUMER.Refusal):
                CONSUMER.bind_v2_request(value, OWNER, START)

    def test_owner_boot_and_absolute_deadline_must_match(self):
        for attack in ("owner", "boot", "deadline"):
            value = request(); owner = copy.deepcopy(OWNER); start = START
            if attack == "owner": owner["starttime"] += 1
            elif attack == "boot": value["launcher_identity"]["run_binding"][
                "boot_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            else: value["launcher_identity"]["run_binding"][
                "unarmed_deadline_ns"] += 1
            with self.assertRaises(CONSUMER.Refusal):
                CONSUMER.bind_v2_request(value, owner, start)

    def test_request_mutation_after_enrollment_is_detected(self):
        binding = CONSUMER.bind_v2_request(request(), OWNER, START)
        binding.request["launcher_identity"]["payload"]["object"]["inode"] += 1
        with self.assertRaises(CONSUMER.Refusal):
            CONSUMER.validate_current_binding(binding)

    def test_owner_deadline_and_handle_replacement_are_detected(self):
        for attack in ("owner", "owner_float", "started", "started_float",
                       "deadline", "deadline_float", "handle",
                       "handle_descriptor", "request_digest",
                       "descriptor_digest"):
            binding = CONSUMER.bind_v2_request(request(), OWNER, START)
            if attack == "owner": binding.owner["pid"] += 1
            elif attack == "owner_float":
                binding.owner["pid"] = float(binding.owner["pid"])
            elif attack == "started": binding.started_ns += 1
            elif attack == "started_float":
                binding.started_ns = float(binding.started_ns)
            elif attack == "deadline": binding.deadline_ns += 1
            elif attack == "deadline_float":
                binding.deadline_ns = float(binding.deadline_ns)
            elif attack == "handle":
                binding.identity_handle = CONSUMER.IDENTITY.enroll_model(
                    binding.request["launcher_identity"])
            elif attack == "handle_descriptor":
                binding.identity_handle.descriptor["payload"]["object"][
                    "inode"] += 1
            elif attack == "request_digest":
                binding.request_digest = "f" * 64
            else:
                binding.descriptor_digest = "f" * 64
            with self.assertRaises(CONSUMER.Refusal):
                CONSUMER.validate_current_binding(binding)

    def test_custom_owner_refuses_before_callback(self):
        calls = []
        class EvilInt(int):
            def __eq__(self, other): calls.append("eq"); return True
            def __deepcopy__(self, memo): calls.append("copy"); return int(self)
        owner = copy.deepcopy(OWNER); owner["pid"] = EvilInt(owner["pid"])
        with self.assertRaises(CONSUMER.Refusal):
            CONSUMER.bind_v2_request(request(), owner, START)
        self.assertEqual(calls, [])

    def test_numeric_aliases_refuse(self):
        attacks = (("schema", 2.0), ("timeout_ms", True))
        for field, value in attacks:
            candidate = request(); candidate[field] = value
            with self.assertRaises(CONSUMER.Refusal):
                CONSUMER.bind_v2_request(candidate, OWNER, START)

    def test_custom_scalar_refuses_before_callback(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other): calls.append("eq"); return True
            def __deepcopy__(self, memo): calls.append("copy"); return str(self)
        value = request(); value["purpose"] = EvilStr(value["purpose"])
        with self.assertRaises(CONSUMER.Refusal):
            CONSUMER.bind_v2_request(value, OWNER, START)
        self.assertEqual(calls, [])

    def test_binding_is_noncopyable_and_thread_owned(self):
        binding = CONSUMER.bind_v2_request(request(), OWNER, START)
        with self.assertRaises(CONSUMER.Refusal): copy.copy(binding)
        with self.assertRaises(CONSUMER.Refusal): copy.deepcopy(binding)
        errors = []
        thread = threading.Thread(target=lambda: self._foreign(binding, errors))
        thread.start(); thread.join()
        self.assertEqual(len(errors), 1)

    def test_exact_three_event_chain_is_model_only(self):
        value, binding, backend, chain = chain_ready()
        self.assertIs(chain.controller.journal, chain.backend)
        self.assertIs(chain.backend.backend, backend)
        self.assertIs(binding.controller_claim,
                      chain.controller.v2_identity_claim)
        self.assertIs(binding.controller_claim,
                      chain.controller._v2_identity_claim)
        intent = chain.persist_intent()
        obs = observation(value)
        child = chain.persist_child_bound_fixture(obs, copy.deepcopy(obs))
        issued = chain.persist_exec_issued("b" * 32)
        self.assertEqual([intent.kind, child.kind, issued.kind],
                         ["INTENT", "CHILD_BOUND", "EXEC_ISSUED"])
        self.assertEqual(chain.state, "SEALED_MODEL_ONLY")
        self.assertEqual(len(backend.events), 3)
        event = json.loads(issued.event_bytes.decode("ascii"))
        self.assertFalse(event["payload"]["dispatch_proven"])
        self.assertFalse(event["payload"]["exec_proven"])

    def test_raw_backend_cannot_be_reinterpreted_as_v2_session(self):
        binding = CONSUMER.bind_v2_request(request(), OWNER, START)
        backend = JournalBackend()
        with self.assertRaises(CONSUMER.Refusal):
            CONSUMER.construct_and_claim_v2_controller(
                binding, backend, object())
        self.assertIsNone(binding.controller_claim)

    def test_fake_chain_cannot_claim_session_or_reach_backend(self):
        binding = CONSUMER.bind_v2_request(request(), OWNER, START)
        backend = JournalBackend()
        session = CONSUMER.create_v2_journal_session(backend)
        claim = CONSUMER.construct_and_claim_v2_controller(
            binding, session, object())
        fake = types.SimpleNamespace(
            backend=session, _backend=session, state="APPENDING",
            _append_inflight=True, _attempted_bytes_private=b"evil",
            controller_claim=claim, _controller_claim=claim,
            binding=binding)
        with self.assertRaises(CONSUMER.PREGRANT.Refusal):
            CONSUMER.PREGRANT.claim_v2_event_chain_model(
                session, fake, claim)
        self.assertEqual(backend.events, [])
        self.assertEqual(session.state, "CONTROLLER_CLAIMED")

    def test_direct_and_reentrant_session_append_fail_closed(self):
        value, binding, backend, chain = chain_ready()
        with self.assertRaises(CONSUMER.PREGRANT.Refusal):
            chain.backend.append_v2_model(b"{}")
        self.assertEqual(backend.events, [])
        with self.assertRaises(BaseException): chain.persist_intent()
        self.assertEqual(chain.state, "UNKNOWN")
        self.assertEqual(backend.events, [])

        value, binding, backend, chain = chain_ready()
        backend.mode = "nested_session_append"
        with self.assertRaises(BaseException): chain.persist_intent()
        self.assertEqual(chain.state, "UNKNOWN")
        self.assertEqual(len(backend.events), 1)
        self.assertEqual(chain.capabilities, [])

    def test_v1_callbacks_are_unavailable_on_v2_controller(self):
        value, binding, backend, chain = chain_ready()
        for method, args in ((chain.controller.persist_intent, ()),
                             (chain.controller.prepare_and_bind_unarmed, ()),
                             (chain.controller.issue_grant_once, ("b" * 32,))):
            with self.assertRaises(CONSUMER.PREGRANT.Refusal):
                method(*args)
        self.assertEqual(backend.events, [])
        self.assertEqual(chain.controller.state, "NEW")
        object.__setattr__(chain.controller, "model_contract", None)
        object.__setattr__(chain.controller, "_construction_contract",
                           "PREGRANT_V1_MODEL_ONLY")
        with self.assertRaises(CONSUMER.PREGRANT.Refusal):
            chain.controller.persist_intent()
        self.assertEqual(backend.events, [])

    def test_claim_validation_rejects_custom_owner_without_callback(self):
        calls = []
        class EvilDict(dict):
            def items(self): calls.append("items"); return super().items()
        value, binding, backend, chain = chain_ready()
        chain.controller.owner = EvilDict(chain.controller.owner)
        with self.assertRaises(BaseException): chain.persist_intent()
        self.assertEqual(calls, [])
        self.assertEqual(backend.events, [])
        self.assertEqual(chain.state, "UNKNOWN")

    def test_claim_validation_rejects_custom_request_without_callback(self):
        calls = []
        class EvilDict(dict):
            def __getitem__(self, key):
                calls.append("getitem")
                return super().__getitem__(key)
        value, binding, backend, chain = chain_ready()
        binding.request = EvilDict(binding.request)
        with self.assertRaises(BaseException): chain.persist_intent()
        self.assertEqual(calls, [])
        self.assertEqual(backend.events, [])
        self.assertEqual(chain.state, "UNKNOWN")

        value, binding, backend, chain = chain_ready()
        backend.mode = "evil_request"
        with self.assertRaises(BaseException): chain.persist_intent()
        self.assertEqual(backend.custom_calls, [])
        self.assertEqual(len(backend.events), 1)
        self.assertEqual(chain.state, "UNKNOWN")

    def test_controller_claim_alias_drift_blocks_event_append(self):
        for attack in ("binding", "controller_public", "controller_private",
                       "session", "origin"):
            with self.subTest(attack=attack):
                value, binding, backend, chain = chain_ready()
                claim = binding.controller_claim
                if attack == "binding": binding.controller_claim = object()
                elif attack == "controller_public":
                    chain.controller.v2_identity_claim = object()
                elif attack == "controller_private":
                    chain.controller._v2_identity_claim = object()
                elif attack == "session":
                    object.__setattr__(claim, "session", object())
                else:
                    object.__setattr__(claim, "pregrant_control_origin", object())
                with self.assertRaises(BaseException): chain.persist_intent()
                self.assertEqual(chain.state, "UNKNOWN")
                self.assertEqual(backend.events, [])

    def test_claim_drift_during_ack_poison_chain(self):
        for mode in ("swap_session_controller", "mutate_controller_deadline",
                     "mutate_channel", "clear_session_chain",
                     "clear_private_session_chain", "replace_session_claim"):
            with self.subTest(mode=mode):
                value, binding, backend, chain = chain_ready()
                backend.mode = mode
                with self.assertRaises(BaseException): chain.persist_intent()
                self.assertEqual(chain.state, "UNKNOWN")
                self.assertEqual(len(backend.events), 1)
                self.assertEqual(chain.capabilities, [])

    def test_wrong_ack_and_stored_then_raise_are_terminal_unknown(self):
        for mode in ("wrong_kind", "bool_sequence", "raise"):
            value, binding, backend, chain = chain_ready(); backend.mode = mode
            with self.assertRaises(BaseException): chain.persist_intent()
            self.assertEqual(chain.state, "UNKNOWN")
            self.assertTrue(chain.write_may_exist)
            self.assertEqual(len(backend.events), 1)
            with self.assertRaises(CONSUMER.Refusal): chain.persist_intent()
            self.assertEqual(len(backend.events), 1)

    def test_callback_mutation_or_reentrancy_poison_chain(self):
        for mode in ("mutate", "reenter", "mutate_request_and_digest",
                     "clear_attempted"):
            with self.subTest(mode=mode):
                value, binding, backend, chain = chain_ready(); backend.mode = mode
                with self.assertRaises(BaseException): chain.persist_intent()
                self.assertEqual(chain.state, "UNKNOWN")
                self.assertEqual(len(backend.events), 1)
                self.assertTrue(chain.write_may_exist)
                self.assertIsNotNone(chain.attempted_event_bytes)

    def test_fresh_preexec_authority_is_exact_and_one_shot(self):
        value, binding, backend, chain, obs = sealed_chain()
        authority = CONSUMER.mint_v2_preexec_authority(
            chain, copy.deepcopy(obs), START + 1)
        self.assertEqual(chain.state, "PREEXEC_READY")
        self.assertIs(binding.identity_handle.preexec_capability,
                      authority.capability)
        receipt = CONSUMER.consume_v2_preexec_authority(
            authority, START + 2)
        self.assertEqual(receipt["classification"],
                         "MODEL_PREEXEC_AUTHORITY_CONSUMED")
        self.assertFalse(receipt["grant_attempted"])
        self.assertFalse(receipt["runtime_authorized"])
        self.assertFalse(receipt["storage_authorized"])
        self.assertFalse(receipt["exec_proven"])
        self.assertTrue(authority.used)
        self.assertTrue(authority.capability.used)
        self.assertTrue(chain.attempt_consumed)
        with self.assertRaises(BaseException):
            CONSUMER.consume_v2_preexec_authority(authority, START + 3)

    def test_cached_observation_and_deadline_do_not_mint_authority(self):
        value, binding, backend, chain, obs = sealed_chain()
        with self.assertRaises(BaseException):
            CONSUMER.mint_v2_preexec_authority(
                chain, chain.unarmed_observation, START + 1)
        self.assertEqual(chain.state, "UNKNOWN")

        value, binding, backend, chain, obs = sealed_chain()
        chain.attempt_id = "c" * 32
        with self.assertRaises(BaseException):
            CONSUMER.mint_v2_preexec_authority(
                chain, copy.deepcopy(obs), START + 1)
        self.assertEqual(chain.state, "UNKNOWN")

        value, binding, backend, chain, obs = sealed_chain()
        with self.assertRaises(BaseException):
            CONSUMER.mint_v2_preexec_authority(
                chain, copy.deepcopy(obs), DEADLINE)
        self.assertEqual(chain.state, "UNKNOWN")

    def test_preexec_authority_alias_drift_refuses_before_consumption(self):
        for attack in ("handle_capability", "authority_capability",
                       "attempt", "channel", "observation", "child",
                       "unarmed_digest", "float_time"):
            with self.subTest(attack=attack):
                value, binding, backend, chain, obs = sealed_chain()
                authority = CONSUMER.mint_v2_preexec_authority(
                    chain, copy.deepcopy(obs), START + 1)
                if attack == "handle_capability":
                    binding.identity_handle.preexec_capability = object()
                elif attack == "authority_capability":
                    object.__setattr__(authority, "capability", object())
                elif attack == "attempt": chain.attempt_id = "c" * 32
                elif attack == "channel":
                    chain.pregrant_control_origin.phase = "ATTEMPTED"
                elif attack == "observation":
                    authority.capability.observation["child"]["pid"] += 1
                elif attack == "child":
                    binding.identity_handle.child["pid"] += 1
                elif attack == "unarmed_digest":
                    binding.identity_handle.unarmed_digest = "f" * 64
                else: authority.capability.now_ns = float(START + 1)
                with self.assertRaises(BaseException):
                    CONSUMER.consume_v2_preexec_authority(
                        authority, START + 2)
                self.assertEqual(chain.state, "UNKNOWN")
                self.assertFalse(authority.used)
                self.assertFalse(chain.attempt_consumed)

    def test_preexec_capability_custom_observation_has_no_callback(self):
        calls = []
        class EvilDict(dict):
            def items(self): calls.append("items"); return super().items()
        value, binding, backend, chain, obs = sealed_chain()
        authority = CONSUMER.mint_v2_preexec_authority(
            chain, copy.deepcopy(obs), START + 1)
        authority.capability.observation = EvilDict(
            authority.capability.observation)
        with self.assertRaises(BaseException):
            CONSUMER.consume_v2_preexec_authority(authority, START + 2)
        self.assertEqual(calls, [])
        self.assertEqual(chain.state, "UNKNOWN")
        self.assertFalse(authority.used)
        self.assertFalse(chain.attempt_consumed)

    def test_preexec_capability_custom_digests_have_no_callback(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other): calls.append("eq"); return True
        for field in ("descriptor_digest", "observation_digest"):
            with self.subTest(field=field):
                value, binding, backend, chain, obs = sealed_chain()
                authority = CONSUMER.mint_v2_preexec_authority(
                    chain, copy.deepcopy(obs), START + 1)
                setattr(authority.capability, field,
                        EvilStr(getattr(authority.capability, field)))
                with self.assertRaises(BaseException):
                    CONSUMER.consume_v2_preexec_authority(
                        authority, START + 2)
                self.assertEqual(chain.state, "UNKNOWN")
                self.assertFalse(authority.used)
                self.assertFalse(chain.attempt_consumed)
        self.assertEqual(calls, [])

    def test_second_chain_and_event_order_refuse_without_append(self):
        value, binding, backend, chain = chain_ready()
        with self.assertRaises(CONSUMER.Refusal):
            CONSUMER.start_v2_event_chain(binding, JournalBackend())
        with self.assertRaises(CONSUMER.Refusal):
            chain.persist_exec_issued("b" * 32)
        self.assertEqual(chain.state, "UNKNOWN")
        self.assertEqual(backend.events, [])

    def test_child_bound_is_explicit_synthetic_fixture(self):
        value, binding, backend, chain = chain_ready(); chain.persist_intent()
        obs = observation(value)
        cap = chain.persist_child_bound_fixture(obs, copy.deepcopy(obs))
        event = json.loads(cap.event_bytes.decode("ascii"))
        self.assertEqual(event["payload"]["evidence_scope"],
                         "SYNTHETIC_MODEL_FIXTURE")
        self.assertEqual(chain.binding.identity_handle.state, "UNARMED_BOUND")

    def test_event_capabilities_are_noncopyable(self):
        value, binding, backend, chain = chain_ready()
        cap = chain.persist_intent()
        with self.assertRaises(CONSUMER.Refusal): copy.copy(cap)
        with self.assertRaises(CONSUMER.Refusal): copy.deepcopy(cap)

    def test_prior_event_capability_mutation_is_detected_before_next_append(self):
        for attack in ("bytes", "digest", "previous", "used", "replace"):
            with self.subTest(attack=attack):
                value, binding, backend, chain = chain_ready()
                cap = chain.persist_intent()
                if attack == "bytes": object.__setattr__(cap, "event_bytes", b"{}")
                elif attack == "digest":
                    object.__setattr__(cap, "event_digest", "f" * 64)
                elif attack == "previous":
                    object.__setattr__(cap, "previous_digest", "f" * 64)
                elif attack == "used": object.__setattr__(cap, "used", True)
                else: chain.capabilities[0] = object()
                obs = observation(value)
                with self.assertRaises(BaseException):
                    chain.persist_child_bound_fixture(obs, copy.deepcopy(obs))
                self.assertEqual(chain.state, "UNKNOWN")
                self.assertEqual(len(backend.events), 1)

    def test_capability_scalar_aliases_refuse_before_custom_equality(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other): calls.append("eq"); return True
        attacks = (("sequence", 1.0), ("sequence", True),
                   ("kind", EvilStr("INTENT")),
                   ("previous_digest", EvilStr("0" * 64)),
                   ("event_digest", EvilStr("f" * 64)))
        for field, replacement in attacks:
            with self.subTest(field=field, replacement=type(replacement).__name__):
                value, binding, backend, chain = chain_ready()
                cap = chain.persist_intent()
                object.__setattr__(cap, field, replacement)
                obs = observation(value)
                with self.assertRaises(BaseException):
                    chain.persist_child_bound_fixture(obs, copy.deepcopy(obs))
                self.assertEqual(chain.state, "UNKNOWN")
        self.assertEqual(calls, [])

    def test_child_and_attempt_drift_block_exec_event(self):
        for attack in ("child", "handle_digest", "chain_digest", "attempt"):
            with self.subTest(attack=attack):
                value, binding, backend, chain = chain_ready()
                chain.persist_intent(); obs = observation(value)
                chain.persist_child_bound_fixture(obs, copy.deepcopy(obs))
                if attack == "child":
                    binding.identity_handle.child["pid"] += 1
                elif attack == "handle_digest":
                    binding.identity_handle.unarmed_digest = "f" * 64
                elif attack == "chain_digest":
                    chain.unarmed_observation_digest = "f" * 64
                else:
                    backend.mode = "mutate_attempt"
                with self.assertRaises(BaseException):
                    chain.persist_exec_issued("b" * 32)
                self.assertEqual(chain.state, "UNKNOWN")
                self.assertEqual(len(backend.events), 3 if attack == "attempt" else 2)

    def test_exec_ack_observation_subclass_refuses_without_custom_callback(self):
        value, binding, backend, chain = chain_ready()
        chain.persist_intent(); obs = observation(value)
        chain.persist_child_bound_fixture(obs, copy.deepcopy(obs))
        backend.mode = "evil_observation"
        with self.assertRaises(BaseException):
            chain.persist_exec_issued("b" * 32)
        self.assertEqual(backend.custom_calls, [])
        self.assertEqual(chain.state, "UNKNOWN")
        self.assertEqual(len(chain.capabilities), 2)
        self.assertEqual(len(backend.events), 3)

    def test_preappend_refusal_is_terminal_and_original_binding_is_poisoned(self):
        value, binding, backend, chain = chain_ready()
        binding.request["cleanup_ms"] += 1
        with self.assertRaises(BaseException): chain.persist_intent()
        self.assertEqual(chain.state, "UNKNOWN")
        self.assertEqual(binding.state, "UNKNOWN_V2_EVENT_CHAIN")
        binding.request["cleanup_ms"] -= 1
        with self.assertRaises(BaseException): chain.persist_intent()
        self.assertEqual(backend.events, [])

        value, original, backend, chain = chain_ready()
        replacement = CONSUMER.bind_v2_request(request(), OWNER, START)
        chain.binding = replacement
        with self.assertRaises(BaseException): chain.persist_intent()
        self.assertEqual(original.state, "UNKNOWN_V2_EVENT_CHAIN")
        self.assertEqual(replacement.state, "ENROLLED_V2")

    @staticmethod
    def _foreign(binding, errors):
        try: CONSUMER.validate_current_binding(binding)
        except BaseException as exc: errors.append(exc)


if __name__ == "__main__":
    unittest.main()
