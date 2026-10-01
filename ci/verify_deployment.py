"""Observe Argo locally with get-only Kubernetes credentials; no public Argo endpoint.

This is Argo evidence, not live Pod imageID or revision-bound external HTTP evidence.
Run only on the trusted CD worker, never on the user-code/Docker runner.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'platform'))
from observability import OperationError, event_record


def target_matches(app, name, repo, path):
    spec, meta = app.get('spec', {}), app.get('metadata', {})
    source, dest = spec.get('source', {}), spec.get('destination', {})
    return (meta.get('name') == name and meta.get('namespace') == 'argocd'
            and not meta.get('deletionTimestamp') and spec.get('project') == 'railshot-tenants'
            and not spec.get('sources') and source.get('repoURL') == repo
            and source.get('path') == path and source.get('targetRevision') == 'main'
            and dest.get('server') == 'https://kubernetes.default.svc'
            and dest.get('namespace') == name)


def matches(app, revision, images):
    status = app.get('status', {})
    operation = status.get('operationState', {})
    return (not app.get('operation') and not status.get('conditions')
            and status.get('sync', {}).get('status') == 'Synced'
            and status.get('sync', {}).get('revision') == revision
            and status.get('health', {}).get('status') == 'Healthy'
            and operation.get('phase') == 'Succeeded'
            and operation.get('syncResult', {}).get('revision') == revision
            and set(images).issubset(status.get('summary', {}).get('images', [])))


def read_application(name, timeout):
    result = subprocess.run(['kubectl', '--namespace=argocd', f'--request-timeout={timeout}s',
                             'get', 'applications.argoproj.io', name, '--ignore-not-found', '-o', 'json'],
                            capture_output=True, text=True, timeout=timeout + 1, check=False)
    if result.returncode:
        # Never copy kubectl stderr: an exec credential plugin may emit credentials.
        cause = subprocess.CalledProcessError(result.returncode, 'kubectl')
        raise OperationError('CD_OBSERVATION_UNAVAILABLE', component='cd', phase='argocd.observe',
                             outcome='UNKNOWN', retry_policy='after_reconcile', side_effect='unknown', cause=cause) from cause
    app = json.loads(result.stdout) if result.stdout.strip() else None
    if app is not None and not isinstance(app, dict):
        raise ValueError('Kubernetes object shape invalid')
    return app


def observe(name, revision, images, repo, path, timeout):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        last = read_application(name, max(1, min(15, int(deadline - time.monotonic()))))
        if last:
            if not target_matches(last, name, repo, path):
                return 'BLOCKED', OperationError('CD_TARGET_MISMATCH', component='cd', phase='argocd.observe'), last
            if matches(last, revision, images):
                return 'PASS', None, last
            op = last.get('status', {}).get('operationState', {})
            if op.get('syncResult', {}).get('revision') == revision and op.get('phase') in ('Failed', 'Error'):
                return 'FAIL', OperationError('CD_OPERATION_FAILED', component='cd', phase='argocd.observe',
                                             outcome='FAIL', retry_policy='after_reconcile', side_effect='possible'), last
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    return 'UNKNOWN', OperationError('CD_OBSERVATION_TIMEOUT', component='cd', phase='argocd.observe',
                                    outcome='UNKNOWN', retry_policy='after_reconcile', side_effect='unknown'), last


def validate_inputs(a):
    path = re.fullmatch(r'apps/([a-z][a-z0-9-]{1,39})/([a-z0-9]{1,20})/([a-z][a-z0-9-]{1,28}[a-z0-9])', a.path)
    if (not path or a.app != f't-{path[2]}-{path[3]}'
            or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', a.repo_url)
            or not re.fullmatch('[a-f0-9]{40}', a.revision) or not 1 <= a.timeout <= 900):
        raise ValueError('invalid registered target')
    raw = json.loads(Path(a.images).read_text())
    if (not isinstance(raw, dict) or not raw or any(not isinstance(i, str)
            or not re.fullmatch(r'[a-z0-9./_-]+@sha256:[0-9a-f]{64}', i) for i in raw.values())):
        raise ValueError('invalid image binding')
    return raw


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('app'); p.add_argument('revision'); p.add_argument('images')
    p.add_argument('--repo-url', required=True)
    p.add_argument('--path', required=True, help='Trusted apps/<cluster>/<tenant>/<app> binding')
    p.add_argument('--timeout', type=int, default=600)
    a = p.parse_args()
    status, error, app, raw = 'BLOCKED', None, None, {}
    try:
        raw = validate_inputs(a)
    except (OSError, ValueError) as exc:
        error = OperationError('CD_CONFIG_INVALID', component='cd', phase='argocd.configure',
                               retry_policy='after_configuration', cause=exc)
    if error is None:
        try:
            status, error, app = observe(a.app, a.revision, list(raw.values()), a.repo_url, a.path, a.timeout)
        except OperationError as exc:
            error, status = exc, exc.outcome
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError) as exc:
            status = 'UNKNOWN'
            error = OperationError('CD_OBSERVATION_UNAVAILABLE', component='cd', phase='argocd.observe',
                                   outcome=status, retry_policy='after_reconcile', side_effect='unknown', cause=exc)
    meta, observed = (app or {}).get('metadata', {}), (app or {}).get('status', {})
    attributes = {'scope': 'argocd-application', 'workflow_run_id': os.environ.get('GITHUB_RUN_ID'),
                      'workflow_run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
                      'app': a.app if raw else None, 'repo_url': a.repo_url if raw else None,
                      'path': a.path if raw else None, 'expected_revision': a.revision if raw else None,
                      'observed_revision': observed.get('sync', {}).get('revision'),
                      'operation_phase': observed.get('operationState', {}).get('phase'),
                      'application_uid': meta.get('uid'), 'resource_version': meta.get('resourceVersion'),
                      'images': raw}
    record = event_record('deployment.observed', component='cd', phase=error.phase if error else 'argocd.observe', outcome=status,
                          run_id=os.environ.get('RAILSHOT_RUN_ID'), error=error, attributes=attributes)
    # Compatibility fields are projections; consumers should read the v1 envelope.
    record.update(status=status, scope='argocd-application')
    print(json.dumps(record))
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
