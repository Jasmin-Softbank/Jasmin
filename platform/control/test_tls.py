import sys
from pathlib import Path
import unittest
import importlib.util
import tempfile
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control.tls import manifests


class TLSPlanTest(unittest.TestCase):
    def test_https_render_is_trusted_opt_in_and_http_parent_remains(self):
        path = Path(__file__).resolve().parents[1] / 'render/render.py'
        spec = importlib.util.spec_from_file_location('tls_test_renderer', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        app = {'apiVersion': 'jasmin/v0', 'app': 'demo', 'services': [
            {'name': 'web', 'build': {'dockerfile': 'Dockerfile'}, 'port': 3000, 'route': '/'}]}
        with tempfile.TemporaryDirectory() as tmp:
            plain = module.render(app, Path(tmp) / 'plain', 'demo', 'example.com', {'web': 'repo@sha256:' + 'a' * 64}, 'abcdef', 'local-path')
            secure = module.render(app, Path(tmp) / 'secure', 'demo', 'example.com', {'web': 'repo@sha256:' + 'a' * 64}, 'abcdef', 'local-path', https_gateway='railshot-demo-demo')
            parents = yaml.safe_load((Path(tmp) / 'secure/22-route.yaml').read_text())['spec']['parentRefs']
            self.assertEqual(['web', 'websecure'], [p['sectionName'] for p in parents])
            self.assertTrue(plain['url'].startswith('http://'))
            self.assertTrue(secure['url'].startswith('https://'))
            with self.assertRaises(ValueError):
                module.render(app, Path(tmp) / 'bad', 'demo', 'example.com', {'web': 'x'}, 'abcdef', 'local-path', https_gateway='someone-else')

    def test_gateway_scope_and_controller_are_explicit(self):
        value = manifests(tenant='demo', app='demo', host='demo-abcdef.34-50-30-121.sslip.io')
        self.assertEqual('RENDERED_NOT_APPLIED', value['status'])
        self.assertEqual('staging-untrusted', value['trust'])
        self.assertEqual('v1.21.2', value['controller']['spec']['source']['targetRevision'])
        http, https = value['gateway']['spec']['listeners']
        self.assertEqual('Same', http['allowedRoutes']['namespaces']['from'])
        self.assertEqual({'railshot.dev/tenant': 'demo'}, https['allowedRoutes']['namespaces']['selector']['matchLabels'])
        self.assertEqual(['demo-abcdef.34-50-30-121.sslip.io'], value['certificate']['spec']['dnsNames'])

    def test_wildcard_or_cross_namespace_injection_rejected(self):
        for host in ['*.sslip.io', 'a.example/path', 'a.example\nkind: Secret']:
            with self.assertRaises(ValueError):
                manifests(tenant='demo', app='demo', host=host)

    def test_production_is_explicit(self):
        value = manifests(tenant='demo', app='demo', host='demo.example.com', production=True)
        self.assertIn('https://acme-v02.', value['issuer']['spec']['acme']['server'])


if __name__ == '__main__':
    unittest.main()
