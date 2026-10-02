import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"


class ThickDeactivationTransitionSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = PLUGIN.read_text(encoding="utf-8")
        start = source.index("sub _thick_deactivate_volume {")
        end = source.index("sub _with_lazy_volume_executor_lock {", start)
        cls.body = source[start:end]

    def test_transitional_deactivation_serializes_with_materializer(self):
        self.assertIn("if ($state->{phase} ne 'MATERIALIZED')", self.body)
        self.assertIn("_with_thick_transition_executor_lock(", self.body)
        self.assertIn("my ($locked_state)", self.body)
        self.assertIn("_thick_read_anchor($storeid, $scfg, $volname)", self.body)

    def test_post_lock_state_drives_recursive_materialized_path(self):
        self.assertIn("if $locked_state->{phase} eq 'MATERIALIZED'", self.body)
        self.assertIn("_thick_deactivate_volume(", self.body)

    def test_unfinished_transition_is_verified_and_retained(self):
        self.assertIn("_thick_verify_published_transition_frontend(", self.body)
        transition = self.body.split("if ($state->{phase} ne 'MATERIALIZED')", 1)[1]
        transition = transition.split("if ($class->_thick_frontend_present", 1)[0]
        self.assertNotIn("_thick_remove_mapper_exact(", transition)


if __name__ == "__main__":
    unittest.main()
