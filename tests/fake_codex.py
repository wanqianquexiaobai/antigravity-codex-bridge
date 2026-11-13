"""Deterministic App Server fixture; never contacts a model service."""
import json
from pathlib import Path
import sys
import threading
import time

lock=threading.Lock()
prompt=''
session='session-fixture-123'
turn='turn-fixture-1'


def emit(value):
    with lock:
        print(json.dumps(value),flush=True)


def notify(method,params):
    emit({'method':method,'params':params})


def finish(text, error=None):
    notify('item/completed',{'threadId':session,'item':{'type':'agentMessage','phase':'final_answer','text':text}})
    notify('turn/completed',{'threadId':session,'turn':{'id':turn,'status':'failed' if error else 'completed','error':error}})


def run_task():
    time.sleep(.15)
    notify('thread/tokenUsage/updated',{'threadId':session,'tokenUsage':{'last':{'inputTokens':12,'outputTokens':5},'total':{'inputTokens':12,'outputTokens':5},'modelContextWindow':128000}})
    if 'QUOTA_FAIL' in prompt:
        finish('',{'message':'quota exhausted','codexErrorInfo':'usageLimitExceeded'});return
    if 'REPORT_FAILURE' in prompt:
        finish('',{'message':'fixture verification failed','codexErrorInfo':'badRequest'});return
    if 'TRANSIENT' in prompt:
        flag=Path.cwd()/'.attempts'
        count=int(flag.read_text())+1 if flag.exists() else 1
        flag.write_text(str(count))
        if 'ALWAYS_TRANSIENT' in prompt or count==1:
            finish('',{'message':'stream disconnected','codexErrorInfo':{'responseStreamDisconnected':{'httpStatusCode':503}}});return
    if 'CRASH_RECOVER' in prompt:
        flag=Path.cwd()/'.started'
        if not flag.exists():
            flag.write_text('started');return
    if 'WAIT_FOR_CANCEL' in prompt or 'STEER_ME' in prompt:
        return
    if 'ASK_ME' in prompt:
        emit({'id':'question-rpc','method':'item/tool/call','params':{'threadId':session,'turnId':turn,
             'tool':'ask_gemini','callId':'call-1','arguments':{'question':'Choose A or B','options':['A','B']}}});return
    if 'NATIVE_QUESTION' in prompt:
        emit({'id':'native-rpc','method':'item/tool/requestUserInput','params':{'threadId':session,'turnId':turn,
             'itemId':'q-native','isBlocking':True,'questions':[{'id':'choice','header':'Choice','question':'Pick a color','options':None}]}});return
    if 'LONG_REPORT' in prompt:
        finish('start\n'+'x'*20000+'\nfinal checks');return
    finish(json.dumps({'session':session,'cwd':str(Path.cwd()),'continued':continued}))


continued=False
for line in sys.stdin:
    message=json.loads(line)
    if message.get('id') in ('question-rpc','native-rpc') and 'method' not in message:
        finish('Received answer: '+json.dumps(message.get('result')))
        continue
    method=message.get('method');params=message.get('params',{})
    if 'id' not in message:
        continue
    result={}
    if method=='account/read':result={'account':{'type':'chatgpt'}}
    elif method=='config/read':result={'config':{'model':None}}
    elif method=='model/list':result={'data':[{'id':'fixture-model','model':'fixture-model','displayName':'Fixture','isDefault':True}]}
    elif method=='account/rateLimits/read':result={'rateLimits':{'limitId':'codex','primary':{'usedPercent':25,'windowDurationMins':300,'resetsAt':2000000000},'secondary':None}}
    elif method in ('thread/start','thread/resume'):
        continued=method=='thread/resume'
        session=params.get('threadId','session-fixture-123')
        assert params.get('config',{}).get('agents.enabled') is False
        result={'thread':{'id':session},'model':params.get('model','fixture-model')}
    elif method=='turn/start':
        prompt=params['input'][0]['text'];result={'turn':{'id':turn}}
    elif method=='turn/steer':
        assert params['expectedTurnId']==turn
        result={'turnId':turn}
    emit({'id':message['id'],'result':result})
    if method=='turn/start':threading.Thread(target=run_task,daemon=True).start()
    elif method=='turn/steer':finish('Steering received: '+params['input'][0]['text'])
