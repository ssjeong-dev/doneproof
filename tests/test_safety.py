import copy
import tempfile
import unittest
from pathlib import Path
from execution_audit.core import AuditStore, AuditError


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'db.sqlite')
        self.config = {'integration':'observe','writer_model':'writer',
            'audit':{'enabled':True,'action':'block','on_error':'block','require_different_model':True},
            'capabilities':{k:True for k in ['observe_execution','detect_turn_end','read_conversation','block_next_action','enforce_final_report']}}
        self.store = AuditStore(self.path, self.config)
        self.task = self.store.create_task(goal='Fix',criteria=['Behavior'],request_id='create')['id']

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def submitted(self, exit_code=0):
        turn = self.store.begin_turn(task_id=self.task,request_id='begin')['id']
        ev = self.store.record_evidence(turn_id=turn,evidence={'source':'observed','exit_code':exit_code,'snapshot_before':'same','snapshot_after':'same'},trusted=True)['id']
        sub = self.store.submit_turn(turn_id=turn,claims=[{'criterion_id':'C1','text':'Verified','evidence_ids':[ev]}],request_id='submit')
        return sub['audit_id']

    def test_failed_execution_cannot_pass(self):
        audit = self.submitted(1)
        with self.assertRaises(AuditError):
            self.store.submit_audit(audit_id=audit,result={'status':'passed','model':'auditor','findings':[]},request_id='audit')

    def test_inconclusive_blocks(self):
        audit = self.submitted()
        self.store.submit_audit(audit_id=audit,result={'status':'inconclusive','model':'auditor','findings':[]},request_id='audit')
        r = self.store.request_completion(task_id=self.task,request_id='complete')
        self.assertNotEqual(self.store.get_task(task_id=self.task)['status'], 'completed')
        self.assertIn(r['decision'], ['block','denied'])

    def test_active_turn_prevents_completion(self):
        audit = self.submitted()
        self.store.submit_audit(audit_id=audit,result={'status':'passed','model':'auditor','findings':[]},request_id='audit')
        self.store.begin_turn(task_id=self.task,request_id='begin-next')
        self.store.request_completion(task_id=self.task,request_id='complete')
        self.assertNotEqual(self.store.get_task(task_id=self.task)['status'], 'completed')

    def test_block_decision_available_before_completion(self):
        audit = self.submitted()
        self.store.submit_audit(audit_id=audit,result={'status':'findings','model':'auditor','findings':[{'message':'Failure'}]},request_id='audit')
        self.assertIn(self.store.get_report(task_id=self.task)['decision'], ['block','denied'])

    def test_policy_cannot_silently_weaken_on_reopen(self):
        weaker = copy.deepcopy(self.config)
        weaker['audit']['action'] = 'record'
        with self.assertRaises(AuditError):
            other = AuditStore(self.path, weaker)
            other.close()

    def test_unknown_policy_rejected(self):
        invalid = copy.deepcopy(self.config)
        invalid['audit']['action'] = 'blok'
        with self.assertRaises(AuditError):
            other = AuditStore(':memory:', invalid)
            other.close()

    def test_explicit_admin_update_is_logged(self):
        changed = copy.deepcopy(self.config)
        changed['audit']['action'] = 'warn'
        result = self.store.update_policy(config=changed, request_id='policy-update')
        self.assertNotEqual(result['old_version'], result['policy_version'])
        self.assertEqual(self.store.update_policy(config=changed, request_id='policy-update'), result)
        other = AuditStore(self.path, changed)
        other.close()
        count = self.store._conn.execute("SELECT count(*) FROM ledger_events WHERE entity='policy' AND operation='UPDATE'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_append_only_ledger(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute("DELETE FROM ledger_events")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute("UPDATE ledger_events SET operation='fake'")

    def test_concurrent_idempotent_creation(self):
        from concurrent.futures import ThreadPoolExecutor
        def create(_):
            return self.store.create_task(goal='same',criteria=['same'],request_id='concurrent')['id']
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(create, range(12)))
        self.assertEqual(len(set(ids)),1)

    def test_each_claim_requires_evidence(self):
        task = self.store.create_task(goal='two',criteria=['one','two'],request_id='two')['id']
        turn = self.store.begin_turn(task_id=task,request_id='two-turn')['id']
        ev = self.store.record_evidence(turn_id=turn,evidence={'source':'reported'})['id']
        sub = self.store.submit_turn(turn_id=turn,claims=[{'criterion_id':'C1','text':'one','evidence_ids':[ev]}, {'criterion_id':'C2','text':'two','evidence_ids':[]}],request_id='two-submit')
        with self.assertRaises(AuditError):
            self.store.submit_audit(audit_id=sub['audit_id'],result={'status':'passed','model':'auditor','findings':[]},request_id='two-audit')

    def test_same_timestamp_conversation_cutoff(self):
        from unittest.mock import patch
        with patch('execution_audit.core._now', return_value='2026-01-01T00:00:00Z'):
            self.store.add_message(task_id=self.task,role='user',content='Before')
            audit = self.submitted()
            late = self.store.add_message(task_id=self.task,role='user',content='After')
            self.assertEqual(self.store.search_conversation(audit_id=audit,query='After'),[])
            with self.assertRaises(AuditError):
                self.store.read_conversation(audit_id=audit,message_id=late['id'])
