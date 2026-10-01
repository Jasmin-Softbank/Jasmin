"""Real loopback HTTP and migrated SQLite; fake remote transport, zero cloud/model calls."""
import base64
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from control.database import engine_for, upgrade
from control.server import Console, make_server, MAX_BODY, MAX_INSPECT_DIAGNOSTICS
from control.state import ControlState
from control.worker import REQUEST_BYTES
from observability import OperationError


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'private'; self.root.mkdir(mode=0o700)
        self.database=self.root/'state.sqlite3'
        engine=engine_for(self.database); upgrade(engine); engine.dispose()
        self.config={'transport_argv_prefix':['synthetic-ssh','--command'],
                     'worker_argv':['sudo','-n','/usr/bin/python3','/trusted/worker.py','--root','/trusted/work'],
                     'provider':'gcp','resource_id':'synthetic-vm'}
        self.calls=[]; self.remote_ready=True; self.behavior='pass'
        self.app=Console(self.root,self.config,transport=self.transport)
        self.addCleanup(self.app.store.engine.dispose); self.addCleanup(self.app.close)
        self.server=make_server(self.app); self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.cookie=None

    def transport(self,argv,**kwargs):
        request=json.loads(kwargs['input']); self.calls.append(request)
        self.assertEqual(argv[-1],"sudo -n /usr/bin/python3 /trusted/worker.py --root /trusted/work")
        if self.behavior=='timeout': raise subprocess.TimeoutExpired(argv,1)
        if self.behavior=='start-fail': raise FileNotFoundError('synthetic startup')
        identity={k:request[k] for k in ('workspace_id','job_id','generation')}
        if self.behavior=='wrong-generation': identity['generation']+=1
        def emit(kind,**fields):
            line=(json.dumps({'type':kind,**identity,**fields})+'\n').encode()
            kwargs['on_output']('stdout',line[:7]); kwargs['on_output']('stdout',line[7:])
        emit('log',stream='stdout',text='synthetic worker log')
        emit('stage',phase='inspect' if request['operation']=='inspect' else 'prepare',outcome='RUNNING')
        details={'hostname':'synthetic-host','ci_ready':self.remote_ready} if request['operation']=='inspect' else {'synthetic':True}
        outcome='FAIL' if self.behavior=='fail' else 'PASS'
        failure=OperationError('GATE_CHECK_FAILED',component='control.worker',phase='ci',outcome='FAIL',side_effect='completed').as_dict() if outcome=='FAIL' else None
        emit('result',outcome=outcome,returncode=9 if outcome=='FAIL' else 0,details=details,receipt={'sha256':'a'*64},error=failure)
        if self.behavior=='after-result': emit('log',stream='stdout',text='not allowed')
        return subprocess.CompletedProcess(argv,1 if outcome=='FAIL' else 0,'','')

    def http(self,method,path,body=None,**headers):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=3)
        base={'Origin':self.server.origin}
        if self.cookie: base['Cookie']=self.cookie
        if body is not None: base['Content-Type']='application/json'; body=json.dumps(body)
        connection.request(method,path,body=body,headers={**base,**headers})
        response=connection.getresponse(); data=response.read(); response_headers=dict(response.getheaders())
        connection.close()
        try: data=json.loads(data)
        except ValueError: pass
        return response.status,data,response_headers

    def login(self):
        status,_,headers=self.http('POST','/api/session',{'token':self.app.bootstrap})
        self.assertEqual(status,200); self.cookie=headers['Set-Cookie'].split(';')[0]
        return headers

    def upload(self):
        return self.app.upload({'name':'Synthetic fixture','files':[{'path':'src/main.py','content_base64':base64.b64encode(b'print(1)\n').decode()}]})

    def claimed(self,kind='prepare'):
        self.app.inspect(); self.upload()
        job=self.app.submit({'kind':kind,'idempotency_key':'synthetic-'+str(len(self.calls))})
        claimed=self.app.store.claim(self.app.worker_id,tenant_id=self.app.ctx.tenant_id,workspace_id=self.app.workspace_id)
        self.assertEqual(job['id'],claimed['id']); return claimed

    def test_auth_one_time_bootstrap_origin_host_and_static_allowlist(self):
        self.assertEqual(self.http('GET','/api/state')[0],403)
        token=self.app.bootstrap; headers=self.login()
        self.assertIn('HttpOnly',headers['Set-Cookie']); self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        self.assertEqual(self.http('POST','/api/session',{'token':token})[0],403)
        self.assertEqual(self.http('GET','/api/state',Origin='https://attacker.invalid')[0],403)
        self.assertEqual(self.http('GET','/api/state',Host='localhost:'+str(self.server.server_address[1]))[0],403)
        self.assertEqual(self.http('GET','/../../platform/control/state.py')[0],404)
        status,state,_=self.http('GET','/api/state'); self.assertEqual(status,200)
        self.assertEqual(state['connection']['status'],'UNAVAILABLE'); self.assertFalse(state['workspace']['ready'])
        self.assertEqual(state['allows'],[]); self.assertFalse(state['capabilities']['browser'])
        self.assertNotIn('credential_ref',json.dumps(state))

    def test_upload_secret_traversal_blocked_before_persist_and_limits_match_worker(self):
        self.assertEqual(MAX_BODY,REQUEST_BYTES)
        for name,content in [('../escape',b'x'),('.envrc',b'export X=x'),('src/key.py',b'-----BEGIN PRIVATE KEY-----')]:
            with self.subTest(name=name),self.assertRaises(OperationError):
                self.app.upload({'files':[{'path':name,'content_base64':base64.b64encode(content).decode()}]})
        self.assertIsNone(self.app.current_upload()); self.assertEqual(list((self.root/'uploads').iterdir()),[])
        self.upload(); self.assertEqual(len(self.app.snapshot()['files']),1)
        self.assertFalse((self.root/'src/main.py').exists())
        self.assertEqual(os.stat(self.root/'current-upload.json').st_mode&0o777,0o600)

    def test_observed_connectivity_is_not_ci_readiness(self):
        self.remote_ready=False; self.app.inspect(); self.upload()
        state=self.app.snapshot(); self.assertEqual(state['connection']['status'],'CONNECTED')
        self.assertTrue(state['capabilities']['prepare']); self.assertFalse(state['capabilities']['ci'])
        with self.assertRaises(OperationError) as caught: self.app.submit({'kind':'ci','idempotency_key':'ci'})
        self.assertEqual(caught.exception.code,'CONTROL_NOT_READY')

    def test_selected_root_idempotency_and_immutable_upload_binding(self):
        self.app.inspect(); upload=self.upload()
        data={'kind':'prepare','idempotency_key':'same','selected_root':'apps/api'}
        first=self.app.submit(data); again=self.app.submit(data)
        self.assertEqual(first['id'],again['id'])
        payload=json.loads((self.root/'requests'/(first['request_hash']+'.json')).read_text())
        self.assertEqual(payload['selected_root'],'apps/api'); self.assertEqual(payload['upload_id'],upload['upload_id'])
        with self.assertRaises(OperationError): self.app.submit({**data,'selected_root':'../escape'})
        with self.assertRaises(OperationError): self.app.submit({**data,'selected_root':'different'})
        self.upload(); self.assertEqual(payload['upload_id'],upload['upload_id'])

    def test_dispatch_pass_and_fail_capture_logs_and_preserve_result(self):
        job=self.claimed(); self.app.dispatch(job)
        self.assertEqual(self.app.store.get_job(self.app.ctx,job['id'])['status'],'PASS')
        self.assertTrue(any('synthetic worker log' in row['text'] for row in self.app.snapshot()['logs']))
        job=self.claimed(); self.behavior='fail'; self.app.dispatch(job)
        failed=self.app.store.get_job(self.app.ctx,job['id'])
        self.assertEqual(failed['status'],'FAIL'); self.assertEqual(failed['error']['code'],'GATE_CHECK_FAILED')
        self.assertEqual(self.app.snapshot()['last_result']['returncode'],9)

    def test_ci_api_preserves_live_stage_identity_and_upload_boundary(self):
        self.login(); job=self.claimed('ci')
        fence={'job_id':job['id'],'worker_id':self.app.worker_id,'generation':job['generation']}
        for outcome in ('RUNNING','PASS'):
            self.app.store.append_observation(self.app.ctx,self.app.workspace_id,event='control.worker.stage',outcome=outcome,
                attributes={'phase':'L0','started_at':'2026-10-01T00:00:00Z','duration_s':1.2 if outcome=='PASS' else None},**fence)
        self.app.store.append_observation(self.app.ctx,self.app.workspace_id,event='control.worker.log',text='bounded gate log\n',attributes={'phase':'L0'},**fence)
        status,run,_=self.http('GET','/api/ci?job_id='+job['id'])
        self.assertEqual(status,200); self.assertEqual((run['completed'],run['passed']),(1,1))
        self.assertEqual(run['steps'][0]['duration_s'],1.2); self.assertEqual(run['steps'][1]['status'],'QUEUED')
        self.assertEqual(run['logs'][0]['text'],'bounded gate log\n')
        self.assertEqual(self.app.snapshot()['ci']['job_id'],job['id'])
        self.assertTrue(self.app.snapshot()['jobs'][0]['same_upload'])
        self.app.dispatch(job); self.upload()
        self.assertIsNone(self.app.snapshot()['ci']); self.assertFalse(self.app.snapshot()['jobs'][0]['same_upload'])
        self.assertEqual(self.http('GET','/api/ci?job_id='+job['id']+'&job_id='+job['id'])[0],400)

    def test_unknown_remote_timeout_holds_slot_and_cannot_reclaim(self):
        job=self.claimed(); self.behavior='timeout'; self.app.dispatch(job)
        self.assertEqual(self.app.store.get_job(self.app.ctx,job['id'])['status'],'UNKNOWN')
        self.assertIsNone(self.app.store.claim(self.app.worker_id))
        with self.assertRaises(OperationError): self.app.control_action({'action':'acquire'})

    def test_predispatch_missing_evidence_and_spawn_failure_are_blocked(self):
        job=self.claimed(); (self.root/'requests'/(job['request_hash']+'.json')).unlink()
        before=len(self.calls); self.app.dispatch(job)
        self.assertEqual(len(self.calls),before)
        self.assertEqual(self.app.store.get_job(self.app.ctx,job['id'])['status'],'BLOCKED')
        job=self.claimed(); self.behavior='start-fail'; self.app.dispatch(job)
        record=self.app.store.get_job(self.app.ctx,job['id'])
        self.assertEqual(record['status'],'BLOCKED'); self.assertEqual(record['error']['side_effect'],'none')

    def test_forged_generation_and_frames_after_result_never_pass(self):
        for behavior in ('wrong-generation','after-result'):
            with self.subTest(behavior=behavior):
                self.behavior=behavior
                request={'operation':'inspect','workspace_id':self.app.remote_workspace_id,'job_id':'synthetic','generation':1}
                with self.assertRaises((OperationError,ValueError)): self.app._request(request)

    def test_control_excludes_ci_and_terminal_requires_actual_lease(self):
        self.app.inspect(); self.upload()
        with self.assertRaises(OperationError): self.app.submit({'command':'pwd'},terminal=True)
        self.app.control_action({'action':'acquire'})
        with self.assertRaises(OperationError): self.app.submit({'kind':'ci','idempotency_key':'ci'})
        terminal=self.app.submit({'command':'pwd','idempotency_key':'term'},terminal=True)
        job=self.app.store.claim(self.app.worker_id); self.assertEqual(job['id'],terminal['id'])
        self.app.dispatch(job); self.app.control_action({'action':'release'})
        job=self.claimed('ci')
        with self.assertRaises(OperationError): self.app.control_action({'action':'acquire'})

    def test_sse_query_cursor_and_last_event_id_precedence(self):
        self.login(); self.app.inspect(); before=self.app.snapshot()['cursor']
        upload=self.upload(); next_rows=self.app.event_page(before)
        self.assertEqual(len(next_rows),1)
        for query,headers in [(before,{}),(0,{'Last-Event-ID':str(before)})]:
            connection=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=3)
            connection.request('GET','/api/events?after='+str(query),headers={'Cookie':self.cookie,**headers})
            response=connection.getresponse(); self.assertEqual(response.status,200)
            self.assertEqual(response.readline(),b'retry: 1000\n'); response.readline()
            self.assertEqual(response.readline().decode().strip(),'id: '+str(next_rows[0]['sequence']))
            data=json.loads(response.readline().decode().removeprefix('data: '))
            self.assertEqual(data['attributes']['upload_id'],upload['upload_id']); response.close(); connection.close()

    def test_restart_replays_durable_events_without_session_or_remote_call(self):
        self.app.inspect(); self.upload(); old=self.app.snapshot()['cursor']
        session=self.app.exchange(self.app.bootstrap)
        self.app.close()
        other=Console(self.root,self.config,transport=self.transport)
        try:
            self.assertFalse(other.authenticated('railshot_session='+session))
            self.assertTrue(other.event_page(0)); self.assertGreater(other.snapshot()['cursor'],old)
            self.assertEqual(other.snapshot()['connection']['status'],'UNAVAILABLE')
        finally: other.close(); other.store.engine.dispose()

    def test_post_remote_result_storage_failure_is_unknown(self):
        job=self.claimed()
        with patch('control.server.save_json',side_effect=FileNotFoundError('synthetic disappeared evidence directory')):
            self.app.dispatch(job)
        record=self.app.store.get_job(self.app.ctx,job['id'])
        self.assertEqual(record['status'],'UNKNOWN'); self.assertEqual(record['error']['side_effect'],'unknown')

    def test_http_job_selected_root_and_log_newline(self):
        self.login(); self.app.inspect(); self.upload()
        status,job,_=self.http('POST','/api/jobs',{'kind':'prepare','idempotency_key':'http-prepare','selected_root':None})
        self.assertEqual(status,202)
        self.app.dispatch(self.app.store.claim(self.app.worker_id))
        self.assertEqual(self.app.store.get_job(self.app.ctx,job['id'])['status'],'PASS')
        logs=self.app.snapshot()['logs']; self.assertTrue(logs); self.assertTrue(all(row['text'].endswith('\n') for row in logs))

    def test_native_child_streaming_protocol_without_shell(self):
        from process import run_bounded
        script = "import json,sys; r=json.loads(sys.stdin.readline()); i={k:r[k] for k in ('workspace_id','job_id','generation')}; print(json.dumps(dict(i,type='log',stream='stdout',text='native subprocess line')),flush=True); print(json.dumps(dict(i,type='result',outcome='PASS',returncode=0,details={'hostname':'native-test','ci_ready':False})),flush=True)"
        self.app.transport=run_bounded; self.app.command=[sys.executable,'-c',script]
        self.app.inspect()
        self.assertEqual(self.app.snapshot()['connection']['hostname'],'native-test')
        self.assertFalse(self.app.snapshot()['capabilities']['ci'])
        self.assertEqual(self.app.snapshot()['logs'],[])
        self.assertIn('native subprocess line',json.loads((self.root/'inspect-latest.json').read_text())['diagnostics'])

    def test_snapshot_over_200_events_returns_recent_logs_and_replay_cursor(self):
        for i in range(205):
            self.app.store.append_observation(self.app.ctx,self.app.workspace_id,event='control.worker.log',text=f'line {i}\n')
        state=self.app.snapshot()
        self.assertTrue(state['logs_truncated']); self.assertEqual(len(state['logs']),200)
        self.assertEqual(state['logs'][0]['text'],'line 5\n'); self.assertEqual(state['logs'][-1]['text'],'line 204\n')
        self.assertEqual(self.app.event_page(state['cursor']),[])
        seq=self.app.store.append_observation(self.app.ctx,self.app.workspace_id,event='control.worker.log',text='new line\n')
        replay=self.app.event_page(state['cursor']); self.assertEqual([row['sequence'] for row in replay],[seq])
        self.assertEqual(replay[0]['attributes']['text'],'new line\n')

    def test_sse_connection_limit_and_expired_session_close(self):
        self.login(); active=[]
        try:
            for _ in range(4):
                connection=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=3)
                connection.request('GET','/api/events?after=999999',headers={'Cookie':self.cookie})
                response=connection.getresponse(); self.assertEqual(response.status,200)
                self.assertEqual(response.readline(),b'retry: 1000\n'); response.readline()
                active.append((connection,response))
            self.assertEqual(self.http('GET','/api/events?after=999999')[0],409)
            self.app.session_expires=time.time()-1
            for _,response in active: self.assertEqual(response.read(),b'')
        finally:
            for connection,response in active: response.close(); connection.close()

    def test_concurrent_log_between_event_and_diagnostic_read_is_not_lost(self):
        cursor=self.app.snapshot()['cursor']
        original=self.app.store.diagnostics
        inserted=[]
        def raced(*args,**kwargs):
            if not inserted:
                inserted.append(self.app.store.append_observation(self.app.ctx,self.app.workspace_id,event='control.worker.log',text='concurrent line\n'))
            return original(*args,**kwargs)
        with patch.object(self.app.store,'diagnostics',side_effect=raced):
            first=self.app.event_page(cursor)
        self.assertEqual(first,[])
        replay=self.app.event_page(cursor)
        self.assertEqual(replay[0]['sequence'],inserted[0]); self.assertEqual(replay[0]['attributes']['text'],'concurrent line\n')

    def test_build_roots_come_from_current_upload_prepare_receipt_only(self):
        from control.server import save_json
        job=self.claimed()
        result={'outcome':'BLOCKED','returncode':2,'details':{'plan':{'status':'NEEDS_SELECTION',
                'projects':[{'build_root':'apps/api'},{'build_root':'apps/web'}]}}}
        save_json(self.root/'results'/(job['id']+'.json'),result)
        self.assertEqual(self.app.snapshot()['build_roots'],['apps/api','apps/web'])
        self.upload()
        self.assertEqual(self.app.snapshot()['build_roots'],[])

    def test_inspect_refresh_keeps_last_observation_until_real_failure(self):
        self.app.inspect(); before=self.app.snapshot()['connection']
        entered=threading.Event(); release=threading.Event()
        def delayed(argv,**kwargs):
            entered.set()
            if not release.wait(3): raise TimeoutError('synthetic test wait')
            return self.transport(argv,**kwargs)
        self.app.transport=delayed
        thread=threading.Thread(target=self.app.inspect); thread.start()
        try:
            self.assertTrue(entered.wait(2))
            current=self.app.snapshot()
            self.assertEqual(current['connection'],before)
            self.assertTrue(current['capabilities']['prepare']); self.assertTrue(current['capabilities']['ci'])
        finally: release.set(); thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.app.snapshot()['connection']['status'],'CONNECTED')
        self.behavior='timeout'; self.app.transport=self.transport; self.app.inspect()
        current=self.app.snapshot()
        self.assertEqual(current['connection']['status'],'UNAVAILABLE')
        self.assertEqual(current['connection']['error']['code'],'CONTROL_NOT_READY')
        self.assertFalse(current['capabilities']['ci'])
        self.assertEqual(current['logs'],[])

    def test_inspect_noise_stays_bounded_private_and_protocol_checks_remain(self):
        def noisy(argv,**kwargs):
            for _ in range(6): kwargs['on_output']('stderr',b'warning '+b'x'*60000+b'\n')
            kwargs['on_output']('stderr',b'password=synthetic-sensitive-value\n')
            return self.transport(argv,**kwargs)
        self.app.transport=noisy; self.app.inspect()
        state=self.app.snapshot(); self.assertEqual(state['connection']['status'],'CONNECTED')
        self.assertEqual(state['logs'],[])
        self.assertFalse(any(e['event'] in ('log','control.worker.stage') for e in self.app.event_page(0)))
        path=self.root/'inspect-latest.json'; receipt=json.loads(path.read_text())
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertLessEqual(len(receipt['diagnostics'].encode()),MAX_INSPECT_DIAGNOSTICS)
        self.assertTrue(receipt['diagnostics_truncated']); self.assertTrue(receipt['protocol_validated'])
        self.assertNotIn('synthetic-sensitive-value',path.read_text())
        self.behavior='wrong-generation'; self.app.inspect()
        receipt=json.loads(path.read_text())
        self.assertFalse(receipt['protocol_validated'])
        self.assertEqual(receipt['error']['code'],'STEP_OUTPUT_INVALID')
        self.assertEqual(self.app.snapshot()['connection']['status'],'UNAVAILABLE')

    def test_inspect_private_receipt_write_failure_blocks_readiness(self):
        with patch('control.server.save_json',side_effect=OSError('synthetic private disk failure')):
            self.app.inspect()
        state=self.app.snapshot()
        self.assertEqual(state['connection']['status'],'UNAVAILABLE')
        self.assertEqual(state['connection']['error']['code'],'OBSERVATION_WRITE_FAILED')
        self.assertFalse(state['workspace']['ready']); self.assertEqual(state['logs'],[])

    def test_authenticated_chat_queue_does_not_block_ci_and_narration_is_async(self):
        self.login(); self.app.chat.command=['synthetic-chat']
        entered=threading.Event(); release=threading.Event()
        def delayed(argv,**kwargs):
            request=json.loads(kwargs['input']); entered.set()
            if not release.wait(5): raise TimeoutError('synthetic chat wait')
            result={'message_id':request['message_id'],'request_sha256':request['request_sha256'],
                    'outcome':'PASS','reply':'Synthetic model response for the test.','error':None}
            return subprocess.CompletedProcess(argv,0,json.dumps(result),'')
        self.app.chat.transport=delayed; self.app.chat.start()
        status,record,_=self.http('POST','/api/chat',{'message':'What is happening?','idempotency_key':'user-question'})
        self.assertEqual(status,202)
        try:
            self.assertTrue(entered.wait(3))
            job=self.claimed('ci'); self.app.dispatch(job)
            self.assertEqual(self.app.store.get_job(self.app.ctx,job['id'])['status'],'PASS')
            state=self.app.snapshot()['chat']
            self.assertEqual(state['pending'],2)
            self.assertEqual(len(state['messages']),1); self.assertEqual(state['messages'][0]['role'],'user')
            self.assertEqual(state['messages'][0]['status'],'RUNNING')
        finally: release.set(); self.app.chat.close()

    def test_chat_storage_failure_does_not_rewrite_completed_ci_or_hide_ci_state(self):
        job=self.claimed('ci')
        with patch.object(self.app.chat,'narrate_job',side_effect=OSError('synthetic chat storage failure')):
            self.app.dispatch(job)
        self.assertEqual(self.app.store.get_job(self.app.ctx,job['id'])['status'],'PASS')
        self.assertIsNone(self.app.fatal_error)
        self.assertEqual(self.app.chat.error['code'],'OBSERVATION_WRITE_FAILED')
        with patch.object(self.app.chat,'snapshot',side_effect=OSError('synthetic chat read failure')):
            state=self.app.snapshot()
        self.assertEqual(state['jobs'][0]['status'],'PASS')
        self.assertEqual(state['connection']['status'],'CONNECTED')
        self.assertFalse(state['chat']['configured']); self.assertEqual(state['chat']['error']['code'],'STATE_STORAGE_FAILED')

    def test_chat_history_http_cursor_is_authenticated_exclusive_and_bounded(self):
        self.assertEqual(self.http('GET','/api/chat')[0],403);self.login()
        self.app.chat.command=['synthetic-chat']
        for i in range(3):self.app.chat.submit('question '+str(i),'history-'+str(i))
        status,page,_=self.http('GET','/api/chat?limit=2');self.assertEqual(status,200)
        self.assertEqual(len(page['messages']),2);self.assertTrue(page['has_more'])
        status,older,_=self.http('GET','/api/chat?before='+str(page['before'])+'&limit=2')
        self.assertEqual(status,200);self.assertEqual(len(older['messages']),1);self.assertFalse(older['has_more'])
        self.assertLess(older['messages'][0]['sequence'],page['before'])
        for query in ('before=0','before=-1','limit=51','limit=abc','limit=1&limit=2','extra=1'):
            self.assertEqual(self.http('GET','/api/chat?'+query)[0],400)

    def test_chat_grounding_keeps_prior_ci_pass_separate_from_later_prepare_and_unobserved_deploy(self):
        ci=self.claimed('ci');self.app.dispatch(ci)
        prepare=self.app.submit({'kind':'prepare','idempotency_key':'after-ci'})
        claimed=self.app.store.claim(self.app.worker_id);self.assertEqual(claimed['id'],prepare['id'])
        self.app.dispatch(claimed)
        context=self.app.chat_context(job_id=prepare['id'])
        self.assertEqual(context['triggering_job']['kind'],'prepare')
        self.assertEqual(context['stage_summary']['last_successful_ci']['id'],ci['id'])
        self.assertEqual(context['stage_summary']['ci']['status'],'PASS')
        self.assertEqual(context['ci']['job_id'],ci['id'])
        self.assertEqual(context['ci']['total'],6)
        self.assertEqual(context['ci']['passed'],0)  # Synthetic worker supplied no individual check evidence.
        self.assertTrue(context['stage_summary']['ci']['same_upload'])
        self.assertTrue(context['stage_summary']['ci']['observed_at'])
        self.assertEqual(context['deployment']['status'],'NOT_OBSERVED')
        self.assertFalse(context['capabilities']['topology_apply'])
        self.assertEqual(context['trusted_contract']['app_schema']['max_services'],5)
        self.assertIn('job:'+ci['id'],context['evidence_refs'])
        self.assertNotIn('credential_ref',json.dumps(context))
        old=context['captured_at'];self.upload();fresh=self.app.chat_context()
        self.assertGreaterEqual(fresh['captured_at'],old)
        self.assertFalse(fresh['stage_summary']['ci']['same_upload'])

    def test_startup_requires_explicit_migration_and_does_not_create_schema(self):
        fresh=Path(self.tmp.name)/'unmigrated'
        with self.assertRaises(OperationError) as caught: Console(fresh,{})
        self.assertEqual(caught.exception.code,'CONTROL_CONFIG_INVALID')


if __name__=='__main__': unittest.main()
