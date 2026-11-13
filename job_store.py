"""Durable jobs, bounded summaries, questions, and retry accounting."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

ACTIVE = ('queued', 'running', 'waiting_input', 'retry_wait')
TERMINAL = ('succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted')


def clip(value, limit):
    text = str(value or '')
    return text if len(text) <= limit else text[:max(0,limit-1)] + '…'


class JobStore:
    def migrate(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            additions = {'actual_model':'TEXT', 'attempts':'INTEGER DEFAULT 0',
                'max_retries':'INTEGER DEFAULT 2', 'retry_at':'REAL', 'error_kind':"TEXT DEFAULT ''",
                'turn_id':'TEXT', 'lease':"TEXT DEFAULT ''", 'worker_started':'REAL DEFAULT 0'}
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
            db.execute('''CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                kind TEXT NOT NULL, summary TEXT NOT NULL, created REAL NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS events_job_seq ON events(job_id,seq)')
            db.execute('''CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                rpc_id TEXT NOT NULL, method TEXT NOT NULL, payload TEXT NOT NULL,
                answer TEXT, state TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, content TEXT NOT NULL,
                state TEXT NOT NULL, error TEXT DEFAULT '', created REAL NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def setting(self, name, default, low, high):
        return max(low, min(high, int(self.config.get(name, default))))

    def cache(self, key, value=None):
        with self.db() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))
                return value
            row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else None

    def emit(self, job_id, kind, summary):
        summary = clip(summary, self.setting('progress_max_chars', 600, 100, 1500))
        with self.db() as db:
            last = db.execute('SELECT kind,summary FROM events WHERE job_id=? ORDER BY seq DESC LIMIT 1', (job_id,)).fetchone()
            if not last or (last['kind'], last['summary']) != (kind, summary):
                db.execute('INSERT INTO events(job_id,kind,summary,created) VALUES (?,?,?,?)',
                           (job_id, kind, summary, time.time()))
                db.execute('UPDATE jobs SET progress=?,updated=? WHERE id=?', (summary, time.time(), job_id))

    def start_worker(self, job_id, lease):
        directory = self.job_dir(job_id)
        directory.mkdir(exist_ok=True, mode=0o700)
        try:
            with (directory / 'worker.log').open('ab') as log:
                worker = subprocess.Popen([sys.executable, str(Path(__file__).with_name('bridge.py')),
                    '--config', str(self.config_path), 'worker', job_id, '--lease', lease],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                    close_fds=True, env=self.environment())
            self._workers.append(worker)
            with self.db() as db:
                db.execute('UPDATE jobs SET worker_pid=?,worker_identity=? WHERE id=? AND lease=?',
                    (worker.pid, self.identity(worker.pid), job_id, lease))
        except Exception as exc:
            self.update(job_id, state='failed', error=clip(exc, 600), error_kind='worker_start')
            self.emit(job_id, 'failed', 'Could not start the task worker.')
            raise

    def expire_requests(self, job_id):
        with self.db() as db:
            db.execute("UPDATE questions SET state='expired' WHERE job_id=? AND state IN ('pending','answered')", (job_id,))
            failed = db.execute("UPDATE messages SET state='failed',error='Turn ended or connection changed; inspect before sending again.' WHERE job_id=? AND state='pending'", (job_id,)).rowcount
        if failed:
            self.emit(job_id,'instruction_failed','Turn ended before instruction delivery was confirmed; inspect the report before sending again.')

    def reap(self):
        self._workers = [w for w in self._workers if w.poll() is None]
        launches = []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM jobs WHERE state IN ('queued','running','waiting_input','retry_wait')").fetchall()
            for row in rows:
                if time.time() - max(row['created'], row['worker_started'] or 0) < 15:
                    continue
                if row['worker_identity'] and self.identity(row['worker_pid']) == row['worker_identity']:
                    continue
                self.terminate_group(row['child_pid'], row['child_identity'], signal.SIGKILL)
                state = 'cancelled' if row['cancel_requested'] else 'timed_out' if time.time() >= row['deadline'] else None
                if state is None and row['attempts'] >= row['max_retries'] + 1:
                    state = 'interrupted'
                if state:
                    db.execute('UPDATE jobs SET state=?,updated=?,error=?,error_kind=? WHERE id=?',
                        (state, time.time(), 'Worker stopped; retry budget or task deadline reached.', 'worker_lost', row['id']))
                    db.execute('INSERT INTO events(job_id,kind,summary,created) VALUES (?,?,?,?)',
                        (row['id'],state,'Worker stopped; retry budget or task deadline reached.',time.time()))
                else:
                    lease = str(uuid.uuid4())
                    # A crash before registering an attempt still consumes one attempt.
                    db.execute("UPDATE jobs SET state='queued',lease=?,worker_started=?,worker_pid=NULL,worker_identity=NULL,attempts=max(attempts,1),error_kind='worker_lost' WHERE id=?",
                        (lease, time.time(), row['id']))
                    launches.append((row['id'], lease))
                db.execute("UPDATE questions SET state='expired' WHERE job_id=? AND state IN ('pending','answered')", (row['id'],))
                db.execute("UPDATE messages SET state='failed',error='Worker stopped; delivery may be unknown.' WHERE job_id=? AND state='pending'", (row['id'],))
        for job_id, lease in launches:
            self.emit(job_id, 'recovery', 'Recovering the saved session after a worker interruption; inspect existing changes.')
            self.start_worker(job_id, lease)

    def submit(self, prompt, cwd, mode, request_id, model=None, timeout_minutes=30, parent=None, max_retries=None):
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 100000:
            raise ValueError('Task must contain 1–100000 characters.')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', request_id or ''):
            raise ValueError('request_id must be a stable unique label (letters/digits/_.:-).')
        if mode not in ('read-only', 'workspace-write') or not 1 <= timeout_minutes <= 120:
            raise ValueError('Use read-only/workspace-write and a timeout between 1 and 120 minutes.')
        if model and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', model):
            raise ValueError('Invalid model identifier.')
        max_retries = self.setting('max_retries', 2, 0, 3) if max_retries is None else max_retries
        if not isinstance(max_retries, int) or not 0 <= max_retries <= self.setting('max_retries', 2, 0, 3):
            raise ValueError('max_retries exceeds the configured retry limit.')
        cwd = self.workspace(cwd)
        fingerprint = hashlib.sha256(json.dumps([prompt, cwd, mode, model, timeout_minutes, parent, max_retries], ensure_ascii=False).encode()).hexdigest()
        self.reap()
        job_id, lease, now = str(uuid.uuid4()), str(uuid.uuid4()), time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM jobs WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('request_id already exists with different arguments.')
                return self.public(dict(previous))
            session_id = None
            if parent:
                previous = db.execute('SELECT * FROM jobs WHERE id=?', (parent,)).fetchone()
                if not previous or previous['state'] not in TERMINAL or not previous['session_id']:
                    raise ValueError('Continue requires a finished job with a saved Codex session.')
                session_id = previous['session_id']
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','waiting_input','retry_wait')").fetchone()[0]:
                raise ValueError('A Codex job is already active; wait, answer its question, or cancel it.')
            db.execute('''INSERT INTO jobs (id,request_id,fingerprint,prompt,cwd,mode,model,parent_id,session_id,
                state,created,updated,deadline,max_retries,lease,worker_started)
                VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,?)''',
                (job_id,request_id,fingerprint,prompt,cwd,mode,model,parent,session_id,now,now,
                 now + timeout_minutes * 60,max_retries,lease,now))
        self.emit(job_id, 'queued', 'Task accepted.')
        self.start_worker(job_id, lease)
        return self.public(self.get(job_id))

    def questions(self, job_id):
        with self.db() as db:
            rows = db.execute("SELECT * FROM questions WHERE job_id=? AND state='pending' ORDER BY created", (job_id,)).fetchall()
        return [{'question_id': row['id'], 'questions': json.loads(row['payload']),
                 'expires_at': row['expires']} for row in rows]

    def public(self, job):
        keys = ('request_id','session_id','state','mode','model','actual_model','attempts','max_retries',
                'retry_at','deadline','error_kind','turn_id')
        with self.db() as db:
            cursor = db.execute('SELECT coalesce(max(seq),0) FROM events WHERE job_id=?', (job['id'],)).fetchone()[0]
        return {'job_id': job['id'], **{key: job.get(key) for key in keys}, 'cursor': cursor,
                'progress': clip(job['progress'], self.setting('progress_max_chars',600,100,1500)),
                'error': clip(job['error'],600), 'usage': json.loads(job['usage']),
                'next_action': 'codex_reply' if job['state']=='waiting_input' else 'codex_status' if job['state'] in ACTIVE else 'codex_result'}

    async def status(self, job_id, wait_seconds=0, after_cursor=None):
        if not 0 <= wait_seconds <= 30 or (after_cursor is not None and after_cursor < 0):
            raise ValueError('Invalid wait_seconds or after_cursor.')
        until = time.monotonic() + wait_seconds
        while True:
            self.reap()
            job = self.get(job_id)
            public = self.public(job)
            changed = after_cursor is None or public['cursor'] > after_cursor
            if job['state'] in TERMINAL or job['state']=='waiting_input' or (after_cursor is not None and changed) or time.monotonic() >= until:
                break
            await asyncio.sleep(0.2)
        if not changed:
            return {'job_id':job_id, 'state':job['state'], 'cursor':public['cursor'],
                    'unchanged':True, 'next_action':public['next_action']}
        with self.db() as db:
            events = db.execute('SELECT seq,kind,summary FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?',
                (job_id, after_cursor or 0, self.setting('status_event_limit',4,1,8))).fetchall()
        delivered = events[-1]['seq'] if events else public['cursor']
        public.update({'events':[dict(e) for e in events], 'cursor':delivered,
                       'more_events':delivered < public['cursor'], 'questions':self.questions(job_id)})
        return public

    def result(self, job_id, offset=0, limit=4000, view='summary'):
        if offset < 0 or not 1 <= limit <= 8000 or view not in ('summary','full'):
            raise ValueError('Use summary/full, nonnegative offset, and limit 1–8000.')
        job = self.get(job_id)
        path = self.job_dir(job_id) / 'result.txt'
        content = path.read_text(errors='replace') if path.exists() else ''
        cap = self.setting('result_summary_chars',2000,300,4000)
        if view == 'summary':
            marker = '\n[…report truncated; use view="full" to inspect all checks…]\n'
            budget = cap-len(marker)
            output = content if len(content) <= cap else content[:budget-budget//4] + marker + content[-(budget//4):]
            next_offset = None
        else:
            output = content[offset:offset+limit]
            next_offset = offset+limit if offset+limit < len(content) else None
        return {**self.public(job), 'output':output,'view':view, 'report_characters':len(content),
                'truncated':view=='summary' and len(content)>cap, 'next_offset':next_offset,
                'complete':job['state'] in TERMINAL}

    def cancel(self, job_id):
        self.get(job_id)
        with self.db() as db:
            db.execute("UPDATE jobs SET cancel_requested=1,updated=? WHERE id=? AND state IN ('queued','running','waiting_input','retry_wait')", (time.time(),job_id))
        return self.public(self.get(job_id))

    def list_jobs(self, limit=10):
        if not 1 <= limit <= 50:
            raise ValueError('limit must be 1–50.')
        self.reap()
        with self.db() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
        return [self.public(dict(row)) for row in rows]

    def reply(self, job_id, question_id, answers):
        if not isinstance(answers,dict) or not answers or any(not isinstance(k,str) or not isinstance(v,str) or not v.strip() or len(v)>4000 for k,v in answers.items()):
            raise ValueError('answers maps each question id to a nonempty string (maximum 4000 characters).')
        encoded = json.dumps(answers,sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM questions WHERE id=? AND job_id=?',(question_id,job_id)).fetchone()
            job = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not row or not job:
                raise ValueError('Unknown question_id for this job.')
            if row['answer'] is not None:
                if row['answer'] != encoded:
                    raise ValueError('This question already has a different answer.')
                return {'job_id':job_id,'question_id':question_id,'state':row['state']}
            if row['state']!='pending' or job['state'] not in ACTIVE or row['expires']<=time.time():
                raise ValueError('This question has expired; fetch the current task status.')
            if set(answers) != {q['id'] for q in json.loads(row['payload'])}:
                raise ValueError('Answer every question using its exact id.')
            db.execute("UPDATE questions SET answer=?,state='answered' WHERE id=?",(encoded,question_id))
        self.emit(job_id,'reply_queued','Gemini supplied an answer; awaiting delivery to Codex.')
        return {'job_id':job_id,'question_id':question_id,'state':'answered'}

    def steer(self, job_id, message, request_id):
        if not isinstance(message,str) or not 1 <= len(message.strip()) <= 8000 or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}',request_id or ''):
            raise ValueError('Use a nonempty message (up to 8000 characters) and a unique request_id.')
        key = job_id + ':' + request_id
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM messages WHERE id=?',(key,)).fetchone()
            if old:
                if old['content']!=message:
                    raise ValueError('request_id already exists with different arguments.')
                return {'message_id':key,'state':old['state'],'error':old['error']}
            job = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not job or job['state']!='running' or not job['turn_id']:
                raise ValueError('Only a running turn accepts steering; use codex_reply for a question.')
            if db.execute("SELECT count(*) FROM messages WHERE job_id=? AND state='pending'",(job_id,)).fetchone()[0]>=3:
                raise ValueError('Too many pending instructions; wait for delivery.')
            db.execute("INSERT INTO messages(id,job_id,content,state,created) VALUES (?,?,?,'pending',?)",(key,job_id,message,time.time()))
        return {'message_id':key,'state':'pending','next_action':'codex_status'}

    async def health(self, refresh=False):
        from worker import inspect_account
        return await inspect_account(self, refresh)

    async def worker(self, job_id, lease):
        from worker import run_worker
        await run_worker(self, job_id, lease)
