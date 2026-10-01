#!/usr/bin/env python3
"""Single-writer chat queue over control events; Codex runs only on the trusted control VM."""
import argparse
import fcntl
import hashlib
import json
import os
from jsonschema import Draft7Validator, ValidationError
from pathlib import Path
import sys
import threading
import time
import uuid

PLATFORM=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PLATFORM))
from observability import OperationError
from poc.intake import SECRET_TEXT
from process import run_bounded
from runner.runtime_boundary import private_directory
from runner.run_agent import lifecycle, load_yaml, run_codex
from storage import durable_write

MAX_REQUEST=28*1024
MAX_REPLY=8000
MODEL='gpt-6.1-sol'
EVENTS={'control.chat.queued','control.chat.started','control.chat.result'}
TOPOLOGY_SCHEMA=json.loads((PLATFORM/'schemas/topology-draft.schema.json').read_text())
SCHEMA={'type':'object','properties':{'reply':{'type':'string','minLength':1,'maxLength':8000},'proposal':{'anyOf':[TOPOLOGY_SCHEMA,{'type':'null'}]}},
        'required':['reply'],'additionalProperties':False}
PROMPT=Path(__file__).with_name('prompts')/'messenger.md'

def system_prompt():
    try:
        value=PROMPT.read_text()
        if not value.strip() or len(value.encode())>16000: raise ValueError('invalid trusted prompt')
        return value
    except (OSError,ValueError) as exc:
        raise failure('SDK_CONFIG_INVALID','prompt',retry_policy='after_configuration',cause=exc) from exc


def failure(code,phase,**kwargs):
    return OperationError(code,component='control.chat',phase=phase,**kwargs)


def encoded(value): return json.dumps(value,sort_keys=True).encode()
def digest(value): return hashlib.sha256(encoded(value)).hexdigest()


def write_once(path,value):
    data=encoded(value)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes()!=data:
            raise failure('STATE_EVIDENCE_MISMATCH','artifact',retry_policy='after_reconcile')
        return
    durable_write(path,data)


def read_artifact(path,sha=None):
    try:
        if path.is_symlink(): raise ValueError('symlink artifact')
        raw=path.read_bytes()
        if sha and hashlib.sha256(raw).hexdigest()!=sha: raise ValueError('artifact changed')
        return json.loads(raw)
    except (OSError,ValueError) as exc:
        raise failure('STATE_EVIDENCE_MISMATCH','artifact',retry_policy='after_reconcile',cause=exc) from exc


def validate_request(request):
    if not isinstance(request,dict) or set(request)!={'workspace_id','message_id','request_sha256','messages','context'}:
        raise failure('CONTROL_CONFIG_INVALID','request')
    for key in ('workspace_id','message_id'):
        try:
            if str(uuid.UUID(request[key]))!=request[key]: raise ValueError()
        except (ValueError,TypeError,AttributeError) as exc:
            raise failure('CONTROL_CONFIG_INVALID','identity',cause=exc) from exc
    payload={k:request[k] for k in ('workspace_id','messages','context')}
    if request['request_sha256']!=digest(payload) or len(encoded(request))>MAX_REQUEST:
        raise failure('STATE_BINDING_MISMATCH','request')
    messages=request['messages']
    if (not isinstance(messages,list) or not 1<=len(messages)<=17 or not isinstance(request['context'],dict)
            or not isinstance(messages[-1],dict) or messages[-1].get('role')!='user'):
        raise failure('CONTROL_CONFIG_INVALID','request')
    for message in messages:
        if (not isinstance(message,dict) or set(message)!={'role','content'} or message['role'] not in ('user','assistant')
                or not isinstance(message['content'],str) or not message['content'].strip()
                or len(message['content'].encode())>MAX_REPLY or SECRET_TEXT.search(message['content'].encode())):
            raise failure('SDK_POLICY_DENIED','request')
    if SECRET_TEXT.search(encoded(request['context'])): raise failure('SDK_POLICY_DENIED','context')
    return request


def proposal_review(proposal,context):
    """Schema/identity validation permits rendering, never side effects or an Allow."""
    if proposal is None: return None
    try:
        Draft7Validator(TOPOLOGY_SCHEMA).validate(proposal)
        ids=[node['id'] for node in proposal['nodes']]
        if len(set(ids))!=len(ids) or any(e['from'] not in ids or e['to'] not in ids for e in proposal['edges']):
            raise ValueError('invalid graph identity')
        known=set(context.get('evidence_refs',[]))
        if any(ref not in known for ref in proposal['evidence_refs']): raise ValueError('unbound evidence reference')
    except (ValidationError,ValueError,TypeError) as exc:
        raise failure('SDK_OUTPUT_INVALID','proposal',outcome='FAIL',side_effect='completed',cause=exc) from exc
    contract=context.get('trusted_contract',{})
    owners=contract.get('topology_owners',{})
    conflicts=[{'node_id':n['id'],'reason':'ownership_not_supported'} for n in proposal['nodes']
               if n['managed_by'] not in owners.get(n['kind'],[])]
    limit=contract.get('app_schema',{}).get('max_services')
    if type(limit) is int and sum(n['kind']=='service' for n in proposal['nodes'])>limit:
        conflicts.append({'reason':'service_limit_exceeded','limit':limit})
    # The actual apply dispatcher is not connected to this chat surface.
    return {'status':'REVIEW_REQUIRED','apply_supported':False,'unsupported':['topology_apply_not_connected'],
            'conflicts':conflicts,'evidence_refs':proposal['evidence_refs'],
            'contract_sha256':digest(contract)}


def run_once(request,root):
    """Remote trusted CLI. No credentials are read or copied by this bridge."""
    validate_request(request)
    if sys.platform!='linux': raise failure('SDK_CONFIG_INVALID','platform',retry_policy='after_configuration')
    root=private_directory(root)
    directory=root/request['message_id']
    if directory.exists():
        original=read_artifact(directory/'request.json')
        if original!=request: raise failure('STATE_BINDING_MISMATCH','replay')
        if (directory/'result.json').exists(): return read_artifact(directory/'result.json')
        raise failure('SDK_OUTCOME_UNKNOWN','replay',outcome='UNKNOWN',side_effect='unknown',retry_policy='after_reconcile')
    directory.mkdir(mode=0o700)
    write_once(directory/'request.json',request)
    workspace=private_directory(directory/'context'); run=private_directory(directory/'run')
    write_once(workspace/'context.json',request['context'])
    profile=load_yaml(PLATFORM/'runner/profiles.yaml'); cfg=profile['providers']['codex']
    if cfg.get('model')!=MODEL: raise failure('SDK_CONFIG_INVALID','model')
    state,emit=lifecycle(run,'chat','codex',MODEL)
    result={'message_id':request['message_id'],'request_sha256':request['request_sha256']}
    emit('agent.started',status='running')
    phase='invoke'
    try:
        prompt=system_prompt()
        out,meta=run_codex(cfg,prompt,json.dumps({'conversation':request['messages'],'observed_context':request['context']},ensure_ascii=False),
                           SCHEMA,workspace,run,profile['read_deny'],emit)
        phase='output'
        if (not isinstance(out,dict) or set(out)-{'reply','proposal'} or not isinstance(out['reply'],str)
                or not out['reply'].strip() or len(out['reply'].encode())>MAX_REPLY):
            raise failure('SDK_OUTPUT_INVALID','output',outcome='FAIL',side_effect='completed')
        review=proposal_review(out.get('proposal'),request['context'])
        safe_meta={k:meta.get(k) for k in ('session_id','thread_id','turn_id','duration_ms','conversation_resume')}
        safe_meta.update(provider='codex',model=MODEL,prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
        result.update(outcome='PASS',reply=out['reply'],meta=safe_meta,error=None)
        if out.get('proposal') is not None: result.update(proposal=out['proposal'],proposal_review=review)
        emit('agent.completed',status='completed')
    except Exception as exc:
        detail=exc if isinstance(exc,OperationError) else failure(
            'SDK_OUTCOME_UNKNOWN' if phase=='invoke' else 'SDK_OUTPUT_INVALID',phase,
            outcome='UNKNOWN' if phase=='invoke' else 'FAIL',side_effect='unknown' if phase=='invoke' else 'completed',
            retry_policy='after_reconcile',cause=exc)
        result.update(outcome=detail.outcome,error=detail.as_dict())
        emit('agent.unknown' if detail.outcome=='UNKNOWN' else 'agent.failed',status='failed',error=detail)
    if len(json.dumps(result,ensure_ascii=False).encode())>20*1024:
        raise failure('SDK_OUTPUT_INVALID','output',outcome='FAIL',side_effect='completed')
    write_once(directory/'result.json',result)
    return result


class ChatQueue:
    """Events are queue/status authority; files contain immutable request/result artifacts only."""
    def __init__(self,root,store,ctx,workspace_id,remote_workspace_id,command,context,*,transport=run_bounded):
        self.root=private_directory(root); self.store=store; self.ctx=ctx; self.workspace_id=workspace_id
        self.remote_workspace_id=remote_workspace_id; self.command=command; self.context=context; self.transport=transport
        self.lock=threading.RLock(); self.stop=threading.Event(); self.thread=None; self.cursor=0; self.records={}; self.error=None
        self.owner=os.open(self.root/'writer.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try: fcntl.flock(self.owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.owner); self.owner=None
            raise failure('STATE_WRITER_CONFLICT','chat',retry_policy='after_reconcile',cause=exc) from exc
        self._refresh()
        # No provider call is repeated after an interrupted dispatch, even if a result file exists.
        for record in list(self.records.values()):
            if record['status']=='RUNNING':
                detail=failure('SDK_OUTCOME_UNKNOWN','restart',outcome='UNKNOWN',side_effect='unknown',retry_policy='after_reconcile')
                self._event('result',record['id'],outcome='UNKNOWN',error=detail)

    def _refresh(self):
        with self.lock:
            while True:
                rows=self.store.events(self.ctx,after=self.cursor,workspace_id=self.workspace_id)
                for row in rows:
                    self.cursor=row['sequence']
                    if row['event_name'] not in EVENTS: continue
                    attrs=row['attributes']; identity=attrs['message_id']; kind=row['event_name'].rsplit('.',1)[-1]
                    if kind=='queued':
                        self.records[identity]={'id':identity,'status':'QUEUED','created_at':row['occurred_at'],
                            'request_sha256':attrs['receipt_sha256'],'sequence':row['sequence'],'error':None}
                    elif identity in self.records:
                        self.records[identity].update(status='RUNNING' if kind=='started' else row['outcome'],error=row['error'])
                        if kind=='result': self.records[identity].update(result_sha256=attrs.get('receipt_sha256'),completed_at=row['occurred_at'])
                if len(rows)<200: return

    def _event(self,kind,identity,*,outcome='RUNNING',error=None,sha=None,meta=None):
        attrs={'message_id':identity,'provider':'codex','model':MODEL,'phase':'chat'}
        if sha: attrs['receipt_sha256']=sha
        if meta: attrs.update({k:meta[k] for k in ('session_id','thread_id','turn_id') if meta.get(k)})
        with self.lock:
            self.store.append_observation(self.ctx,self.workspace_id,event='control.chat.'+kind,outcome=outcome,error=error,attributes=attrs)
            self._refresh()

    def start(self):
        if self.command:
            self.thread=threading.Thread(target=self._run,name='railshot-chat',daemon=True); self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=5)
        if self.owner is not None and (not self.thread or not self.thread.is_alive()):
            os.close(self.owner); self.owner=None

    def submit(self,message,key,*,origin='user',job_id=None):
        if (not self.command or not isinstance(message,str) or not message.strip() or len(message.encode())>4000
                or not isinstance(key,str) or not 1<=len(key)<=128 or SECRET_TEXT.search(message.encode())):
            raise failure('CONTROL_CONFIG_INVALID','chat.submit')
        identity=str(uuid.uuid5(uuid.UUID(self.remote_workspace_id),key))
        with self.lock:
            self._refresh(); path=self.root/(identity+'.request.json')
            if identity in self.records:
                if read_artifact(path,self.records[identity]['request_sha256'])['message']!=message:
                    raise failure('CONTROL_CONFLICT','chat.submit')
                return self.records[identity]
            if self.error or any(r['status']=='UNKNOWN' for r in self.records.values()):
                raise failure('SDK_OUTCOME_UNKNOWN','chat.queue',outcome='UNKNOWN',side_effect='unknown',retry_policy='after_reconcile')
            if sum(r['status'] in ('QUEUED','RUNNING') for r in self.records.values())>=4:
                raise failure('CONTROL_CONFLICT','chat.queue')
            artifact={'message':message,'origin':origin,'job_id':job_id}
            write_once(path,artifact)
            self._event('queued',identity,sha=digest(artifact))
            return self.records[identity]

    def narrate_job(self,job):
        if not self.command or job['kind'] not in ('prepare','ci'): return
        key='job-result:'+job['id']
        message=('Platform job-result observation: explain the supplied triggering_job outcome and next step briefly in Korean. '
                 'This is a system-triggered narration, not a user message. Explain only what changed, its impact, and at most one next action. '
                 'Do not repeat a checklist or dismiss an earlier full CI PASS because a newer prepare ran. Do not substitute a newer job result. '
                 'Never claim success for BLOCKED, FAIL or UNKNOWN. Job ID: '+job['id'])
        try: self.submit(message,key,origin='job_result',job_id=job['id'])
        except OperationError as exc:
            # Bounded queue refusal is observable but never changes an already completed CI result.
            self._event('result',str(uuid.uuid5(uuid.UUID(self.remote_workspace_id),key)),outcome=exc.outcome,error=exc)

    def page(self,*,before=None,limit=20):
        # Cursor groups both bubbles of one queued request; hidden narration still advances it.
        if type(limit) is not int or not 1<=limit<=50 or (before is not None and (type(before) is not int or before<1)):
            raise failure('CONTROL_CONFIG_INVALID','chat.page')
        with self.lock:
            self._refresh(); messages=[]
            records=[r for r in self.records.values() if before is None or r['sequence']<before]
            selected=records[-limit:]
            for record in selected:
                artifact=read_artifact(self.root/(record['id']+'.request.json'),record['request_sha256'])
                common={'sequence':record['sequence'],'status':record['status'],'error':record['error']}
                if artifact.get('origin','user')=='user':
                    messages.append({'id':record['id'],'role':'user','text':artifact['message'],
                        'created_at':record['created_at'],**common})
                if record['status']=='PASS':
                    result=read_artifact(self.root/(record['id']+'.result.json'),record['result_sha256'])
                    messages.append({'id':record['id']+':reply','role':'assistant','text':result['reply'],
                        'created_at':record.get('completed_at',record['created_at']),'origin':artifact.get('origin','user'),
                        'job_id':artifact.get('job_id'),**common,
                        **({k:result[k] for k in ('proposal','proposal_review') if k in result})})
            return {'configured':bool(self.command),'pending':sum(r['status'] in ('QUEUED','RUNNING') for r in self.records.values()),
                    'messages':messages,'model':MODEL,'has_more':len(records)>len(selected),
                    'before':selected[0]['sequence'] if selected else None,
                    'error':self.error or next((r['error'] for r in reversed(list(self.records.values())) if r['status']=='UNKNOWN'),None)}

    def snapshot(self):
        return self.page()

    def _dispatch(self,record):
        identity=record['id']; started=False
        try:
            path=self.root/(identity+'.dispatch.json')
            if path.exists(): request=read_artifact(path)
            else:
                artifact=read_artifact(self.root/(identity+'.request.json'),record['request_sha256'])
                history=[]
                with self.lock: previous=[r.copy() for r in self.records.values()]
                for old in previous:
                    if old['sequence']>=record['sequence']: break
                    if old['status']=='PASS':
                        user=read_artifact(self.root/(old['id']+'.request.json'),old['request_sha256'])
                        result=read_artifact(self.root/(old['id']+'.result.json'),old['result_sha256'])
                        if user.get('origin','user')=='user': history.append({'role':'user','content':user['message']})
                        history.append({'role':'assistant','content':result['reply']})
                payload={'workspace_id':self.remote_workspace_id,'messages':history[-16:]+[{'role':'user','content':artifact['message']}], 'context':self.context(job_id=artifact.get('job_id'))}
                while len(encoded(payload))>MAX_REQUEST-300 and len(payload['messages'])>1: del payload['messages'][0]
                request={**payload,'message_id':identity,'request_sha256':digest(payload)}
                validate_request(request); write_once(path,request)
            validate_request(request)
            self._event('started',identity,sha=request['request_sha256']); started=True
            def tick():
                if self.stop.is_set(): raise InterruptedError('chat stopping')
            try:
                process=self.transport(self.command,input=encoded(request)+b'\n',timeout=360,max_output_bytes=32*1024,on_tick=tick)
            except (FileNotFoundError,PermissionError) as exc:
                raise failure('STEP_START_FAILED','chat.transport.start',retry_policy='after_configuration',cause=exc) from exc
            result=json.loads(process.stdout)
            if (not isinstance(result,dict) or result.get('message_id')!=identity or result.get('request_sha256')!=request['request_sha256']
                    or result.get('outcome') not in ('PASS','FAIL','BLOCKED','UNKNOWN') or process.returncode!=(0 if result['outcome']=='PASS' else 1)):
                raise ValueError('chat result contract mismatch')
            detail=OperationError.from_dict(result['error']) if result.get('error') else None
            if result['outcome']=='PASS':
                if detail or not isinstance(result.get('reply'),str) or not result['reply'].strip() or len(result['reply'].encode())>MAX_REPLY:
                    raise ValueError('chat reply invalid')
            elif detail is None or detail.outcome!=result['outcome']: raise ValueError('chat error invalid')
            if result['outcome']=='PASS' and result.get('proposal') is not None:
                review=proposal_review(result['proposal'],request['context'])
                if result.get('proposal_review')!=review: raise ValueError('proposal review differs')
            write_once(self.root/(identity+'.result.json'),result)
            self._event('result',identity,outcome=result['outcome'],error=detail,sha=digest(result),meta=result.get('meta'))
        except Exception as exc:
            detail=exc if isinstance(exc,OperationError) and (not started or exc.code=='STEP_START_FAILED') else failure(
                'SDK_OUTCOME_UNKNOWN' if started else 'SDK_CONFIG_INVALID','chat.dispatch',outcome='UNKNOWN' if started else 'BLOCKED',
                side_effect='unknown' if started else 'none',retry_policy='after_reconcile',cause=exc)
            self._event('result',identity,outcome=detail.outcome,error=detail)

    def _run(self):
        try:
            while not self.stop.is_set():
                with self.lock:
                    self._refresh()
                    if any(r['status']=='UNKNOWN' for r in self.records.values()): return
                    record=next((r.copy() for r in self.records.values() if r['status']=='QUEUED'),None)
                if record: self._dispatch(record)
                else: self.stop.wait(0.25)
        except Exception as exc:
            detail=exc if isinstance(exc,OperationError) else failure('INTERNAL_ERROR','chat.queue',outcome='UNKNOWN',side_effect='unknown',cause=exc)
            self.error=detail.as_dict()


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('action',choices=['once']); parser.add_argument('--root',required=True,type=Path)
    args=parser.parse_args(); request={}
    try:
        raw=sys.stdin.buffer.readline(MAX_REQUEST+1)
        if len(raw)>MAX_REQUEST: raise failure('CONTROL_CONFIG_INVALID','request')
        request=json.loads(raw); result=run_once(request,args.root)
    except Exception as exc:
        detail=exc if isinstance(exc,OperationError) else failure('SDK_OUTCOME_UNKNOWN','bridge',outcome='UNKNOWN',side_effect='unknown',retry_policy='after_reconcile',cause=exc)
        result={'message_id':request.get('message_id') if isinstance(request,dict) else None,
                'request_sha256':request.get('request_sha256') if isinstance(request,dict) else None,'outcome':detail.outcome,'error':detail.as_dict()}
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['outcome']=='PASS' else 1


if __name__=='__main__': raise SystemExit(main())
