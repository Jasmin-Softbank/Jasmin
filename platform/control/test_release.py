"""Local contract tests; transport doubles are not live publication evidence."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control import release


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.image_id = 'sha256:' + 'a' * 64
        self.local_ref = 'railshot-gate/demo-web:test'
        self.images = {'web': 'registry.example/project/demo-web@sha256:' + 'b' * 64}
        (self.bundle / 'jasmin.yaml').write_text('apiVersion: jasmin/v0\napp: demo\nservices:\n'
            '  - name: web\n    build: {dockerfile: Dockerfile}\n    port: 3000\n    route: /\n')
        self.verdict = {'ok': True, 'release_eligible': True, 'status': 'PASS',
            'layers': [{'layer': name, 'ok': True} for name in release.bundle.LAYERS],
            'source_sha256': 'c' * 64, 'images': {'web': self.local_ref}, 'image_ids': {'web': self.image_id}}
        (self.bundle / 'verdict.json').write_text(json.dumps(self.verdict))
        (self.bundle / 'images.tar').write_bytes(b'synthetic archive; never published by this test')
        self.write_manifest()
        self.target = {'workspace_id': str(uuid.uuid4()), 'generation': 1, 'tenant': 'demo',
            'cluster': 'gcp', 'repo_url': 'https://github.com/example/gitops',
            'registry_prefix': 'registry.example/project/demo', 'tag': 'test1',
            'domain': '127-0-0-1.sslip.io', 'suffix': 'abcdef', 'storage_class': 'local-path',
            'gitops_revision': 'd' * 40}
        self.plan = release.prepare_release(self.bundle, self.target)
        self.operation = {'id': str(uuid.uuid4()), 'allow_id': str(uuid.uuid4()),
            'tenant_id': 'demo', 'workspace_id': self.target['workspace_id'], 'generation': 1,
            'operation': 'deploy', 'plan_hash': self.plan['plan_hash']}

    def write_manifest(self):
        manifest = {'version': 1, 'trust': release.bundle.TRUST, 'source_sha256': 'c' * 64,
            'images': {'web': {'local_ref': self.local_ref, 'id': self.image_id}},
            'files': {name: release.bundle.file_hash(self.bundle / name) for name in release.bundle.FILES}}
        (self.bundle / 'manifest.json').write_text(json.dumps(manifest))

    def run_release(self, *, observe_status='PASS', publish_error=None):
        writer = Mock()
        writer.preflight.return_value = self.target['gitops_revision']
        writer.create_commit.return_value = 'e' * 40
        error = None if observe_status == 'PASS' else release.fail('CD_OBSERVATION_TIMEOUT', 'observe', unknown=True)
        observer = Mock(return_value=(observe_status, error,
                        {'metadata': {'uid': 'synthetic'}, 'status': {'sync': {'revision': 'e' * 40}}}))
        with patch.object(release, 'GitHubWriter', return_value=writer), \
                patch.object(release.bundle, 'publish', side_effect=publish_error, return_value=self.images) as publish:
            result = release.execute_release(self.plan, self.bundle, self.operation,
                                             work_dir=self.root / 'run', observer=observer)
        return result, writer, publish, observer

    def test_prepare_is_deterministic_and_target_is_bound(self):
        self.assertEqual(self.plan, release.prepare_release(self.bundle, self.target))
        changed = {**self.target, 'domain': 'example.com'}
        self.assertNotEqual(self.plan['plan_hash'], release.prepare_release(self.bundle, changed)['plan_hash'])

    def test_approved_binding_cannot_be_changed(self):
        for key, value in [('generation', 2), ('tenant_id', 'other'), ('plan_hash', '0' * 64), ('operation', 'vm.start')]:
            with self.subTest(key=key), self.assertRaises(release.OperationError), patch.object(release.bundle, 'publish') as publish:
                release.execute_release(self.plan, self.bundle, {**self.operation, key: value}, work_dir=self.root / 'run', observer=Mock())
                publish.assert_not_called()

    def test_full_argo_receipt_does_not_claim_http_or_tls(self):
        result, writer, publish, observer = self.run_release()
        self.assertEqual('PASS', result['status'])
        self.assertEqual('NOT_VERIFIED', result['external_access'])
        self.assertEqual('NOT_VERIFIED', result['pod_image_identity'])
        publish.assert_called_once()
        files = writer.create_commit.call_args.args[2]
        self.assertIn(self.images['web'], files['20-app.yaml'])
        self.assertEqual('apps/gcp/demo/demo', writer.create_commit.call_args.args[1])
        self.assertEqual('e' * 40, observer.call_args.args[1])

    def test_interrupted_publication_never_replays_automatically(self):
        with self.assertRaises(release.OperationError):
            self.run_release(publish_error=OSError('synthetic secret must not escape'))
        saved = (self.root / 'run/release.json').read_text()
        self.assertNotIn('synthetic secret', saved)
        with patch.object(release.bundle, 'publish') as publish, self.assertRaises(release.OperationError) as caught:
            release.execute_release(self.plan, self.bundle, self.operation, work_dir=self.root / 'run', observer=Mock())
        self.assertEqual('STATE_INFLIGHT_UNCERTAIN', caught.exception.code)
        publish.assert_not_called()

    def test_timeout_is_unknown_and_never_pass(self):
        result, *_ = self.run_release(observe_status='UNKNOWN')
        self.assertEqual('UNKNOWN', result['status'])
        self.assertEqual('CD_OBSERVATION_TIMEOUT', result['error']['code'])

    def test_tampered_artifact_is_denied_before_network(self):
        (self.bundle / 'images.tar').write_bytes(b'tampered')
        with patch.object(release, 'GitHubWriter') as writer, self.assertRaises(release.OperationError):
            release.execute_release(self.plan, self.bundle, self.operation, work_dir=self.root / 'run', observer=Mock())
        writer.assert_not_called()

    def test_stale_gitops_head_is_denied_before_publish(self):
        writer = Mock(); writer.preflight.return_value = 'f' * 40
        with patch.object(release, 'GitHubWriter', return_value=writer), patch.object(release.bundle, 'publish') as publish:
            with self.assertRaises(release.OperationError) as caught:
                release.execute_release(self.plan, self.bundle, self.operation, work_dir=self.root / 'run', observer=Mock())
        self.assertEqual('STATE_BINDING_MISMATCH', caught.exception.code)
        publish.assert_not_called()

    def test_partial_ci_never_prepares(self):
        self.verdict['layers'].pop()
        (self.bundle / 'verdict.json').write_text(json.dumps(self.verdict)); self.write_manifest()
        with self.assertRaises(release.OperationError):
            release.prepare_release(self.bundle, self.target)

    def test_gitops_tree_only_changes_bound_app_and_ref_is_nonforce(self):
        writer = release.GitHubWriter(self.target['repo_url'])
        writer.call = Mock(side_effect=[{'sha': 'a' * 40, 'tree': [
            {'type': 'blob', 'path': 'apps/gcp/demo/demo/stale.yaml'},
            {'type': 'blob', 'path': 'apps/gcp/other/demo/keep.yaml'}]},
            {'sha': 'b' * 40}, {'sha': 'e' * 40}, {}, {'object': {'sha': 'e' * 40}}])
        revision = writer.create_commit('d' * 40, 'apps/gcp/demo/demo', {'20-app.yaml': 'safe'}, self.operation['id'])
        tree = writer.call.call_args_list[1].args[2]['tree']
        self.assertTrue(all(row['path'].startswith('apps/gcp/demo/demo/') for row in tree))
        self.assertEqual(2, len(tree))
        writer.push(revision)
        self.assertEqual({'sha': revision, 'force': False}, writer.call.call_args_list[3].args[2])

    def test_gitops_repository_preflight_uses_canonical_endpoint(self):
        writer = release.GitHubWriter(self.target['repo_url'])
        with patch.object(release, 'run_bounded', return_value=Mock(returncode=0, stdout='{}')) as run:
            writer.call('')
        self.assertEqual('repos/example/gitops', run.call_args.args[0][4])

    def test_dispatch_resolves_only_registered_ci_job_bundle(self):
        job = str(uuid.uuid4())
        parent = self.root / 'bundles'; parent.mkdir()
        self.bundle.rename(parent / job)
        request = {'phase': 'prepare', 'ci_job_id': job, 'bundle': {'relative_path': job,
            'manifest_sha256': self.plan['manifest_sha256'], 'source_sha256': self.plan['source_sha256']}}
        config = {'bundle_root': str(parent), 'target': self.target}
        with patch.object(release, 'GitHubWriter') as network:
            actual = release.dispatch(request, config)
        self.assertEqual(self.plan, actual['plan'])
        network.assert_not_called()
        request['bundle']['relative_path'] = '../other'
        with self.assertRaises(release.OperationError):
            release.dispatch(request, config)

    def test_dispatch_requires_explicit_observer_before_publish(self):
        job = str(uuid.uuid4())
        parent = self.root / 'bundles'; parent.mkdir()
        self.bundle.rename(parent / job)
        request = {'phase': 'execute', 'ci_job_id': job, 'plan': self.plan,
            'operation': self.operation, 'bundle': {'relative_path': job,
            'manifest_sha256': self.plan['manifest_sha256'], 'source_sha256': self.plan['source_sha256']}}
        config = {'bundle_root': str(parent), 'target': self.target, 'work_root': str(self.root / 'work'),
                  'observer_kubeconfig': str(self.root / 'not-registered')}
        with patch.object(release.bundle, 'publish') as publish, self.assertRaises(release.OperationError):
            release.dispatch(request, config)
        publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
