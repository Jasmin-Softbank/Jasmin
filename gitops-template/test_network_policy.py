"""Offline network/IAM regressions; these do not prove live Cilium enforcement.

KEDA labels/ports were checked with the official 2.21.0 chart rendered for K8s
1.36.4. Set KEDA_RENDERED_PATH to that Helm output to repeat the label check.
"""
import os
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / 'gitops-template/clusters/aws/platform'
NAMESPACE = 'io.kubernetes.pod.namespace'
KEDA_NAMES = ('keda-operator', 'keda-operator-metrics-apiserver', 'keda-admission-webhooks')


def selected(selector, labels):
    """Evaluate only the label selector operators used by this baseline."""
    if any(labels.get(key) != value for key, value in selector.get('matchLabels', {}).items()):
        return False
    for expr in selector.get('matchExpressions', []):
        key, op = expr['key'], expr['operator']
        if op == 'NotIn':
            if labels.get(key) in expr['values']:
                return False
        elif op == 'Exists':
            if key not in labels:
                return False
        elif op == 'DoesNotExist':
            if key in labels:
                return False
        else:
            raise AssertionError(f'unreviewed selector operator: {op}')
    return True


class NetworkPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        docs = list(yaml.safe_load_all((PLATFORM / '30-network-baseline.yaml').read_text()))
        cls.policies = {doc['metadata']['name']: doc['spec'] for doc in docs}
        assert len(docs) == len(cls.policies), 'duplicate policy name'

    def test_keda_is_not_selected_by_tenant_baseline_or_api_deny(self):
        for name in KEDA_NAMES:
            labels = {NAMESPACE: 'keda', 'app.kubernetes.io/name': name}
            for policy_name in ('railshot-default-deny', 'railshot-deny-sensitive',
                                'railshot-deny-apiserver', 'railshot-cnpg-instances'):
                with self.subTest(component=name, policy=policy_name):
                    self.assertFalse(selected(self.policies[policy_name]['endpointSelector'], labels))

    def test_tenant_api_imds_deny_and_cnpg_exception_remain(self):
        tenant = {NAMESPACE: 't-demo-app'}
        api = self.policies['railshot-deny-apiserver']
        sensitive = self.policies['railshot-deny-sensitive']
        self.assertTrue(selected(api['endpointSelector'], tenant))
        self.assertEqual(api['egressDeny'], [{'toEntities': ['kube-apiserver']}])
        self.assertTrue(selected(sensitive['endpointSelector'], tenant))
        self.assertIn({'toCIDR': ['169.254.169.254/32']}, sensitive['egressDeny'])
        self.assertIn({'toCIDR': ['168.63.129.16/32']}, sensitive['egressDeny'])
        db = {**tenant, 'cnpg.io/cluster': 'app-db'}
        self.assertFalse(selected(api['endpointSelector'], db))
        cnpg = self.policies['railshot-cnpg-instances']
        self.assertTrue(selected(cnpg['endpointSelector'], db))
        self.assertEqual(cnpg['egress'], [{'toEntities': ['kube-apiserver']}])
        self.assertEqual(cnpg['ingress'][0]['toPorts'][0]['ports'], [{'port': '8000', 'protocol': 'TCP'}])

    def test_keda_base_egress_has_only_dns_and_api(self):
        policy = self.policies['railshot-keda-egress']
        self.assertEqual(policy['endpointSelector'], {'matchLabels': {NAMESPACE: 'keda'}})
        self.assertEqual(set(policy), {'endpointSelector', 'egress'})  # no new ingress allowance
        self.assertEqual(policy['egress'], [
            {'toEndpoints': [{'matchLabels': {NAMESPACE: 'kube-system', 'k8s-app': 'kube-dns'}}],
             'toPorts': [{'ports': [{'port': '53', 'protocol': 'ANY'}],
                          'rules': {'dns': [{'matchPattern': '*'}]}}]},
            {'toEntities': ['kube-apiserver'],
             'toPorts': [{'ports': [{'port': '443', 'protocol': 'TCP'}, {'port': '6443', 'protocol': 'TCP'}]}]},
        ])

    def test_only_metrics_adapter_can_use_operator_grpc(self):
        policy = self.policies['railshot-keda-metrics-egress']
        self.assertEqual(set(policy), {'endpointSelector', 'egress'})
        for name in KEDA_NAMES:
            self.assertEqual(selected(policy['endpointSelector'],
                                      {NAMESPACE: 'keda', 'app.kubernetes.io/name': name}),
                             name == 'keda-operator-metrics-apiserver')
        self.assertFalse(selected(policy['endpointSelector'],
                                  {NAMESPACE: 't-demo-app', 'app.kubernetes.io/name': KEDA_NAMES[1]}))
        self.assertEqual(policy['egress'], [{
            'toEndpoints': [{'matchLabels': {NAMESPACE: 'keda', 'app.kubernetes.io/name': 'keda-operator'}}],
            'toPorts': [{'ports': [{'port': '9666', 'protocol': 'TCP'}]}],
        }])

    def test_keda_contract_is_bound_to_reviewed_chart(self):
        app = yaml.safe_load((PLATFORM / '15-keda.yaml').read_text())
        self.assertEqual(app['spec']['destination']['namespace'], 'keda')
        self.assertEqual(app['spec']['source']['targetRevision'], '2.21.0')

    @unittest.skipUnless(os.environ.get('KEDA_RENDERED_PATH'), 'optional native Helm chart readback')
    def test_native_rendered_chart_labels_and_grpc_port(self):
        docs = yaml.safe_load_all(Path(os.environ['KEDA_RENDERED_PATH']).read_text())
        deployments = {doc['metadata']['name']: doc for doc in docs if doc and doc['kind'] == 'Deployment'}
        self.assertEqual(set(deployments), set(KEDA_NAMES))
        for name in KEDA_NAMES:
            labels = deployments[name]['spec']['template']['metadata']['labels']
            self.assertEqual(labels['app.kubernetes.io/name'], name)
            self.assertEqual(labels['app.kubernetes.io/version'], '2.21.0')
        containers = deployments['keda-operator']['spec']['template']['spec']['containers']
        self.assertTrue(any(port.get('containerPort') == 9666 and port.get('protocol') == 'TCP'
                            for container in containers for port in container.get('ports', [])))


class AppNodeParameterPolicyTests(unittest.TestCase):
    def test_managed_policy_parameter_read_grant_is_explicitly_narrowed(self):
        source = (ROOT / 'infra/terraform/aws/main.tf').read_text()
        policy = source.split('resource "aws_iam_role_policy" "node_params" {', 1)[1].split('\nresource ', 1)[0]
        self.assertIn('jsonencode(local.node_parameter_policy)', policy)
        self.assertRegex(source, r'Effect\s*=\s*"Deny"\s*,\s*Action\s*=\s*\["ssm:GetParameter"\]\s*,\s*NotResource\s*=\s*local.node_parameter_arns')
        self.assertIn('"ssm:GetParametersByPath"', source)
        self.assertNotIn('parameter/${var.name}/*', policy)

    def test_allowed_parameters_are_exact_bootstrap_refs_not_control_auth(self):
        source = (ROOT / 'infra/terraform/aws/main.tf').read_text()
        self.assertNotIn('ghcr_token_param', source)
        self.assertNotIn('ghcr-read-token', source)
        self.assertNotIn('codex/operator-primary/auth', source)
        self.assertIn('parameter${var.gitops_token_param}', source)
        self.assertIn('var.gitops_token_param == null ? {} : { gitops_token_param = var.gitops_token_param }', source)


if __name__ == '__main__':
    unittest.main()
