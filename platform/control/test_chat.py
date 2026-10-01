"""Durable chat/SDK adapter tests: migrated local DB and fake Codex/transport, no models."""
import hashlib
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from control.chat import ChatQueue, MODEL, digest, encoded, run_once, write_once, validate_request, proposal_review, system_prompt, PROMPT, PINNED_FAILED_TURN_SHA256
from control.database import engine_for, upgrade
from control.state import ControlState, Principal
from observability import OperationError


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        engine=engine_for(self.root/'state.sqlite3');upgrade(engine);engine.dispose()
        self.store=ControlState(self.root/'state.sqlite3',Path(__file__).resolve().parents[1]/'runner/accounts.yaml')
        self.addCleanup(self.store.engine.dispose)
        self.ctx=Principal('operator','admin',True);self.store.create_workspace(self.ctx,'workspace')
        self.workspace=str(uuid.uuid4());self.calls=[];self.contexts=[]
        self.queue=self.make_queue();self.addCleanup(self.queue.close)

    def context(self,job_id=None):
        self.contexts.append(job_id);return {'captured_at':1234,'job_id':job_id,'jobs':[{'status':'RUNNING','kind':'ci'}]}

    def transport(self,argv,**kwargs):
        request=json.loads(kwargs['input']);self.calls.append(request)
        result={'message_id':request['message_id'],'request_sha256':request['request_sha256'],'outcome':'PASS',
                'reply':'Synthetic SDK response for this regression.','meta':{'provider':'codex','model':MODEL,'thread_id':'synthetic-thread'},'error':None}
        return subprocess.CompletedProcess(argv,0,json.dumps(result),'')

    def make_queue(self):
        return ChatQueue(self.root/'chat',self.store,self.ctx,'workspace',self.workspace,['synthetic-control-transport'],self.context,transport=self.transport)

    def dispatch(self,record): self.queue._dispatch(dict(record))

    def test_idempotency_is_durable_and_does_not_duplicate_sdk_request(self):
        first=self.queue.submit('What is CI doing?','same');again=self.queue.submit('What is CI doing?','same')
        self.assertEqual(first['id'],again['id']);self.assertEqual(self.queue.snapshot()['pending'],1)
        with self.assertRaises(OperationError): self.queue.submit('Changed body','same')
        self.dispatch(first);self.assertEqual(len(self.calls),1)
        self.queue.close();self.queue=self.make_queue();self.addCleanup(self.queue.close)
        retry=self.queue.submit('What is CI doing?','same');self.assertEqual(retry['status'],'PASS')
        self.assertEqual(len(self.calls),1);self.assertEqual([m['role'] for m in self.queue.snapshot()['messages']],['user','assistant'])

    def test_single_writer_and_four_pending_limit(self):
        with self.assertRaises(OperationError) as caught:self.make_queue()
        self.assertEqual(caught.exception.code,'STATE_WRITER_CONFLICT')
        for i in range(4):self.queue.submit('question '+str(i),'key-'+str(i))
        with self.assertRaises(OperationError):self.queue.submit('fifth','five')
        self.assertEqual(self.queue.snapshot()['pending'],4)
        self.assertTrue(all(m['role']=='user' for m in self.queue.snapshot()['messages']))

    def test_restart_unknown_never_dispatches_queued_or_started_again(self):
        first=self.queue.submit('question','first');self.queue.submit('next','second')
        self.queue._event('started',first['id'],sha='a'*64);self.queue.close()
        self.queue=self.make_queue();self.addCleanup(self.queue.close)
        self.assertEqual(self.queue.snapshot()['messages'][0]['status'],'UNKNOWN')
        self.queue._run();self.assertEqual(self.calls,[])
        with self.assertRaises(OperationError):self.queue.submit('again','third')

    def reconciliation_fixture(self):
        record=self.queue.submit('question','failed-request')
        def failed(argv,**kwargs):
            request=json.loads(kwargs['input']);self.calls.append(request)
            detail=OperationError('SDK_OUTCOME_UNKNOWN',component='control.chat',phase='invoke',outcome='UNKNOWN',side_effect='unknown',retry_policy='after_reconcile').as_dict()
            detail['causes']=[{'type':'builtins.RuntimeError','frames':[{'file':'_run.py','function':'_raise_for_failed_turn','line':64}]}]
            result={'message_id':request['message_id'],'request_sha256':request['request_sha256'],'outcome':'UNKNOWN','error':detail}
            return subprocess.CompletedProcess(argv,1,json.dumps(result),'')
        self.queue.transport=failed;self.dispatch(record)
        identity=record['id'];request=json.loads((self.root/'chat'/(identity+'.dispatch.json')).read_text())
        result=json.loads((self.root/'chat'/(identity+'.result.json')).read_text())
        command=str(uuid.uuid4());instance='i-synthetic';source='a'*64
        files={'binding':{'message_id':identity,'request_sha256':request['request_sha256'],'original_result_sha256':digest(result),'source_sha256':source},
            'intent':{'phase':'DISPATCHED','command_id':command,'binding':{'source_sha256':source,'instance':instance,'request_sha256':request['request_sha256']}},
            'invocation':{'CommandId':command,'InstanceId':instance,'Status':'Failed','ResponseCode':1,'StandardOutputContent':json.dumps(result),'ExecutionEndDateTime':'2026-10-01T12:03:05Z'},
            'session':{'event_name':'agent.unknown','error':result['error'],'thread_id':str(uuid.uuid4()),'turn_id':str(uuid.uuid4())},
            'sdk':{'version':'0.159.3','failed_turn_source_sha256':PINNED_FAILED_TURN_SHA256}}
        directory=self.root/'chat'/'reconciliation'/identity;directory.parent.mkdir(mode=0o700,exist_ok=True);directory.mkdir(mode=0o700)
        for name,value in files.items():
            path=directory/(name+'.json');path.write_text(json.dumps(value));path.chmod(0o600)
        return identity,directory

    def test_explicit_terminal_reconciliation_preserves_original_and_never_repeats_call(self):
        identity,directory=self.reconciliation_fixture()
        original=(self.root/'chat'/(identity+'.result.json')).read_bytes()
        self.assertEqual(self.queue.reconcile_terminal_failure(identity)['status'],'FAIL')
        self.assertEqual(len(self.calls),1)
        self.assertEqual((self.root/'chat'/(identity+'.result.json')).read_bytes(),original)
        events=self.store.events(self.ctx,workspace_id='workspace')
        self.assertEqual(events[-2]['outcome'],'UNKNOWN');self.assertEqual(events[-1]['event_name'],'control.chat.reconciled')
        self.assertEqual(events[-1]['outcome'],'FAIL')
        count=len(events);self.queue.reconcile_terminal_failure(identity)
        self.assertEqual(len(self.store.events(self.ctx,workspace_id='workspace')),count)
        self.queue.close();self.queue=self.make_queue();self.addCleanup(self.queue.close)
        self.assertEqual(self.queue.records[identity]['status'],'FAIL')
        self.assertEqual(self.queue.submit('new request','after-reconcile')['status'],'QUEUED')
        self.assertEqual(len(self.calls),1)

    def test_reconcile_rejects_nonadmin_different_message_or_command_and_missing_evidence(self):
        identity,directory=self.reconciliation_fixture()
        from control.state import Principal
        ctx=self.queue.ctx;self.queue.ctx=Principal('operator','viewer',False)
        with self.assertRaises(OperationError) as caught:self.queue.reconcile_terminal_failure(identity)
        self.assertEqual(caught.exception.code,'CONTROL_ACCESS_DENIED');self.queue.ctx=ctx
        for name,key,value in [('binding','message_id',str(uuid.uuid4())),('invocation','CommandId',str(uuid.uuid4()))]:
            path=directory/(name+'.json');before=path.read_text();data=json.loads(before);data[key]=value;path.write_text(json.dumps(data))
            with self.assertRaises(OperationError) as caught:self.queue.reconcile_terminal_failure(identity)
            self.assertEqual(caught.exception.code,'STATE_EVIDENCE_MISMATCH');path.write_text(before)
        (directory/'sdk.json').chmod(0o644)
        with self.assertRaises(OperationError):self.queue.reconcile_terminal_failure(identity)
        self.assertEqual(self.queue.records[identity]['status'],'UNKNOWN');self.assertEqual(len(self.calls),1)

    def success_reconciliation_fixture(self):
        record=self.queue.submit('Interrupted request','interrupted')
        with patch.object(self.queue,'transport',side_effect=subprocess.TimeoutExpired(['synthetic'],1)):
            self.dispatch(record)
        identity=record['id'];request=json.loads((self.root/'chat'/(identity+'.dispatch.json')).read_text())
        meta={k:str(uuid.uuid4()) for k in ('session_id','thread_id','turn_id')}
        meta.update(provider='codex',model=MODEL)
        result={'message_id':identity,'request_sha256':request['request_sha256'],'outcome':'PASS',
                'reply':'Original remote response, never a second model request.','meta':meta,'error':None}
        command=str(uuid.uuid4());source='b'*64
        files={'binding':{'message_id':identity,'request_sha256':request['request_sha256'],'original_result_sha256':None,'source_sha256':source},
            'intent':{'phase':'DISPATCHED','command_id':command,'binding':{'source_sha256':source,'instance':'i-test','request_sha256':request['request_sha256']}},
            'invocation':{'CommandId':command,'InstanceId':'i-test','Status':'Success','ResponseCode':0,'StandardOutputContent':json.dumps(result),'ExecutionEndDateTime':'2026-10-01T12:37:00Z'},
            'remote':{'request_sha256':digest(request),'result_sha256':digest(result),'sdk_version':'0.159.3',
                      'session':{'event_name':'agent.completed',**meta}}}
        directory=self.root/'chat'/'reconciliation'/identity;directory.parent.mkdir(mode=0o700,exist_ok=True);directory.mkdir(mode=0o700)
        for name,value in files.items():
            path=directory/(name+'.json');path.write_text(json.dumps(value));path.chmod(0o600)
        return identity,directory,result

    def test_success_reconciliation_restores_missing_response_append_only_and_survives_restart(self):
        identity,directory,result=self.success_reconciliation_fixture()
        self.assertEqual(self.queue.snapshot()['reconciliation_ids'],[identity])
        original_events=self.store.events(self.ctx,workspace_id='workspace')
        with patch.object(self.queue,'start'):
            self.assertEqual(self.queue.reconcile(identity)['status'],'PASS')
        self.assertFalse((self.root/'chat'/(identity+'.result.json')).exists())
        self.assertEqual(self.queue.snapshot()['messages'][-1]['text'],result['reply'])
        events=self.store.events(self.ctx,workspace_id='workspace')
        self.assertEqual(events[:-1],original_events);self.assertEqual(events[-1]['event_name'],'control.chat.reconciled')
        self.queue.reconcile(identity);self.assertEqual(len(self.store.events(self.ctx,workspace_id='workspace')),len(events))
        self.queue.close();self.queue=self.make_queue();self.addCleanup(self.queue.close)
        self.assertEqual(self.queue.snapshot()['reconciliation_ids'],[])
        self.assertEqual(self.queue.snapshot()['messages'][-1]['text'],result['reply'])
        record=self.queue.submit('Follow up','following');self.dispatch(record)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['messages'][-2]['content'],result['reply'])

    def test_success_reconciliation_requires_exact_remote_result_and_turn_identity(self):
        identity,directory,result=self.success_reconciliation_fixture()
        for field in ('result_sha256','request_sha256','session'):
            path=directory/'remote.json';original=path.read_text();changed=json.loads(original)
            changed[field]='0'*64 if field!='session' else {**changed[field],'turn_id':str(uuid.uuid4())}
            path.write_text(json.dumps(changed))
            with self.assertRaises(OperationError) as caught:self.queue.reconcile(identity)
            self.assertEqual(caught.exception.code,'STATE_EVIDENCE_MISMATCH');path.write_text(original)
        self.assertEqual(self.queue.records[identity]['status'],'UNKNOWN')
        self.assertFalse((self.root/'chat'/(identity+'.reconciliation.json')).exists());self.assertEqual(self.calls,[])

    def test_close_drains_started_call_and_keeps_queued_work_without_new_dispatch(self):
        entered=threading.Event();finish=threading.Event()
        def transport(argv,**kwargs):
            entered.set();self.assertTrue(finish.wait(5))
            if kwargs.get('on_tick'): kwargs['on_tick']()
            return self.transport(argv,**kwargs)
        self.queue.transport=transport
        first=self.queue.submit('Active','active');second=self.queue.submit('Queued','queued');self.queue.start()
        self.assertTrue(entered.wait(2));closer=threading.Thread(target=self.queue.close);closer.start()
        self.assertTrue(self.queue.stop.wait(2))
        with self.assertRaises(OperationError) as caught:self.queue.submit('During shutdown','late')
        self.assertEqual(caught.exception.phase,'chat.draining');self.assertTrue(closer.is_alive())
        finish.set();closer.join(5);self.assertFalse(closer.is_alive())
        self.assertEqual(self.queue.records[first['id']]['status'],'PASS')
        self.assertEqual(self.queue.records[second['id']]['status'],'QUEUED');self.assertEqual(len(self.calls),1)
        self.queue=self.make_queue();self.addCleanup(self.queue.close)
        self.assertEqual(self.queue.records[first['id']]['status'],'PASS')
        self.assertEqual(self.queue.records[second['id']]['status'],'QUEUED')

    def test_history_and_context_are_fixed_at_dispatch_after_previous_reply(self):
        first=self.queue.submit('First question','first');second=self.queue.submit('Follow up','second')
        self.dispatch(first);self.dispatch(second)
        self.assertEqual([m['role'] for m in self.calls[1]['messages']],['user','assistant','user'])
        self.assertEqual(self.calls[1]['messages'][-1]['content'],'Follow up')
        self.assertEqual(self.calls[1]['context']['jobs'][0]['status'],'RUNNING')
        saved=json.loads((self.root/'chat'/(second['id']+'.dispatch.json')).read_text())
        self.assertEqual(saved,self.calls[1]);self.assertEqual(saved['request_sha256'],digest({k:saved[k] for k in ('workspace_id','messages','context')}))

    def test_narration_is_job_bound_once_and_never_renders_a_fake_user(self):
        job={'id':str(uuid.uuid4()),'kind':'ci'}
        self.queue.narrate_job(job);self.queue.narrate_job(job)
        self.assertEqual(self.queue.snapshot()['pending'],1);self.assertEqual(self.queue.snapshot()['messages'],[])
        record=next(iter(self.queue.records.values()));self.dispatch(record)
        messages=self.queue.snapshot()['messages'];self.assertEqual(len(messages),1)
        self.assertEqual(messages[0]['role'],'assistant');self.assertEqual(messages[0]['origin'],'job_result')
        self.assertEqual(messages[0]['job_id'],job['id']);self.assertEqual(self.contexts,[job['id']])

    def test_narration_full_queue_is_durably_blocked_without_changing_other_jobs(self):
        for i in range(4):self.queue.submit('question','key-'+str(i))
        self.queue.narrate_job({'id':str(uuid.uuid4()),'kind':'prepare'})
        self.assertEqual(self.queue.snapshot()['pending'],4)
        event=self.store.events(self.ctx,workspace_id='workspace')[-1]
        self.assertEqual(event['event_name'],'control.chat.result');self.assertEqual(event['outcome'],'BLOCKED')
        self.assertEqual(event['error']['code'],'CONTROL_CONFLICT');self.assertEqual(self.calls,[])

    def test_timeout_or_wrong_result_never_invents_reply(self):
        record=self.queue.submit('question','first')
        with patch.object(self.queue,'transport',side_effect=subprocess.TimeoutExpired(['synthetic'],1)):
            self.dispatch(record)
        messages=self.queue.snapshot()['messages'];self.assertEqual(len(messages),1);self.assertEqual(messages[0]['status'],'UNKNOWN')
        self.assertEqual(messages[0]['error']['code'],'SDK_OUTCOME_UNKNOWN')

    def test_missing_request_artifact_blocks_before_transport(self):
        record=self.queue.submit('question','first');(self.root/'chat'/(record['id']+'.request.json')).unlink()
        self.dispatch(record)
        self.assertEqual(self.queue.records[record['id']]['status'],'BLOCKED');self.assertEqual(self.calls,[])

    def test_history_pages_keep_all_records_across_restart_and_concurrent_append(self):
        expected=[]
        for index in range(55):
            record=self.queue.submit('question '+str(index),'history-'+str(index));expected.append(record['id'])
            self.dispatch(record)
        self.queue.close();self.queue=self.make_queue();self.addCleanup(self.queue.close)
        page=self.queue.snapshot();self.assertEqual(len(page['messages']),40);self.assertTrue(page['has_more'])
        self.assertTrue(all(type(m['sequence']) is int and m['created_at'] for m in page['messages']))
        pages=[page];new=self.queue.submit('new after cursor','new-cursor');self.dispatch(new)
        while pages[-1]['has_more']:
            pages.append(self.queue.page(before=pages[-1]['before']))
        found=[m['id'] for p in reversed(pages) for m in p['messages'] if m['role']=='user']
        self.assertEqual(found,expected);self.assertEqual(len(set(found)),55)
        self.assertEqual(len(self.queue.records),56)
        for fields in ({'before':0},{'before':True},{'limit':0},{'limit':51}):
            with self.subTest(fields=fields),self.assertRaises(OperationError):self.queue.page(**fields)

    def test_bounded_history_preserves_user_message_after_long_system_narration(self):
        self.queue.context=lambda **kwargs:{'captured_at':1,'known_data':'x'*10000}
        first=self.queue.submit('internal narration','narration',origin='job_result')
        def transport(argv,**kwargs):
            request=json.loads(kwargs['input']);self.calls.append(request)
            result={'message_id':request['message_id'],'request_sha256':request['request_sha256'],
                    'outcome':'PASS','reply':'가'*2666,'error':None}
            return subprocess.CompletedProcess(argv,0,json.dumps(result),'')
        self.queue.transport=transport;self.dispatch(first)
        second=self.queue.submit('u'*3500,'question');self.dispatch(second)
        self.assertEqual(self.queue.records[second['id']]['status'],'PASS')
        self.assertEqual(self.calls[-1]['messages'],[{'role':'user','content':'u'*3500}])

    def topology(self):
        return {'version':1,'status':'DRAFT','intent':'create',
            'nodes':[{'id':'web','kind':'service','label':'Web application','managed_by':'argocd'},
                     {'id':'cluster','kind':'cluster','label':'App cluster','managed_by':'ansible'}],
            'edges':[{'from':'web','to':'cluster','kind':'runs_on'}],'assumptions':[],
            'required_capabilities':['application_deploy'],'evidence_refs':['contract:app-schema']}

    def grounded_context(self):
        return {'evidence_refs':['contract:app-schema'],'trusted_contract':{
            'topology_owners':{'service':['argocd'],'cluster':['ansible']},'app_schema':{'max_services':5}}}

    def test_topology_render_data_is_validated_and_never_becomes_an_apply_plan(self):
        draft=self.topology();context=self.grounded_context();review=proposal_review(draft,context)
        self.assertFalse(review['apply_supported']);self.assertEqual(review['status'],'REVIEW_REQUIRED')
        self.assertEqual(review['conflicts'],[]);self.assertEqual(review['contract_sha256'],digest(context['trusted_contract']))
        described=copy.deepcopy(draft);described['intent']='describe'
        self.assertFalse(proposal_review(described,context)['apply_supported'])
        wrong=copy.deepcopy(draft);wrong['nodes'][0]['managed_by']='terraform'
        self.assertEqual(proposal_review(wrong,context)['conflicts'][0]['reason'],'ownership_not_supported')
        invalid=[]
        item=copy.deepcopy(draft);item['edges'][0]['to']='invented';invalid.append(item)
        item=copy.deepcopy(draft);item['nodes'][1]['id']='web';invalid.append(item)
        item=copy.deepcopy(draft);item['nodes'][0]['label']='<svg onload=alert(1)>';invalid.append(item)
        item=copy.deepcopy(draft);item['status']='APPLIED';invalid.append(item)
        item=copy.deepcopy(draft);item['intent']='execute';invalid.append(item)
        item=copy.deepcopy(draft);item['evidence_refs']=['job:invented'];invalid.append(item)
        item=copy.deepcopy(draft);item['nodes']*=13;invalid.append(item)
        for value in invalid:
            with self.subTest(value=value),self.assertRaises(OperationError) as caught:proposal_review(value,context)
            self.assertEqual(caught.exception.code,'SDK_OUTPUT_INVALID')

    def test_remote_terminal_failure_preserves_safe_ids_without_blocking_as_unknown(self):
        request=self.request()
        def failed(cfg,system,task,schema,workspace,run,deny,emit):
            detail=OperationError('SDK_EXECUTION_FAILED',component='runner',phase='invoke',outcome='FAIL',side_effect='completed')
            emit('session.finished',sdk_status='failed',thread_id='synthetic-thread',turn_id='synthetic-turn',
                 sdk_failure={'category':'invalid_output_schema','message':'The provider rejected the structured output schema.','message_sha256':'a'*64,'codes':['other']},error=detail)
            raise detail
        with patch('control.chat.sys.platform','linux'),patch('control.chat.run_codex',side_effect=failed):
            result=run_once(request,self.root/'remote')
        self.assertEqual(result['outcome'],'FAIL');self.assertEqual(result['meta']['sdk_status'],'failed')
        self.assertEqual(result['meta']['thread_id'],'synthetic-thread')
        self.assertEqual(result['meta']['sdk_failure']['category'],'invalid_output_schema')

    def test_remote_prompt_file_and_draft_are_bound_to_response_receipt(self):
        request=self.request();request['context']=self.grounded_context()
        request['request_sha256']=digest({k:request[k] for k in ('workspace_id','messages','context')})
        def sdk(cfg,system,*args):
            self.assertEqual(system,PROMPT.read_text());self.assertIn('Prepare PASS',system)
            self.assertIn('cannot itself start CI',system)
            return {'reply':'A draft for review only.','proposal':self.topology()},{'thread_id':'synthetic'}
        with patch('control.chat.sys.platform','linux'),patch('control.chat.run_codex',side_effect=sdk):
            result=run_once(request,self.root/'remote')
        self.assertEqual(result['outcome'],'PASS');self.assertFalse(result['proposal_review']['apply_supported'])
        self.assertEqual(result['meta']['prompt_sha256'],hashlib.sha256(system_prompt().encode()).hexdigest())


    def request(self):
        payload={'workspace_id':self.workspace,'messages':[{'role':'user','content':'Explain CI state'}], 'context':{'ci':'RUNNING'}}
        return {**payload,'message_id':str(uuid.uuid4()),'request_sha256':digest(payload)}

    def sdk(self,cfg,system,task,schema,workspace,run,deny,emit):
        self.assertEqual(cfg['model'],MODEL);self.assertTrue(deny);self.assertEqual(schema['required'],['reply'])
        emit('session.starting',sdk_status='running');emit('session.started',session_id='session',thread_id='thread')
        emit('session.finished',sdk_status='completed',turn_id='turn')
        return {'reply':'Synthetic response from mocked SDK.'},{'session_id':'session','thread_id':'thread','turn_id':'turn','conversation_resume':'unsupported'}

    def test_remote_reuses_existing_run_codex_and_terminal_receipt_without_second_call(self):
        request=self.request()
        with patch('control.chat.sys.platform','linux'),patch('control.chat.run_codex',side_effect=self.sdk) as sdk:
            first=run_once(request,self.root/'remote');second=run_once(request,self.root/'remote')
        self.assertEqual(first,second);self.assertEqual(sdk.call_count,1);self.assertEqual(first['meta']['model'],MODEL)
        self.assertEqual(first['meta']['conversation_resume'],'unsupported')

    def test_remote_interrupted_identity_is_unknown_and_cannot_call_sdk(self):
        request=self.request();directory=self.root/'remote'/request['message_id'];directory.mkdir(parents=True)
        write_once(directory/'request.json',request)
        with patch('control.chat.sys.platform','linux'),patch('control.chat.run_codex') as sdk:
            with self.assertRaises(OperationError) as caught:run_once(request,self.root/'remote')
        self.assertEqual(caught.exception.code,'SDK_OUTCOME_UNKNOWN');sdk.assert_not_called()

    def test_bad_binding_and_oversized_reply_are_blocked_without_fake_text(self):
        request=self.request();request['request_sha256']='b'*64
        with self.assertRaises(OperationError):validate_request(request)
        request=self.request()
        def oversized(*args,**kwargs):return {'reply':'x'*8001},{'model':MODEL}
        with patch('control.chat.sys.platform','linux'),patch('control.chat.run_codex',side_effect=oversized):
            result=run_once(request,self.root/'remote')
        self.assertEqual(result['outcome'],'FAIL');self.assertEqual(result['error']['code'],'SDK_OUTPUT_INVALID')
        self.assertNotIn('reply',result)


if __name__=='__main__':unittest.main()
