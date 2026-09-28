import copy
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments/thick-generations"
sys.path.insert(0, str(EXP))

LAB = importlib.import_module("prelive_allocated_graph_enrollment")
FDS = importlib.import_module("prelive_real_fd_adapter")
DESC = importlib.import_module("prelive_descendant_accounting")
CONSUMER = importlib.import_module("prelive_launcher_identity_consumer")
BRIDGE = importlib.import_module("prelive_v2_intent_file_bridge")
from tests.unit import test_prelive_launcher_identity_consumer as FIXTURE


@unittest.skipUnless(os.name == "posix" and hasattr(os, "pipe2"),
                     "real Linux pipe semantics required")
class AllocatedGraphEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="slt-bound-intent-")
        self.parent_patch = mock.patch.object(
            BRIDGE.FILES, "PARENT", self.temp.name)
        self.parent_patch.start()
        self.backends = []
        self.calls = FDS.LinuxFDCalls()
        self.owner = self.calls.owner_identity()
        self.handle = FDS.allocate_real_fd_graph(
            self.calls, FDS.CLOSE_ONLY_NO_WRITE)
        self.started = 1000000000
        self.deadline = 2000000000
        self.enrollment = LAB.enroll_allocated_no_grant_graph(
            self.handle, self.owner, "1" * 32, "2" * 32, "3" * 32,
            self.started, self.deadline)

    def tearDown(self):
        if getattr(self, "handle", None) is not None and self.handle.owned:
            self.handle.close_all_once()
        for backend in getattr(self, "backends", []):
            if backend.root_fd is not None or backend.parent_fd is not None:
                try: backend.release_descriptors_preserving_state()
                except BaseException: pass
        self.parent_patch.stop()
        self.temp.cleanup()

    def request(self):
        value = FIXTURE.request()
        value["boot_id"] = self.owner["boot_id"]
        value["request_id"] = self.enrollment.request_id
        run = value["launcher_identity"]["run_binding"]
        run["request_id"] = self.enrollment.request_id
        run["boot_id"] = self.owner["boot_id"]
        run["supervisor"] = copy.deepcopy(self.owner)
        run["unarmed_deadline_ns"] = self.deadline
        run["fd_graph_digest"] = self.enrollment.graph_digest
        return value

    def descendant_domain(self, cleanup_ms=100, request_id=None):
        domain = DESC.ChildDomain(
            DESC._DOMAIN_KEY, self.owner,
            self.enrollment.request_id if request_id is None else request_id,
            "4" * 32,
            self.deadline + cleanup_ms * 1000000)
        domain.last_clock_ns = self.started
        return domain

    def bind(self):
        request = self.request()
        intent = CONSUMER.encode_v2_intent(request, self.owner)
        capability = self.enrollment.bind_once(
            request, intent, self.started + 1)
        return request, intent, capability

    def bridge(self, nonce="d" * 32):
        backend = BRIDGE.FILES.create_fresh_file_backend(nonce)
        self.backends.append(backend)
        return backend, BRIDGE.create_v2_intent_file_bridge(backend)

    def persist(self, capability, nonce="d" * 32, now=None):
        backend, bridge = self.bridge(nonce)
        persisted = LAB.persist_bound_intent_once(
            capability, bridge,
            self.started + 2 if now is None else now)
        return backend, bridge, persisted

    def test_exact_graph_request_intent_and_ticket_bind_once(self):
        request, intent, capability = self.bind()
        self.assertEqual(intent, CONSUMER.encode_v2_intent(
            request, self.owner))
        self.assertIs(capability.handle, self.handle)
        self.assertIs(capability.ticket, self.enrollment.ticket)
        self.assertFalse(capability.ticket.used)
        with self.assertRaisesRegex(LAB.Refusal, "persisted"):
            capability.claim_for_abort_once(object(), self.started + 2)
        backend, bridge, persisted = self.persist(capability)
        path = Path(backend.artifact_path()) / "exact-event-000001.json"
        self.assertEqual(path.read_bytes(), intent)
        self.assertEqual(bridge.state, "SEALED_INTENT")
        receipt = persisted.claim_for_abort_once(object(), self.started + 3)
        self.assertEqual(receipt["classification"],
                         "NO_GRANT_GRAPH_INTENT_CLAIMED")
        self.assertFalse(receipt["ticket_used"])
        self.assertFalse(receipt["grant_attempted"])
        with self.assertRaises(LAB.Refusal):
            persisted.claim_for_abort_once(object(), self.started + 4)

    def test_descendant_domain_reuses_exact_graph_ticket_and_binding(self):
        domain = self.descendant_domain()
        shared = LAB.enroll_allocated_descendant_domain(
            self.enrollment, domain, 100, self.started + 1)
        self.assertIs(shared.graph, self.enrollment)
        self.assertIs(shared.domain, domain)
        self.assertIs(shared.binding, self.enrollment.binding)
        self.assertIs(shared.pre_fork_enrollment, self.enrollment.ticket)
        self.assertIs(domain.bridge_enrollment, self.enrollment.binding)
        self.assertEqual(shared.execution_deadline_ns, self.deadline)
        self.assertEqual(shared.cleanup_hard_limit_ns,
                         self.deadline + 100 * 1000000)
        request, _, capability = self.bind()
        self.assertEqual(request["cleanup_ms"], shared.cleanup_ms)
        self.assertIs(capability.enrollment.domain_enrollment, shared)

    def test_second_descendant_domain_is_terminal_for_both_branches(self):
        first = self.descendant_domain()
        shared = LAB.enroll_allocated_descendant_domain(
            self.enrollment, first, 100, self.started + 1)
        second = self.descendant_domain()
        with self.assertRaises(LAB.Refusal):
            LAB.enroll_allocated_descendant_domain(
                self.enrollment, second, 100, self.started + 2)
        self.assertEqual(self.enrollment.state, "UNKNOWN")
        self.assertEqual(shared.state, "UNKNOWN")
        self.assertEqual(first.state, "UNKNOWN")
        self.assertEqual(second.state, "UNKNOWN")

    def test_descendant_cleanup_budget_must_match_bound_request(self):
        domain = self.descendant_domain(cleanup_ms=999)
        LAB.enroll_allocated_descendant_domain(
            self.enrollment, domain, 999, self.started + 1)
        with self.assertRaisesRegex(
                LAB.Refusal, "bind does not follow"):
            self.bind()
        self.assertEqual(self.enrollment.state, "UNKNOWN")
        self.assertEqual(domain.state, "UNKNOWN")

    def test_descendant_deadline_or_request_mismatch_is_terminal(self):
        for attack in ("deadline", "request"):
            with self.subTest(attack=attack):
                if attack != "deadline":
                    self.tearDown(); self.setUp()
                domain = self.descendant_domain(
                    request_id=("9" * 32 if attack == "request" else None))
                if attack == "deadline":
                    domain.deadline_ns -= 1
                with self.assertRaises(LAB.Refusal):
                    LAB.enroll_allocated_descendant_domain(
                        self.enrollment, domain, 100, self.started + 1)
                self.assertEqual(self.enrollment.state, "UNKNOWN")
                self.assertEqual(domain.state, "UNKNOWN")

    def test_descendant_enrollment_is_opaque_and_noncopyable(self):
        shared = LAB.enroll_allocated_descendant_domain(
            self.enrollment, self.descendant_domain(),
            100, self.started + 1)
        with self.assertRaises(LAB.Refusal):
            shared.cleanup_ms = 1
        with self.assertRaises(LAB.Refusal):
            copy.copy(shared)
        with self.assertRaises(LAB.Refusal):
            copy.deepcopy(shared)

    def test_descendant_domain_strict_preflight_invokes_no_custom_equality(self):
        attacks = ("request_id", "state", "deadline_ns", "records")
        for attack in attacks:
            with self.subTest(attack=attack):
                if attack != attacks[0]:
                    self.tearDown(); self.setUp()
                calls = []
                class EvilStr(str):
                    def __eq__(self, other):
                        calls.append("str-eq")
                        return True
                class EvilInt(int):
                    def __eq__(self, other):
                        calls.append("int-eq")
                        return True
                class EvilList(list):
                    def __eq__(self, other):
                        calls.append("list-eq")
                        return True
                domain = self.descendant_domain()
                values = {"request_id": EvilStr(self.enrollment.request_id),
                          "state": EvilStr("PREPARED"),
                          "deadline_ns": EvilInt(domain.deadline_ns),
                          "records": EvilList()}
                setattr(domain, attack, values[attack])
                with self.assertRaises(LAB.Refusal):
                    LAB.enroll_allocated_descendant_domain(
                        self.enrollment, domain, 100, self.started + 1)
                self.assertEqual(calls, [])

    def test_float_descendant_deadline_is_never_equal_authority(self):
        domain = self.descendant_domain()
        domain.deadline_ns = float(domain.deadline_ns)
        with self.assertRaises(LAB.Refusal):
            LAB.enroll_allocated_descendant_domain(
                self.enrollment, domain, 100, self.started + 1)
        self.assertEqual(self.enrollment.state, "UNKNOWN")
        self.assertEqual(domain.state, "UNKNOWN")

    def test_custom_domain_namespace_key_invokes_no_equality(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other):
                calls.append("eq")
                return super().__eq__(other)
            __hash__ = str.__hash__
        domain = self.descendant_domain()
        value = domain.__dict__.pop("state")
        domain.__dict__[EvilStr("state")] = value
        calls.clear()
        with self.assertRaises(LAB.Refusal):
            LAB.enroll_allocated_descendant_domain(
                self.enrollment, domain, 100, self.started + 1)
        self.assertEqual(calls, [])

    def test_callback_injected_domain_namespace_key_is_callback_free(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other):
                calls.append("eq")
                return super().__eq__(other)
            __hash__ = str.__hash__
        domain = self.descendant_domain()
        LAB.enroll_allocated_descendant_domain(
            self.enrollment, domain, 100, self.started + 1)
        real_fstat = self.calls.fstat
        fired = []
        def inject(fd):
            result = real_fstat(fd)
            if not fired:
                fired.append(True)
                value = domain.__dict__.pop("state")
                domain.__dict__[EvilStr("state")] = value
                calls.clear()
            return result
        self.calls.fstat = inject
        try:
            with self.assertRaises(LAB.Refusal):
                self.bind()
        finally:
            self.calls.fstat = real_fstat
        self.assertTrue(fired)
        self.assertEqual(calls, [])
        self.assertEqual(self.enrollment.state, "UNKNOWN")

    def test_domain_id_or_seal_drift_blocks_subsequent_bind(self):
        for attack in ("domain_id", "seal"):
            with self.subTest(attack=attack):
                if attack != "domain_id":
                    self.tearDown(); self.setUp()
                domain = self.descendant_domain()
                LAB.enroll_allocated_descendant_domain(
                    self.enrollment, domain, 100, self.started + 1)
                if attack == "domain_id":
                    domain.domain_id = "9" * 32
                else:
                    domain._seal = object()
                with self.assertRaises(LAB.Refusal):
                    self.bind()
                self.assertEqual(self.enrollment.state, "UNKNOWN")
                self.assertEqual(domain.state, "UNKNOWN")

    def test_descendant_enrollment_and_bind_watermarks_are_monotonic(self):
        domain = self.descendant_domain()
        domain.last_clock_ns = self.started + 2
        with self.assertRaises(LAB.Refusal):
            LAB.enroll_allocated_descendant_domain(
                self.enrollment, domain, 100, self.started + 1)
        self.tearDown(); self.setUp()
        domain = self.descendant_domain()
        LAB.enroll_allocated_descendant_domain(
            self.enrollment, domain, 100, self.started + 5)
        with self.assertRaisesRegex(LAB.Refusal, "bind does not follow"):
            self.bind()
        self.assertEqual(self.enrollment.state, "UNKNOWN")

    def test_bind_at_exact_descendant_enrollment_watermark_is_allowed(self):
        domain = self.descendant_domain()
        LAB.enroll_allocated_descendant_domain(
            self.enrollment, domain, 100, self.started + 1)
        _, _, capability = self.bind()
        self.assertEqual(capability.bind_now_ns, self.started + 1)

    def test_same_handle_cannot_enroll_twice(self):
        with self.assertRaises(FDS.Refusal):
            LAB.enroll_allocated_no_grant_graph(
                self.handle, self.owner, "1" * 32, "4" * 32, "5" * 32,
                self.started, self.deadline)
        self.assertTrue(self.handle.poisoned)

    def test_handle_owner_and_enrollment_owner_must_match(self):
        self.handle.close_all_once()
        self.handle = FDS.allocate_real_fd_graph(
            self.calls, FDS.CLOSE_ONLY_NO_WRITE)
        foreign = copy.deepcopy(self.owner)
        foreign["pid"] += 1
        with self.assertRaises(BaseException):
            LAB.enroll_allocated_no_grant_graph(
                self.handle, foreign, "1" * 32, "2" * 32, "3" * 32,
                self.started, self.deadline)
        self.assertFalse(self.handle.poisoned)
        self.assertIsNone(self.handle._consumer_claim)

    def test_request_intent_or_deadline_mismatch_is_terminal(self):
        attacks = ("graph", "intent", "deadline")
        for attack in attacks:
            with self.subTest(attack=attack):
                if attack != attacks[0]:
                    self.tearDown(); self.setUp()
                request = self.request()
                intent = CONSUMER.encode_v2_intent(request, self.owner)
                now = self.started + 1
                if attack == "graph":
                    request["launcher_identity"]["run_binding"][
                        "fd_graph_digest"] = "9" * 64
                    intent = CONSUMER.encode_v2_intent(request, self.owner)
                elif attack == "intent":
                    intent = intent[:-1] + b"X"
                else:
                    now = self.deadline
                with self.assertRaises(BaseException):
                    self.enrollment.bind_once(request, intent, now)
                self.assertEqual(self.enrollment.state, "UNKNOWN")

    def test_descriptor_mutation_during_handle_callback_refuses(self):
        request = self.request()
        intent = CONSUMER.encode_v2_intent(request, self.owner)
        real_fstat = self.calls.fstat
        fired = []
        def mutate(fd):
            result = real_fstat(fd)
            if not fired and fd == self.handle.bundle["grant_child"]["fd"]:
                fired.append(True)
                self.enrollment.descriptor["graph_id"] = "9" * 32
            return result
        self.calls.fstat = mutate
        try:
            with self.assertRaises(BaseException):
                self.enrollment.bind_once(
                    request, intent, self.started + 1)
        finally:
            self.calls.fstat = real_fstat
        self.assertEqual(self.enrollment.state, "UNKNOWN")

    def test_nested_capability_claim_cannot_be_repaired_by_outer_claim(self):
        _, _, capability = self.bind()
        _, _, persisted = self.persist(capability)
        real_fstat = self.calls.fstat
        fired = []
        nested_consumer = object()
        def nested(fd):
            result = real_fstat(fd)
            if not fired:
                fired.append(True)
                try:
                    persisted.claim_for_abort_once(
                        nested_consumer, self.started + 3)
                except LAB.Refusal:
                    pass
            return result
        self.calls.fstat = nested
        try:
            with self.assertRaises(BaseException):
                persisted.claim_for_abort_once(object(), self.started + 3)
        finally:
            self.calls.fstat = real_fstat
        self.assertEqual(capability.state, "UNKNOWN")
        self.assertEqual(self.enrollment.state, "UNKNOWN")

    def test_last_callback_custom_request_never_reaches_custom_method(self):
        _, _, capability = self.bind()
        _, _, persisted = self.persist(capability)
        real_fstat = self.calls.fstat
        custom_calls = []
        fired = []
        original = capability.request["environment"]
        class EvilDict(dict):
            def items(self):
                custom_calls.append("items")
                return super().items()
            def __deepcopy__(self, memo):
                custom_calls.append("deepcopy")
                return dict(self)
        target = self.handle.bundle["stderr_parent"]["fd"]
        def mutate(fd):
            result = real_fstat(fd)
            if fd == target and not fired:
                fired.append(True)
                capability.request["environment"] = EvilDict(original)
            return result
        self.calls.fstat = mutate
        try:
            with self.assertRaises(BaseException):
                persisted.claim_for_abort_once(object(), self.started + 3)
        finally:
            self.calls.fstat = real_fstat
        self.assertEqual(custom_calls, [])
        self.assertEqual(capability.state, "UNKNOWN")
        self.assertEqual(self.enrollment.state, "UNKNOWN")

    def test_stored_then_error_is_terminal_without_retry_or_reclaim(self):
        _, intent, capability = self.bind()
        backend, bridge = self.bridge("e" * 32)
        real = backend.persist_exact_file
        calls = []
        def stored_then_error(name, raw):
            calls.append((name, raw))
            real(name, raw)
            raise OSError("ack lost after durable record")
        with mock.patch.object(backend, "persist_exact_file",
                               side_effect=stored_then_error):
            with self.assertRaises(BaseException):
                LAB.persist_bound_intent_once(
                    capability, bridge, self.started + 2)
        path = Path(backend.artifact_path()) / "exact-event-000001.json"
        self.assertEqual(path.read_bytes(), intent)
        self.assertEqual(len(calls), 1)
        self.assertEqual(bridge.state, "UNKNOWN")
        self.assertEqual(capability.state, "UNKNOWN")
        self.assertEqual(self.enrollment.state, "UNKNOWN")
        self.assertFalse(self.enrollment.ticket.used)
        self.assertIs(self.handle._consumer_claim, self.enrollment)
        with self.assertRaises(BaseException):
            LAB.persist_bound_intent_once(
                capability, bridge, self.started + 3)
        self.assertEqual(len(calls), 1)

    def test_persistence_callback_reentrancy_poison_is_monotonic(self):
        _, _, capability = self.bind()
        backend, bridge = self.bridge("f" * 32)
        real = backend.persist_exact_file
        nested = []
        def reenter(name, raw):
            try:
                LAB.persist_bound_intent_once(
                    capability, bridge, self.started + 2)
            except BaseException as exc:
                nested.append(exc)
            return real(name, raw)
        with mock.patch.object(backend, "persist_exact_file",
                               side_effect=reenter):
            with self.assertRaises(BaseException):
                LAB.persist_bound_intent_once(
                    capability, bridge, self.started + 2)
        self.assertEqual(len(nested), 1)
        self.assertEqual(capability.state, "UNKNOWN")
        self.assertEqual(self.enrollment.state, "UNKNOWN")

    def test_receipt_mutation_during_post_persist_fd_callback_refuses(self):
        _, _, capability = self.bind()
        backend, bridge = self.bridge("a" * 32)
        real_persist = bridge.persist_intent
        returned = {}
        def persist(raw):
            receipt = real_persist(raw)
            returned["receipt"] = receipt
            return receipt
        real_fstat = self.calls.fstat
        fired = []
        def mutate(fd):
            result = real_fstat(fd)
            if bridge.state == "SEALED_INTENT" and not fired:
                fired.append(True)
                returned["receipt"]["record_identity"]["inode"] += 1
            return result
        self.calls.fstat = mutate
        with mock.patch.object(bridge, "persist_intent", side_effect=persist):
            try:
                with self.assertRaises(BaseException):
                    LAB.persist_bound_intent_once(
                        capability, bridge, self.started + 2)
            finally:
                self.calls.fstat = real_fstat
        self.assertTrue(fired)
        self.assertEqual(capability.state, "UNKNOWN")
        self.assertEqual(self.enrollment.state, "UNKNOWN")

    def test_final_claim_callback_bridge_poison_cannot_return_claimed(self):
        _, _, capability = self.bind()
        _, bridge, persisted = self.persist(capability, "b" * 32)
        real_fstat = self.calls.fstat
        fired = []
        def mutate(fd):
            result = real_fstat(fd)
            if not fired:
                fired.append(True)
                bridge.state = "UNKNOWN"
            return result
        self.calls.fstat = mutate
        try:
            with self.assertRaises(BaseException):
                persisted.claim_for_abort_once(object(), self.started + 3)
        finally:
            self.calls.fstat = real_fstat
        self.assertEqual(persisted.state, "UNKNOWN")
        self.assertNotEqual(capability.state, "CLAIMED")

    def test_post_persist_custom_request_refuses_without_custom_callback(self):
        _, _, capability = self.bind()
        backend, bridge = self.bridge("c" * 32)
        real = bridge.persist_intent
        custom_calls = []
        original = capability.request["environment"]
        class EvilDict(dict):
            def items(self):
                custom_calls.append("items")
                return super().items()
            def __deepcopy__(self, memo):
                custom_calls.append("deepcopy")
                return dict(self)
        def mutate(raw):
            receipt = real(raw)
            capability.request["environment"] = EvilDict(original)
            return receipt
        with mock.patch.object(bridge, "persist_intent", side_effect=mutate):
            with self.assertRaises(BaseException):
                LAB.persist_bound_intent_once(
                    capability, bridge, self.started + 2)
        self.assertEqual(custom_calls, [])
        self.assertEqual(capability.state, "UNKNOWN")

    def test_claim_time_cannot_precede_binding_watermark(self):
        request = self.request()
        intent = CONSUMER.encode_v2_intent(request, self.owner)
        capability = self.enrollment.bind_once(
            request, intent, self.deadline - 2)
        _, _, persisted = self.persist(
            capability, now=self.deadline - 1)
        with self.assertRaises(BaseException):
            persisted.claim_for_abort_once(object(), self.started + 1)
        self.assertEqual(persisted.state, "UNKNOWN")

    def test_enrollment_seal_cannot_be_replaced_or_deleted(self):
        with self.assertRaises(LAB.Refusal):
            self.enrollment._sealed = False
        with self.assertRaises(LAB.Refusal):
            del self.enrollment._sealed
        with self.assertRaises(LAB.Refusal):
            self.enrollment.ticket = object()

    def test_custom_deepcopy_inputs_refuse_without_callback(self):
        calls = []
        class EvilDict(dict):
            def __deepcopy__(self, memo):
                calls.append("deepcopy")
                return dict(self)
        request = EvilDict(self.request())
        with self.assertRaises(BaseException):
            CONSUMER.encode_v2_intent(request, self.owner)
        self.assertEqual(calls, [])
        intent = CONSUMER.encode_v2_intent(self.request(), self.owner)
        with self.assertRaises(BaseException):
            self.enrollment.bind_once(
                request, intent, self.started + 1)
        self.assertEqual(calls, [])

    def test_constructor_reentrancy_cannot_be_repaired(self):
        self.handle.close_all_once()
        self.handle = FDS.allocate_real_fd_graph(
            self.calls, FDS.CLOSE_ONLY_NO_WRITE)
        real_fstat = self.calls.fstat
        fired = []
        def nested(fd):
            result = real_fstat(fd)
            enrollment = self.handle._consumer_claim
            if not fired and enrollment is not None:
                fired.append(True)
                try:
                    enrollment.bind_once({}, b"", self.started + 1)
                except BaseException:
                    pass
            return result
        self.calls.fstat = nested
        try:
            with self.assertRaises(BaseException):
                LAB.enroll_allocated_no_grant_graph(
                    self.handle, self.owner, "1" * 32, "2" * 32,
                    "3" * 32, self.started, self.deadline)
        finally:
            self.calls.fstat = real_fstat
        self.assertTrue(fired)

    def test_custom_owner_refuses_before_deepcopy_or_handle_claim(self):
        self.handle.close_all_once()
        self.handle = FDS.allocate_real_fd_graph(
            self.calls, FDS.CLOSE_ONLY_NO_WRITE)
        calls = []
        class EvilOwner(dict):
            def __deepcopy__(self, memo):
                calls.append("deepcopy")
                return dict(self)
        with self.assertRaises(BaseException):
            LAB.enroll_allocated_no_grant_graph(
                self.handle, EvilOwner(self.owner), "1" * 32, "2" * 32,
                "3" * 32, self.started, self.deadline)
        self.assertEqual(calls, [])
        self.assertIsNone(self.handle._consumer_claim)

    def test_pure_encoder_matches_existing_event_chain_intent(self):
        request = self.request()
        binding = CONSUMER.bind_v2_request(
            request, self.owner, self.started)
        backend = FIXTURE.JournalBackend()
        session = CONSUMER.create_v2_journal_session(backend)
        backend.session = session
        CONSUMER.construct_and_claim_v2_controller(
            binding, session, object())
        chain = CONSUMER.start_v2_event_chain(binding, session)
        backend.chain = chain
        actual = chain.persist_intent().event_bytes
        self.assertEqual(actual, CONSUMER.encode_v2_intent(
            request, self.owner))


if __name__ == "__main__":
    unittest.main()
