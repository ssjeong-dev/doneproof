import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CLIIntegrationTest(unittest.TestCase):
    def test_observed_failure_blocks_next_command(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            config = root/'config.json'
            config.write_text(json.dumps({'integration':'observe','writer_model':'writer',
                'audit':{'enabled':True,'action':'block','on_error':'block','require_different_model':True},
                'capabilities':{k:True for k in ['observe_execution','detect_turn_end','read_conversation','block_next_action','enforce_final_report']}}))
            base=[sys.executable,'-m','execution_audit.cli','--root',str(root),'--config',str(config)]
            def cli(*args, code=0):
                r=subprocess.run(base+list(args),capture_output=True,text=True,timeout=10)
                self.assertEqual(r.returncode,code,r.stderr+r.stdout)
                return json.loads(r.stdout) if r.stdout.strip() else None
            task=cli('task','--goal','Test behavior','--criteria','Pass test')['id']
            turn=cli('begin','--task-id',task)['id']
            evidence=cli('run','--turn-id',turn,'--',sys.executable,'-c','raise SystemExit(1)',code=1)
            self.assertEqual(evidence['evidence']['exit_code'],1)
            sub=cli('submit','--turn-id',turn,'--claims',json.dumps([{'criterion_id':'C1','text':'claimed pass','evidence_ids':[evidence['recorded']['id']]}]))
            code='import json,sys; json.load(sys.stdin); print(json.dumps({"status":"findings","findings":[{"message":"Actual test failed"}]}))'
            cli('audit','--audit-id',sub['audit_id'],'--model','auditor','--',sys.executable,'-c',code)
            next_turn=cli('begin','--task-id',task)['id']
            marker=root/'must-not-exist'
            command=[sys.executable,'-c',f'open({str(marker)!r},"w").write("ran")']
            cli('run','--turn-id',next_turn,'--',*command,code=3)
            self.assertFalse(marker.exists())
            other=cli('task','--goal','Unrelated','--criteria','Other')['id']
            cli('run','--turn-id',next_turn,'--task-id',other,'--',*command,code=3)
            self.assertFalse(marker.exists())
            cli('complete','--task-id',task,code=3)

    def test_default_initialization_and_doctor(self):
        with tempfile.TemporaryDirectory() as root:
            base=[sys.executable,'-m','execution_audit.cli','--root',root]
            for command in ['init','doctor']:
                r=subprocess.run(base+[command],capture_output=True,text=True,timeout=10)
                self.assertEqual(r.returncode,0,r.stderr)
                self.assertTrue(json.loads(r.stdout)['ok'])
