"""Deterministic graph mutation tests. These are not native deployment receipts."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control import deployment as d


class DeploymentGraphTest(unittest.TestCase):
    def setUp(self):
        self.ns = 't-demo-demo'
        self.image = 'registry.example/project/demo-web@sha256:' + 'a' * 64
        self.spec = {'app': 'demo', 'services': [{'name': 'web', 'port': 3000, 'route': '/'}]}
        def obj(kind, name, uid):
            return {'kind': kind, 'metadata': {'name': name, 'namespace': self.ns, 'uid': uid, 'generation': 1}}
        def owner(kind, uid):
            return [{'kind': kind, 'uid': uid, 'controller': True}]
        labels = {'app.kubernetes.io/name': 'demo-web'}
        container = {'name': 'web', 'image': self.image}
        dep = obj('Deployment', 'demo-web', 'deployment-uid')
        dep.update(spec={'replicas': 1, 'selector': {'matchLabels': labels}, 'template': {'spec': {'containers': [container]}}},
                   status={'observedGeneration': 1, 'replicas': 1, 'updatedReplicas': 1, 'readyReplicas': 1,
                           'availableReplicas': 1, 'conditions': [{'type': 'Available', 'status': 'True'}]})
        rs = obj('ReplicaSet', 'demo-web-rs', 'rs-uid'); rs['metadata']['ownerReferences'] = owner('Deployment', 'deployment-uid')
        pod = obj('Pod', 'demo-web-pod', 'pod-uid');pod['metadata'].update(labels=labels, ownerReferences=owner('ReplicaSet', 'rs-uid'))
        pod.update(spec={'containers': [container]}, status={'phase': 'Running', 'podIP': '10.42.0.10',
            'conditions': [{'type': 'Ready', 'status': 'True'}], 'containerStatuses': [
                {'name': 'web', 'ready': True, 'imageID': self.image}]})
        svc = obj('Service', 'demo-web', 'svc-uid'); svc['spec'] = {'selector': labels, 'ports': [{'port': 80, 'targetPort': 3000}]}
        ep = obj('EndpointSlice', 'demo-web-slice', 'slice-uid')
        ep['metadata'].update(labels={'kubernetes.io/service-name': 'demo-web'}, ownerReferences=owner('Service', 'svc-uid'))
        ep.update(ports=[{'port': 3000}], endpoints=[{'conditions': {'ready': True}, 'addresses': ['10.42.0.10'],
            'targetRef': {'kind': 'Pod', 'namespace': self.ns, 'name': 'demo-web-pod', 'uid': 'pod-uid'}}])
        route = obj('HTTPRoute', 'demo', 'route-uid')
        route['spec'] = {'hostnames': ['demo.example.com'], 'rules': [{'matches': [{'path': {'type': 'PathPrefix', 'value': '/'}}],
                      'backendRefs': [{'name': 'demo-web', 'port': 80}]}]}
        route['status'] = {'parents': [{'controllerName': 'traefik.io/gateway-controller',
            'parentRef': {'name': 'railshot-demo-demo', 'namespace': 'kube-system', 'sectionName': 'websecure'},
            'conditions': [{'type': c, 'status': 'True', 'observedGeneration': 1} for c in ('Accepted', 'ResolvedRefs')]}]}
        self.items = [dep, rs, pod, svc, ep, route]

    def check(self):
        return d.check_graph(self.items, self.spec, {'web': self.image}, self.ns, 'demo.example.com', 'railshot-demo-demo')

    def test_complete_graph_records_pod_image_and_endpoint_binding(self):
        record = self.check()
        self.assertEqual('PASS', record['status'])
        self.assertEqual(['pod-uid'], record['services'][0]['endpoint_pod_uids'])
        self.assertEqual(self.image, record['services'][0]['pods'][0]['image_id'])

    def test_stale_generation_image_owner_endpoint_and_route_never_pass(self):
        mutations = [lambda x: x[0]['status'].update(observedGeneration=0),
            lambda x: x[2]['status']['containerStatuses'][0].update(imageID='sha256:' + 'b' * 64),
            lambda x: x[1]['metadata']['ownerReferences'][0].update(uid='foreign-deployment'),
            lambda x: x[3]['spec'].update(selector={'unrelated': 'app'}),
            lambda x: x[4]['endpoints'][0]['targetRef'].update(uid='foreign-pod'),
            lambda x: x[5]['spec']['rules'][0]['backendRefs'][0].update(name='foreign-service'),
            lambda x: x[5]['status']['parents'][0]['conditions'][0].update(observedGeneration=0)]
        original = copy.deepcopy(self.items)
        for change in mutations:
            with self.subTest(change=change):
                self.items = copy.deepcopy(original);change(self.items)
                with self.assertRaises(d.OperationError): self.check()

    def test_foreign_ready_endpoint_and_old_pod_are_not_ignored(self):
        self.items[4]['endpoints'].append({'conditions': {'ready': True}, 'targetRef': {'kind': 'Pod', 'uid': 'other'}})
        with self.assertRaises(d.OperationError): self.check()

    def test_permission_failure_is_unknown_not_missing_deployment(self):
        with patch.object(d, 'run_bounded', return_value=Mock(returncode=1)):
            with self.assertRaises(d.OperationError) as caught:
                d.observe_graph('/private/observer', self.spec, {'web': self.image}, self.ns, 'demo.example.com', 'railshot-demo-demo', timeout=1)
        self.assertEqual('UNKNOWN', caught.exception.outcome)

    def test_native_request_is_namespace_bound_get_only(self):
        with patch.object(d, 'run_bounded', return_value=Mock(returncode=0, stdout=json.dumps({'items': self.items}))) as run:
            d.observe_graph('/private/observer', self.spec, {'web': self.image}, self.ns, 'demo.example.com', 'railshot-demo-demo')
        cmd = run.call_args.args[0]
        self.assertEqual(self.ns, cmd[cmd.index('-n') + 1]);self.assertIn('get', cmd)
        self.assertNotIn('secrets', ','.join(cmd));self.assertNotIn('exec', cmd)

    def test_external_probe_disallows_redirect_and_requires_https(self):
        self.assertIsNone(d.NoRedirect().redirect_request(None, None, 302, None, None, 'https://other.example'))
        with self.assertRaises(d.OperationError): d.probe_https('http://demo.example.com/')

    def test_external_body_read_has_process_deadline_in_addition_to_socket_timeout(self):
        with patch.object(d, 'run_bounded', side_effect=d.subprocess.TimeoutExpired('probe', 20)) as run:
            with self.assertRaises(d.OperationError) as caught: d.probe_https('https://demo.example.com/')
        self.assertEqual('UNKNOWN', caught.exception.outcome)
        self.assertEqual(20, run.call_args.kwargs['timeout'])


if __name__ == '__main__':
    unittest.main()
