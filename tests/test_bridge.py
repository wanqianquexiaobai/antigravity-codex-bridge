import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import shutil
import signal
import sqlite3
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.client.stdio import StdioServerParameters

SOURCE = Path(__file__).resolve().parents[1] / 'bridge.py'
sys.path.insert(0, str(SOURCE.parent))
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
        fake.write_text('#!' + sys.executable + '\n' + (SOURCE.parent/'tests/fake_codex.py').read_text())
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
            self.assertEqual(len(tools.tools),11)
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

    async def test_windows_case_alias_uses_filesystem_identity(self):
        value=str(self.project)
        if not value.startswith('/mnt/c/'):
            self.skipTest('Requires a Windows-mounted directory')
        alias=Path('/mnt/c/'+value[len('/mnt/c/'):].swapcase())
        if not alias.exists():
            self.skipTest('Windows directory is case-sensitive')
        self.assertTrue(Path(self.bridge.workspace(str(alias))).samefile(self.project))

    async def test_paginated_results(self):
        job=self.bridge.submit('small report',str(self.project),'read-only','pagination')
        await self.finish(job)
        full=self.bridge.result(job['job_id'])['output']
        first=self.bridge.result(job['job_id'],0,10,'full')
        second=self.bridge.result(job['job_id'],first['next_offset'],8000,'full')
        self.assertEqual(first['output']+second['output'],full)

    async def until(self, job_id, states, timeout=12):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            result=await self.bridge.status(job_id,0)
            if result['state'] in states:
                return result
            await asyncio.sleep(.1)
        self.fail('Timed out waiting for '+str(states)+': '+str(result))

    async def test_models_quota_and_actual_model(self):
        health=await self.bridge.health(True)
        self.assertEqual(health['default_model'],'fixture-model')
        self.assertEqual(health['quota']['limits'][0]['windows'][0]['remaining_percent'],75)
        job=self.bridge.submit('metadata',str(self.project),'read-only','metadata',model='chosen-model')
        result=await self.finish(job)
        self.assertEqual(result['actual_model'],'chosen-model')

    async def test_live_question_reply_and_duplicate_answer(self):
        job=self.bridge.submit('ASK_ME',str(self.project),'read-only','question')
        waiting=await self.until(job['job_id'],{'waiting_input'})
        question=waiting['questions'][0]
        with self.assertRaisesRegex(ValueError,'already active'):
            self.bridge.submit('another',str(self.project),'read-only','busy')
        with self.assertRaises(ValueError):
            self.bridge.reply(job['job_id'],question['question_id'],{'wrong':'A'})
        self.bridge.reply(job['job_id'],question['question_id'],{'answer':'A'})
        self.bridge.reply(job['job_id'],question['question_id'],{'answer':'A'})
        with self.assertRaisesRegex(ValueError,'different answer'):
            self.bridge.reply(job['job_id'],question['question_id'],{'answer':'B'})
        result=await self.until(job['job_id'],{'succeeded'})
        self.assertEqual(result['attempts'],1)
        self.assertIn('Received answer',self.bridge.result(job['job_id'])['output'])

    async def test_native_question_and_live_steering(self):
        job=self.bridge.submit('NATIVE_QUESTION',str(self.project),'read-only','native')
        waiting=await self.until(job['job_id'],{'waiting_input'})
        self.bridge.reply(job['job_id'],waiting['questions'][0]['question_id'],{'choice':'blue'})
        await self.until(job['job_id'],{'succeeded'})
        job=self.bridge.submit('STEER_ME',str(self.project),'read-only','steer')
        for _ in range(100):
            if self.bridge.get(job['job_id'])['turn_id']:break
            await asyncio.sleep(.05)
        self.bridge.steer(job['job_id'],'Use blue','steer-1')
        await self.until(job['job_id'],{'succeeded'})
        self.assertEqual(self.bridge.steer(job['job_id'],'Use blue','steer-1')['state'],'delivered')
        self.assertIn('Use blue',self.bridge.result(job['job_id'])['output'])

    async def test_transient_retry_resumes_and_has_a_cap(self):
        job=self.bridge.submit('TRANSIENT',str(self.project),'workspace-write','retry')
        result=await self.until(job['job_id'],{'succeeded'},16)
        self.assertEqual(result['attempts'],2)
        self.assertTrue(json.loads(self.bridge.result(job['job_id'])['output'])['continued'])
        job=self.bridge.submit('ALWAYS_TRANSIENT',str(self.project),'read-only','retry-cap',max_retries=1)
        result=await self.until(job['job_id'],{'failed'},16)
        self.assertEqual(result['attempts'],2)

    async def test_quota_errors_do_not_retry(self):
        job=self.bridge.submit('QUOTA_FAIL',str(self.project),'read-only','quota')
        result=await self.finish(job)
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['attempts'],1)
        self.assertEqual(result['error_kind'],'usageLimitExceeded')

    async def test_context_cursor_and_bounded_report(self):
        job=self.bridge.submit('LONG_REPORT',str(self.project),'read-only','long')
        await self.finish(job)
        status=await self.bridge.status(job['job_id'],0)
        while status.get('more_events'):
            status=await self.bridge.status(job['job_id'],0,status['cursor'])
        unchanged=await self.bridge.status(job['job_id'],0,status['cursor'])
        self.assertTrue(unchanged['unchanged'])
        self.assertNotIn('progress',unchanged)
        result=self.bridge.result(job['job_id'])
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['output']),2000)
        self.assertLessEqual(len(self.bridge.result(job['job_id'],0,8000,'full')['output']),8000)

    async def test_cancel_waiting_and_expired_question(self):
        job=self.bridge.submit('ASK_ME',str(self.project),'read-only','cancel-question')
        status=await self.until(job['job_id'],{'waiting_input'})
        question=status['questions'][0]['question_id']
        self.bridge.cancel(job['job_id'])
        result=await self.until(job['job_id'],{'cancelled'})
        self.assertEqual(result['attempts'],1)
        with self.assertRaisesRegex(ValueError,'expired'):
            self.bridge.reply(job['job_id'],question,{'answer':'A'})

    async def test_legacy_database_is_migrated_without_losing_jobs(self):
        job=self.bridge.submit('legacy report',str(self.project),'read-only','legacy')
        await self.finish(job)
        for worker in self.bridge._workers:worker.wait(timeout=3)
        added=['actual_model','attempts','max_retries','lease','turn_id','worker_started','retry_at','error_kind']
        with self.bridge.db() as db:
            for col in added:db.execute('ALTER TABLE jobs DROP COLUMN '+col)
        reopened=module.Bridge(self.config)
        self.assertEqual(reopened.get(job['job_id'])['session_id'],'session-fixture-123')
        self.assertEqual(reopened.get(job['job_id'])['state'],'succeeded')
        with self.bridge.db() as db:
            columns={row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
        self.assertTrue({'actual_model','attempts','max_retries','lease'}<=columns)
        before=self.bridge.list_jobs()
        module.Bridge(self.config)
        self.assertEqual(before,self.bridge.list_jobs())

    async def test_dead_worker_recovers_exact_session_without_duplicate_launch(self):
        job=self.bridge.submit('CRASH_RECOVER',str(self.project),'read-only','crash')
        for _ in range(100):
            if (self.project/'.started').exists():break
            await asyncio.sleep(.05)
        row=self.bridge.get(job['job_id'])
        os.kill(row['worker_pid'],signal.SIGKILL)
        for worker in self.bridge._workers:worker.wait(timeout=3)
        self.bridge.update(job['job_id'],created=time.time()-30,worker_started=time.time()-30)
        self.bridge.reap()
        lease=self.bridge.get(job['job_id'])['lease']
        module.Bridge(self.config).reap()
        self.assertEqual(lease,self.bridge.get(job['job_id'])['lease'])
        result=await self.until(job['job_id'],{'succeeded'})
        self.assertEqual(result['attempts'],2)
        self.assertTrue(json.loads(self.bridge.result(job['job_id'])['output'])['continued'])

    async def test_question_timeout_and_cancellation_during_retry(self):
        job=self.bridge.submit('ASK_ME',str(self.project),'read-only','question-timeout')
        waiting=await self.until(job['job_id'],{'waiting_input'})
        with self.bridge.db() as db:
            db.execute('UPDATE questions SET expires=? WHERE job_id=?',(time.time()-1,job['job_id']))
        result=await self.until(job['job_id'],{'failed'})
        self.assertEqual(result['error_kind'],'question_timeout')
        self.assertEqual(result['attempts'],1)
        job=self.bridge.submit('ALWAYS_TRANSIENT',str(self.project),'read-only','retry-cancel')
        await self.until(job['job_id'],{'retry_wait'})
        self.bridge.cancel(job['job_id'])
        result=await self.until(job['job_id'],{'cancelled'})
        self.assertEqual(result['attempts'],1)

    async def test_error_classification_and_unknown_quota(self):
        from worker import classify,normalize_quota
        self.assertFalse(classify({'codexErrorInfo':{'httpConnectionFailed':{'httpStatusCode':401}}}).retry)
        self.assertFalse(classify({'codexErrorInfo':'contextWindowExceeded'}).retry)
        quota=normalize_quota({'rateLimits':{'limitId':'unknown','primary':{'usedPercent':None}}})
        self.assertIsNone(quota['limits'][0]['windows'][0]['remaining_percent'])
        self.assertFalse(normalize_quota({})['available'])

    async def test_worker_loss_with_no_retry_emits_terminal_event(self):
        job=self.bridge.submit('CRASH_RECOVER',str(self.project),'read-only','no-recovery',max_retries=0)
        for _ in range(100):
            if (self.project/'.started').exists():break
            await asyncio.sleep(.05)
        row=self.bridge.get(job['job_id'])
        cursor=self.bridge.public(row)['cursor']
        os.kill(row['worker_pid'],signal.SIGKILL)
        for worker in self.bridge._workers:worker.wait(timeout=3)
        self.bridge.update(job['job_id'],created=time.time()-30,worker_started=time.time()-30)
        result=await self.bridge.status(job['job_id'],0,cursor)
        self.assertEqual(result['state'],'interrupted')
        self.assertFalse(result.get('unchanged',False))
        self.assertEqual(result['attempts'],1)

    async def test_unconfirmed_instruction_is_visible_on_turn_close(self):
        with patch.object(self.bridge,'start_worker'):
            job=self.bridge.submit('instruction race',str(self.project),'read-only','undelivered')
        self.bridge.update(job['job_id'],state='running',turn_id='fixture-turn')
        message=self.bridge.steer(job['job_id'],'Late instruction','late-message')
        self.bridge.update(job['job_id'],state='succeeded')
        self.bridge.expire_requests(job['job_id'])
        with self.bridge.db() as db:
            row=db.execute('SELECT state FROM messages WHERE id=?',(message['message_id'],)).fetchone()
            event=db.execute("SELECT kind FROM events WHERE job_id=? ORDER BY seq DESC LIMIT 1",(job['job_id'],)).fetchone()
        self.assertEqual(row['state'],'failed')
        self.assertEqual(event['kind'],'instruction_failed')


if __name__=='__main__':
    unittest.main(verbosity=2)
