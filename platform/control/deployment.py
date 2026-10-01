"""Namespace-scoped deployment evidence; no write, Secret, exec or log access.

The graph binds an Argo-approved render to currently ready Pods and route endpoints.
A public HTTP response has no embedded revision requirement: report its observation
time with the graph, never claim that the body itself proves a source revision.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observability import OperationError
from process import run_bounded


def unavailable(phase, cause=None):
    return OperationError('CD_OBSERVATION_UNAVAILABLE', component='control.release', phase=phase,
                          outcome='UNKNOWN', retry_policy='after_reconcile', side_effect='unknown', cause=cause)


def require(value):
    if not value:
        raise OperationError('CD_TARGET_MISMATCH', component='control.release', phase='workload.observe',
                             outcome='BLOCKED', retry_policy='after_reconcile', side_effect='possible')


def owner(obj, kind, uid):
    return any(r.get('kind') == kind and r.get('uid') == uid and r.get('controller') is True
               for r in obj.get('metadata', {}).get('ownerReferences', []))


def matches_labels(obj, selector):
    labels = obj.get('metadata', {}).get('labels', {})
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def ready_condition(obj, type_):
    return any(c.get('type') == type_ and c.get('status') == 'True' for c in obj.get('status', {}).get('conditions', []))


def check_graph(items, spec, images, namespace, host, gateway):
    """Pure validation of an actual API read; returns only safe identity fields."""
    require(isinstance(items, list) and images and gateway)
    require(all(o.get('metadata', {}).get('namespace') == namespace for o in items))
    objects = {}
    for obj in items:
        key = (obj.get('kind'), obj.get('metadata', {}).get('name'))
        require(key not in objects)
        objects[key] = obj
    observations = []
    for svc in spec['services']:
        name = f"{spec['app']}-{svc['name']}"
        dep = objects.get(('Deployment', name), {})
        meta, status, ds = dep.get('metadata', {}), dep.get('status', {}), dep.get('spec', {})
        desired = ds.get('replicas', 1)
        require(type(desired) is int and desired > 0 and meta.get('uid') and not meta.get('deletionTimestamp'))
        require(status.get('observedGeneration') == meta.get('generation') and ready_condition(dep, 'Available'))
        require(all(status.get(k, 0) == desired for k in ('replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas')))
        require(status.get('unavailableReplicas', 0) == 0)
        expected = images[svc['name']]
        containers = ds.get('template', {}).get('spec', {}).get('containers', [])
        require([(c.get('name'), c.get('image')) for c in containers] == [(svc['name'], expected)])
        selector = ds.get('selector', {}).get('matchLabels')
        require(selector and not ds.get('selector', {}).get('matchExpressions'))
        rs = {o['metadata']['uid']: o for o in items if o.get('kind') == 'ReplicaSet'
              and owner(o, 'Deployment', meta['uid']) and not o['metadata'].get('deletionTimestamp')}
        pods = [o for o in items if o.get('kind') == 'Pod' and matches_labels(o, selector)
                and not o['metadata'].get('deletionTimestamp')]
        require(len(pods) == desired)
        pod_receipts = []
        for pod in pods:
            parents = [uid for uid in rs if owner(pod, 'ReplicaSet', uid)]
            require(len(parents) == 1 and ready_condition(pod, 'Ready') and pod.get('status', {}).get('phase') == 'Running')
            require([(c.get('name'), c.get('image')) for c in pod.get('spec', {}).get('containers', [])]
                    == [(svc['name'], expected)])
            states = pod.get('status', {}).get('containerStatuses', [])
            require(len(states) == 1 and states[0].get('name') == svc['name'] and states[0].get('ready') is True)
            image_id = states[0].get('imageID', '')
            actual_digest = re.search(r'(sha256:[a-f0-9]{64})$', image_id)
            require(actual_digest is not None and actual_digest[1] == expected.rsplit('@', 1)[1])
            require(pod['metadata'].get('uid') and pod.get('status', {}).get('podIP'))
            pod_receipts.append({'uid': pod['metadata']['uid'], 'name': pod['metadata']['name'],
                                 'replica_set_uid': parents[0], 'image_id': image_id,
                                 'pod_ip': pod['status']['podIP']})
        service = objects.get(('Service', name), {})
        service_selector = service.get('spec', {}).get('selector', {})
        require(service.get('metadata', {}).get('uid') and service_selector == selector)
        require(any(p.get('port') == 80 and p.get('targetPort') == svc['port'] for p in service.get('spec', {}).get('ports', [])))
        slices = [o for o in items if o.get('kind') == 'EndpointSlice'
                  and o['metadata'].get('labels', {}).get('kubernetes.io/service-name') == name]
        require(slices)
        pod_by_uid = {p['uid']: p for p in pod_receipts}
        endpoint_uids = set()
        for ep_slice in slices:
            require(owner(ep_slice, 'Service', service['metadata']['uid']))
            require(any(p.get('port') == svc['port'] for p in ep_slice.get('ports', [])))
            for endpoint in ep_slice.get('endpoints', []):
                if endpoint.get('conditions', {}).get('ready') is not True:
                    continue
                target = endpoint.get('targetRef', {})
                require(target.get('kind') == 'Pod' and target.get('namespace') == namespace and target.get('uid') in pod_by_uid)
                pod = pod_by_uid[target['uid']]
                require(endpoint.get('conditions', {}).get('terminating') is not True
                        and target.get('name') == pod['name'] and pod['pod_ip'] in endpoint.get('addresses', []))
                endpoint_uids.add(pod['uid'])
        require(endpoint_uids == set(pod_by_uid))
        observations.append({'service': name, 'deployment_uid': meta['uid'], 'generation': meta['generation'],
                             'service_uid': service['metadata']['uid'], 'pods': sorted(pod_receipts, key=lambda p: p['uid']),
                             'endpoint_pod_uids': sorted(endpoint_uids)})
    route = objects.get(('HTTPRoute', spec['app']), {})
    require(route.get('spec', {}).get('hostnames') == [host])
    expected_rules = {s['route']: f"{spec['app']}-{s['name']}" for s in spec['services'] if s.get('route')}
    actual_rules = {}
    for rule in route.get('spec', {}).get('rules', []):
        require(not rule.get('filters') and len(rule.get('matches', [])) == 1 and len(rule.get('backendRefs', [])) == 1)
        match, backend = rule['matches'][0], rule['backendRefs'][0]
        require(set(match) == {'path'} and match['path'].get('type') == 'PathPrefix'
                and backend.get('namespace', namespace) == namespace and backend.get('port') == 80
                and backend.get('kind', 'Service') == 'Service' and backend.get('group', '') == '')
        require(match['path']['value'] not in actual_rules)
        actual_rules[match['path']['value']] = backend.get('name')
    require(actual_rules == expected_rules)
    parents = [p for p in route.get('status', {}).get('parents', [])
               if p.get('parentRef', {}).get('name') == gateway and p['parentRef'].get('namespace') == 'kube-system'
               and p['parentRef'].get('sectionName') == 'websecure']
    require(len(parents) == 1 and parents[0].get('controllerName') == 'traefik.io/gateway-controller')
    for condition in ('Accepted', 'ResolvedRefs'):
        require(any(c.get('type') == condition and c.get('status') == 'True'
                    and c.get('observedGeneration') == route['metadata'].get('generation')
                    for c in parents[0].get('conditions', [])))
    return {'version': 1, 'status': 'PASS', 'scope': 'namespace-workload-route-graph',
            'observed_at': datetime.now(timezone.utc).isoformat(), 'namespace': namespace,
            'route_uid': route['metadata']['uid'], 'route_generation': route['metadata']['generation'],
            'services': observations}


def observe_graph(kubeconfig, spec, images, namespace, host, gateway, timeout=90):
    kinds = 'deployments,replicasets,pods,services,endpointslices.discovery.k8s.io,httproutes.gateway.networking.k8s.io'
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise unavailable('workload.configure')
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(1, min(15, int(deadline - time.monotonic())))
        try:
            result = run_bounded(['kubectl', '--kubeconfig', str(kubeconfig), f'--request-timeout={remaining}s',
                                  '-n', namespace, 'get', kinds, '-o', 'json'], timeout=remaining + 1,
                                 max_output_bytes=4 * 1024 * 1024)
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, 'kubectl')
            return check_graph(json.loads(result.stdout)['items'], spec, images, namespace, host, gateway)
        except OperationError as exc:
            error = exc
        except Exception as exc:
            error = unavailable('workload.observe', exc)
        if time.monotonic() >= deadline:
            raise error
        time.sleep(min(3, max(0, deadline - time.monotonic())))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe_https(url):
    if not re.fullmatch(r'https://[a-z0-9.-]+/[^\s?#]*', url):
        raise unavailable('external.configure')
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(urllib.request.Request(url, headers={'User-Agent': 'RAILSHOT-CD/1'}), timeout=15) as response:
            body = response.read(1024 * 1024 + 1)
            if not 200 <= response.status < 300 or len(body) > 1024 * 1024:
                raise ValueError('unexpected bounded response')
            return {'version': 1, 'status': 'PASS', 'scope': 'external-https', 'url': url,
                    'observed_at': datetime.now(timezone.utc).isoformat(), 'http_status': response.status,
                    'body_sha256': hashlib.sha256(body).hexdigest(), 'body_size': len(body),
                    'tls_verified': True, 'body_revision_binding': 'NOT_PROVIDED'}
    except Exception as exc:
        raise unavailable('external.observe', exc) from exc


def probe_https(url):
    """Native TLS with a process wall deadline, including slow-drip responses."""
    if not re.fullmatch(r'https://[a-z0-9.-]+/[^\s?#]*', url):
        raise unavailable('external.configure')
    try:
        result = run_bounded([sys.executable, str(Path(__file__).resolve()), url], timeout=20,
                             max_output_bytes=32768)
        record = json.loads(result.stdout)
        if result.returncode or record.get('status') != 'PASS' or record.get('url') != url:
            raise unavailable('external.observe')
        return record
    except OperationError:
        raise
    except Exception as exc:
        raise unavailable('external.observe', exc) from exc


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('one registered HTTPS URL required')
        print(json.dumps(_probe_https(sys.argv[1])))
    except Exception:
        # No endpoint body, TLS exception detail, headers or credentials in stdout.
        print(json.dumps({'status': 'UNKNOWN', 'scope': 'external-https'}))
        sys.exit(1)
