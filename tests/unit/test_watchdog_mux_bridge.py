import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "experiments/thin-lease/watchdog_mux_bridge.py"
SPEC = importlib.util.spec_from_file_location("watchdog_mux_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


class FakeSocket:
    def __init__(self):
        self.writes = []
        self.closed = False

    def sendall(self, data):
        if self.closed:
            raise RuntimeError("write after close")
        self.writes.append(data)

    def close(self):
        self.closed = True


def evidence(**changes):
    values = dict(
        lockspace_healthy=True,
        resource_held=True,
        owner_matches=True,
        quorum=True,
        storage_identity=True,
    )
    values.update(changes)
    return bridge.LeaseEvidence(**values)


class WatchdogMuxBridgeTests(unittest.TestCase):
    def test_prototype_cannot_arm_by_default(self):
        sock = FakeSocket()
        model = bridge.WatchdogMuxBridge(sock, lambda: True)
        with self.assertRaisesRegex(RuntimeError, "arming is disabled"):
            model.arm(evidence())
        self.assertEqual(sock.writes, [])
        self.assertEqual(model.state, bridge.BridgeState.DISARMED)

    def test_positive_evidence_is_required_before_first_refresh(self):
        sock = FakeSocket()
        model = bridge.WatchdogMuxBridge(sock, lambda: True, arm_enabled=True)
        model.arm(evidence(quorum=False))
        self.assertEqual(sock.writes, [])
        self.assertEqual(model.state, bridge.BridgeState.LEASE_UNCERTAIN)

    def test_positive_evidence_refreshes_with_non_magic_byte(self):
        sock = FakeSocket()
        model = bridge.WatchdogMuxBridge(sock, lambda: True, arm_enabled=True)
        model.arm(evidence())
        self.assertTrue(model.refresh(evidence()))
        self.assertEqual(sock.writes, [b"1", b"1"])
        self.assertNotIn(b"V", sock.writes)

    def test_uncertainty_stops_feeding_and_disarms_only_after_io_stop(self):
        sock = FakeSocket()
        model = bridge.WatchdogMuxBridge(sock, lambda: True, arm_enabled=True)
        model.arm(evidence())
        self.assertFalse(model.refresh(evidence(resource_held=False)))
        self.assertEqual(sock.writes, [b"1", b"V"])
        self.assertTrue(sock.closed)
        self.assertEqual(model.state, bridge.BridgeState.DISARMED)

    def test_failed_io_stop_never_magic_closes_or_resumes_feeding(self):
        sock = FakeSocket()
        model = bridge.WatchdogMuxBridge(sock, lambda: False, arm_enabled=True)
        model.arm(evidence())
        self.assertFalse(model.refresh(evidence(storage_identity=False)))
        self.assertEqual(sock.writes, [b"1"])
        self.assertFalse(sock.closed)
        self.assertEqual(model.state, bridge.BridgeState.FENCING)
        self.assertFalse(model.refresh(evidence()))
        self.assertEqual(sock.writes, [b"1"])

    def test_magic_close_is_illegal_without_fencing_proof(self):
        sock = FakeSocket()
        model = bridge.WatchdogMuxBridge(sock, lambda: True, arm_enabled=True)
        model.arm(evidence())
        with self.assertRaisesRegex(RuntimeError, "cannot disarm"):
            model.disarm_after_positive_io_stop()
        self.assertNotIn(b"V", sock.writes)


if __name__ == "__main__":
    unittest.main()

