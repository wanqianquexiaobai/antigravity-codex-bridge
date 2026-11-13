"""App Server worker: bidirectional questions, bounded retries and compact events."""
import asyncio
import json
import os
import signal
import time
import uuid

from app_rpc import AppServer, RpcError, TransportError
from job_store import ACTIVE, clip

ASK_TOOL = {'type':'function', 'name':'ask_gemini',
    'description':'Ask the supervising Gemini for a missing requirement or design decision and wait for its answer. Never use for credentials or permission escalation.',
    'inputSchema':{'type':'object','properties':{
        'question':{'type':'string','maxLength':2000},
        'options':{'type':'array','items':{'type':'string','maxLength':200},'maxItems':5}},
        'required':['question','options'],'additionalProperties':False}}


class TaskFailure(Exception):
    def __init__(self, message, kind='task_failed', retry=False):
        super().__init__(message)
        self.kind, self.retry = kind, retry


def classify(error):
    info = error.get('codexErrorInfo') or 'other'
    kind = next(iter(info), 'other') if isinstance(info,dict) else str(info)
    detail = info.get(kind) if isinstance(info,dict) else {}
    http = (detail or {}).get('httpStatusCode') if isinstance(detail,dict) else None
    transient = kind in {'serverOverloaded','internalServerError','httpConnectionFailed',
        'responseStreamConnectionFailed','responseStreamDisconnected','responseTooManyFailedAttempts'}
    if http is not None and http < 500:
        transient = False
    return TaskFailure(clip(error.get('message','Codex turn failed'),600), kind, transient)


def normalize_quota(raw):
    buckets = raw.get('rateLimitsByLimitId') or {}
    if not buckets and raw.get('rateLimits'):
        buckets = {raw['rateLimits'].get('limitId') or 'codex':raw['rateLimits']}
    limits = []
    for key, value in list(buckets.items())[:12]:
        item = {'limit_id':key,'plan':value.get('planType'),
                'limit_reached':value.get('rateLimitReachedType'), 'windows':[]}
        for name in ('primary','secondary'):
            window = value.get(name)
            if window:
                used = window.get('usedPercent')
                item['windows'].append({'name':name,'used_percent':used,
                    'remaining_percent':max(0,100-used) if isinstance(used,(int,float)) else None,
                    'duration_minutes':window.get('windowDurationMins'),'resets_at':window.get('resetsAt')})
        credits = value.get('credits')
        if credits is not None:
            item['credits'] = {k:credits.get(k) for k in ('hasCredits','unlimited','balance')}
        limits.append(item)
    return {'available':bool(limits),'observed_at':time.time(),'limits':limits,
            'note':'Provider quota windows, not a count of remaining tokens. Missing values are unknown.'}


async def inspect_account(bridge, refresh=False):
    cached = bridge.cache('account')
    if cached and not refresh and time.time()-cached['observed_at'] < 60:
        result = dict(cached)
    else:
        result = {'observed_at':time.time(), 'authenticated':None, 'default_model':None,
                  'models':[], 'metadata_error':None}
        server = AppServer(bridge.binary, bridge.environment())
        try:
            await server.start()
            account = await server.call('account/read')
            result['authenticated'] = account.get('account') is not None
            config, models = await asyncio.gather(server.call('config/read'), server.call('model/list',{'limit':50}))
            result['models'] = [{'model':m.get('model') or m.get('id'),'name':m.get('displayName'),
                                 'is_default':m.get('isDefault',False)} for m in models.get('data',[])[:50]]
            configured = config.get('config',{}).get('model')
            result['default_model'] = configured or next((m['model'] for m in result['models'] if m['is_default']),None)
            result['default_model_source'] = 'config' if configured else 'model_catalog'
            try:
                bridge.cache('quota',normalize_quota(await server.call('account/rateLimits/read')))
            except (RpcError,TransportError) as exc:
                bridge.cache('quota',{'available':False,'observed_at':time.time(),'limits':[], 'reason':clip(exc,300)})
        except (RpcError,TransportError,OSError) as exc:
            result['metadata_error'] = clip(exc,300)
        finally:
            await server.close()
        bridge.cache('account',result)
    quota = bridge.cache('quota') or {'available':False,'limits':[],'observed_at':None}
    quota = {**quota,'stale':quota.get('observed_at') is None or time.time()-quota['observed_at']>60}
    return {**result,'bridge_version':'2.0.0','environment':'WSL '+bridge.config['wsl_distribution'],
            'max_concurrent_jobs':1, 'max_retries':bridge.setting('max_retries',2,0,3),
            'subagents_enabled':False,'quota':quota,'workspace_roots':bridge.config['workspace_roots'],
            'login_command':str(bridge.config['launcher'])+' login --device-auth'}


def add_question(bridge, job, message):
    params = message.get('params',{})
    if message['method']=='item/tool/call':
        if params.get('tool')!='ask_gemini':
            raise ValueError('Unknown delegated tool')
        args = params.get('arguments',{})
        if isinstance(args,str):
            args = json.loads(args)
        questions = [{'id':'answer','question':str(args.get('question','')),
                      'options':[{'label':str(x)} for x in args.get('options',[])]}]
    else:
        questions = params.get('questions',[])
    if not 1<=len(questions)<=3 or any(q.get('isSecret') for q in questions):
        raise ValueError('Only 1–3 non-secret clarification questions are supported.')
    clean = []
    for q in questions:
        if not q.get('id') or not q.get('question') or len(q['question'])>2000:
            raise ValueError('Invalid or oversized clarification question.')
        clean.append({'id':clip(q['id'],120),'question':q['question'],
                      'options':[{'label':clip(x.get('label'),200),'description':clip(x.get('description'),300)}
                                 for x in (q.get('options') or [])[:5]]})
    question_id = str(uuid.uuid4())
    expires = min(job['deadline'],time.time()+bridge.setting('question_timeout_seconds',600,10,3600))
    with bridge.db() as db:
        if db.execute("SELECT count(*) FROM questions WHERE job_id=? AND state IN ('pending','answered')",(job['id'],)).fetchone()[0]>=3:
            raise ValueError('Too many pending clarification requests.')
        db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,NULL,'pending',?,?)",
            (question_id,job['id'],job['attempts'],json.dumps(message['id']),message['method'],
             json.dumps(clean),time.time(),expires))
    bridge.update(job['id'],state='waiting_input')
    bridge.emit(job['id'],'question','Codex needs a decision. Reply using codex_reply and the supplied question_id.')


async def deliver_inputs(bridge, server, job):
    with bridge.db() as db:
        questions = db.execute("SELECT * FROM questions WHERE job_id=? AND state IN ('pending','answered')",(job['id'],)).fetchall()
        messages = db.execute("SELECT * FROM messages WHERE job_id=? AND state='pending' ORDER BY created",(job['id'],)).fetchall()
    for row in questions:
        if time.time()>=row['expires']:
            raise TaskFailure('Clarification answer timed out; resume after deciding how to proceed.','question_timeout')
        if row['state']!='answered':
            continue
        answers = json.loads(row['answer'])
        result = {'contentItems':[{'type':'inputText','text':json.dumps(answers,ensure_ascii=False)}], 'success':True} if row['method']=='item/tool/call' else {'answers':{k:{'answers':[v]} for k,v in answers.items()}}
        await server.send({'id':json.loads(row['rpc_id']),'result':result})
        with bridge.db() as db:
            db.execute("UPDATE questions SET state='delivered' WHERE id=? AND state='answered'",(row['id'],))
        bridge.emit(job['id'],'answer_delivered','Answer delivered; Codex is continuing the same turn.')
    with bridge.db() as db:
        pending = db.execute("SELECT count(*) FROM questions WHERE job_id=? AND state IN ('pending','answered')",(job['id'],)).fetchone()[0]
    if not pending and bridge.get(job['id'])['state']=='waiting_input':
        bridge.update(job['id'],state='running')
    for row in messages:
        try:
            await server.call('turn/steer', {'threadId':job['session_id'], 'expectedTurnId':job['turn_id'],
                'input':[{'type':'text','text':row['content']}]})
            state,error = 'delivered',''
        except (RpcError,TransportError) as exc:
            state,error = 'failed',clip(exc,300)
        with bridge.db() as db:
            db.execute('UPDATE messages SET state=?,error=? WHERE id=?',(state,error,row['id']))
        bridge.emit(job['id'],'instruction_'+state,'Additional instruction '+state+'. '+error)


async def attempt(bridge, job_id, server):
    job = bridge.get(job_id)
    await server.start()
    bridge.update(job_id,child_pid=server.process.pid,child_identity=bridge.identity(server.process.pid))
    instructions = bridge.role + '''
The ask_gemini tool is your live clarification channel. Use it when a necessary
requirement or material design decision is missing. Continue after its reply.
Do not ask for secrets, credentials, or permission escalation through this tool.
Do not launch nested Codex processes or subagents. Keep progress brief.
End with a concise report (aim under 2000 characters): work done, changed files,
actual checks and results, unresolved issues. Never hide failed/unrun checks.
'''
    params = {'cwd':job['cwd'],'approvalPolicy':'never','sandbox':job['mode'],
              'developerInstructions':instructions,'config':{'agents.enabled':False,
              'allow_login_shell':False,'features.shell_snapshot':False,
              'features.default_mode_request_user_input':True}}
    if job['model']:
        params['model'] = job['model']
    if job['session_id']:
        params.update({'threadId':job['session_id'],'excludeTurns':True})
        opened = await server.call('thread/resume',params)
    else:
        params['dynamicTools'] = [ASK_TOOL]
        opened = await server.call('thread/start',params)
    session_id = opened['thread']['id']
    bridge.update(job_id,session_id=session_id,actual_model=opened.get('model'))
    bridge.emit(job_id,'model','Using model: '+str(opened.get('model','unknown')))
    prompt = job['prompt']
    if job['attempts']>1:
        prompt = ('Recover from an interrupted attempt of the SAME task. Inspect existing changes and completed actions first. '
                  'Do not blindly repeat side effects. Preserve clarification answers and supervising feedback already in '
                  'this session. The original task below is background, not an override of newer decisions.\nOriginal task:\n')+prompt
    started = await server.call('turn/start',{'threadId':session_id,'input':[{'type':'text','text':prompt}], 'approvalPolicy':'never'})
    bridge.update(job_id,turn_id=started['turn']['id'])
    final_text, last_text = '', ''
    while True:
        job = bridge.get(job_id)
        await deliver_inputs(bridge,server,job)
        try:
            message = await asyncio.wait_for(server.events.get(),0.2)
        except asyncio.TimeoutError:
            continue
        method, params = message.get('method',''), message.get('params',{})
        if method=='_disconnected':
            raise TransportError('App Server connection closed during the task')
        if 'id' in message:
            if method in ('item/tool/call','item/tool/requestUserInput','tool/requestUserInput'):
                try:
                    add_question(bridge,job,message)
                except (ValueError,TypeError) as exc:
                    await server.send({'id':message['id'],'error':{'code':-32602,'message':str(exc)}})
            elif method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
                await server.send({'id':message['id'],'result':{'decision':'decline'}})
            elif method=='item/permissions/requestApproval':
                await server.send({'id':message['id'],'result':{'permissions':{}}})
            else:
                await server.send({'id':message['id'],'error':{'code':-32601,'message':'Unsupported request; ask the user outside this worker.'}})
            continue
        if params.get('threadId') and params['threadId']!=session_id:
            continue
        if method=='account/rateLimits/updated':
            bridge.cache('quota',normalize_quota(params))
        elif method=='thread/tokenUsage/updated':
            usage = params.get('tokenUsage',{})
            bridge.update(job_id,usage=json.dumps({'session_total':usage.get('total'),
                'last_reported':usage.get('last'),'model_context_window':usage.get('modelContextWindow')}))
        elif method=='model/rerouted':
            bridge.update(job_id,actual_model=params.get('toModel'))
            bridge.emit(job_id,'model','Provider routed this turn to '+str(params.get('toModel')))
        elif method=='item/completed':
            item = params.get('item',{})
            kind = item.get('type')
            if kind=='agentMessage':
                last_text = str(item.get('text',''))[:1000000]
                if item.get('phase')=='final_answer':
                    final_text = last_text
                bridge.emit(job_id,'message',last_text)
            elif kind=='commandExecution':
                bridge.emit(job_id,'command','Command finished (exit '+str(item.get('exitCode'))+'): '+clip(item.get('command'),240))
            elif kind=='fileChange':
                paths = [str(c.get('path','')) for c in item.get('changes',[])][:6]
                bridge.emit(job_id,'files','Changed: '+', '.join(paths))
        elif method=='item/started':
            item = params.get('item',{})
            if item.get('type')=='commandExecution':
                bridge.emit(job_id,'command','Running: '+clip(item.get('command'),240))
        elif method=='serverRequest/resolved':
            with bridge.db() as db:
                db.execute("UPDATE questions SET state='expired' WHERE job_id=? AND rpc_id=? AND state='pending'",(job_id,json.dumps(params.get('requestId'))))
        elif method=='turn/completed':
            turn = params.get('turn',{})
            if turn.get('id')!=job['turn_id']:
                continue
            (bridge.job_dir(job_id)/'result.txt').write_text(final_text or last_text)
            if turn.get('status')=='completed':
                return
            if turn.get('status')=='interrupted':
                raise TaskFailure('Codex turn was interrupted. Review changes before continuing.','interrupted')
            raise classify(turn.get('error') or {})


async def run_worker(bridge, job_id, lease):
    os.umask(0o077)
    job = bridge.get(job_id)
    if job['lease']!=lease or job['state'] not in ACTIVE:
        return
    bridge.update(job_id,worker_pid=os.getpid(),worker_identity=bridge.identity(os.getpid()))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        loop.add_signal_handler(sig,stop.set)
    while True:
        job = bridge.get(job_id)
        if job['cancel_requested'] or stop.is_set() or time.time()>=job['deadline']:
            state = 'timed_out' if time.time()>=job['deadline'] else 'cancelled'
            bridge.update(job_id,state=state,error='Task stopped; existing file changes remain.')
            bridge.emit(job_id,state,'Task stopped; review existing changes before continuing.')
            bridge.expire_requests(job_id)
            return
        if job['attempts']>=job['max_retries']+1:
            bridge.update(job_id,state='failed',error='Retry budget exhausted.')
            bridge.emit(job_id,'failed','Retry budget exhausted.')
            return
        bridge.update(job_id,state='running',attempts=job['attempts']+1,turn_id=None,retry_at=None,error='')
        bridge.emit(job_id,'attempt','Starting attempt '+str(job['attempts']+1))
        failure = None
        with (bridge.job_dir(job_id)/'stderr.log').open('ab') as stderr:
            server = AppServer(bridge.binary,bridge.environment(),job['cwd'],stderr)
            task = asyncio.create_task(attempt(bridge,job_id,server))
            try:
                while not task.done():
                    current = bridge.get(job_id)
                    if current['cancel_requested'] or stop.is_set() or time.time()>=current['deadline']:
                        state = 'timed_out' if time.time()>=current['deadline'] else 'cancelled'
                        task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
                        await server.close()
                        bridge.update(job_id,state=state,error='Task stopped; existing file changes remain.',error_kind=state)
                        bridge.emit(job_id,state,'Task stopped; no automatic retry.')
                        return
                    await asyncio.sleep(0.2)
                await task
                await server.close()
                bridge.update(job_id,state='succeeded',returncode=0,error='',error_kind='')
                bridge.emit(job_id,'succeeded','Codex finished this turn. Inspect the report and its actual test evidence.')
                return
            except TransportError as exc:
                failure = TaskFailure(str(exc),'transport',True)
            except RpcError as exc:
                data = exc.error.get('data')
                failure = classify({'message':str(exc),'codexErrorInfo':data.get('codexErrorInfo','other') if isinstance(data,dict) else 'other'})
            except TaskFailure as exc:
                failure = exc
            except Exception as exc:
                failure = TaskFailure(type(exc).__name__+': '+str(exc),'worker_error')
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
                await server.close()
                bridge.expire_requests(job_id)
        current = bridge.get(job_id)
        bridge.update(job_id,error=clip(failure,600),error_kind=failure.kind)
        if not failure.retry or current['attempts']>=current['max_retries']+1:
            bridge.update(job_id,state='failed',returncode=1)
            bridge.emit(job_id,'failed',str(failure))
            return
        delay = min(30, bridge.setting('retry_base_seconds',2,1,30)*2**(current['attempts']-1))
        bridge.update(job_id,state='retry_wait',retry_at=time.time()+delay)
        bridge.emit(job_id,'retry','Transient connection/service failure; retrying the saved session in '+str(delay)+' seconds.')
        until = time.monotonic()+delay
        while time.monotonic()<until:
            if stop.is_set() or bridge.get(job_id)['cancel_requested'] or time.time()>=current['deadline']:
                break
            await asyncio.sleep(0.2)
