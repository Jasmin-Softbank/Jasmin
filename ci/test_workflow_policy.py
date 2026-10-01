"""Regression checks for the trusted CD boundary; no cloud access."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest

import yaml

HERE = Path(__file__).resolve().parent


class WorkflowPolicyTest(unittest.TestCase):
    def test_registry_target_and_credentials_belong_only_to_trusted_release(self):
        jobs = yaml.safe_load((HERE / 'railshot-deploy.yml').read_text())['jobs']
        release = jobs['release']
        self.assertEqual(release['environment'], 'railshot-release')
        self.assertEqual(release['env']['REGISTRY_PREFIX'], '${{ vars.REGISTRY_PREFIX }}')
        self.assertEqual(release['env']['REGISTRY_VISIBILITY'], "${{ vars.REGISTRY_VISIBILITY || 'private' }}")
        self.assertEqual(release['permissions'], {'contents': 'read', 'packages': 'write'})
        self.assertNotIn('REGISTRY_', json.dumps(jobs['loop']))
        self.assertNotIn('REGISTRY_', json.dumps(jobs['gitops']))
        login = next(s for s in release['steps'] if s.get('name') == 'Log in to GHCR')
        self.assertEqual(login['env'], {'GHCR_TOKEN': '${{ secrets.GITHUB_TOKEN }}'})
        self.assertEqual(sum('secrets.GITHUB_TOKEN' in json.dumps(s) for s in release['steps']), 1)
        scripts = '\n'.join(s.get('run', '') for s in release['steps'])
        self.assertIn('"$REGISTRY_PREFIX/${TENANT}-${APP}"', scripts)
        self.assertIn('docker login ghcr.io', scripts)
        self.assertNotIn('REGISTRY_PASSWORD', json.dumps(release))
        self.assertNotIn('REGISTRY_USERNAME', json.dumps(release))
        self.assertNotIn('GITOPS_TOKEN', json.dumps(release))

    def test_registry_binding_rejects_ambiguous_or_source_controlled_targets(self):
        release = yaml.safe_load((HERE / 'railshot-deploy.yml').read_text())['jobs']['release']
        script = next(s['run'] for s in release['steps'] if s.get('name') == 'Validate trusted platform bindings')
        env = {**os.environ, 'PLATFORM_REF': 'a' * 40, 'GITOPS_REPO': 'owner/gitops',
               'DOMAIN': 'example.test', 'GITOPS_CLUSTER': 'gcp', 'STORAGE_CLASS': 'local-path', 'REGISTRY_VISIBILITY': 'public'}
        for prefix in ('ghcr.io/owner', 'ghcr.io/owner/project/nested'):
            with self.subTest(prefix=prefix):
                result = subprocess.run(['bash', '-c', script], env={**env, 'REGISTRY_PREFIX': prefix}, capture_output=True)
                self.assertEqual(result.returncode, 0)
        for prefix in ('', 'owner/project', 'https://ghcr.io/owner', 'ghcr.io/owner/',
                       'user:password@ghcr.io/owner', '--help', 'ghcr.io/owner; false',
                       'ghcr.io/owner\nother/project', 'ghcr.io/../project', 'harbor.example.test/project', 'ghcr.io:443/owner'):
            with self.subTest(prefix=prefix):
                result = subprocess.run(['bash', '-c', script], env={**env, 'REGISTRY_PREFIX': prefix}, capture_output=True)
                self.assertNotEqual(result.returncode, 0)

    def test_private_default_blocks_before_registry_login_and_cannot_use_ready_flag(self):
        release = yaml.safe_load((HERE / 'railshot-deploy.yml').read_text())['jobs']['release']
        guard = release['steps'][0]
        self.assertEqual(guard['name'], 'Validate trusted platform bindings')
        env = {**os.environ, 'PLATFORM_REF': 'a' * 40, 'GITOPS_REPO': 'owner/gitops',
               'DOMAIN': 'example.test', 'GITOPS_CLUSTER': 'gcp', 'STORAGE_CLASS': 'local-path',
               'REGISTRY_PREFIX': 'ghcr.io/owner', 'REGISTRY_READY': 'true', 'IMAGE_PULL_SECRET_READY': 'true'}
        for visibility in ('', 'private', 'true', 'PUBLIC'):
            with self.subTest(visibility=visibility):
                result = subprocess.run(['bash', '-c', guard['run']], env={**env, 'REGISTRY_VISIBILITY': visibility}, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('BLOCKED:', result.stderr)
        env.pop('REGISTRY_VISIBILITY', None)
        self.assertNotEqual(subprocess.run(['bash', '-c', guard['run']], env=env, capture_output=True).returncode, 0)

    def test_login_uses_password_stdin_and_private_ephemeral_config(self):
        release = yaml.safe_load((HERE / 'railshot-deploy.yml').read_text())['jobs']['release']
        login = next(s for s in release['steps'] if s.get('name') == 'Log in to GHCR')
        cleanup = next(s for s in release['steps'] if s.get('name') == 'Remove ephemeral registry credentials')
        self.assertEqual(cleanup['if'], 'always()')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, 'CAPTURE': tmp, 'DOCKER_CONFIG': str(root / 'docker'),
                   'REGISTRY_PREFIX': 'ghcr.io/owner',
                   'GITHUB_ACTOR': 'workflow-actor', 'GHCR_TOKEN': 'synthetic-test-secret'}
            # This shell function captures the actual workflow's argv/stdin without contacting a registry.
            stub = 'docker() { printf "%s\\n" "$@" > "$CAPTURE/argv"; cat > "$CAPTURE/stdin"; }\n'
            result = subprocess.run(['bash', '-c', stub + login['run']], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'argv').read_text().splitlines(),
                             ['login', 'ghcr.io', '--username', 'workflow-actor', '--password-stdin'])
            self.assertEqual((root / 'stdin').read_text(), env['GHCR_TOKEN'])
            self.assertNotIn(env['GHCR_TOKEN'], result.stdout + result.stderr + (root / 'argv').read_text())
            self.assertEqual((root / 'docker').stat().st_mode & 0o777, 0o700)
            subprocess.run(['bash', '-c', cleanup['run']], env=env, check=True)
            self.assertFalse((root / 'docker').exists())

    def test_public_mode_requires_each_digest_without_inherited_docker_credentials(self):
        release = yaml.safe_load((HERE / 'railshot-deploy.yml').read_text())['jobs']['release']
        check = next(s for s in release['steps'] if s.get('name') == 'Require anonymous access to published digests')
        steps = release['steps']
        self.assertLess(steps.index(check), next(i for i,s in enumerate(steps) if s.get('id') == 'rendered'))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inherited = root / 'authenticated'; inherited.mkdir(); (inherited / 'config.json').write_text('{"auths":{"ghcr.io":{"auth":"synthetic-only"}}}')
            refs = {'web': 'ghcr.io/owner/tenant-app-web@sha256:' + 'a' * 64,
                    'api': 'ghcr.io/owner/tenant-app-api@sha256:' + 'b' * 64}
            (root / 'images.json').write_text(json.dumps(refs))
            env = {**os.environ, 'RUNNER_TEMP': tmp, 'REGISTRY_PREFIX': 'ghcr.io/owner',
                   'DOCKER_CONFIG': str(inherited), 'DOCKER_AUTH_CONFIG': 'synthetic-inherited-auth',
                   'CAPTURE': tmp, 'PRIVATE_DIGEST': ''}
            stub = '''docker() {
              test "$DOCKER_CONFIG" != "$CAPTURE/authenticated" && test -z "$DOCKER_AUTH_CONFIG" || return 90
              test "$(cat "$DOCKER_CONFIG/config.json")" = '{"auths":{"ghcr.io":{}}}' || return 91
              printf '%s\\n' "$*" >> "$CAPTURE/calls"
              test "$3" != "$PRIVATE_DIGEST"
            }
            '''
            result = subprocess.run(['bash', '-c', stub + check['run']], cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['manifest inspect ' + v for v in refs.values()])
            self.assertFalse(list(root.glob('railshot-anonymous.*')))
            result = subprocess.run(['bash', '-c', stub + check['run']], cwd=tmp,
                                    env={**env, 'PRIVATE_DIGEST': refs['api']}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not anonymously readable', result.stderr)
            self.assertFalse(list(root.glob('railshot-anonymous.*')))
            (root / 'images.json').write_text('{}')
            self.assertNotEqual(subprocess.run(['bash', '-c', stub + check['run']], cwd=tmp, env=env, capture_output=True).returncode, 0)

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
