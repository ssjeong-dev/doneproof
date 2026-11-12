"""Integration tests against real SQLite and a separate auditor process."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from execution_audit.worker import run_audit


class AuditIntegrationTest(unittest.TestCase):
    def setUp(self):
        from execution_audit.core import AuditStore
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {
            'integration': 'tools_only', 'writer_model': 'writer',
            'audit': {'enabled': True, 'action': 'record', 'on_error': 'record', 'require_different_model': True},
            'capabilities': {k: False for k in ['observe_execution', 'detect_turn_end', 'read_conversation', 'block_next_action', 'enforce_final_report']},
        }
        self.store = AuditStore(str(Path(self.tmp.name) / 'audit.sqlite'), self.config)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def submitted(self):
        task = self.store.create_task(goal='Fix behavior', criteria=['Expected behavior verified'], request_id='task')
        turn = self.store.begin_turn(task_id=task['id'], request_id='turn')
        self.store.add_message(task_id=task['id'], role='user', content='Verify expected behavior')
        ev = self.store.record_evidence(turn_id=turn['id'], evidence={'source':'reported', 'exit_code':0, 'stdout':'1 passed'})
        submission = self.store.submit_turn(turn_id=turn['id'], claims=[{'criterion_id':'C1','text':'Verified','evidence_ids':[ev['id']]}], request_id='submit')
        return task, submission

    def test_external_auditor_error_is_persisted(self):
        task, sub = self.submitted()
        result = run_audit(self.store, sub['audit_id'], [sys.executable, '-c', 'print("not json")'], 'auditor')
        report = self.store.get_report(task_id=task['id'])
        self.assertIn('error', json.dumps(report).lower())
        self.assertIsInstance(result, dict)

    def test_independent_finding_survives_report(self):
        task, sub = self.submitted()
        code = 'import json,sys; p=json.load(sys.stdin); assert p["data"]["evidence"]; print(json.dumps({"status":"findings","findings":[{"message":"Only self-reported evidence"}]}))'
        run_audit(self.store, sub['audit_id'], [sys.executable, '-c', code], 'auditor')
        self.assertIn('Only self-reported evidence', json.dumps(self.store.get_report(task_id=task['id'])))

    def test_conversation_cutoff(self):
        task, sub = self.submitted()
        self.store.add_message(task_id=task['id'], role='user', content='Later requirement')
        found = self.store.search_conversation(audit_id=sub['audit_id'], query='Later')
        self.assertEqual(found, [])
