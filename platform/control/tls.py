"""Render administrator-reviewed Gateway API TLS bootstrap; never applies it.

Sources: https://doc.traefik.io/traefik/setup/kubernetes/#gateway-api-acme
https://cert-manager.io/docs/configuration/acme/http01/#configuring-the-http-01-gateway-api-solver
The existing Helm HTTP Gateway remains Helm-owned. These platform resources live
outside tenant Applications. A successful render is not a certificate or ingress test.
"""
import re


def manifests(*, tenant, app, host, production=False):
    if (not re.fullmatch(r'[a-z0-9]{1,20}', tenant)
            or not re.fullmatch(r'[a-z][a-z0-9-]{1,28}[a-z0-9]', app)
            or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host)
            or len(host) > 253 or type(production) is not bool):
        raise ValueError('registered TLS target required')
    name = f'railshot-{tenant}-{app}'
    if len(name) > 63:
        raise ValueError('TLS resource name too long')
    namespace = 'kube-system'
    metadata = {'name': name, 'namespace': namespace,
                'annotations': {'argocd.argoproj.io/sync-options': 'Prune=confirm,Delete=false'}}
    application = {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Application',
        'metadata': {'name': 'railshot-cert-manager', 'namespace': 'argocd'},
        'spec': {'project': 'default', 'source': {'repoURL': 'https://charts.jetstack.io',
            'chart': 'cert-manager', 'targetRevision': 'v1.21.2', 'helm': {'valuesObject': {
                'crds': {'enabled': True, 'keep': True}, 'config': {'enableGatewayAPI': True},
                'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}}}}},
            'destination': {'server': 'https://kubernetes.default.svc', 'namespace': namespace},
            'syncPolicy': {'automated': {'prune': False, 'selfHeal': True},
                           'syncOptions': ['ServerSideApply=true']}}}
    gateway = {'apiVersion': 'gateway.networking.k8s.io/v1', 'kind': 'Gateway', 'metadata': metadata,
        'spec': {'gatewayClassName': 'traefik', 'listeners': [
            {'name': 'acme', 'hostname': host, 'port': 8000, 'protocol': 'HTTP',
             'allowedRoutes': {'namespaces': {'from': 'Same'}}},
            {'name': 'websecure', 'hostname': host, 'port': 8443, 'protocol': 'HTTPS',
             'tls': {'mode': 'Terminate', 'certificateRefs': [{'kind': 'Secret', 'name': name}]},
             'allowedRoutes': {'namespaces': {'from': 'Selector',
                 'selector': {'matchLabels': {'railshot.dev/tenant': tenant}}}}}]}}
    issuer = {'apiVersion': 'cert-manager.io/v1', 'kind': 'Issuer', 'metadata': metadata,
        'spec': {'acme': {'server': 'https://acme-v02.api.letsencrypt.org/directory' if production
                        else 'https://acme-staging-v02.api.letsencrypt.org/directory',
            'privateKeySecretRef': {'name': name + '-account'}, 'solvers': [{'http01': {'gatewayHTTPRoute': {
                'parentRefs': [{'name': name, 'namespace': namespace, 'kind': 'Gateway', 'sectionName': 'acme'}]}}}]}}}
    certificate = {'apiVersion': 'cert-manager.io/v1', 'kind': 'Certificate', 'metadata': metadata,
        'spec': {'secretName': name, 'dnsNames': [host], 'issuerRef': {'name': name, 'kind': 'Issuer'},
                 'privateKey': {'rotationPolicy': 'Always'}}}
    return {'controller': application, 'gateway': gateway, 'issuer': issuer, 'certificate': certificate,
            'https_parent_ref': {'name': name, 'namespace': namespace, 'sectionName': 'websecure'},
            'trust': 'public' if production else 'staging-untrusted', 'status': 'RENDERED_NOT_APPLIED'}
