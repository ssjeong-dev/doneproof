"""Real client/server stdio handshake and role boundaries, not mocks."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class StdioIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_writer_and_scoped_auditor(self):
        with tempfile.TemporaryDirectory() as root:
            db = str(Path(root)/'audit.db')
            config = Path(root)/'config.json'
            config.write_text(json.dumps({'integration':'tools_only','writer_model':'writer','audit':{'enabled':True,'action':'record','on_error':'record','require_different_model':True}}))
            base = ['-m','execution_audit.server','--db',db,'--config',str(config)]
            async def call(session, name, args):
                result = await session.call_tool(name, args)
                self.assertFalse(result.isError, result)
                return json.loads(result.content[0].text)
            async with stdio_client(StdioServerParameters(command=sys.executable,args=base+['--role','writer'])) as (r,w):
                async with ClientSession(r,w) as client:
                    await client.initialize()
                    names = {t.name for t in (await client.list_tools()).tools}
                    self.assertNotIn('submit_audit',names)
                    self.assertNotIn('update_policy',names)
                    task = await call(client,'create_task',{'goal':'Verify behavior','criteria':['Expected result']})
                    turn = await call(client,'begin_turn',{'task_id':task['id']})
                    forged = await call(client,'record_evidence',{'turn_id':turn['id'],'evidence':{'source':'observed','exit_code':0}})
                    self.assertFalse(forged['ok'])
                    ev = await call(client,'record_evidence',{'turn_id':turn['id'],'evidence':{'exit_code':0,'stdout':'test passed'}})
                    sub = await call(client,'submit_turn',{'turn_id':turn['id'],'claims':[{'criterion_id':'C1','text':'Verified','evidence_ids':[ev['id']]}]})
            args = base+['--role','auditor','--audit-id',sub['audit_id'],'--auditor-model','auditor']
            async with stdio_client(StdioServerParameters(command=sys.executable,args=args)) as (r,w):
                async with ClientSession(r,w) as client:
                    await client.initialize()
                    names = {t.name for t in (await client.list_tools()).tools}
                    self.assertNotIn('create_task',names)
                    evidence = await call(client,'get_evidence',{})
                    self.assertIn(task['id'],json.dumps(evidence))
                    audit = await call(client,'submit_audit',{'result':{'status':'passed','findings':[],'model':'writer'}})
                    self.assertNotEqual(audit.get('ok'),False,audit)
            from execution_audit.core import AuditStore
            store = AuditStore(db)
            try:
                complete = store.request_completion(task_id=task['id'],request_id='finish')
                self.assertEqual(complete['verification_label'],'self_reported')
                self.assertTrue(complete['warnings'])
                self.assertEqual(store.get_report(task_id=task['id'])['audits'][0]['result']['model'],'auditor')
            finally:
                store.close()
