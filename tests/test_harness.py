import sys
import tempfile
import time
import unittest
from pathlib import Path

from execution_audit.harness import (
    GateBlocked,
    HarnessError,
    make_report_gate,
    report_allows,
    run_command,
    snapshot_tree,
)

PY = sys.executable


def run_py(code, **kwargs):
    return run_command([PY, "-c", code], **kwargs)


class SnapshotTest(unittest.TestCase):
    def test_stable_across_mtime_and_ignores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n")
            (root / ".venv").mkdir()
            (root / ".venv" / "junk.py").write_text("ignored\n")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "a.pyc").write_bytes(b"ignored")
            (root / "state.db").write_bytes(b"ignored")
            first = snapshot_tree(root)
            # mtimes and ignored paths must not change the snapshot
            (root / ".venv" / "junk.py").write_text("changed\n")
            (root / "state.db").write_bytes(b"changed")
            time.sleep(0.01)
            (root / "a.py").touch()
            self.assertEqual(first, snapshot_tree(root))
            # dirty/untracked code IS included (filesystem walk, not git)
            (root / "new_untracked.py").write_text("y = 2\n")
            self.assertNotEqual(first, snapshot_tree(root))


class RunCommandTest(unittest.TestCase):
    def test_success(self):
        ev = run_py("print('hello')")
        self.assertEqual(ev["exit_code"], 0)
        self.assertEqual(ev["stdout"].strip(), "hello")
        self.assertEqual(ev["stderr"], "")
        self.assertFalse(ev["timed_out"])
        self.assertTrue(ev["valid"])
        self.assertFalse(ev["snapshot_mutated"])
        self.assertEqual(ev["source"], "observed")
        self.assertTrue(ev["trusted"])
        self.assertIsInstance(ev["command"], list)

    def test_failed_command(self):
        ev = run_py("import sys; sys.stderr.write('boom'); sys.exit(3)")
        self.assertEqual(ev["exit_code"], 3)
        self.assertEqual(ev["stderr"], "boom")
        self.assertTrue(ev["valid"])  # failure alone does not invalidate evidence

    def test_timeout(self):
        started = time.time()
        ev = run_py("import time; time.sleep(30)", timeout=0.3)
        self.assertTrue(ev["timed_out"])
        self.assertIsNone(ev["exit_code"])
        self.assertLess(time.time() - started, 10)
        self.assertTrue(ev["valid"])  # timeout alone does not invalidate evidence

    def test_snapshot_mutation_marks_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "code.py").write_text("v = 1\n")
            ev = run_py(
                "from pathlib import Path; Path('new_file.py').write_text('created')",
                root=root, cwd=root)
            self.assertEqual(ev["exit_code"], 0)
            self.assertTrue(ev["snapshot_mutated"])
            self.assertFalse(ev["valid"])
            self.assertNotEqual(ev["snapshot_before"], ev["snapshot_after"])

    def test_bounded_output(self):
        ev = run_py("print('x' * 300000)", max_output=4096)
        self.assertTrue(ev["stdout_truncated"])
        self.assertLessEqual(len(ev["stdout"]), 4096 + 64)
        self.assertIn("truncated", ev["stdout"])

    def test_never_shell(self):
        # shell metacharacters must be passed through literally, not interpreted
        ev = run_command([PY, "-c", "import sys; print(sys.argv[1])", "a; echo INJECTED"])
        self.assertEqual(ev["stdout"].strip(), "a; echo INJECTED")
        with self.assertRaises(HarnessError):
            run_command("echo hi")  # string command rejected

    def test_missing_cwd(self):
        with self.assertRaises(HarnessError):
            run_py("pass", cwd="/nonexistent/dir/for/test")


class GateTest(unittest.TestCase):
    def test_report_allows(self):
        self.assertTrue(report_allows({"decision": "approved"}))
        self.assertTrue(report_allows({"decision": "Passed"}))
        self.assertFalse(report_allows({"decision": "findings"}))
        self.assertFalse(report_allows({}))
        self.assertFalse(report_allows(None))

    def test_block_gate(self):
        gate = make_report_gate(lambda: {"decision": "findings"}, "block")
        with self.assertRaises(GateBlocked):
            gate()
        ok_gate = make_report_gate(lambda: {"decision": "approved"}, "block")
        ok_gate()  # no raise

    def test_gate_blocks_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "ran.txt"
            with self.assertRaises(GateBlocked):
                run_py(f"open({str(marker)!r}, 'w').write('x')",
                       root=tmp, gate=make_report_gate(lambda: {"decision": "rejected"}, "block"))
            self.assertFalse(marker.exists())

    def test_warn_gate_allows(self):
        gate = make_report_gate(lambda: {"decision": "pending"}, "warn")
        gate()  # warns but does not raise

    def test_invalid_action(self):
        with self.assertRaises(HarnessError):
            make_report_gate(lambda: {}, "record")


if __name__ == "__main__":
    unittest.main()
