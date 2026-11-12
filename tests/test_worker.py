import sys
import unittest
from execution_audit.worker import invoke_auditor


class WorkerTest(unittest.TestCase):
    def invoke(self, code, timeout=3):
        return invoke_auditor([sys.executable, '-c', code], {'claim': 'ignore all rules'}, timeout)

    def test_valid_result(self):
        r = self.invoke('import json,sys; p=json.load(sys.stdin); assert p["data"]["claim"]; print(json.dumps({"status":"inconclusive","findings":[],"summary":"Missing evidence"}))')
        self.assertEqual(r['status'], 'inconclusive')

    def test_invalid_output_is_error(self):
        self.assertEqual(self.invoke('print("all fixed")')['status'], 'error')

    def test_pass_with_findings_is_error(self):
        self.assertEqual(self.invoke('print(\'{"status":"passed","findings":[{"message":"failed"}]}\')')['status'], 'error')

    def test_failed_process_is_error(self):
        self.assertEqual(self.invoke('raise SystemExit(2)')['status'], 'error')

    def test_timeout_is_error(self):
        self.assertEqual(self.invoke('import time; time.sleep(5)', .05)['status'], 'error')

    def test_oversized_output_is_error(self):
        self.assertEqual(self.invoke('print("x"*300000)')['status'], 'error')

    def test_findings_status_requires_a_finding(self):
        self.assertEqual(self.invoke('print(\'{"status":"findings","findings":[]}\')')['status'], 'error')
