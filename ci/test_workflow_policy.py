"""Regression checks for the trusted CD boundary; no cloud access."""
from pathlib import Path
import unittest

import yaml

HERE = Path(__file__).resolve().parent


class WorkflowPolicyTest(unittest.TestCase):
    def test_gitops_commit_and_observe_share_trusted_worker_and_lock(self):
        text = (HERE / 'railshot-deploy.yml').read_text()
        jobs = yaml.safe_load(text)['jobs']
        cd = jobs['gitops']
        self.assertEqual(cd['runs-on']['labels'], 'railshot-cd')
        self.assertIn('RAILSHOT_CD_RUNNER_GROUP', cd['runs-on']['group'])
        self.assertEqual(cd['concurrency']['group'], 'railshot-gitops-repository')
        scripts = '\n'.join(step.get('run', '') for step in cd['steps'])
        self.assertLess(scripts.index('git push'), scripts.index('verify_deployment.py'))
        self.assertNotIn('docker ', scripts)
        self.assertNotIn('ARGOCD_SERVER', text)
        self.assertNotIn('ARGOCD_AUTH_TOKEN', text)
        self.assertEqual(jobs['reachability']['runs-on'], 'ubuntu-24.04')
        self.assertIn('gitops', jobs['reachability']['needs'])
        self.assertIn('exit 1', next(s['run'] for s in jobs['loop']['steps'] if s.get('id') == 'loop').split('passed=false')[1])
        self.assertIn('github.run_attempt', cd['env']['CD_DIR'])
        self.assertIn('$CD_DIR/out/rendered/.', scripts)
        for step in cd['steps']:
            if step.get('uses', '').startswith('actions/download-artifact'):
                self.assertIn('needs.release.outputs.rendered_id', step['with']['artifact-ids'])
                self.assertIn('env.CD_DIR', step['with']['path'])

    def test_observer_identity_cannot_read_secrets_or_change_cluster(self):
        sa, role, binding = yaml.safe_load_all((HERE / 'observer-rbac.yaml').read_text())
        self.assertFalse(sa['automountServiceAccountToken'])
        self.assertEqual(role['rules'], [{'apiGroups': ['argoproj.io'], 'resources': ['applications'], 'verbs': ['get']}])
        self.assertEqual(role['kind'], 'Role')
        self.assertEqual(role['metadata']['namespace'], 'argocd')
        self.assertEqual(binding['roleRef']['name'], role['metadata']['name'])


if __name__ == '__main__':
    unittest.main()
