import tempfile
import unittest
from pathlib import Path
from execution_audit.harness import snapshot_tree, make_report_gate, HarnessError


class HarnessSafetyTest(unittest.TestCase):
    def test_large_file_change_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / 'large-source.txt'
            p.write_bytes(b'a' * (3*1024*1024))
            before = snapshot_tree(root)
            p.write_bytes(b'b' * (3*1024*1024))
            self.assertNotEqual(snapshot_tree(root), before)

    def test_missing_root_is_not_empty_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(HarnessError):
                snapshot_tree(str(Path(root) / 'missing'))

    def test_initial_task_can_run(self):
        gate = make_report_gate(lambda: {'decision':'pending', 'audits':[], 'task_status':'open'}, 'block')
        gate()
