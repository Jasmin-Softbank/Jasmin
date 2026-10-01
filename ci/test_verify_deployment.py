import copy
import subprocess
import unittest
from unittest.mock import patch

from verify_deployment import OperationError, matches, observe, read_application, target_matches


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.name = 't-demo-memo'
        self.repo = 'https://github.com/owner/gitops.git'
        self.path = 'apps/gcp/demo/memo'
        self.app = {'metadata': {'name': self.name, 'namespace': 'argocd'},
                    'spec': {'project': 'railshot-tenants',
                             'source': {'repoURL': self.repo, 'path': self.path, 'targetRevision': 'main'},
                             'destination': {'server': 'https://kubernetes.default.svc', 'namespace': self.name}},
                    'status': {'sync': {'status': 'Synced', 'revision': 'new'}, 'health': {'status': 'Healthy'},
                               'operationState': {'phase': 'Succeeded', 'syncResult': {'revision': 'new'}},
                               'summary': {'images': ['image@sha256:expected']}}}

    def test_health_cannot_promote_wrong_revision_image_or_incomplete_hooks(self):
        self.assertTrue(matches(self.app, 'new', ['image@sha256:expected']))
        for field, value in [('sync', {'status': 'Synced', 'revision': 'old'}),
                             ('summary', {'images': ['image@sha256:other']}),
                             ('conditions', [{'type': 'ComparisonError'}]),
                             ('operationState', {'phase': 'Running', 'syncResult': {'revision': 'new'}}),
                             ('operationState', {'phase': 'Failed', 'syncResult': {'revision': 'new'}}),
                             ('operationState', {'phase': 'Succeeded', 'syncResult': {'revision': 'old'}})]:
            with self.subTest(field=field, value=value):
                app = copy.deepcopy(self.app)
                app['status'][field] = value
                self.assertFalse(matches(app, 'new', ['image@sha256:expected']))
        self.app['operation'] = {'sync': {}}
        self.assertFalse(matches(self.app, 'new', ['image@sha256:expected']))

    def test_wrong_target_cannot_pass_even_with_good_status(self):
        self.assertTrue(target_matches(self.app, self.name, self.repo, self.path))
        for parent, field, value in [('metadata', 'name', 't-other-memo'), ('metadata', 'namespace', 'other'),
                                     ('spec', 'project', 'default'), ('source', 'repoURL', 'https://bad/repo'),
                                     ('source', 'path', 'apps/other/demo/memo'), ('source', 'targetRevision', 'other'),
                                     ('destination', 'namespace', 'other'), ('destination', 'server', 'https://other')]:
            with self.subTest(field=field):
                app = copy.deepcopy(self.app)
                part = app[parent] if parent in app else app['spec'][parent]
                part[field] = value
                self.assertFalse(target_matches(app, self.name, self.repo, self.path))

    def test_local_transport_has_fixed_resource_and_does_not_leak_error(self):
        with patch('verify_deployment.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'secret-token')) as run:
            with self.assertRaises(OperationError) as caught:
                read_application(self.name, 2)
            self.assertEqual(caught.exception.code, 'CD_OBSERVATION_UNAVAILABLE')
            self.assertNotIn('secret-token', str(caught.exception.as_dict()))
            self.assertEqual(caught.exception.as_dict()['causes'][0]['returncode'], 1)
            self.assertEqual(run.call_args.args[0], ['kubectl', '--namespace=argocd', '--request-timeout=2s',
                             'get', 'applications.argoproj.io', self.name, '--ignore-not-found', '-o', 'json'])

    def test_failed_operation_is_failed_and_other_target_is_blocked(self):
        self.app['status']['operationState']['phase'] = 'Failed'
        with patch('verify_deployment.read_application', return_value=self.app):
            self.assertEqual(observe(self.name, 'new', ['image@sha256:expected'], self.repo, self.path, 2)[0], 'FAIL')
            self.assertEqual(observe(self.name, 'new', ['image@sha256:expected'], self.repo, 'wrong', 2)[0], 'BLOCKED')

    def test_absent_observation_timeout_is_unknown_not_deployment_failure(self):
        with patch('verify_deployment.time.monotonic', side_effect=[0, 2]):
            status, error, app = observe(self.name, 'new', ['image@sha256:expected'], self.repo, self.path, 1)
        self.assertEqual(status, 'UNKNOWN')
        self.assertEqual(error.code, 'CD_OBSERVATION_TIMEOUT')
        self.assertEqual(error.side_effect, 'unknown')
        self.assertEqual(error.retry_policy, 'after_reconcile')
        self.assertIsNone(app)


if __name__ == '__main__':
    unittest.main()
