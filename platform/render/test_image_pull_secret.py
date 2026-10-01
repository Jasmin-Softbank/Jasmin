"""Trusted pull-secret references only; no cluster or registry is contacted."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import jsonschema
import yaml

import render


class ImagePullSecretTest(unittest.TestCase):
    def setUp(self):
        self.spec = {'apiVersion': 'jasmin/v0', 'app': 'memo', 'resources': {'postgres': {'size': 'small'}},
                     'services': [{'name': 'api', 'build': {'dockerfile': 'Dockerfile'}, 'port': 8000,
                                   'route': '/', 'migrate': {'command': ['python', 'migrate.py']}}]}
        self.image = 'ghcr.io/example/memo-api@sha256:' + 'a' * 64

    def generate(self, out, spec=None, **kwargs):
        return render.render(spec or self.spec, out, 'demo', 'example.test', {'api': self.image},
                             'abcdef', 'local-path', **kwargs)

    def test_reference_reaches_app_and_same_image_migration_but_not_platform_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = self.generate(root, image_pull_secret_name='railshot-pull')
            docs = [d for name in info['files'] for d in yaml.safe_load_all((root / name).read_text())]
            pods = [d['spec']['template']['spec'] for d in docs if d['kind'] in {'Deployment', 'Job'}]
            app_pods = [p for p in pods if p['containers'][0]['image'] == self.image]
            self.assertEqual(len(app_pods), 2)
            self.assertTrue(all(p['imagePullSecrets'] == [{'name': 'railshot-pull'}] for p in app_pods))
            self.assertTrue(all(p['containers'][0]['imagePullPolicy'] == 'Always' for p in app_pods))
            self.assertTrue(all('imagePullSecrets' not in p for p in pods if p not in app_pods))
            self.assertTrue(all('imagePullPolicy' not in p['containers'][0] for p in pods if p not in app_pods))
            self.assertNotIn('Secret', [d['kind'] for d in docs])
            self.assertNotIn('.dockerconfigjson', ''.join(p.read_text() for p in root.iterdir()))

    def test_default_emits_no_reference_and_invalid_names_fail_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.generate(Path(tmp))
            self.assertNotIn('imagePullSecrets', ''.join(p.read_text() for p in Path(tmp).iterdir()))
        for name in ('', '-pull', 'pull-', 'Pull', 'pull.secret', 'x' * 64, '../secret', 'a\nb', {'name': 'pull'}):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / 'out'
                with self.assertRaises(ValueError): self.generate(out, image_pull_secret_name=name)
                self.assertFalse(out.exists())

    def test_uploaded_spec_cannot_choose_reference_or_secret_content(self):
        schema = json.loads((render.PLATFORM / 'schemas/jasmin.schema.json').read_text())
        for location in ('root', 'service'):
            for key, value in [('image_pull_secret_name', 'attacker-selected'),
                               ('imagePullSecrets', [{'name': 'attacker-selected'}]),
                               ('registry_auth', {'password': 'synthetic-password'})]:
                spec = copy.deepcopy(self.spec)
                (spec if location == 'root' else spec['services'][0])[key] = value
                with self.subTest(location=location, key=key), self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(spec, schema)


if __name__ == '__main__':
    unittest.main()
