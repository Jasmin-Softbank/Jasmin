"""CD admission and migration/runtime privilege consistency, without a cluster."""
import copy
from pathlib import Path
import tempfile
import unittest

import jsonschema
import yaml

import render


class CDContractTest(unittest.TestCase):
    def setUp(self):
        self.spec = {'apiVersion': 'jasmin/v0', 'app': 'memo', 'resources': {'postgres': {'size': 'small'}},
                     'services': [{'name': 'api', 'build': {'dockerfile': 'Dockerfile'}, 'port': 8000,
                                   'route': '/', 'health': '/health', 'env': {'MODE': 'prod'},
                                   'secrets': ['APP_KEY', 'DATABASE_URL'], 'migrate': {'command': ['python', 'migrate.py']}}]}

    def generate(self, directory, spec=None):
        return render.render(spec or self.spec, Path(directory), 'demo', 'example.test',
                             {'api': 'test@sha256:' + 'a' * 64}, 'abc', 'local-path')

    def test_same_image_migration_keeps_env_but_only_job_gets_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = self.generate(tmp)
            docs = [d for f in info['files'] for d in yaml.safe_load_all((Path(tmp) / f).read_text())]
            dep = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['spec']
            mig = next(d for d in docs if d['metadata']['name'].endswith('-migrate'))['spec']['template']['spec']
            self.assertFalse(dep['automountServiceAccountToken'])
            self.assertFalse(mig['automountServiceAccountToken'])
            runtime, migration = [{e['name']: e for e in pod['containers'][0]['env']} for pod in (dep, mig)]
            for key in ('PORT', 'MODE', 'APP_KEY'):
                self.assertEqual(runtime[key], migration[key])
            self.assertEqual(runtime['DATABASE_URL']['valueFrom']['secretKeyRef']['name'], 'memo-db-rw')
            self.assertNotIn('MIGRATION_DATABASE_URL', runtime)
            for key in ('DATABASE_URL', 'MIGRATION_DATABASE_URL'):
                self.assertEqual(migration[key]['valueFrom']['secretKeyRef']['name'], 'memo-db-app')
            smoke = next(d for d in docs if d['metadata']['name'] == 'memo-smoke')
            url = smoke['spec']['template']['spec']['containers'][0]['args'][-1]
            self.assertEqual(url, 'http://memo-api.t-demo-memo/health')

    def test_unsupported_strategies_and_reserved_env_rejected_before_files(self):
        for key, value in [('strategy', 'canary'), ('strategy', 'bluegreen'), ('env', {'PORT': '1'}),
                           ('env', {'DATABASE_URL': 'owner'}), ('secrets', ['MIGRATION_DATABASE_URL'])]:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as tmp:
                spec = copy.deepcopy(self.spec)
                spec['services'][0][key] = value
                out = Path(tmp) / 'absent'
                with self.assertRaises((ValueError, jsonschema.ValidationError)):
                    self.generate(out, spec)
                self.assertFalse(out.exists())

    def test_no_silent_migration_or_bucket_omission(self):
        for resources in ({}, {"bucket": {"public_read": False}}):
            with self.subTest(resources=resources), tempfile.TemporaryDirectory() as tmp:
                spec = copy.deepcopy(self.spec)
                spec['resources'] = resources
                with self.assertRaises(jsonschema.ValidationError):
                    self.generate(Path(tmp) / 'absent', spec)
                self.assertFalse((Path(tmp) / 'absent').exists())

    def test_existing_manifest_cannot_survive_outside_new_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.generate(tmp)
            before = {p.name: p.read_bytes() for p in Path(tmp).iterdir()}
            spec = copy.deepcopy(self.spec)
            del spec['resources']
            del spec['services'][0]['migrate']
            with self.assertRaisesRegex(ValueError, 'empty directory'):
                self.generate(tmp, spec)
            self.assertEqual(before, {p.name: p.read_bytes() for p in Path(tmp).iterdir()})

    def test_public_probe_uses_declared_non_root_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.spec['services'][0]['route'] = '/health'
            result = self.generate(tmp)
            self.assertEqual(result['probe_url'], 'http://memo-abc.example.test/health')


if __name__ == '__main__':
    unittest.main()
