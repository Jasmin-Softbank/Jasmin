#!/usr/bin/env python3
"""Authenticated loopback console for one administrator, not a hosted tenant service.

Run migrations separately before starting. Configuration and credentials stay outside
uploads. A registered resource is not ready until the trusted worker inspect response
has been observed. Chat and approved release use separate trusted transports;
CI workers never receive their credentials.
"""
import argparse
import base64
import binascii
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path, PurePosixPath
import secrets
import signal
import shlex
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

PLATFORM=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PLATFORM))
from control.state import ControlState, Principal
from control.ci_view import project as project_ci
from control.chat import ChatQueue, MODEL as CHAT_MODEL, write_once
from control.release import digest as release_digest, checked_target, require_registry_prerequisites
from execution import GATE_ORDER
from control.worker import MAX_BYTES, MAX_FILES, MAX_OUTPUT, REQUEST_BYTES, SECRET_NAME, SECRET_TEXT, relative, redact
from observability import OperationError, event_record
from process import run_bounded
from runner.runtime_boundary import private_directory
from storage import durable_write

MAX_BODY=REQUEST_BYTES
MAX_INSPECT_DIAGNOSTICS=256*1024


def error(code,phase,**kwargs):
    return OperationError(code,component='control.api',phase=phase,**kwargs)


def save_json(path,value):
    durable_write(path,json.dumps(value,sort_keys=True,ensure_ascii=False).encode())


def read_json(path,default=None):
    try: return json.loads(Path(path).read_text())
    except FileNotFoundError: return default
    except (OSError,ValueError) as exc:
        raise error('STATE_EVIDENCE_MISMATCH','read',retry_policy='after_reconcile',cause=exc) from exc


def private_text(path):
    path=Path(path)
    if path.is_symlink() or path.stat().st_uid!=os.geteuid() or path.stat().st_mode & 0o077:
        raise error('CONTROL_CONFIG_INVALID','config')
    return path.read_text().strip()


def load_config(path):
    if path is None: return {}
    path=Path(path)
    if path.is_symlink() or path.stat().st_uid!=os.geteuid() or path.stat().st_mode & 0o077:
        raise error('CONTROL_CONFIG_INVALID','config')
    data=read_json(path)
    if not isinstance(data,dict): raise error('CONTROL_CONFIG_INVALID','config')
    return data


class Console:
    def __init__(self,state_dir,config=None,*,store=None,transport=run_bounded):
        self.root=private_directory(state_dir); self.config=config or {}; self.transport=transport
        self.ctx=Principal(self.config.get('tenant_id','operator'),self.config.get('principal_id','operator'),True)
        self.workspace_id=self.config.get('workspace_id','operator-default')
        self.remote_workspace_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'railshot:'+self.ctx.tenant_id+':'+self.workspace_id))
        database=self.config.get('database_url',self.root/'state.sqlite3')
        if self.config.get('database_url_file'):
            if 'database_url' in self.config: raise error('CONTROL_CONFIG_INVALID','config')
            database=private_text(self.config['database_url_file'])
        self.store=store or ControlState(database,
                                        self.config.get('accounts_path',PLATFORM/'runner/accounts.yaml'))
        self.store.create_workspace(self.ctx,self.workspace_id)
        for name in ('uploads','requests','results','release-plans'): private_directory(self.root/name)
        self.bootstrap=secrets.token_urlsafe(32); self.session=None; self.session_expires=0
        self.lock=threading.RLock(); self.stop=threading.Event(); self.thread=None
        self.worker_id='console-'+uuid.uuid4().hex; self.stream_slots=threading.BoundedSemaphore(4)
        self.connection={'status':'UNAVAILABLE','hostname':None,'observed_at':None,'error':None}
        self.last_inspect=0; self.fatal_error=None
        prefix=self.config.get('transport_argv_prefix')
        self.configured=bool(prefix)
        if self.configured:
            required=('provider','resource_id')
            worker_argv=self.config.get('worker_argv')
            if (not isinstance(prefix,list) or not all(isinstance(x,str) and x and '\x00' not in x for x in prefix)
                    or not isinstance(worker_argv,list) or not worker_argv
                    or not all(isinstance(x,str) and x and '\x00' not in x for x in worker_argv)
                    or any(not isinstance(self.config.get(k),str) or not self.config[k] for k in required)):
                raise error('CONTROL_CONFIG_INVALID','transport')
            self.command=[*prefix,shlex.join(worker_argv)]
            current=self.store.get_workspace(self.ctx,self.workspace_id)
            if (current.get('provider'),current.get('resource_id')) != (self.config['provider'],self.config['resource_id']):
                self.store.register_resource(self.ctx,self.workspace_id,provider=self.config['provider'],resource_id=self.config['resource_id'])
        else:
            self.connection['error']=error('CONTROL_NOT_READY','connection',retry_policy='after_configuration').as_dict()
        self.store.append_observation(self.ctx,self.workspace_id,event='control.connection.observed',
            outcome='BLOCKED' if not self.configured else 'RUNNING',error=self.connection['error'],
            attributes={'status':'UNAVAILABLE' if not self.configured else 'CONNECTING'})
        command=self.config.get('chat_transport_argv')
        if command is not None and (not isinstance(command,list) or not command or not all(isinstance(x,str) and x and '\x00' not in x for x in command)):
            raise error('CONTROL_CONFIG_INVALID','chat.config')
        self.chat=ChatQueue(self.root/'chat',self.store,self.ctx,self.workspace_id,self.remote_workspace_id,command,self.chat_context)
        self.release_command=self.config.get('release_transport_argv')
        if self.release_command is not None and (not isinstance(self.release_command,list) or not self.release_command
                or not all(isinstance(x,str) and x and '\x00' not in x for x in self.release_command)):
            raise error('CONTROL_CONFIG_INVALID','release.config')


    def start(self):
        self.chat.start()
        self.thread=threading.Thread(target=self._run,name='railshot-console-worker',daemon=True); self.thread.start()

    def close(self):
        self.stop.set(); self.chat.close()
        if self.thread: self.thread.join(timeout=5)

    def exchange(self,token):
        with self.lock:
            if not isinstance(token,str) or self.bootstrap is None or not secrets.compare_digest(token,self.bootstrap):
                raise error('CONTROL_ACCESS_DENIED','session')
            self.bootstrap=None; self.session=secrets.token_urlsafe(32); self.session_expires=time.time()+8*3600
            return self.session

    def authenticated(self,cookie):
        parsed=SimpleCookie()
        try: parsed.load(cookie or '')
        except Exception: return False
        value=parsed.get('railshot_session')
        return bool(value and self.session and time.time()<self.session_expires and secrets.compare_digest(value.value,self.session))

    def upload(self,data):
        with self.lock:
            self._assert_no_release()
            return self._upload(data)

    def _upload(self,data):
        if not isinstance(data,dict) or set(data)-{'files','name'} or not isinstance(data.get('files'),list) or not 1<=len(data['files'])<=MAX_FILES:
            raise error('CONTROL_CONFIG_INVALID','upload')
        name=data.get('name','Uploaded repository')
        if not isinstance(name,str) or not 1<=len(name)<=120: raise error('CONTROL_CONFIG_INVALID','upload')
        files=[]; seen=set(); total=0
        for item in data['files']:
            if not isinstance(item,dict) or set(item)!={'path','content_base64'} or not isinstance(item['path'],str):
                raise error('CONTROL_CONFIG_INVALID','upload')
            relative(item['path'])
            if item['path'] in seen or SECRET_NAME.search(item['path']):
                raise error('INTAKE_REJECTED','upload')
            try: content=base64.b64decode(item['content_base64'],validate=True)
            except (ValueError,TypeError,binascii.Error) as exc:
                raise error('CONTROL_CONFIG_INVALID','upload',cause=exc) from exc
            total+=len(content)
            if total>MAX_BYTES or SECRET_TEXT.search(content): raise error('INTAKE_REJECTED','upload')
            seen.add(item['path']); files.append({**item,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()})
        upload_id=str(uuid.uuid4()); directory=private_directory(self.root/'uploads'/upload_id)
        record={'id':upload_id,'name':name,'files':files}
        save_json(directory/'manifest.json',record)
        save_json(self.root/'current-upload.json',{'id':upload_id})
        self.store.append_observation(self.ctx,self.workspace_id,event='control.upload.saved',
            attributes={'upload_id':upload_id,'file_count':len(files)})
        return {'upload_id':upload_id,'files':[{k:f[k] for k in ('path','bytes','sha256')} for f in files]}

    def current_upload(self):
        pointer=read_json(self.root/'current-upload.json')
        if pointer is None: return None
        try: upload_id=str(uuid.UUID(pointer['id']))
        except (KeyError,ValueError,TypeError) as exc: raise error('STATE_EVIDENCE_MISMATCH','upload',cause=exc) from exc
        value=read_json(self.root/'uploads'/upload_id/'manifest.json')
        if value is None: raise error('STATE_EVIDENCE_MISMATCH','upload')
        return value

    def submit(self,data,*,terminal=False):
        with self.lock:
            self._assert_no_release()
            return self._submit(data,terminal=terminal)

    def _submit(self,data,*,terminal=False):
        if not isinstance(data,dict): raise error('CONTROL_CONFIG_INVALID','submit')
        with self.lock: connection=dict(self.connection)
        if connection['status']!='CONNECTED': raise error('CONTROL_NOT_READY','submit',retry_policy='after_configuration')
        control=self.store.control(self.ctx,self.workspace_id)
        upload=self.current_upload()
        if terminal:
            if set(data)-{'command','idempotency_key'} or not isinstance(data.get('command'),str) or not 0<len(data['command'])<=4096:
                raise error('CONTROL_CONFIG_INVALID','terminal')
            if control['owner']!='human': raise error('CONTROL_LEASE_EXPIRED','terminal')
            kind='terminal'; lease=control['lease_id']
        else:
            if set(data)-{'kind','idempotency_key','selected_root'} or data.get('kind') not in ('prepare','ci') or upload is None:
                raise error('CONTROL_CONFIG_INVALID','submit')
            if control['owner']=='human': raise error('CONTROL_CONFLICT','submit')
            kind=data['kind']; lease=None
            if kind=='ci' and not self.store.get_workspace(self.ctx,self.workspace_id)['ready']:
                raise error('CONTROL_NOT_READY','submit')
        payload={'operation':kind,'upload_id':upload['id'] if upload else None,
                 'files':[{k:f[k] for k in ('path','content_base64')} for f in upload['files']] if upload else []}
        selected_root=data.get('selected_root',self.config.get('selected_root'))
        if selected_root not in (None,'.'): relative(selected_root)
        if selected_root is not None: payload['selected_root']=selected_root
        if terminal:
            if not data['command'].strip() or '\x00' in data['command'] or SECRET_TEXT.search(data['command'].encode()):
                raise error('INTAKE_REJECTED','terminal')
            payload['command']=data['command']
        request_hash=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        save_json(self.root/'requests'/(request_hash+'.json'),payload)
        return self.store.submit(self.ctx,self.workspace_id,kind=kind,request_hash=request_hash,
            idempotency_key=data.get('idempotency_key') or str(uuid.uuid4()),requires_ready=kind=='ci',input_lease_id=lease)

    def control_action(self,data):
        with self.lock:
            if isinstance(data,dict) and data.get('action')=='acquire': self._assert_no_release()
            return self._control_action(data)

    def _control_action(self,data):
        if not isinstance(data,dict) or set(data)!={'action'}: raise error('CONTROL_CONFIG_INVALID','control')
        if data['action']=='acquire':
            with self.lock:
                if self.connection['status']!='CONNECTED': raise error('CONTROL_NOT_READY','control')
            self.store.acquire_lease(self.ctx,self.workspace_id,ttl_seconds=120)
        elif data['action']=='release':
            current=self.store.control(self.ctx,self.workspace_id)
            if current['lease_id']: self.store.release_lease(self.ctx,current['lease_id'])
        else: raise error('CONTROL_CONFIG_INVALID','control')
        return self.store.control(self.ctx,self.workspace_id)

    def _assert_no_release(self):
        if any(j['kind']=='deploy' and j['status'] in ('QUEUED','DISPATCHED','UNKNOWN')
               for j in self.store.jobs(self.ctx,self.workspace_id)):
            raise error('CONTROL_CONFLICT','release.source_frozen')

    def _ci_release_binding(self,identity):
        job=self.store.get_job(self.ctx,identity); upload=self.current_upload()
        jobs=self.store.jobs(self.ctx,self.workspace_id)
        request=read_json(self.root/'requests'/(job['request_hash']+'.json'))
        result=read_json(self.root/'results'/(identity+'.json'))
        workspace=self.store.get_workspace(self.ctx,self.workspace_id)
        try:
            if (job['workspace_id']!=self.workspace_id or job['kind']!='ci' or job['status']!='PASS'
                    or not any(row['id']==identity for row in jobs)
                    or not upload or request['upload_id']!=upload['id']
                    or hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()!=job['request_hash']
                    or result['job_id']!=identity or result['workspace_id']!=self.remote_workspace_id
                    or result['generation']!=job['generation'] or result['outcome']!='PASS'
                    or result['details']['passed'] is not True):
                raise ValueError('CI result does not bind current upload')
            for newer in jobs:
                if newer['id']==identity: break
                if newer['kind'] in ('ci','terminal'):
                    raise ValueError('newer CI or mutable terminal operation exists')
            layers=result['details']['layers']
            if ([r['layer'] for r in layers]!=list(GATE_ORDER)
                    or any(r.get('outcome')!='PASS' or r.get('ok') is not True for r in layers)):
                raise ValueError('not a full CI receipt')
            bundle=result['details']['bundle']
            if (bundle['relative_path']!=f"{self.remote_workspace_id}/{identity}/bundle"
                    or any(not isinstance(bundle[k],str) or not re.fullmatch('[a-f0-9]{64}',bundle[k])
                           for k in ('manifest_sha256','source_sha256'))
                    or type(bundle['bytes']) is not int or bundle['bytes']<=0):
                raise ValueError('invalid CI bundle binding')
            return {'ci_job_id':identity,'ci_request_hash':job['request_hash'],
                    'ci_receipt_sha256':release_digest(result),'upload_id':upload['id'],
                    'workspace_id':self.workspace_id,'tenant':self.ctx.tenant_id,
                    'workspace_generation':workspace['generation'],'bundle':bundle}
        except (KeyError,ValueError,TypeError) as exc:
            raise error('STATE_EVIDENCE_MISMATCH','release.ci',retry_policy='after_configuration',cause=exc) from exc

    def _release_call(self,request,job=None):
        if not self.release_command: raise error('CONTROL_NOT_READY','release.config',retry_policy='after_configuration')
        last=time.monotonic()
        def tick():
            nonlocal last
            if self.stop.is_set(): raise InterruptedError('release supervision stopped')
            if job and time.monotonic()-last>=10:
                self.store.heartbeat(job['id'],self.worker_id,job['generation'],lease_seconds=120);last=time.monotonic()
        try:
            process=self.transport(self.release_command,input=(json.dumps(request)+'\n').encode(),
                timeout=960 if job else 120,max_output_bytes=2*1024*1024,on_tick=tick)
        except (FileNotFoundError,PermissionError) as exc:
            raise error('STEP_START_FAILED','release.start',retry_policy='after_configuration',cause=exc) from exc
        response=json.loads(process.stdout)
        if process.returncode:
            detail=OperationError.from_dict(response['error'])
            if response.get('status')!=detail.outcome: raise ValueError('release exit/status mismatch')
            raise detail
        if not isinstance(response,dict) or response.get('phase')!=request['phase'] or response.get('error'):
            raise ValueError('release protocol mismatch')
        return response

    def _check_release_plan(self,plan,binding):
        try:
            target=checked_target(plan['target'])
            for key in ('deployment_url','probe_url'):
                url=urlsplit(plan[key])
                if url.scheme!='https' or url.netloc!=plan['host'] or url.query or url.fragment:
                    raise ValueError('release URL differs from bound HTTPS host')
            if (plan['version']!=1 or plan['operation']!='deploy'
                    or plan['plan_hash']!=release_digest({k:v for k,v in plan.items() if k!='plan_hash'})
                    or (target['tenant'],target['workspace_id'],target['generation'])!=(self.ctx.tenant_id,self.workspace_id,binding['workspace_generation'])
                    or any(plan[k]!=binding['bundle'][k] for k in ('manifest_sha256','source_sha256'))
                    or plan['external_access']!='NOT_VERIFIED' or not plan.get('images')):
                raise ValueError('release plan binding differs')
        except (KeyError,ValueError,TypeError) as exc:
            raise error('STATE_BINDING_MISMATCH','release.plan',cause=exc) from exc

    def release_plan(self,data):
        if not self.ctx.is_admin: raise error('CONTROL_ACCESS_DENIED','release.plan')
        if not isinstance(data,dict) or set(data)!={'ci_job_id'}: raise error('CONTROL_CONFIG_INVALID','release.plan')
        with self.lock: binding=self._ci_release_binding(data['ci_job_id'])
        response=self._release_call({'phase':'prepare','ci_job_id':binding['ci_job_id'],'bundle':binding['bundle']})
        if response.get('ci_job_id')!=binding['ci_job_id']: raise error('STATE_BINDING_MISMATCH','release.plan')
        plan=response['plan'];self._check_release_plan(plan,binding)
        with self.lock:
            self._assert_no_release()
            if self._ci_release_binding(binding['ci_job_id'])!=binding: raise error('STATE_BINDING_MISMATCH','release.plan')
            write_once(self.root/'release-plans'/(plan['plan_hash']+'.json'),{**binding,'plan':plan})
            allows=self.store.allows(self.ctx,self.workspace_id)
            existing=next((a for a in allows if a['plan_hash']==plan['plan_hash'] and a['state'] in ('PENDING','APPROVED') and a['expires_at']>time.time()),None)
            allow=existing or self.store.create_allow(self.ctx,self.workspace_id,operation='deploy',plan_hash=plan['plan_hash'])
            return {'allow':allow,'plan':plan,'ci_job_id':binding['ci_job_id']}

    def decide_allow(self,identity,approve):
        if type(approve) is not bool: raise error('CONTROL_CONFIG_INVALID','allow')
        with self.lock:
            row=self.store.get_allow(self.ctx,identity)
            if row['workspace_id']!=self.workspace_id: raise error('CONTROL_ACCESS_DENIED','allow')
            if not approve or row['operation']!='deploy':
                return self.store.decide_allow(self.ctx,identity,approve=approve)
            if not self.ctx.is_admin: raise error('CONTROL_ACCESS_DENIED','release.approve')
            record=read_json(self.root/'release-plans'/(row['plan_hash']+'.json'))
            if not record: raise error('STATE_EVIDENCE_MISMATCH','release.plan')
            plan=record['plan'];binding={k:v for k,v in record.items() if k!='plan'}
            self._check_release_plan(plan,binding)
            if row['state']!='CONSUMED':
                require_registry_prerequisites(plan)
                if self._ci_release_binding(record['ci_job_id'])!=binding:
                    raise error('STATE_BINDING_MISMATCH','release.approve')
                self._assert_no_release()
            job=self.store.approve_and_submit(self.ctx,identity,plan_hash=plan['plan_hash'],generation=plan['target']['generation'])
            return {'allow_id':identity,'job':job,'plan_hash':plan['plan_hash']}

    def release_snapshot(self,jobs):
        receipt=None; job=None
        for candidate in jobs:
            if candidate['kind']=='deploy':
                job=candidate;receipt=read_json(self.root/'results'/(job['id']+'.json'),{}).get('details',{}).get('release');break
        allows=self.store.allows(self.ctx,self.workspace_id)
        for row in allows:
            if row['operation']=='deploy':
                record=read_json(self.root/'release-plans'/(row['plan_hash']+'.json'),{})
                row['plan']=record.get('plan');row['ci_job_id']=record.get('ci_job_id')
        return {'configured':bool(self.release_command),'job_id':job['id'] if job else None,
                'status':job['status'] if job else 'NOT_RUN','receipt':receipt},allows

    def dispatch_release(self,job):
        started=False
        try:
            with self.lock:
                record=read_json(self.root/'release-plans'/(job['request_hash']+'.json'))
                if not record: raise error('STATE_EVIDENCE_MISMATCH','release.plan')
                plan=record['plan'];binding={k:v for k,v in record.items() if k!='plan'}
                self._check_release_plan(plan,binding)
                if self._ci_release_binding(record['ci_job_id'])!=binding: raise error('STATE_BINDING_MISMATCH','release.dispatch')
                operation=self.store.get_operation(self.ctx,job['operation_id'])
                if operation['plan_hash']!=job['request_hash']: raise error('STATE_BINDING_MISMATCH','release.operation')
            self.store.append_observation(self.ctx,self.workspace_id,event='control.worker.stage',
                attributes={'phase':'release'},job_id=job['id'],worker_id=self.worker_id,generation=job['generation'])
            started=True
            response=self._release_call({'phase':'execute','ci_job_id':record['ci_job_id'],'bundle':record['bundle'],
                'plan':plan,'operation':operation,'job_id':job['id'],'job_generation':job['generation']},job)
            if response.get('job_id')!=job['id'] or response.get('job_generation')!=job['generation']:
                raise ValueError('stale release response')
            receipt=response['receipt'];status=receipt['status']
            if (receipt['operation_id']!=operation['id'] or receipt['plan_hash']!=plan['plan_hash']
                    or status not in ('PASS','FAIL','BLOCKED','UNKNOWN')): raise ValueError('release receipt mismatch')
            detail=OperationError.from_dict(receipt['error']) if receipt.get('error') else None
            if status=='PASS':
                if (detail or receipt.get('phase')!='deployment.verified' or receipt.get('external_access')!='PASS'
                        or receipt.get('pod_image_identity')!='PASS' or receipt.get('deployment_url')!=plan['deployment_url']
                        or not re.fullmatch('[a-f0-9]{40}',receipt.get('revision',''))
                        or receipt.get('observed_revision')!=receipt['revision']
                        or receipt.get('external',{}).get('tls_verified') is not True
                        or receipt.get('external',{}).get('url')!=plan['probe_url']
                        or any(receipt.get(k,{}).get('status')!='PASS' for k in ('runtime_before','runtime_after'))):
                    raise ValueError('strong deployment evidence missing')
            elif detail is None or detail.outcome!=status: raise ValueError('release error mismatch')
            result={'outcome':status,'job_id':job['id'],'generation':job['generation'],
                    'details':{'release':receipt},'error':detail.as_dict() if detail else None}
            write_once(self.root/'results'/(job['id']+'.json'),result)
            self.store.append_observation(self.ctx,self.workspace_id,event='control.worker.result',outcome=status,error=detail,
                attributes={'phase':'release'},job_id=job['id'],worker_id=self.worker_id,generation=job['generation'])
            self.store.complete(job['id'],self.worker_id,job['generation'],outcome=status,error=detail)
        except Exception as exc:
            detail=exc if isinstance(exc,OperationError) else error('STEP_OUTPUT_INVALID' if started else 'STATE_EVIDENCE_MISMATCH',
                'release.dispatch',outcome='UNKNOWN' if started else 'BLOCKED',side_effect='unknown' if started else 'none',
                retry_policy='after_reconcile',cause=exc)
            failure_result={'outcome':detail.outcome,'job_id':job['id'],'generation':job['generation'],'error':detail.as_dict()}
            result_path=self.root/'results'/(job['id']+'.json')
            write_once(result_path if not result_path.exists() else self.root/'results'/(job['id']+'.failure.json'),failure_result)
            try:self.store.complete(job['id'],self.worker_id,job['generation'],outcome=detail.outcome,error=detail)
            except OperationError as record_error:
                if record_error.code!='CONTROL_LEASE_EXPIRED':raise

    def snapshot(self):
        # Capture the cursor first: concurrent changes after it must remain replayable.
        observed=self.store.snapshot_events(self.ctx,self.workspace_id)
        workspace=self.store.get_workspace(self.ctx,self.workspace_id); jobs=self.store.jobs(self.ctx,self.workspace_id)
        upload=self.current_upload(); workspace['name']=upload['name'] if upload else self.config.get('workspace_name','Workspace')
        with self.lock: connection=dict(self.connection)
        if self.fatal_error: connection.update(status='UNAVAILABLE',error=self.fatal_error)
        control=self.store.control(self.ctx,self.workspace_id)
        connected=connection['status']=='CONNECTED'
        last_result=None; build_roots=[]; plan_seen=False
        for job in jobs:
            request=read_json(self.root/('release-plans' if job['kind']=='deploy' else 'requests')/(job['request_hash']+'.json'),{})
            job['same_upload']=bool(upload and request.get('upload_id')==upload['id'])
            candidate=read_json(self.root/'results'/(job['id']+'.json'))
            if candidate is not None and last_result is None: last_result=candidate
            if candidate is not None and job['kind']=='prepare' and not plan_seen:
                if job['same_upload']:
                    plan_seen=True
                    projects=(candidate.get('details') or {}).get('plan',{}).get('projects',[])
                    build_roots=list(dict.fromkeys(p['build_root'] for p in projects if isinstance(p,dict) and isinstance(p.get('build_root'),str)))
        deployment,allows=self.release_snapshot(jobs)
        return {'workspace':workspace,'jobs':jobs,'allows':allows,'deployment':deployment,
                'files':[{k:f[k] for k in ('path','bytes','sha256')} for f in upload['files']] if upload else [],
                'connection':connection,'control':control,'build_roots':build_roots,'capabilities':{'prepare':connected,'ci':connected and bool(workspace['ready']),
                'terminal':connected,'browser':bool(deployment['status']=='PASS' and deployment['receipt']),'deploy':bool(self.release_command)},'last_result':last_result,'chat':self.chat_snapshot(),'ci':self.ci_snapshot(jobs,upload),**observed}

    def ci_snapshot(self,jobs,upload):
        for job in jobs:
            if job['kind']!='ci': continue
            request=read_json(self.root/'requests'/(job['request_hash']+'.json'),{})
            if upload and request.get('upload_id')!=upload['id']: continue
            receipt=read_json(self.root/'results'/(job['id']+'.json'),{})
            return project_ci(job,self.store.job_observations(self.ctx,job['id']),receipt)
        return None

    def ci_run(self,job_id):
        job=self.store.get_job(self.ctx,job_id)
        if job['kind']!='ci' or job['workspace_id']!=self.workspace_id: raise error('CONTROL_ACCESS_DENIED','ci.read')
        result=project_ci(job,self.store.job_observations(self.ctx,job_id),read_json(self.root/'results'/(job_id+'.json'),{}))
        diagnostics=self.store.job_observations(self.ctx,job_id,logs=True)
        result['logs']=[{'sequence':r['sequence'],'phase':r['phase'],'text':r['text']} for r in diagnostics['records']]
        result['logs_truncated']=diagnostics['truncated']
        return result

    def chat_snapshot(self):
        try: return self.chat.snapshot()
        except Exception as exc:
            detail=exc if isinstance(exc,OperationError) else error('STATE_STORAGE_FAILED','chat.snapshot',outcome='UNKNOWN',side_effect='unknown',retry_policy='after_reconcile',cause=exc)
            self.chat.error=detail.as_dict(); self.chat.stop.set()
            return {'configured':False,'pending':sum(r['status'] in ('QUEUED','RUNNING') for r in self.chat.records.values()),
                    'messages':[],'model':CHAT_MODEL,'has_more':False,'before':None,'error':detail.as_dict()}

    def chat_context(self,job_id=None):
        from observability import OUTCOMES, RETRY_POLICIES, SIDE_EFFECTS
        import yaml
        snapshot=self.snapshot(); upload=self.current_upload()
        schema_path=PLATFORM/'schemas/jasmin.schema.json'; catalog_path=PLATFORM/'contract/catalog.yaml'
        schema=read_json(schema_path); catalog=yaml.safe_load(catalog_path.read_text())
        contract={'version':1,'components':{'control':'authenticated API + durable jobs/events',
            'ci':'credential-free worker; deterministic full gate; model calls disabled',
            'sdk':'trusted control VM; read-only Codex; no chat action dispatch',
            'release':'separate executor; product dispatch not connected to chat'},
            'outcomes':sorted(OUTCOMES),'retry_policies':sorted(RETRY_POLICIES),'side_effects':sorted(SIDE_EFFECTS),
            'app_schema':{'api_version':schema['properties']['apiVersion']['const'],
                'required':schema['required'],'max_services':schema['properties']['services']['maxItems'],
                'resources':[k for k,v in schema['properties']['resources']['properties'].items() if v is not False],
                'disabled_resources':[k for k,v in schema['properties']['resources']['properties'].items() if v is False],
                'schema_sha256':hashlib.sha256(schema_path.read_bytes()).hexdigest()},
            'catalog':{'unsupported':catalog['unsupported_mvp'],'custom_domain':catalog['domain']['custom_domain'],
                'schema_support_is_not_runtime_readiness':True,'sha256':hashlib.sha256(catalog_path.read_bytes()).hexdigest()},
            'topology_owners':{'vm':['terraform','external'],'network':['terraform','external'],
                'gateway':['terraform','argocd','external'],'cluster':['ansible','external'],
                'service':['argocd','external'],'database':['argocd','external'],
                'registry':['terraform','external'],'agent':['ansible','external'],
                'runner':['ansible','external'],'control':['ansible','external']},
            'chat_actions':{'explain':True,'draft_topology':True,'execute':False,'approve':False,'topology_apply':False}}
        # Read only a bounded recent event window. Older evidence without a timestamp stays explicit.
        recent=self.store.events(self.ctx,after=max(0,snapshot['cursor']-200),workspace_id=self.workspace_id)
        observations={}
        for row in recent:
            if row['event_name']=='control.worker.result': observations[row['attributes'].get('job_id')]=row
        refs={'contract:app-schema','contract:catalog'}
        def job_view(job):
            result=read_json(self.root/'results'/(job['id']+'.json'),{})
            request=read_json(self.root/('release-plans' if job['kind']=='deploy' else 'requests')/(job['request_hash']+'.json'),{})
            seen=observations.get(job['id']); refs.add('job:'+job['id'])
            details=result.get('details') or {}; receipt=result.get('receipt') or {}
            error_value=job.get('error') or {}
            value={k:job.get(k) for k in ('id','kind','status','created_at')}
            value.update(evidence_ref='job:'+job['id'],observed_at=seen['occurred_at'] if seen else None,
                same_upload=bool(upload and request.get('upload_id')==upload['id']),
                receipt_sha256=receipt.get('sha256'),outcome=result.get('outcome'),
                error={k:error_value.get(k) for k in ('code','summary','phase','retry_policy','side_effect')})
            if job['kind']=='prepare':
                plan=details.get('plan') or {}
                value['preparation']={'status':plan.get('status'),'checks_executed':details.get('checks_executed'),
                    'projects':[{'build_root':p.get('build_root'),'language':p.get('language'),'status':p.get('status')}
                        for p in plan.get('projects',[])[:5] if isinstance(p,dict)]}
            if job['kind']=='ci': value['checks']={k:details.get(k) for k in ('passed','layers','sdk_invocations','llm_calls')}
            return value
        jobs=snapshot['jobs']
        context={'captured_at':time.time(),'trusted_contract':contract,
            'workspace':{k:snapshot['workspace'].get(k) for k in ('id','name','ready','observed_at')},
            'connection':{k:snapshot['connection'].get(k) for k in ('status','hostname','observed_at')},
            'capabilities':{**snapshot['capabilities'],'topology_apply':False,'chat_execution':False},
            'control_owner':snapshot['control']['owner'],'jobs':[job_view(job) for job in jobs[:4]],
            'stage_summary':{},'uploaded_files':[f['path'][:128] for f in snapshot['files'][:12]],
            'recent_diagnostics':[redact(row['text'])[-400:] for row in snapshot['logs'][-2:]],
            'recent_stages':[{'sequence':r['sequence'],'observed_at':r['occurred_at'],'phase':r['phase'],
                'outcome':r['outcome'],'job_id':r['attributes'].get('job_id')}
                for r in recent if r['event_name']=='control.worker.stage'][-6:],
            'deployment':{'status':'NOT_OBSERVED','external_access':'NOT_VERIFIED','chat_apply_configured':False}}
        for name,predicate in [('prepare',lambda j:j['kind']=='prepare'),('ci',lambda j:j['kind']=='ci'),
                ('last_successful_ci',lambda j:j['kind']=='ci' and j['status']=='PASS')]:
            candidate=next((j for j in jobs if predicate(j)),None)
            context['stage_summary'][name]=job_view(candidate) if candidate else {'status':'NOT_OBSERVED','scope':'latest_100_jobs'}
        deployment=snapshot['deployment']; deployed=deployment.get('receipt') or {}
        observed=observations.get(deployment['job_id'])
        context['deployment']={'configured':deployment['configured'],'status':deployment['status'],
            'job_id':deployment['job_id'],'observed_at':observed['occurred_at'] if observed else None,
            'phase':deployed.get('phase'),'external_access':deployed.get('external_access','NOT_VERIFIED'),
            'pod_image_identity':deployed.get('pod_image_identity','NOT_VERIFIED'),
            'revision':deployed.get('revision'),'observed_revision':deployed.get('observed_revision'),
            'deployment_url':deployed.get('deployment_url') if deployment['status']=='PASS' else None,
            'chat_apply_configured':False}
        if deployment['job_id']:
            context['deployment']['evidence_ref']='job:'+deployment['job_id'];refs.add('job:'+deployment['job_id'])
        ci=snapshot.get('ci')
        if ci:
            context['ci']={k:ci.get(k) for k in ('job_id','status','completed','total','passed')}
            context['ci']['steps']=[{k:step.get(k) for k in ('id','status','started_at','duration_s','evidence')} for step in ci.get('steps',[])]
        if job_id: context['triggering_job']=job_view(self.store.get_job(self.ctx,job_id))
        context['evidence_refs']=sorted(refs)
        for key in ('recent_diagnostics','uploaded_files','recent_stages','jobs'):
            while len(json.dumps(context,sort_keys=True).encode())>11000 and context[key]: context[key].pop(0)
        if len(json.dumps(context,sort_keys=True).encode())>11000:
            raise error('CONTROL_CONFIG_INVALID','chat.context')
        return context

    def chat_page(self,query):
        fields=parse_qs(query,keep_blank_values=True)
        if set(fields)-{'before','limit'} or any(len(values)!=1 or not values[0].isdigit() for values in fields.values()):
            raise error('CONTROL_CONFIG_INVALID','chat.page')
        return self.chat.page(before=int(fields['before'][0]) if 'before' in fields else None,
                              limit=int(fields.get('limit',['20'])[0]))

    def chat_submit(self,data):
        if not isinstance(data,dict) or set(data)!={'message','idempotency_key'}: raise error('CONTROL_CONFIG_INVALID','chat.submit')
        return self.chat.submit(data['message'],data['idempotency_key'])

    def event_page(self,after):
        rows=self.store.events(self.ctx,after=after,workspace_id=self.workspace_id)
        logs={row['sequence']:row['text'] for row in self.store.diagnostics(self.ctx,self.workspace_id,after=after)}
        result=[]
        for row in rows:
            if row['attributes'].get('workspace_id') not in (None,self.workspace_id): continue
            event='log' if row['sequence'] in logs else row['event_name']
            attributes={'phase':row['phase'],**row['attributes']}
            if row['sequence'] in logs: attributes['text']=logs[row['sequence']]
            result.append({'sequence':row['sequence'],'event':event,'attributes':attributes,'outcome':row['outcome'],'error':row['error']})
        return result

    def _request(self,request,job=None):
        results=[]; buffer=bytearray(); last_heartbeat=time.monotonic(); current_phase=request['operation']
        inspecting=request['operation']=='inspect'; diagnostics=bytearray(); truncated=False
        def diagnostic(stream,text):
            nonlocal truncated
            line=(json.dumps({'stream':stream,'text':redact(text)},ensure_ascii=False)+'\n').encode()
            if len(diagnostics)+len(line)<=MAX_INSPECT_DIAGNOSTICS: diagnostics.extend(line)
            else: truncated=True
        def append(frame):
            nonlocal current_phase
            if results: raise ValueError('frame after terminal result')
            if not isinstance(frame,dict) or any(frame.get(k)!=request[k] for k in ('workspace_id','job_id','generation')):
                raise error('STEP_OUTPUT_INVALID','transport',outcome='UNKNOWN',side_effect='unknown',retry_policy='after_reconcile')
            kind=frame.get('type')
            common={'job_id':job['id'],'worker_id':self.worker_id,'generation':job['generation']} if job else {}
            if kind=='log':
                text=frame.get('text')
                if not isinstance(text,str) or len(text.encode())>65536: raise ValueError('invalid bounded log')
                if inspecting:
                    diagnostic(frame.get('stream','stdout'),text)
                else:
                    self.store.append_observation(self.ctx,self.workspace_id,event='control.worker.log',text=redact(text).rstrip('\n').encode()[:65535].decode(errors='ignore')+'\n',
                        attributes={'stream':frame.get('stream','stdout'),'phase':frame.get('phase',request['operation'])},**common)
            elif kind=='stage':
                current_phase=frame.get('phase','worker')
                if inspecting:
                    stage=event_record('control.worker.stage',component='control.api',phase=frame.get('phase','worker'),outcome=frame.get('outcome','RUNNING'))
                    diagnostic('stage',json.dumps({'phase':stage['phase'],'outcome':stage['outcome']}))
                else:
                    self.store.append_observation(self.ctx,self.workspace_id,event='control.worker.stage',outcome=frame.get('outcome','RUNNING'),
                        attributes={'phase':current_phase,**{k:frame[k] for k in ('step','started_at','duration_s','attempt_id','check','project') if k in frame}},**common)
            elif kind=='result':
                if results: raise ValueError('duplicate terminal result')
                results.append(frame)
            else: raise ValueError('unknown worker frame')
        def output(stream,chunk):
            if stream=='stderr':
                text=redact(chunk.decode('utf-8',errors='replace'))
                if inspecting:
                    diagnostic('stderr',text); return
                common={'job_id':job['id'],'worker_id':self.worker_id,'generation':job['generation']} if job else {}
                self.store.append_observation(self.ctx,self.workspace_id,event='control.worker.log',text=text,
                                              attributes={'stream':'stderr','phase':'transport'},**common)
                return
            buffer.extend(chunk)
            while b'\n' in buffer:
                line,_,rest=buffer.partition(b'\n'); buffer[:]=rest
                if len(line)>65536: raise ValueError('worker line exceeds limit')
                if line: append(json.loads(line))
            if len(buffer)>65536: raise ValueError('worker line exceeds limit')
        def tick():
            nonlocal last_heartbeat
            if self.stop.is_set(): raise InterruptedError('console stopping')
            if job and time.monotonic()-last_heartbeat>=10:
                self.store.heartbeat(job['id'],self.worker_id,job['generation'],lease_seconds=120)
                last_heartbeat=time.monotonic()
        timeout=45 if request['operation']=='inspect' else 75 if request['operation']=='terminal' else 1800
        failure=None
        try:
            try:
                response=self.transport(self.command,input=(json.dumps(request)+'\n').encode(),timeout=timeout,max_output_bytes=MAX_OUTPUT,
                                        on_output=output,on_tick=tick)
            except (FileNotFoundError,PermissionError) as exc:
                raise error('STEP_START_FAILED','transport',retry_policy='after_configuration',cause=exc) from exc
            if buffer or len(results)!=1: raise ValueError('missing complete worker result')
            result=results[0]
            if result.get('outcome') not in ('PASS','FAIL','BLOCKED','UNKNOWN') or type(result.get('returncode')) is not int:
                raise ValueError('invalid worker result')
            if response.returncode!=(0 if result['outcome']=='PASS' else 1): raise ValueError('worker exit and result disagree')
            return result
        except Exception as exc:
            failure=(exc if isinstance(exc,OperationError) else error('CONTROL_NOT_READY','connection',retry_policy='after_configuration',cause=exc)).as_dict()
            raise
        finally:
            if inspecting:
                try:
                    save_json(self.root/'inspect-latest.json',{'request':request,'recorded_at':time.time(),
                        'protocol_validated':failure is None,'result':redact(results[0]) if len(results)==1 else None,
                        'error':failure,'diagnostics':diagnostics.decode(),'diagnostics_truncated':truncated})
                except Exception as exc:
                    raise error('OBSERVATION_WRITE_FAILED','inspect.receipt',retry_policy='after_reconcile',cause=exc) from exc

    def inspect(self):
        with self.lock:
            if self.connection['observed_at'] is None and self.connection['error'] is None:
                self.connection['status']='CONNECTING'
        self.last_inspect=time.monotonic()
        generation=self.store.get_workspace(self.ctx,self.workspace_id)['generation']
        request={'operation':'inspect','workspace_id':self.remote_workspace_id,'job_id':str(uuid.uuid4()),'generation':generation}
        try:
            result=self._request(request)
            details=result.get('details') or {}
            if result['outcome']!='PASS' or not isinstance(details.get('hostname'),str) or type(details.get('ci_ready')) is not bool:
                raise ValueError('inspect did not observe worker readiness')
            current=self.store.get_workspace(self.ctx,self.workspace_id)
            if current['generation']!=generation or current['resource_id']!=self.config['resource_id']:
                raise error('CONTROL_CONFLICT','connection')
            if not 1<=len(details['hostname'])<=128: raise ValueError('invalid hostname')
            observed=time.time()
            self.store.record_readiness(self.ctx,self.workspace_id,resource_id=self.config['resource_id'],ready=details['ci_ready'],
                                        observed_at=observed,evidence_ref='inspect-'+request['job_id'])
            with self.lock: self.connection={'status':'CONNECTED','hostname':details['hostname'],'observed_at':observed,'error':None,
                'readiness_error':{k:v for k,v in (details.get('observation_error') or {}).items() if k in ('code','summary','phase','retry_policy')}}
            self.store.append_observation(self.ctx,self.workspace_id,event='control.connection.observed',outcome='PASS',
                                          attributes={'status':'CONNECTED','hostname':details['hostname']})
        except Exception as exc:
            failure=exc if isinstance(exc,OperationError) else error('CONTROL_NOT_READY','connection',retry_policy='after_configuration',cause=exc)
            with self.lock: self.connection.update(status='UNAVAILABLE',error=failure.as_dict())
            self.store.append_observation(self.ctx,self.workspace_id,event='control.connection.observed',outcome=failure.outcome,error=failure,
                                          attributes={'status':'UNAVAILABLE'})

    def dispatch(self,job):
        if job['kind']=='deploy': return self.dispatch_release(job)
        started=False
        try:
            payload=read_json(self.root/'requests'/(job['request_hash']+'.json'))
            if payload is None or hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()!=job['request_hash']:
                raise error('STATE_EVIDENCE_MISMATCH','request',retry_policy='after_reconcile')
            request={**payload,'workspace_id':self.remote_workspace_id,'job_id':job['id'],'generation':job['generation']}
            started=True
            result=self._request(request,job)
            detail=OperationError.from_dict(result['error']) if result.get('error') else None
            if result['outcome']!='PASS' and detail is None:
                detail=error('GATE_CHECK_FAILED' if result['outcome']=='FAIL' else 'CONTROL_NOT_READY' if result['outcome']=='BLOCKED' else 'STEP_OUTPUT_INVALID',
                             job['kind'],outcome=result['outcome'],side_effect='unknown' if result['outcome']=='UNKNOWN' else 'completed',
                             retry_policy='after_reconcile' if result['outcome']=='UNKNOWN' else 'after_configuration')
            if detail and detail.outcome!=result['outcome']: raise ValueError('result error disagrees')
            save_json(self.root/'results'/(job['id']+'.json'),result)
            self.store.append_observation(self.ctx,self.workspace_id,event='control.worker.result',outcome=result['outcome'],error=detail,
                attributes={'phase':job['kind']},job_id=job['id'],worker_id=self.worker_id,generation=job['generation'])
            self.store.complete(job['id'],self.worker_id,job['generation'],outcome=result['outcome'],error=detail.as_dict() if detail else None)
        except Exception as exc:
            if not started:
                failure=exc if isinstance(exc,OperationError) else error('STATE_EVIDENCE_MISMATCH','request',cause=exc)
            elif isinstance(exc,OperationError) and exc.code=='STEP_START_FAILED':
                failure=exc
            else:
                failure=exc if isinstance(exc,OperationError) and exc.outcome=='UNKNOWN' else error('STEP_OUTPUT_INVALID','dispatch',
                    outcome='UNKNOWN',retry_policy='after_reconcile',side_effect='unknown',cause=exc)
            try: self.store.complete(job['id'],self.worker_id,job['generation'],outcome=failure.outcome,error=failure.as_dict())
            except OperationError as record_error:
                # A stale fence already preserves UNKNOWN. Other observation failures stop dispatch entirely.
                if record_error.code!='CONTROL_LEASE_EXPIRED': raise
        try:
            self.chat.narrate_job(self.store.get_job(self.ctx,job['id']))
        except Exception as exc:
            detail=exc if isinstance(exc,OperationError) else error('OBSERVATION_WRITE_FAILED','chat.narration',retry_policy='after_reconcile',cause=exc)
            self.chat.error=detail.as_dict(); self.chat.stop.set()

    def _run(self):
        try:
            while not self.stop.is_set():
                if self.configured:
                    if time.monotonic()-self.last_inspect>=30: self.inspect()
                    with self.lock: connected=self.connection['status']=='CONNECTED'
                    if connected:
                        job=self.store.claim(self.worker_id,lease_seconds=120,tenant_id=self.ctx.tenant_id,workspace_id=self.workspace_id)
                        if job: self.dispatch(job); continue
                self.stop.wait(0.5)
        except Exception as exc:
            failure=exc if isinstance(exc,OperationError) else error('INTERNAL_ERROR','worker',outcome='UNKNOWN',side_effect='unknown',cause=exc)
            self.fatal_error=failure.as_dict(); self.stop.set()


class Handler(BaseHTTPRequestHandler):
    server_version='RAILSHOT'; protocol_version='HTTP/1.1'

    def setup(self):
        super().setup(); self.connection.settimeout(15)

    def log_message(self,*args): pass

    @property
    def app(self): return self.server.app

    def headers_ok(self,post=False):
        expected=self.server.origin
        if self.headers.get_all('Host')!=[urlsplit(expected).netloc] or self.headers.get('Origin',expected)!=expected:
            raise error('CONTROL_ACCESS_DENIED','origin')
        if post and self.headers.get('Origin')!=expected: raise error('CONTROL_ACCESS_DENIED','origin')

    def send_json(self,value,status=200,*,cookie=None):
        payload=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(payload))); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers(); self.wfile.write(payload)

    def reject(self,exc):
        failure=exc if isinstance(exc,OperationError) else error('INTERNAL_ERROR','request',cause=exc)
        status=403 if failure.code=='CONTROL_ACCESS_DENIED' else 409 if failure.code in (
            'CONTROL_CONFLICT','CONTROL_LEASE_EXPIRED','CONTROL_ALLOW_INVALID','CONTROL_NOT_READY') else 400 if failure.code in ('CONTROL_CONFIG_INVALID','INTAKE_REJECTED') else 503
        self.close_connection=True
        self.send_json({'error':failure.as_dict()},status)

    def do_GET(self):
        try:
            self.headers_ok()
            path=urlsplit(self.path).path
            if path.startswith('/api/'):
                if not self.app.authenticated(self.headers.get('Cookie')): raise error('CONTROL_ACCESS_DENIED','session')
                if path=='/api/state': self.send_json(self.app.snapshot())
                elif path=='/api/ci':
                    query=parse_qs(urlsplit(self.path).query,strict_parsing=True)
                    if set(query)!={'job_id'} or len(query['job_id'])!=1: raise error('CONTROL_CONFIG_INVALID','ci.read')
                    self.send_json(self.app.ci_run(query['job_id'][0]))
                elif path=='/api/events': self.events()
                elif path=='/api/chat': self.send_json(self.app.chat_page(urlsplit(self.path).query))
                else: self.send_json({'error':'not_found'},404)
                return
            files={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/topology.js':'topology.js','/style.css':'style.css','/assets/nuvlet-pet.png':'assets/nuvlet-pet.png'}
            if path not in files: self.send_json({'error':'not_found'},404); return
            target=PLATFORM.parent/'console'/files[path]
            payload=target.read_bytes()
            self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length',str(len(payload))); self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy',"default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(payload)
        except (BrokenPipeError,ConnectionResetError): return
        except Exception as exc: self.reject(exc)

    def do_POST(self):
        try:
            self.headers_ok(post=True)
            if self.headers.get('Content-Type','').split(';')[0]!='application/json' or self.headers.get('Transfer-Encoding'):
                raise error('CONTROL_CONFIG_INVALID','body')
            lengths=self.headers.get_all('Content-Length') or []
            if len(lengths)!=1 or not lengths[0].isdigit() or not 0<int(lengths[0])<=MAX_BODY:
                raise error('CONTROL_CONFIG_INVALID','body')
            path=urlsplit(self.path).path
            if path!='/api/session' and not self.app.authenticated(self.headers.get('Cookie')):
                raise error('CONTROL_ACCESS_DENIED','session')
            data=json.loads(self.rfile.read(int(lengths[0])))
            if path=='/api/session':
                if not isinstance(data,dict) or set(data)!={'token'}: raise error('CONTROL_CONFIG_INVALID','session')
                token=self.app.exchange(data['token'])
                self.send_json({'ok':True},cookie='railshot_session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'); return
            if not self.app.authenticated(self.headers.get('Cookie')): raise error('CONTROL_ACCESS_DENIED','session')
            if path=='/api/upload': self.send_json(self.app.upload(data),201)
            elif path=='/api/chat': self.send_json(self.app.chat_submit(data),202)
            elif path.startswith('/api/chat/') and path.endswith('/reconcile'):
                if not self.app.ctx.is_admin: raise error('CONTROL_ACCESS_DENIED','chat.reconcile')
                if data!={}: raise error('CONTROL_CONFIG_INVALID','chat.reconcile')
                self.send_json(self.app.chat.reconcile(path[len('/api/chat/'):-len('/reconcile')]))
            elif path=='/api/jobs': self.send_json(self.app.submit(data),202)
            elif path=='/api/releases/plan': self.send_json(self.app.release_plan(data),201)
            elif path=='/api/control': self.send_json(self.app.control_action(data))
            elif path=='/api/terminal': self.send_json(self.app.submit(data,terminal=True),202)
            elif path.startswith('/api/allows/'):
                allow_id=path.removeprefix('/api/allows/')
                if not isinstance(data,dict) or set(data)!={'approve'}: raise error('CONTROL_CONFIG_INVALID','allow')
                self.send_json(self.app.decide_allow(allow_id,data['approve']))
            else: self.send_json({'error':'not_found'},404)
        except (BrokenPipeError,ConnectionResetError): return
        except (ValueError,UnicodeError) as exc: self.reject(error('CONTROL_CONFIG_INVALID','body',cause=exc))
        except Exception as exc: self.reject(exc)

    def events(self):
        query=parse_qs(urlsplit(self.path).query)
        if set(query)-{'after'} or len(query.get('after',['0']))!=1: raise error('CONTROL_CONFIG_INVALID','events')
        value=self.headers.get('Last-Event-ID',query.get('after',['0'])[0])
        if len(value)>18 or not value.isdigit(): raise error('CONTROL_CONFIG_INVALID','events')
        cursor=int(value)
        if not self.app.stream_slots.acquire(blocking=False): raise error('CONTROL_CONFLICT','events')
        self.close_connection=True
        try:
            self.send_response(200); self.send_header('Content-Type','text/event-stream')
            self.send_header('Cache-Control','no-store'); self.send_header('Connection','close'); self.end_headers()
            self.wfile.write(b'retry: 1000\n\n'); self.wfile.flush()
            heartbeat=time.monotonic()
            while not self.app.stop.is_set() and self.app.authenticated(self.headers.get('Cookie')):
                rows=self.app.event_page(cursor)
                for row in rows:
                    payload=f"id: {row['sequence']}\ndata: {json.dumps(row,ensure_ascii=False)}\n\n".encode()
                    self.wfile.write(payload); cursor=row['sequence']
                if time.monotonic()-heartbeat>=15:
                    self.wfile.write(b': heartbeat\n\n'); self.wfile.flush(); heartbeat=time.monotonic()
                if rows: self.wfile.flush()
                self.app.stop.wait(0.25)
        finally: self.app.stream_slots.release()



def make_server(app,port=0):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True; server.app=app; server.origin='http://127.0.0.1:'+str(server.server_address[1])
    return server


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',required=True); parser.add_argument('--config'); parser.add_argument('--port',type=int,default=8787)
    parser.add_argument('--database-url-file',help='Private SQLAlchemy URL file; migrate it separately before startup')
    args=parser.parse_args()
    config=load_config(args.config)
    if args.database_url_file: config['database_url_file']=args.database_url_file
    app=Console(args.state_dir,config); server=make_server(app,args.port)
    def terminate(signum,frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,terminate)
    print(json.dumps({'url':server.origin+'/#token='+app.bootstrap,'scope':'single-administrator loopback','remote_configured':app.configured}),flush=True)
    app.start()
    try: server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt: pass
    finally: app.close(); server.server_close()


if __name__=='__main__': main()
