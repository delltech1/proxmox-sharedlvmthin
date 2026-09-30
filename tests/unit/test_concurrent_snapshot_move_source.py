import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "experiments" / "thick-generations" / "tg53-concurrent-snapshot-move.sh"


class ConcurrentSnapshotMoveSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = RUNNER.read_text(encoding="utf-8")

    def test_receipt_is_recovered_from_exact_node_task_inventory(self):
        self.assertIn('"/nodes/$node/tasks"', self.source)
        self.assertIn('--vmid "$vmid"', self.source)
        self.assertIn('--since "$((started - 1))"', self.source)
        self.assertIn('x.get("type")==kind', self.source)
        self.assertIn('assert len(rows)==1', self.source)
        self.assertNotIn('grep -oE \'UPID:', self.source)

    def test_ambiguous_receipt_is_unknown_and_never_retried(self):
        self.assertIn('RESULT=SNAP_TASK_UNKNOWN', self.source)
        self.assertIn('RESULT=MOVE_TASK_UNKNOWN', self.source)
        self.assertNotIn('for attempt', self.source.lower())
        self.assertNotIn('while ', self.source.lower())

    def test_safe_refusal_proves_source_and_target_postconditions(self):
        refusal = self.source.index('if [[ "$move_exit" != OK ]]')
        tail = self.source[refusal:]
        self.assertIn('"unresolved transaction"*"mutation refused"', tail)
        self.assertIn('[[ "$target_volume" == "$move_volume" ]]', tail)
        self.assertIn('source_inventory="$(pvesm list "$SOURCE_STORAGE"', tail)
        self.assertIn('target_inventory="$(pvesm list "$TARGET_STORAGE"', tail)
        self.assertIn('RESULT=SOURCE_INVENTORY_UNKNOWN', tail)
        self.assertIn('RESULT=TARGET_INVENTORY_UNKNOWN', tail)
        self.assertIn('CONCURRENT_SNAPSHOT_MOVE=SAFE_REFUSAL', tail)

    def test_dispatch_wait_is_bounded_without_restarting_workers(self):
        self.assertGreaterEqual(
            self.source.count('timeout --foreground --kill-after=10s "${DEADLINE_SEC}s"'),
            2,
        )


if __name__ == "__main__":
    unittest.main()
