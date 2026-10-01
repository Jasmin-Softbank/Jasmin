"""Trusted release adapter: consumed Allow -> exact CI bundle -> GitOps -> Argo.

Call only from an authenticated supervisor on a separate trusted release executor.
The operation argument must come from ControlState.get_operation, never HTTP JSON.
Docker push and gh authentication stay on this executor, never on the CI worker.
An Argo PASS is not external HTTP/TLS or Pod imageID verification.
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
        if target['https_gateway'] not in (None, f"railshot-{target['tenant']}-{spec['app']}"):
            raise fail('CD_CONFIG_INVALID', 'gateway')
        body = {'version': 1, 'operation': 'deploy', 'target': target,
                'manifest_sha256': bundle.file_hash(Path(bundle_dir) / 'manifest.json'),
                'source_sha256': manifest['source_sha256'], 'images': manifest['images'],
                'app': spec['app'], 'gitops_path': f"apps/{target['cluster']}/{target['tenant']}/{spec['app']}",
                'host': f"{spec['app']}-{target['suffix']}.{target['domain']}",
                'render_sha256': bundle.file_hash(PLATFORM / 'render/render.py'),
                'execution_sha256': bundle.file_hash(PLATFORM / 'execution.py'),
                'external_access': 'NOT_VERIFIED', 'transport': 'https' if target['https_gateway'] else 'http'}
        if len(body['host']) > 253 or len(body['host'].split('.')[0]) > 63:
            raise fail('CD_CONFIG_INVALID', 'hostname')
        return {**body, 'plan_hash': digest(body)}
    except OperationError:
        raise
    except (ValueError, KeyError, OSError, TypeError) as exc:
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


def execute_release(plan, bundle_dir, operation, *, work_dir, observer, timeout=600, publisher_authfile=None):
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
            receipt['event'] = event_record('release.observed', component='control.release', phase='argocd.observe',
                                           outcome=status, error=error, attributes={'operation_id': operation['id'],
                                           'revision': revision, 'scope': 'argocd-application'})
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
        if binding.get('relative_path') != job:
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
        receipt = execute_release(plan, bundle_dir, operation, work_dir=work_root / operation['id'],
            observer=observer, publisher_authfile=config.get('publisher_authfile'))
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
