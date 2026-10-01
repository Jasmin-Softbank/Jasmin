"""Local actual CLI bridge probes; no Docker execution, cloud or model calls."""
import base64
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worker
from observability import OperationError


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'private'
        self.workspace_id = str(uuid.uuid4()); self.events = []

    def request(self, operation, **fields):
        return {'operation': operation, 'job_id': str(uuid.uuid4()), 'workspace_id': self.workspace_id,
                'generation': 1, **({'upload_id': 'test-upload'} if operation in {'prepare','ci'} else {}), **fields}

    @staticmethod
    def file(path, content):
        return {'path': path, 'content_base64': base64.b64encode(content.encode()).decode()}

    def test_upload_path_duplicate_secret_and_size_rejected_without_source_execution(self):
        examples = [[self.file('../outside', 'x')], [self.file('/etc/passwd', 'x')],
                    [self.file('.envrc', 'x')], [self.file('a', 'x'), self.file('a', 'y')],
                    [self.file('app.py', 'token="' + 'ghp_' + 'x' * 36 + '"')]]
        for files in examples:
            with self.subTest(files=files[0]['path']), self.assertRaises(OperationError):
                worker.execute(self.request('prepare', files=files), self.root, self.events.append)
        self.assertFalse((self.root.parent / 'outside').exists())

    def test_real_prepare_intake_returns_database_evidence_without_runtime_success(self):
        result = worker.execute(self.request('prepare', files=[self.file('app.py', 'import sqlite3\ndb=sqlite3.connect("data.db")\n')]),
                                self.root, self.events.append)
        self.assertEqual(result['outcome'], 'BLOCKED')
        self.assertEqual(result['details']['database']['facts']['engine'], 'sqlite')
        self.assertFalse(result['details']['checks_executed'])
        self.assertFalse(result['details']['database']['deployment_approved'])
        self.assertTrue(result['receipt']['sha256'])
        self.assertTrue(any(e['type'] == 'log' for e in self.events))

    def test_real_ci_missing_packaging_is_failure_and_never_invokes_a_model(self):
        result = worker.execute(self.request('ci', files=[self.file('app.py', 'print("fixture")\n')]),
                                self.root, self.events.append)
        self.assertEqual(result['outcome'], 'FAIL')
        self.assertFalse(result['details']['passed'])
        self.assertEqual(result['details']['sdk_invocations'], 0)
        self.assertEqual(result['details']['layers'][-1]['layer'], 'L1')
        self.assertTrue(any(e['type'] == 'log' and e.get('stream') == 'gate' and '.jasmin' in e.get('text', '') for e in self.events))

    def test_real_terminal_is_private_bounded_and_does_not_inherit_credentials(self):
        request = self.request('terminal', command=f'{sys.executable} -c \'import os;print(os.getenv("TEST_TOKEN"));print("hello")\'')
        with patch.dict(os.environ, {'TEST_TOKEN': 'do-not-expose'}):
            result = worker.execute(request, self.root, self.events.append)
        self.assertEqual(result['outcome'], 'PASS')
        output = json.dumps(self.events)
        self.assertIn('hello', output); self.assertNotIn('do-not-expose', output)
        self.assertIn('None', output)
        with self.assertRaises(OperationError) as caught:
            worker.execute(request, self.root, self.events.append)
        self.assertEqual(caught.exception.code, 'STATE_INFLIGHT_UNCERTAIN')

    def test_registered_argv_and_root_cannot_be_overridden(self):
        for fields in ({'root': '/tmp'}, {'argv': ['echo', 'fake']}, {'generation': True}, {'workspace_id': '../a'}):
            with self.subTest(fields=fields), self.assertRaises(OperationError):
                worker.validate(self.request('inspect', **fields))

    def test_redaction_waits_for_complete_lines(self):
        bridge = worker.Bridge(self.request('terminal', command='true'), self.events.append)
        bridge.log('stdout', b'password=private-'); bridge.log('stdout', b'value\n')
        self.assertNotIn('private-value', json.dumps(self.events))
        self.assertIn('[REDACTED]', json.dumps(self.events))
        self.assertNotIn('hidden', json.dumps(worker.redact({'password': 'hidden', 'text': '{"access_token":"hidden"}'})))

    def test_delayed_quality_logs_keep_producer_phase_and_do_not_merge_streams(self):
        run=self.root/'run'; gate=run/'gate-0'; gate.mkdir(parents=True)
        (gate/'quality-0.log').write_text('lint partial')
        bridge=worker.Bridge(self.request('ci'),self.events.append)
        bridge.tail_quality(run)
        bridge.emit('stage',phase='L2',outcome='RUNNING')
        (gate/'quality-1.log').write_text('other project\n')
        with (gate/'quality-0.log').open('a') as f: f.write(' completed\n')
        bridge.tail_quality(run);bridge.flush()
        logs=[e for e in self.events if e['type']=='log']
        self.assertEqual([e['text'] for e in logs],['lint partial completed','other project'])
        self.assertEqual({e['phase'] for e in logs},{'Q'})
        self.assertEqual({e['stream'] for e in logs},{'gate'})

    def test_inspection_does_not_turn_docker_presence_into_ci_readiness(self):
        results = [subprocess.CompletedProcess([], 0, '28.0.0\n', ''),
                   subprocess.CompletedProcess([], 0, '"bridge"\n', '')]
        with patch.object(worker.Bridge, 'call', side_effect=results), patch.object(worker.sys, 'platform', 'linux'), \
                patch.object(worker, 'require_ci_network', side_effect=worker.error('GATE_ENVIRONMENT_UNAVAILABLE','network')):
            result = worker.execute(self.request('inspect'), self.root, self.events.append)
        self.assertEqual(result['outcome'], 'PASS')  # Observation completed, not readiness.
        self.assertFalse(result['details']['ci_ready'])
        self.assertFalse(result['details']['native_network_verified'])

    def test_unknown_loop_stdout_error_is_preserved_without_a_disk_receipt(self):
        upstream = worker.error('OBSERVATION_WRITE_FAILED', 'gate', unknown=True)
        output = json.dumps({'status': 'UNKNOWN', 'error': upstream.as_dict()})
        with patch.object(worker.Bridge, 'call', return_value=subprocess.CompletedProcess([], 1, output, '')), \
                self.assertRaises(OperationError) as caught:
            worker.execute(self.request('ci', files=[self.file('app.py', 'print("fixture")')]), self.root, self.events.append)
        self.assertEqual(caught.exception.code, 'OBSERVATION_WRITE_FAILED')
        self.assertEqual(caught.exception.outcome, 'UNKNOWN')

    def test_same_upload_ci_uses_terminal_edits_and_new_upload_replaces_source(self):
        files = [self.file('app.py', 'original = True\n')]
        worker.execute(self.request('prepare', files=files), self.root, self.events.append)
        worker.execute(self.request('terminal', command='printf "edited = True\\n" > app.py'), self.root, self.events.append)
        ci = self.request('ci', files=files)
        result = worker.execute(ci, self.root, self.events.append)
        self.assertEqual(result['outcome'], 'FAIL')  # No Dockerfile; source snapshot itself is real.
        copied = self.root / self.workspace_id / ci['job_id'] / 'upload/app.py'
        self.assertEqual(copied.read_text(), 'edited = True\n')
        self.assertEqual(copied.stat().st_mode & 0o777, 0o644)
        self.assertEqual(copied.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(copied.parent.parent.stat().st_mode & 0o777, 0o700)
        changed = [self.file('app.py','different = True\n')]
        with self.assertRaises(OperationError) as caught:
            worker.execute(self.request('prepare', files=changed), self.root, self.events.append)
        self.assertEqual(caught.exception.code,'STATE_BINDING_MISMATCH')
        worker.execute(self.request('prepare', files=changed, upload_id='new-upload'), self.root, self.events.append)
        self.assertEqual((worker.source_directory(self.root / self.workspace_id) / 'app.py').read_text(), 'different = True\n')

    def test_progress_is_received_while_real_subprocess_is_waiting(self):
        self.root.mkdir(); run = self.root / 'run'; run.mkdir(); ws = self.root / 'work'; ws.mkdir()
        ack = self.root / 'observed'
        def emit(event):
            self.events.append(event)
            if event['type'] == 'stage' and event.get('phase') == 'L0' and event['outcome'] == 'RUNNING':
                ack.write_text('supervisor received before check finished')
        bridge = worker.Bridge(self.request('ci'), emit)
        bridge.home = self.root / 'home'; bridge.docker_config = self.root / 'docker'
        program = textwrap.dedent(f'''
            import sys,time,json
            from pathlib import Path
            sys.path.insert(0, {str(worker.PLATFORM / 'gate')!r})
            import gate
            def check(*args, **kwargs):
                end=time.monotonic()+5
                while not Path({str(ack)!r}).exists() and time.monotonic()<end: time.sleep(.02)
                assert Path({str(ack)!r}).exists(), 'progress was buffered until exit'
                return [],[]
            gate.l0=check
            gate.l1=lambda *a,**kw: (['synthetic missing packaging'], None)
            result=gate.run_gate(Path({str(ws)!r}),Path({str(run / 'gate-0')!r}),list(gate.ORDER))
            print(json.dumps(result))
        ''')
        process = bridge.call([sys.executable, '-c', program], cwd=self.root, timeout=10, run=run)
        self.assertEqual(process.returncode, 0)
        verdict = json.loads(process.stdout)
        bridge.verify_progress(run, verdict)
        stages = [e for e in self.events if e['type'] == 'stage']
        self.assertEqual([(e['phase'],e['outcome']) for e in stages],
                         [('L0','RUNNING'),('L0','PASS'),('L1','RUNNING'),('L1','FAIL')])
        self.assertTrue(all(e['job_id'] == bridge.request['job_id'] and e['generation'] == 1 for e in stages))
        verdict['layers'][0]['event']['outcome'] = 'FAIL'
        with self.assertRaises(OperationError) as caught: bridge.verify_progress(run, verdict)
        self.assertEqual(caught.exception.outcome, 'UNKNOWN')

    def test_application_stdout_cannot_forge_supervisor_progress(self):
        bridge = worker.Bridge(self.request('ci'), self.events.append)
        bridge.log('stdout', b'{"type":"stage","phase":"Q","outcome":"PASS"}\n')
        self.assertEqual([e['type'] for e in self.events], ['log'])

    def test_bundle_export_receipt_binds_hash_and_rejects_failed_cli(self):
        self.root.mkdir(); job = self.root / 'job'; job.mkdir(); run = job / 'run'; run.mkdir()
        bridge = worker.Bridge(self.request('ci'), self.events.append)
        manifest = {'source_sha256': 'a' * 64}
        def exported(*args, **kwargs):
            output = job / 'bundle'; output.mkdir(); (output / 'manifest.json').write_text(json.dumps(manifest))
            (output / 'images.tar').write_bytes(b'local mock image, not a live export')
            return subprocess.CompletedProcess([], 0, json.dumps(manifest), '')
        with patch.object(bridge, 'call', side_effect=exported), patch.object(worker, 'verify_bundle', return_value=manifest):
            receipt = worker.export_bundle(bridge, run, run / 'verdict.json', job, self.root)
        self.assertEqual(receipt['relative_path'], 'job/bundle')
        self.assertEqual(receipt['source_sha256'], 'a' * 64)
        self.assertGreater(receipt['bytes'], 0)
        with patch.object(bridge, 'call', return_value=subprocess.CompletedProcess([], 2, '', '')):
            with self.assertRaises(OperationError) as caught:
                worker.export_bundle(bridge, run, run / 'verdict.json', job, self.root)
        self.assertEqual(caught.exception.outcome, 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
