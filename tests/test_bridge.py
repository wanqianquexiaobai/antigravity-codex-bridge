import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest

from mcp import Client
from mcp.client.stdio import StdioServerParameters

SOURCE = Path(__file__).resolve().parents[1] / 'bridge.py'
spec = importlib.util.spec_from_file_location('bridge_under_test', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-test-', dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.project = self.root/'project'
        self.project.mkdir()
        fake = self.root/'fake-codex'
        fake.write_text('#!' + sys.executable + '\n' + textwrap.dedent('''
            import sys, json, time
            from pathlib import Path
            if '--version' in sys.argv:
                print('codex-test')
                raise SystemExit(0)
            if 'login' in sys.argv:
                raise SystemExit(0)
            prompt = sys.stdin.read()
            output = Path(sys.argv[sys.argv.index('-o') + 1])
            def emit(obj):
                print(json.dumps(obj), flush=True)
            session = sys.argv[-2] if 'resume' in sys.argv else 'session-fixture-123'
            emit({'type':'thread.started','thread_id':session})
            emit({'type':'item.completed','item':{'type':'agent_message','text':'Working on the requested project'}})
            if 'WAIT_FOR_CANCEL' in prompt:
                time.sleep(90)
            if 'REPORT_FAILURE' in prompt:
                emit({'type':'turn.failed','error':{'message':'fixture verification failed'}})
                raise SystemExit(1)
            time.sleep(0.5)
            output.write_text(json.dumps({'session':session,'cwd':str(Path.cwd()),'continued':'resume' in sys.argv}))
            emit({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':5}})
        '''))
        fake.chmod(0o700)
        self.config = self.root/'settings.json'
        self.config.write_text(json.dumps({
            'state_directory':str(self.root/'state'),'codex_home':str(self.root/'auth'),
            'codex_binary':str(fake),'launcher':str(fake),'wsl_distribution':'Debian',
            'workspace_roots':[str(self.root)],'protected_directories':[str(self.root/'state')],
            'execution_path':'/usr/bin:/bin','max_concurrent_jobs':1
        }))
        self.bridge = module.Bridge(self.config)
        self.params = StdioServerParameters(command=sys.executable,
            args=[str(SOURCE),'--config',str(self.config),'serve'])

    async def asyncTearDown(self):
        for job in self.bridge.list_jobs(50):
            if job['state'] in module.ACTIVE:
                self.bridge.cancel(job['job_id'])
                await self.bridge.status(job['job_id'], 8)
        for worker in self.bridge._workers:
            worker.wait(timeout=3)
        self.temp.cleanup()

    async def finish(self, job):
        result = await self.bridge.status(job['job_id'], 8)
        self.assertNotIn(result['state'], module.ACTIVE)
        return result

    async def test_mcp_reconnect_result_and_continue(self):
        async with Client(self.params) as client:
            tools = await client.list_tools()
            self.assertEqual(len(tools.tools),7)
            health = await client.call_tool('codex_health',{})
            self.assertTrue(health.structured_content['authenticated'])
            submitted = await client.call_tool('codex_submit',{
                'task':'Read the fixture','working_directory':str(self.project),'request_id':'reconnect'})
            self.assertFalse(submitted.is_error)
            job = submitted.structured_content
        # Closing the MCP connection must not terminate the delegated job.
        await self.finish(job)
        async with Client(self.params) as client:
            result = await client.call_tool('codex_result',{'job_id':job['job_id']})
            self.assertEqual(result.structured_content['state'],'succeeded')
            self.assertEqual(json.loads(result.structured_content['output'])['cwd'],str(self.project))
            continued = await client.call_tool('codex_continue',{
                'job_id':job['job_id'],'feedback':'Refine the result','request_id':'continuation'})
            self.assertFalse(continued.is_error)
            next_job=continued.structured_content
        await self.finish(next_job)
        output=json.loads(self.bridge.result(next_job['job_id'])['output'])
        self.assertTrue(output['continued'])
        self.assertEqual(output['session'],'session-fixture-123')

    async def test_idempotency_and_concurrent_job_guard(self):
        args=('WAIT_FOR_CANCEL',str(self.project),'read-only','same-request')
        job=self.bridge.submit(*args)
        self.assertEqual(self.bridge.submit(*args)['job_id'],job['job_id'])
        with self.assertRaisesRegex(ValueError,'different arguments'):
            self.bridge.submit('changed',str(self.project),'read-only','same-request')
        with self.assertRaisesRegex(ValueError,'already active'):
            self.bridge.submit('another',str(self.project),'read-only','another-request')
        self.bridge.cancel(job['job_id'])
        result=await self.finish(job)
        self.assertEqual(result['state'],'cancelled')
        row=self.bridge.get(job['job_id'])
        self.assertIsNone(self.bridge.identity(row['child_pid']))

    async def test_execution_failure_is_not_success(self):
        job=self.bridge.submit('REPORT_FAILURE',str(self.project),'read-only','failure')
        result=await self.finish(job)
        self.assertEqual(result['state'],'failed')
        self.assertIn('verification failed',result['error'])

    async def test_deadline_stops_child(self):
        job=self.bridge.submit('WAIT_FOR_CANCEL',str(self.project),'read-only','deadline')
        self.bridge.update(job['job_id'],deadline=time.time()+0.4)
        result=await self.finish(job)
        self.assertEqual(result['state'],'timed_out')

    async def test_workspace_boundaries_and_symlinks(self):
        with self.assertRaises(ValueError):
            self.bridge.workspace('/etc')
        with self.assertRaises(ValueError):
            self.bridge.workspace(str(self.root))
        (self.project/'outside').symlink_to('/etc',target_is_directory=True)
        with self.assertRaises(ValueError):
            self.bridge.workspace(str(self.project/'outside'))
        with self.assertRaises(ValueError):
            self.bridge.workspace(str(self.root/'state'))

    async def test_paginated_results(self):
        job=self.bridge.submit('small report',str(self.project),'read-only','pagination')
        await self.finish(job)
        full=self.bridge.result(job['job_id'])['output']
        first=self.bridge.result(job['job_id'],0,10)
        second=self.bridge.result(job['job_id'],first['next_offset'],24000)
        self.assertEqual(first['output']+second['output'],full)


if __name__=='__main__':
    unittest.main(verbosity=2)
