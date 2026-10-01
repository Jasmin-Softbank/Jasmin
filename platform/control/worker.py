#!/usr/bin/env python3
"""One authenticated supervisor request per invocation on a private, credential-free VM.

The HTTP supervisor owns identity, lease and generation fencing. This bridge is not
a public server or a tenant sandbox. It never accepts a filesystem root or CLI argv
from the request. Disconnect/timeout does not prove Docker side effects stopped.
"""
import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import uuid

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM))
sys.path.insert(0, str(PLATFORM / 'gate'))
from observability import OperationError
from poc.intake import SECRET_NAME, SECRET_TEXT
from process import OutputLimitError, ProcessCleanupError, run_bounded
from runner.runtime_boundary import private_directory
from storage import durable_write
from gate import ORDER, require_ci_network, require_ci_builder
from bundle import verify as verify_bundle

MAX_BYTES, MAX_FILES, MAX_OUTPUT = 50 * 1024 * 1024, 5000, 8 * 1024 * 1024
REQUEST_BYTES = 72 * 1024 * 1024
FIELDS = {'operation', 'workspace_id', 'job_id', 'generation', 'files', 'command', 'selected_root', 'upload_id'}


def error(code, phase, *, unknown=False, cause=None):
    return OperationError(code, component='control.worker', phase=phase,
                          outcome='UNKNOWN' if unknown else 'BLOCKED',
                          retry_policy='after_reconcile' if unknown else 'after_configuration',
                          side_effect='unknown' if unknown else 'none', cause=cause)


def relative(value):
    if (not isinstance(value, str) or not value or len(value) > 1024 or '\\' in value
            or any(ord(c) < 32 for c in value) or value.startswith('/')
            or any(p in {'', '.', '..', '.git'} for p in value.split('/'))):
        raise error('CONTROL_CONFIG_INVALID', 'path')
    return PurePosixPath(value)


def validate(request):
    if not isinstance(request, dict) or set(request) - FIELDS:
        raise error('CONTROL_CONFIG_INVALID', 'request')
    for key in ('job_id', 'workspace_id'):
        try:
            valid = isinstance(request.get(key), str) and str(uuid.UUID(request[key])) == request[key]
        except ValueError:
            valid = False
        if not valid:
            raise error('CONTROL_CONFIG_INVALID', 'identity')
    if (request.get('operation') not in {'prepare', 'ci', 'terminal', 'inspect'}
            or type(request.get('generation')) is not int or request['generation'] < 1):
        raise error('CONTROL_CONFIG_INVALID', 'request')
    if request.get('selected_root') not in (None, '.'):
        relative(request['selected_root'])
    if request.get('operation') == 'terminal':
        command = request.get('command')
        if not isinstance(command, str) or not command.strip() or len(command.encode()) > 16384 or '\0' in command:
            raise error('CONTROL_CONFIG_INVALID', 'terminal')
        if SECRET_TEXT.search(command.encode()):
            raise error('INTAKE_REJECTED', 'terminal')
    elif 'command' in request:
        raise error('CONTROL_CONFIG_INVALID', 'request')
    if request.get('upload_id') is not None and (not isinstance(request['upload_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', request['upload_id'])):
        raise error('CONTROL_CONFIG_INVALID', 'upload')
    if request['operation'] in {'prepare', 'ci'} and not request.get('upload_id'):
        raise error('CONTROL_CONFIG_INVALID', 'upload')
    return request


def extract(files, destination):
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise error('INTAKE_REJECTED', 'upload')
    total, decoded = 0, {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'content_base64'}:
            raise error('INTAKE_REJECTED', 'upload')
        name = str(relative(item['path']))
        if name in decoded or SECRET_NAME.search(name):
            raise error('INTAKE_REJECTED', 'upload')
        try:
            data = base64.b64decode(item['content_base64'], validate=True)
        except (ValueError, TypeError) as exc:
            raise error('INTAKE_REJECTED', 'upload', cause=exc) from exc
        total += len(data)
        if total > MAX_BYTES or SECRET_TEXT.search(data):
            raise error('INTAKE_REJECTED', 'upload')
        decoded[name] = data
    # Fresh private directory, regular files only: archive symlink/hardlink modes do not exist.
    destination.mkdir(mode=0o755)
    for name, data in decoded.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with path.open('xb') as stream:
            os.chmod(path, 0o644); stream.write(data)
    return {'files': len(decoded), 'bytes': total}


def redact(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.fullmatch(r'(?i)(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)', k)
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    value = SECRET_TEXT.sub(b'[REDACTED]', value.encode()).decode(errors='replace')
    value = re.sub(r'(?i)(?:https?|postgres(?:ql)?|mysql|redis)://[^\s/@]+:[^\s/@]+@', '[REDACTED_URL]@', value)
    value = re.sub(r'(?i)((?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)["\']?\s*[=:]\s*["\']?)(?:Bearer\s+)?[^\s,;"\'}]+', r'\1[REDACTED]', value)
    value = re.sub(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b', '[REDACTED]', value)
    return ''.join(c for c in value if c in '\n\t' or ord(c) >= 32)


class Bridge:
    def __init__(self, request, emit):
        self.request, self.emit_raw = request, emit
        self.total, self.pending, self.offsets = 0, {}, {}
        self.progress, self.progress_buffer, self.progress_offset = [], b'', 0

    def emit(self, kind, **data):
        self.emit_raw(redact({'type': kind, **{k: self.request[k] for k in ('workspace_id', 'job_id', 'generation')}, **data}))

    def log(self, stream, chunk, *, phase=None, source=None):
        self.total += len(chunk)
        if self.total > MAX_OUTPUT:
            raise OutputLimitError(MAX_OUTPUT)
        phase = phase or self.request['operation']
        key = (stream, phase, source)
        data = self.pending.get(key, b'') + chunk
        lines = data.split(b'\n'); self.pending[key] = lines.pop()
        for line in lines:
            self.emit('log', stream=stream, phase=phase, text=line.decode(errors='replace')[:65536])
        if len(self.pending[key]) > 65536:
            self.pending[key] = b''
            self.emit('log', stream=stream, phase=phase, text='[oversized line omitted]')

    def flush(self):
        for (stream, phase, _), line in self.pending.items():
            if line: self.emit('log', stream=stream, phase=phase, text=line.decode(errors='replace'))
        self.pending.clear()

    def tail_quality(self, run):
        self.tail_progress(run)
        for path in sorted([*run.glob('gate-*/quality-*.log'), *run.glob('gate-*/failure.txt')]):
            if (not re.fullmatch(r'quality-\d+\.log|failure\.txt', path.name)) or path.is_symlink():
                continue
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise error('STEP_OUTPUT_INVALID', 'log', unknown=True)
                stream.seek(self.offsets.get(path, 0))
                data = stream.read(MAX_OUTPUT - self.total + 1)
                self.offsets[path] = stream.tell()
            # File provenance, never whichever stage happened to arrive last.
            if data: self.log('gate', data, phase='Q' if path.name.startswith('quality-') else 'ci', source=str(path))

    def tail_progress(self, run):
        path = run / 'gate-0/progress.jsonl'
        if not path.exists(): return
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_size > 65536:
                    raise ValueError('invalid supervisor journal')
                stream.seek(self.progress_offset)
                data = stream.read(65537); self.progress_offset = stream.tell()
            lines = (self.progress_buffer + data).split(b'\n'); self.progress_buffer = lines.pop()
            for line in lines:
                row = json.loads(line); attrs = row['attributes']; index = len(self.progress)
                completed = index % 2 == 1
                if (index >= len(ORDER) * 2 or row['component'] != 'gate' or row['phase'] != ORDER[index // 2]
                        or row['event_name'] != ('gate.layer.completed' if completed else 'gate.layer.started')
                        or attrs['sequence'] != index + 1 or attrs['total_steps'] != len(ORDER)
                        or attrs['completed_steps'] != (index + 1) // 2
                        or attrs['observation_source'] != 'gate-supervisor'
                        or (not completed and row['outcome'] != 'RUNNING')
                        or (completed and row['outcome'] not in {'PASS', 'FAIL', 'BLOCKED', 'UNKNOWN'})
                        or (self.progress and any(row[k] != self.progress[0][k] for k in ('run_id', 'attempt_id')))):
                    raise ValueError('supervisor sequence mismatch')
                self.progress.append(row)
                self.emit('stage', phase=row['phase'], layer=row['phase'], outcome=row['outcome'],
                          attempt_id=row['attempt_id'], **{key: attrs[key] for key in
                          ('sequence', 'completed_steps', 'total_steps', 'observation_source', 'started_at', 'duration_s') if key in attrs})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise error('STEP_OUTPUT_INVALID', 'ci.progress', unknown=True, cause=exc) from exc

    def verify_progress(self, run, verdict):
        self.tail_progress(run)
        layers = [row for row in verdict.get('layers', []) if row.get('layer') in ORDER]
        if (self.progress_buffer or len(self.progress) != len(layers) * 2
                or any(self.progress[index * 2 + 1] != row.get('event') for index, row in enumerate(layers))
                or any(row.get('run_id') != verdict.get('run_id') or row.get('attempt_id') != verdict.get('attempt_id')
                       for row in self.progress)):
            raise error('STEP_OUTPUT_INVALID', 'ci.progress.receipt', unknown=True)

    def call(self, argv, *, cwd, timeout=1800, run=None):
        env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), 'HOME': str(self.home),
               'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1', 'DOCKER_CONFIG': str(self.docker_config)}
        Path(env['HOME']).mkdir(exist_ok=True, mode=0o700)
        try:
            result = run_bounded(argv, cwd=cwd, env=env, timeout=timeout, on_output=self.log,
                                 on_tick=(lambda: self.tail_quality(run)) if run else None)
            if run: self.tail_quality(run)
            self.flush()
            return result
        except subprocess.TimeoutExpired as exc:
            raise error('STEP_TIMEOUT', 'execute', unknown=True, cause=exc) from exc
        except (FileNotFoundError, PermissionError) as exc:
            raise error('STEP_START_FAILED', 'execute', cause=exc) from exc
        except (OutputLimitError, ProcessCleanupError, OSError) as exc:
            raise error('STEP_OUTPUT_INVALID', 'execute', unknown=True, cause=exc) from exc


def source_directory(workspace):
    ref = workspace / 'source.json'
    if not ref.is_file():
        return None
    path = workspace / relative(json.loads(ref.read_text())['work'])
    if not path.is_dir() or path.is_symlink() or workspace not in path.resolve().parents:
        raise error('STATE_EVIDENCE_MISMATCH', 'source')
    return path


def parse_result(process):
    try:
        result = json.loads(process.stdout)
        if not isinstance(result, dict): raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise error('STEP_OUTPUT_INVALID', 'receipt', unknown=True, cause=exc) from exc


def inspect(bridge, workspace, job):
    details = {'hostname': socket.gethostname(), 'observed_at': datetime.now(timezone.utc).isoformat(),
               'docker': {'available': False}, 'quality_network': {'available': False, 'name': 'railshot-quality'},
               'ci_ready': False, 'native_network_verified': False, 'native_builder_verified': False, 'filetree': []}
    work = source_directory(workspace)
    if work is not None:
        details['filetree'] = [p.relative_to(work).as_posix() for p in sorted(work.rglob('*'))
                               if not p.is_symlink() and p.is_file() and '.git' not in p.relative_to(work).parts][:MAX_FILES]
    try:
        docker = bridge.call(['docker', 'version', '--format', '{{.Server.Version}}'], cwd=job, timeout=15)
        details['docker'] = {'available': docker.returncode == 0, 'server_version': docker.stdout.strip() if docker.returncode == 0 else None}
        network = bridge.call(['docker', 'network', 'inspect', 'railshot-quality', '--format', '{{json .Driver}}'], cwd=job, timeout=15)
        if network.returncode == 0:
            details['quality_network'].update(available=json.loads(network.stdout) == 'bridge', driver=json.loads(network.stdout))
        # The same explicit noncredential Docker config is used by CI subprocesses.
        # This process handles one request only; restore the environment even on failure.
        previous = os.environ.get('DOCKER_CONFIG')
        try:
            os.environ['DOCKER_CONFIG'] = str(bridge.docker_config)
            require_ci_network('railshot-quality')
            details['native_network_verified'] = True
            require_ci_builder()
            details['native_builder_verified'] = True
        finally:
            if previous is None: os.environ.pop('DOCKER_CONFIG', None)
            else: os.environ['DOCKER_CONFIG'] = previous
        details['ci_ready'] = (sys.platform == 'linux' and details['docker']['available']
            and details['quality_network']['available'] and details['native_network_verified'] and details['native_builder_verified']
            and all(shutil.which(tool) for tool in ('git', 'bash')))
    except OperationError as exc:
        details['observation_error'] = exc.as_dict()
    return {'outcome': 'PASS', 'returncode': 0, 'details': details}


def prepare_upload(request, workspace, job):
    """Same upload identity snapshots the current VM source; a new identity replaces it.

    The immutable HTTP upload hash guards accidental reuse of an upload id with
    different bytes. User edits remain on the VM; CI receives a fresh intake copy.
    """
    files = request.get('files')
    upload_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    ref = workspace / 'source.json'
    previous = json.loads(ref.read_text()) if ref.is_file() else None
    source = source_directory(workspace) if previous else None
    if previous and previous.get('upload_id') == request['upload_id']:
        if previous['upload_hash'] != upload_hash:
            raise error('STATE_BINDING_MISMATCH', 'upload')
        # No archive extraction or link traversal. Bound the snapshot before copying.
        total, count = 0, 0
        for path in source.rglob('*'):
            rel = path.relative_to(source)
            if '.git' in rel.parts: continue
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise error('INTAKE_REJECTED', 'snapshot')
            if path.is_file():
                count += 1; total += path.stat().st_size
                if count > MAX_FILES or total > MAX_BYTES:
                    raise error('INTAKE_REJECTED', 'snapshot')
        shutil.copytree(source, job / 'upload', ignore=shutil.ignore_patterns('.git'))
        for path in [job / 'upload', *(job / 'upload').rglob('*')]:
            path.chmod(0o755 if path.is_dir() else 0o644)
    else:
        extract(files, job / 'upload')
    return {'upload_id': request['upload_id'], 'upload_hash': upload_hash}


def execute(request, root, emit):
    request = validate(request)
    root = private_directory(root)
    workspace = private_directory(root / request['workspace_id'])
    bridge = Bridge(request, emit)
    bridge.docker_config = root / 'docker-config'
    with (workspace / '.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise error('STATE_WRITER_CONFLICT', 'claim', cause=exc) from exc
        job = workspace / request['job_id']
        if job.exists():
            raise error('STATE_INFLIGHT_UNCERTAIN', 'claim', unknown=True)
        job.mkdir(mode=0o700)
        bridge.home = job / 'home'
        durable_write(job / 'request.json', json.dumps({k: v for k, v in request.items() if k not in {'files', 'command'}}, sort_keys=True).encode())
        operation = request['operation']; bridge.emit('stage', phase=operation, outcome='RUNNING')
        if operation == 'inspect':
            result = inspect(bridge, workspace, job)
        elif operation == 'terminal':
            cwd = source_directory(workspace) or private_directory(workspace / 'terminal')
            process = bridge.call(['/bin/bash', '--noprofile', '--norc', '-c', request['command']], cwd=cwd, timeout=60)
            result = {'outcome': 'PASS' if process.returncode == 0 else 'FAIL', 'returncode': process.returncode,
                      'details': {'cwd': cwd.relative_to(root).as_posix(), 'source_sync': 'same upload_id CI snapshots this current VM workspace'}}
        else:
            source_binding = prepare_upload(request, workspace, job)
            run = job / 'run'
            selected = ['--selected-root', request['selected_root']] if request.get('selected_root') is not None else []
            if operation == 'prepare':
                process = bridge.call([sys.executable, str(PLATFORM / 'poc/intake.py'), str(job / 'upload'), str(job / 'work'), str(run)], cwd=job)
                intake = parse_result(process)
                if process.returncode != 0 or intake.get('ok') is not True:
                    raise error('INTAKE_REJECTED', 'intake')
                process = bridge.call([sys.executable, str(PLATFORM / 'gate/prepare.py'), str(job / 'work'), *selected], cwd=job)
                plan = parse_result(process)
                if (process.returncode == 0) != (plan.get('status') == 'READY'):
                    raise error('STEP_OUTPUT_INVALID', 'prepare.receipt', unknown=True)
                durable_write(workspace / 'source.json', json.dumps({**source_binding, 'work': (job / 'work').relative_to(workspace).as_posix()}).encode())
                result = {'outcome': 'PASS' if plan['status'] == 'READY' else 'BLOCKED', 'returncode': process.returncode,
                          'details': {'plan': plan, 'database': json.loads((run / 'ir.json').read_text())['database'], 'checks_executed': False}}
            else:
                process = bridge.call([sys.executable, str(PLATFORM / 'loop/loop.py'), str(job / 'upload'), str(run),
                                       '--max-attempts', '0', '--quality-network', 'railshot-quality', *selected], cwd=job, run=run)
                output = parse_result(process)
                if output.get('status') == 'UNKNOWN' and process.returncode != 0 and output.get('error'):
                    raise OperationError.from_dict(output['error'])
                evidence = json.loads((run / 'evidence.json').read_text())
                if (output.get('status') != evidence.get('status') or output.get('passed') != evidence.get('passed')
                        or (process.returncode == 0) != (evidence.get('passed') is True)
                        or output.get('error') != evidence.get('error')):
                    raise error('STEP_OUTPUT_INVALID', 'ci.receipt', unknown=True)
                if (run / 'ir.json').is_file() and (run / 'work').is_dir():
                    durable_write(workspace / 'source.json', json.dumps({**source_binding, 'work': (run / 'work').relative_to(workspace).as_posix()}).encode())
                result = {'outcome': evidence['status'], 'returncode': process.returncode,
                          'details': {k: evidence.get(k) for k in ('run_id', 'passed', 'result', 'agent_attempts', 'sdk_invocations', 'llm_calls', 'duration_s')},
                          **({'error': evidence['error']} if evidence.get('error') else {})}
                verdict_path = run / 'gate-0/verdict.json'
                if verdict_path.is_file():
                    verdict = json.loads(verdict_path.read_text())
                    bridge.verify_progress(run, verdict)
                    if verdict.get('status') != evidence['status'] or verdict.get('ok') != evidence['passed']:
                        raise error('STEP_OUTPUT_INVALID', 'ci.verdict', unknown=True)
                    result['details']['layers'] = [{k: layer.get(k) for k in ('layer', 'outcome', 'ok', 'blocked', 'started_at', 'duration_s')}
                                                    for layer in verdict.get('layers', [])]
                    if evidence['passed'] is True:
                        result['details']['bundle'] = export_bundle(bridge, run, verdict_path, job, root)
                elif evidence['passed'] is True:
                    raise error('STEP_OUTPUT_INVALID', 'ci.verdict', unknown=True)
        payload = json.dumps(redact(result), sort_keys=True).encode()
        durable_write(job / 'result.json', payload)
        result['receipt'] = {'path': (job / 'result.json').relative_to(root).as_posix(), 'sha256': hashlib.sha256(payload).hexdigest()}
        bridge.emit('result', **result)
        return result


def export_bundle(bridge, run, verdict, job, root):
    """Export the already validated images; no rebuild, credentials or payload on stdout."""
    output = job / 'bundle'
    bridge.emit('stage', phase='bundle', outcome='RUNNING')
    process = bridge.call([sys.executable, str(PLATFORM / 'gate/bundle.py'), 'export',
                           str(run / 'work'), str(verdict), str(output)], cwd=job)
    try:
        if process.returncode != 0: raise ValueError('bundle export did not complete')
        manifest = verify_bundle(output)
        if parse_result(process) != manifest: raise ValueError('bundle receipt differs')
        output.chmod(0o700)
        for path in output.iterdir():
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
                os.fchmod(stream.fileno(), 0o600); os.fsync(stream.fileno())
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(directory)
        finally: os.close(directory)
        receipt = {'relative_path': output.relative_to(root).as_posix(),
                   'manifest_sha256': hashlib.sha256((output / 'manifest.json').read_bytes()).hexdigest(),
                   'source_sha256': manifest['source_sha256'],
                   'bytes': sum(p.stat().st_size for p in output.iterdir())}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise error('STEP_OUTPUT_INVALID', 'bundle.receipt', unknown=True, cause=exc) from exc
    bridge.emit('stage', phase='bundle', outcome='PASS')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args(); request = {}
    def interrupted(signum, frame):
        raise error('STEP_TIMEOUT', 'signal', unknown=True)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    emit = lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)
    try:
        raw = sys.stdin.buffer.readline(REQUEST_BYTES + 1)
        if len(raw) > REQUEST_BYTES: raise error('CONTROL_CONFIG_INVALID', 'request')
        request = json.loads(raw)
        result = execute(request, args.root, emit)
        return 0 if result['outcome'] == 'PASS' else 1
    except OperationError as exc:
        failure = exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        failure = error('STEP_OUTPUT_INVALID', 'bridge', unknown=True, cause=exc)
    identity = {k: request.get(k) for k in ('workspace_id', 'job_id', 'generation')} if isinstance(request, dict) else {}
    emit({'type': 'result', **identity, 'outcome': failure.outcome, 'returncode': None, 'error': failure.as_dict()})
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
