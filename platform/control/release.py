"""Trusted release adapter: consumed Allow -> exact CI bundle -> GitOps -> Argo.

Call only from an authenticated supervisor on a separate trusted release executor.
The operation argument must come from ControlState.get_operation, never HTTP JSON.
Docker push and gh authentication stay on this executor, never on the CI worker.
Release PASS requires Argo, Pod/endpoint/route identity and external TLS evidence.
"""
import fcntl
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile
import uuid

import yaml

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM))
sys.path.insert(0, str(PLATFORM.parent / 'ci'))
from observability import OperationError, event_record
from process import run_bounded
from runner.runtime_boundary import private_directory
from storage import durable_write
from verify_deployment import observe
from control.deployment import observe_graph, probe_https, require


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Avoid collisions with gate.py/render.py loaded by other CLI entrypoints.
bundle = load_module('railshot_release_bundle', PLATFORM / 'gate/bundle.py')
render = load_module('railshot_release_render', PLATFORM / 'render/render.py').render

TARGET_FIELDS = {'workspace_id', 'generation', 'tenant', 'cluster', 'repo_url',
                 'registry_prefix', 'tag', 'domain', 'suffix', 'storage_class', 'gitops_revision', 'publisher_backend',
                 'https_gateway'}


def fail(code, phase, *, unknown=False, cause=None):
    return OperationError(code, component='control.release', phase=phase,
                          outcome='UNKNOWN' if unknown else 'BLOCKED',
                          retry_policy='after_reconcile' if unknown else 'after_configuration',
                          side_effect='unknown' if unknown else 'none', cause=cause)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def checked_target(target):
    """Administrator registration only; the uploaded app cannot pick these fields."""
    if isinstance(target, dict):
        target = {'publisher_backend': 'docker', 'https_gateway': None, **target}
    patterns = {'workspace_id': r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}',
                'tenant': r'[a-z0-9]{1,20}', 'cluster': r'[a-z][a-z0-9-]{1,39}',
                'repo_url': r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',
                'registry_prefix': bundle.REPO, 'tag': bundle.TAG,
                'domain': r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}',
                'suffix': r'[a-z0-9]{6,16}', 'storage_class': r'[a-z0-9][a-z0-9.-]{0,62}'}
    patterns['gitops_revision'] = r'[a-f0-9]{40}'
    if (not isinstance(target, dict) or set(target) != TARGET_FIELDS
            or type(target['generation']) is not int or target['generation'] < 1
            or target['publisher_backend'] not in {'docker', 'skopeo'}
            or (target['https_gateway'] is not None and (not isinstance(target['https_gateway'], str)
                or not re.fullmatch(r'railshot-[a-z0-9]{1,20}-[a-z][a-z0-9-]{1,28}[a-z0-9]', target['https_gateway'])))
            or any(not isinstance(target[k], str) or not re.fullmatch(p, target[k]) for k, p in patterns.items())
            or '/' not in target['registry_prefix'] or '.' not in target['registry_prefix'].split('/')[0]):
        raise fail('CD_CONFIG_INVALID', 'configure')
    return dict(target)


def prepare_release(bundle_dir, target):
    """Read-only review plan. Its hash is the exact value passed to create_allow."""
    target = checked_target(target)
    try:
        manifest = bundle.verify(bundle_dir)
        spec = yaml.safe_load((Path(bundle_dir) / 'jasmin.yaml').read_bytes())
        # Initial adapter is deliberately limited to stateless, secret-free apps.
        # DB/operator readiness and secret ingestion must be registered separately.
        if spec.get('resources') or any(s.get('secrets') or s.get('autoscaling') for s in spec['services']):
            raise fail('CONTROL_NOT_READY', 'capabilities')
        if target['https_gateway'] != f"railshot-{target['tenant']}-{spec['app']}":
            raise fail('CD_CONFIG_INVALID', 'gateway')
        # Ask the actual pinned renderer for the selected route; do not duplicate
        # its root-service/subpath selection in the supervisor contract.
        preview_images = {name: f"{target['registry_prefix']}-{name}@{value['id']}"
                          for name, value in manifest['images'].items()}
        with tempfile.TemporaryDirectory(prefix='railshot-release-plan-') as tmp:
            preview = render(spec, Path(tmp) / 'app', target['tenant'], target['domain'], preview_images,
                             target['suffix'], target['storage_class'], https_gateway=target['https_gateway'])
        body = {'version': 1, 'operation': 'deploy', 'target': target,
                'manifest_sha256': bundle.file_hash(Path(bundle_dir) / 'manifest.json'),
                'source_sha256': manifest['source_sha256'], 'images': manifest['images'],
                'app': spec['app'], 'gitops_path': f"apps/{target['cluster']}/{target['tenant']}/{spec['app']}",
                'host': f"{spec['app']}-{target['suffix']}.{target['domain']}",
                'render_sha256': bundle.file_hash(PLATFORM / 'render/render.py'),
                'execution_sha256': bundle.file_hash(PLATFORM / 'execution.py'),
                'release_sha256': bundle.file_hash(Path(__file__)),
                'observer_sha256': bundle.file_hash(PLATFORM / 'control/deployment.py'),
                'deployment_url': preview['url'], 'probe_url': preview['probe_url'],
                'external_access': 'NOT_VERIFIED', 'transport': 'https' if target['https_gateway'] else 'http'}
        if len(body['host']) > 253 or len(body['host'].split('.')[0]) > 63:
            raise fail('CD_CONFIG_INVALID', 'hostname')
        return {**body, 'plan_hash': digest(body)}
    except OperationError:
        raise
    except (ValueError, KeyError, OSError, TypeError, StopIteration) as exc:
        raise fail('CD_CONFIG_INVALID', 'prepare', cause=exc) from exc


def check_operation(plan, operation):
    target = plan['target']
    expected = {'operation': 'deploy', 'plan_hash': plan['plan_hash'],
                'generation': target['generation'], 'tenant_id': target['tenant'],
                'workspace_id': target['workspace_id']}
    if (not isinstance(operation, dict) or any(operation.get(k) != v for k, v in expected.items())
            or any(not isinstance(operation.get(k), str) or not re.fullmatch(r'[a-z0-9-]{36}', operation[k])
                   for k in ('id', 'allow_id'))):
        raise fail('CONTROL_ALLOW_INVALID', 'authorize')


class GitHubWriter:
    """Bounded native GitHub API transport; no checkout, hooks, shell or force push."""
    def __init__(self, repo_url):
        self.repo = repo_url.removeprefix('https://github.com/').removesuffix('.git')

    def call(self, endpoint, method='GET', data=None):
        resource = f'repos/{self.repo}' + ('/' + endpoint if endpoint else '')
        result = run_bounded(['gh', 'api', '--method', method, resource,
                              *(['--input', '-'] if data is not None else [])],
                             input=canonical(data) if data is not None else None, timeout=60)
        if result.returncode:
            raise fail('CD_OBSERVATION_UNAVAILABLE' if method == 'GET' else 'STATE_INFLIGHT_UNCERTAIN',
                       'gitops.' + method.lower(), unknown=method != 'GET',
                       cause=subprocess.CalledProcessError(result.returncode, 'gh'))
        return json.loads(result.stdout)

    def preflight(self):
        repo = self.call('')
        if repo.get('default_branch') != 'main' or repo.get('permissions', {}).get('push') is not True:
            raise fail('CONTROL_NOT_READY', 'gitops.permissions')
        return self.call('git/ref/heads/main')['object']['sha']

    def create_commit(self, base, path, files, operation_id):
        old = self.call('git/trees/' + base + '?recursive=1')
        if old.get('truncated'):
            raise fail('CD_CONFIG_INVALID', 'gitops.tree')
        prefix = path + '/'
        existing = {r['path'] for r in old['tree'] if r['type'] == 'blob' and r['path'].startswith(prefix)}
        # Only replace the selected application's generated manifest directory.
        tree = [{'path': prefix + name, 'mode': '100644', 'type': 'blob', 'content': text}
                for name, text in sorted(files.items())]
        tree += [{'path': name, 'mode': '100644', 'type': 'blob', 'sha': None}
                 for name in sorted(existing - {prefix + name for name in files})]
        result = self.call('git/trees', 'POST', {'base_tree': old['sha'], 'tree': tree})
        commit = self.call('git/commits', 'POST', {'message': f'deploy: {path} ({operation_id})',
                           'tree': result['sha'], 'parents': [base]})
        if not re.fullmatch(r'[a-f0-9]{40}', commit.get('sha', '')):
            raise fail('STATE_INFLIGHT_UNCERTAIN', 'gitops.commit', unknown=True)
        return commit['sha']

    def push(self, revision):
        self.call('git/refs/heads/main', 'PATCH', {'sha': revision, 'force': False})
        if self.call('git/ref/heads/main')['object']['sha'] != revision:
            raise fail('STATE_INFLIGHT_UNCERTAIN', 'gitops.readback', unknown=True)


def verify_and_record_lkg(plan, spec, files, images, meta, revision, receipt, root, *,
                          observer, runtime_observer, external_probe, timeout):
    """A graph on both sides of HTTPS plus a final exact Argo read is mandatory."""
    target = plan['target']
    receipt.update(status='UNKNOWN', phase='runtime.observe')
    durable_write(root / 'release.json', canonical(receipt))
    before = runtime_observer(spec, images, meta)
    receipt.update(phase='external.observe')
    durable_write(root / 'release.json', canonical(receipt))
    external = external_probe(meta['probe_url'])
    after = runtime_observer(spec, images, meta)
    require(external.get('status') == 'PASS' and external.get('tls_verified') is True
            and external.get('url') == meta['probe_url'] == plan['probe_url']
            and meta['url'] == plan['deployment_url'])
    require({k: v for k, v in before.items() if k != 'observed_at'}
            == {k: v for k, v in after.items() if k != 'observed_at'} and after.get('status') == 'PASS')
    final_status, final_error, final_app = observer(f"t-{target['tenant']}-{plan['app']}", revision,
        list(images.values()), target['repo_url'], plan['gitops_path'], timeout)
    if final_status != 'PASS' or final_error is not None:
        raise final_error or fail('CD_OBSERVATION_UNAVAILABLE', 'argocd.recheck', unknown=True)
    require(receipt['application_uid'] and (final_app or {}).get('metadata', {}).get('uid') == receipt['application_uid']
            and (final_app or {}).get('status', {}).get('sync', {}).get('revision') == revision)
    receipt.update(status='PASS', phase='deployment.verified', external_access='PASS',
        pod_image_identity='PASS', deployment_url=meta['url'], external=external,
        runtime_before=before, runtime_after=after, observed_revision=revision)
    lkg = {'version': 1, 'scope': 'stateless-app-manifests-only', 'plan': plan, 'spec': spec,
        'revision': revision, 'images': images, 'files': files, 'files_sha256': digest(files),
        'manifest_sha256': plan['manifest_sha256'], 'source_sha256': plan['source_sha256'],
        'external': external, 'runtime': after, 'application_uid': receipt['application_uid']}
    lkg['sha256'] = digest(lkg)
    durable_write(root / 'lkg.json', canonical(lkg))
    receipt['lkg_sha256'] = lkg['sha256']


def execute_release(plan, bundle_dir, operation, *, work_dir, observer, runtime_observer=None,
                    external_probe=probe_https, timeout=600, publisher_authfile=None):
    """One-shot dispatch; interrupted/uncertain intents need explicit reconciliation.

    The supervisor must fence workspace generation and reserve any budget before
    calling. A local lock prevents duplicate processes, not multi-host scheduling.
    No provider VM, firewall, certificate or DNS changes occur in this adapter.
    """
    if type(timeout) is not int or not 1 <= timeout <= 900:
        raise fail('CD_CONFIG_INVALID', 'configure')
    current = prepare_release(bundle_dir, plan.get('target'))
    if current != plan:
        raise fail('STATE_BINDING_MISMATCH', 'authorize')
    check_operation(plan, operation)
    if runtime_observer is None or not plan['target']['https_gateway']:
        raise fail('CONTROL_NOT_READY', 'runtime.configure')
    root = private_directory(work_dir)
    lock = os.open(root / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise fail('STATE_WRITER_CONFLICT', 'lock', cause=exc) from exc
        receipt_path = root / 'release.json'
        if receipt_path.exists():
            prior = json.loads(receipt_path.read_bytes())
            if prior.get('plan_hash') != plan['plan_hash'] or prior.get('operation_id') != operation['id']:
                raise fail('STATE_BINDING_MISMATCH', 'resume')
            if prior.get('status') == 'PASS':
                return prior
            raise fail('STATE_INFLIGHT_UNCERTAIN', 'resume', unknown=True)
        target = plan['target']
        writer = GitHubWriter(target['repo_url'])
        base = writer.preflight()  # no publication when GitOps binding cannot be read/written
        if base != target['gitops_revision']:
            raise fail('STATE_BINDING_MISMATCH', 'gitops.base')
        receipt = {'version': 1, 'operation_id': operation['id'], 'plan_hash': plan['plan_hash'],
                   'status': 'UNKNOWN', 'phase': 'publish', 'base_revision': base,
                   'external_access': 'NOT_VERIFIED', 'pod_image_identity': 'NOT_VERIFIED'}

        def save():
            durable_write(receipt_path, canonical(receipt))

        save()  # write-ahead uncertainty survives interruption before network side effects
        try:
            images = bundle.publish(bundle_dir, target['registry_prefix'], target['tag'], journal_dir=root / 'publish',
                                    backend=target['publisher_backend'], authfile=publisher_authfile)
            receipt.update(images=images, phase='render'); save()
            spec = yaml.safe_load((Path(bundle_dir) / 'jasmin.yaml').read_bytes())
            with tempfile.TemporaryDirectory(prefix='render-', dir=root) as tmp:
                output = Path(tmp) / 'app'
                meta = render(spec, output, target['tenant'], target['domain'], images,
                              target['suffix'], target['storage_class'], https_gateway=target['https_gateway'])
                files = {p.name: p.read_text() for p in output.iterdir()}
            durable_write(root / 'rendered.json', canonical(files))
            receipt['rendered_files_sha256'] = digest(files)
            receipt.update(phase='gitops.commit', rendered=meta); save()
            revision = writer.create_commit(base, plan['gitops_path'], files, operation['id'])
            receipt.update(phase='gitops.push', revision=revision); save()
            writer.push(revision)
            receipt.update(phase='argocd.observe'); save()
            status, error, app = observer(f"t-{target['tenant']}-{plan['app']}", revision,
                                         list(images.values()), target['repo_url'], plan['gitops_path'], timeout)
            if status not in {'PASS', 'FAIL', 'BLOCKED', 'UNKNOWN'} or (status == 'PASS' and error is not None):
                raise fail('STEP_OUTPUT_INVALID', 'argocd.observe', unknown=True)
            receipt.update(status=status, phase='argocd.observe', error=error.as_dict() if error else None,
                           application_uid=(app or {}).get('metadata', {}).get('uid'),
                           observed_revision=(app or {}).get('status', {}).get('sync', {}).get('revision'))
            if status == 'PASS':
                verify_and_record_lkg(plan, spec, files, images, meta, revision, receipt, root,
                    observer=observer, runtime_observer=runtime_observer, external_probe=external_probe, timeout=timeout)
            receipt['event'] = event_record('release.observed', component='control.release', phase=receipt['phase'],
                                           outcome=receipt['status'], error=error, attributes={'operation_id': operation['id'],
                                           'revision': revision, 'scope': 'argocd-workload-external' if receipt['status'] == 'PASS' else 'argocd-application'})
            save()
            return receipt
        except Exception as exc:
            error = exc if isinstance(exc, OperationError) else fail('STATE_INFLIGHT_UNCERTAIN', receipt['phase'], unknown=True, cause=exc)
            receipt.update(status=error.outcome, error=error.as_dict()); save()
            if error is exc:
                raise
            raise error from exc
    finally:
        os.close(lock)


def prepare_rollback(lkg, target):
    """New deploy approval for immutable app-file restoration, never DB recovery."""
    target = checked_target(target)
    try:
        require(lkg['version'] == 1 and lkg['scope'] == 'stateless-app-manifests-only'
                and lkg['sha256'] == digest({k: v for k, v in lkg.items() if k != 'sha256'})
                and lkg['files_sha256'] == digest(lkg['files'])
                and lkg['external']['status'] == 'PASS' and lkg['runtime']['status'] == 'PASS')
        original = lkg['plan']
        require(original['plan_hash'] == digest({k: v for k, v in original.items() if k != 'plan_hash'}))
        # Generation and base GitOps revision must be refreshed by the supervisor.
        for key in TARGET_FIELDS - {'generation', 'gitops_revision', 'tag'}:
            require(original['target'][key] == target[key])
        require(lkg['spec']['app'] == original['app'] and not lkg['spec'].get('resources')
                and not any(s.get('secrets') or s.get('autoscaling') for s in lkg['spec']['services']))
        require(all(re.fullmatch(r'(?:[0-9]{2}-[a-z0-9-]+\.yaml|meta\.json)', name)
                    and isinstance(text, str) for name, text in lkg['files'].items())
                and sum(len(t.encode()) for t in lkg['files'].values()) < 2 * 1024 * 1024)
        require(lkg['manifest_sha256'] == original['manifest_sha256']
                and lkg['source_sha256'] == original['source_sha256'])
        body = {'version': 1, 'operation': 'deploy', 'mode': 'rollback', 'target': target,
                'lkg_sha256': lkg['sha256'], 'files_sha256': lkg['files_sha256'],
                'manifest_sha256': lkg['manifest_sha256'], 'source_sha256': lkg['source_sha256'],
                'app': original['app'], 'gitops_path': original['gitops_path'], 'host': original['host'],
                'images': lkg['images'], 'restore_revision': lkg['revision'],
                'release_sha256': bundle.file_hash(Path(__file__)),
                'observer_sha256': bundle.file_hash(PLATFORM / 'control/deployment.py'),
                'deployment_url': original['deployment_url'], 'probe_url': original['probe_url'],
                'scope': 'stateless-app-manifests-only', 'database_restore': 'NOT_PERFORMED'}
        return {**body, 'plan_hash': digest(body)}
    except OperationError:
        raise
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise fail('CD_CONFIG_INVALID', 'rollback.prepare', cause=exc) from exc


def execute_rollback(plan, lkg, operation, *, work_dir, observer, runtime_observer,
                     external_probe=probe_https, timeout=600):
    """Restore just this app in a new non-force commit, then run the same proof."""
    if (prepare_rollback(lkg, plan.get('target')) != plan or runtime_observer is None
            or type(timeout) is not int or not 1 <= timeout <= 900):
        raise fail('STATE_BINDING_MISMATCH', 'rollback.authorize')
    check_operation(plan, operation)
    root = private_directory(work_dir)
    lock = os.open(root / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise fail('STATE_WRITER_CONFLICT', 'rollback.lock', cause=exc) from exc
        file = root / 'release.json'
        if file.exists():
            old = json.loads(file.read_bytes())
            if old.get('plan_hash') != plan['plan_hash'] or old.get('operation_id') != operation['id']:
                raise fail('STATE_BINDING_MISMATCH', 'rollback.resume')
            if old.get('status') == 'PASS':
                return old
            raise fail('STATE_INFLIGHT_UNCERTAIN', 'rollback.resume', unknown=True)
        target = plan['target']; writer = GitHubWriter(target['repo_url'])
        base = writer.preflight()
        if base != target['gitops_revision']:
            raise fail('STATE_BINDING_MISMATCH', 'rollback.base')
        receipt = {'version': 1, 'operation_id': operation['id'], 'plan_hash': plan['plan_hash'],
            'mode': 'rollback', 'status': 'UNKNOWN', 'phase': 'gitops.commit', 'base_revision': base,
            'restored_lkg_sha256': lkg['sha256'], 'restored_revision': lkg['revision'],
            'external_access': 'NOT_VERIFIED', 'pod_image_identity': 'NOT_VERIFIED',
            'scope': 'stateless-app-manifests-only', 'database_restore': 'NOT_PERFORMED', 'images': lkg['images']}
        durable_write(file, canonical(receipt))
        try:
            revision = writer.create_commit(base, plan['gitops_path'], lkg['files'], operation['id'])
            receipt.update(revision=revision, phase='gitops.push');durable_write(file, canonical(receipt))
            writer.push(revision)
            status, error, app = observer(f"t-{target['tenant']}-{plan['app']}", revision,
                list(lkg['images'].values()), target['repo_url'], plan['gitops_path'], timeout)
            if status != 'PASS' or error is not None:
                raise error or fail('CD_OBSERVATION_UNAVAILABLE', 'rollback.observe', unknown=True)
            receipt['application_uid'] = (app or {}).get('metadata', {}).get('uid')
            meta = json.loads(lkg['files']['meta.json'])
            require(meta['host'] == plan['host'] and meta['namespace'] == f"t-{target['tenant']}-{plan['app']}")
            verify_and_record_lkg(plan, lkg['spec'], lkg['files'], lkg['images'], meta, revision, receipt, root,
                observer=observer, runtime_observer=runtime_observer, external_probe=external_probe, timeout=timeout)
            receipt['event'] = event_record('release.rollback.verified', component='control.release',
                phase='deployment.verified', outcome='PASS', attributes={'revision': revision, 'scope': receipt['scope']})
            durable_write(file, canonical(receipt))
            return receipt
        except Exception as exc:
            error = exc if isinstance(exc, OperationError) else fail('STATE_INFLIGHT_UNCERTAIN', receipt['phase'], unknown=True, cause=exc)
            receipt.update(status=error.outcome, error=error.as_dict());durable_write(file, canonical(receipt))
            if error is exc: raise
            raise error from exc
    finally:
        os.close(lock)


def dispatch(request, config):
    """Fixed local transport for a trusted supervisor; no artifact transfer here.

    Config is administrator-owned, contains a single registered workspace target
    and private absolute roots. The supervisor separately copies a CI job bundle
    into <bundle_root>/<ci_job_id> through its authenticated artifact channel.
    """
    if not isinstance(request, dict) or not isinstance(config, dict):
        raise fail('CD_CONFIG_INVALID', 'request')
    phase = request.get('phase')
    job = request.get('ci_job_id')
    try:
        if phase not in {'prepare', 'execute'} or str(uuid.UUID(job)) != job:
            raise ValueError('invalid dispatch')
        root = Path(config['bundle_root'])
        if not root.is_absolute() or root.is_symlink():
            raise ValueError('registered absolute bundle root required')
        bundle_dir = root / job
        binding = request['bundle']
        prefix = config.get('ci_workspace_id')
        if prefix is not None and str(uuid.UUID(prefix)) != prefix:
            raise ValueError('registered CI workspace required')
        expected_relative = f'{prefix}/{job}/bundle' if prefix is not None else job
        if binding.get('relative_path') != expected_relative:
            raise ValueError('bundle belongs to another CI job')
        plan = prepare_release(bundle_dir, config['target'])
        if (binding.get('manifest_sha256') != plan['manifest_sha256']
                or binding.get('source_sha256') != plan['source_sha256']
                or ('target' in request and checked_target(request['target']) != plan['target'])):
            raise fail('STATE_BINDING_MISMATCH', 'artifact')
        if phase == 'prepare':
            return {'phase': phase, 'ci_job_id': job, 'plan': plan}
        if request.get('plan') != plan:
            raise fail('STATE_BINDING_MISMATCH', 'authorize')
        # Never consult an ambient kubectl context on the operator's computer.
        kubeconfig = Path(config['observer_kubeconfig'])
        work_root = Path(config['work_root'])
        if not kubeconfig.is_absolute() or not work_root.is_absolute() or not kubeconfig.is_file():
            raise fail('CONTROL_NOT_READY', 'observer.configure')

        def observer(name, revision, images, repo, path, timeout):
            with tempfile.TemporaryDirectory(prefix='observer-', dir=private_directory(work_root)) as tmp:
                bindings = Path(tmp) / 'images.json'
                bindings.write_text(json.dumps({str(i): image for i, image in enumerate(images)}))
                env = {**os.environ, 'KUBECONFIG': str(kubeconfig)}
                result = run_bounded([sys.executable, str(PLATFORM.parent / 'ci/verify_deployment.py'),
                    name, revision, str(bindings), '--repo-url', repo, '--path', path, '--timeout', str(timeout)],
                    env=env, timeout=timeout + 30)
                record = json.loads(result.stdout)
                status = record['status']
                if (record.get('scope') != 'argocd-application'
                        or record.get('attributes', {}).get('expected_revision') != revision
                        or (result.returncode == 0) != (status == 'PASS')):
                    raise fail('STEP_OUTPUT_INVALID', 'argocd.observe', unknown=True)
                error = OperationError.from_dict(record['error']) if record.get('error') else None
                attr = record['attributes']
                return status, error, {'metadata': {'uid': attr.get('application_uid')},
                    'status': {'sync': {'revision': attr.get('observed_revision')}}}

        operation = request['operation']
        check_operation(plan, operation)
        def runtime_observer(spec, images, meta):
            return observe_graph(kubeconfig, spec, images, meta['namespace'], meta['host'], plan['target']['https_gateway'])
        receipt = execute_release(plan, bundle_dir, operation, work_dir=work_root / operation['id'],
            observer=observer, runtime_observer=runtime_observer, publisher_authfile=config.get('publisher_authfile'))
        return {'phase': phase, 'job_id': request.get('job_id'), 'job_generation': request.get('job_generation'),
                'receipt': receipt}
    except OperationError:
        raise
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise fail('CD_CONFIG_INVALID', 'dispatch', cause=exc) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True, help='fixed administrator-owned transport configuration')
    args = parser.parse_args()
    try:
        info = args.config.lstat()
        if not args.config.is_absolute() or args.config.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise fail('CD_CONFIG_INVALID', 'configure')
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise fail('CD_CONFIG_INVALID', 'request')
        result = dispatch(json.loads(raw), json.loads(args.config.read_bytes()))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        error = exc if isinstance(exc, OperationError) else fail('CD_CONFIG_INVALID', 'configure', cause=exc)
        print(json.dumps({'error': error.as_dict(), 'status': error.outcome}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
