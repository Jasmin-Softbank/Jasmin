#!/usr/bin/env python3
"""One fixed AWS SSM chat dispatch; ambiguous execution is never resubmitted.

Private operator transport, not an arbitrary remote shell or tenant credential API.
AWS retains Run Command parameters: only the already bounded, secret-filtered chat
request is sent. Authentication stays on the pre-provisioned control instance.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observability import OperationError
from poc.intake import SECRET_TEXT
from process import run_bounded
from runner.runtime_boundary import private_directory
from storage import durable_write

MAX_REQUEST = 32 * 1024


def error(code, phase, *, unknown=False, cause=None):
    return OperationError(code, component='control.ssm_chat', phase=phase,
                          outcome='UNKNOWN' if unknown else 'BLOCKED',
                          retry_policy='after_reconcile' if unknown else 'after_configuration',
                          side_effect='unknown' if unknown else 'none', cause=cause)


def validate(request, instance, region, source):
    if (not re.fullmatch(r'i-[0-9a-f]{17}', instance)
            or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d', region)
            or not re.fullmatch(r'[0-9a-f]{64}', source)):
        raise error('CONTROL_CONFIG_INVALID', 'target')
    if not isinstance(request, dict) or set(request) != {'workspace_id', 'message_id', 'request_sha256', 'messages', 'context'}:
        raise error('CONTROL_CONFIG_INVALID', 'request')
    try:
        if not all(str(uuid.UUID(request[k])) == request[k] for k in ('workspace_id', 'message_id')):
            raise ValueError('invalid identity')
        if not isinstance(request['context'], dict) or not isinstance(request['messages'], list) or not request['messages']:
            raise ValueError('invalid context')
        if not all(isinstance(m, dict) and set(m) == {'role', 'content'} and m['role'] in {'user', 'assistant'}
                   and isinstance(m['content'], str) for m in request['messages']):
            raise ValueError('invalid messages')
        payload = {k: request[k] for k in ('workspace_id', 'messages', 'context')}
        if hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest() != request['request_sha256']:
            raise ValueError('request hash differs')
        encoded = json.dumps(request, sort_keys=True).encode()
        if len(encoded) > MAX_REQUEST or SECRET_TEXT.search(encoded):
            raise ValueError('request exceeds policy')
    except (ValueError, TypeError, AttributeError) as exc:
        raise error('CONTROL_CONFIG_INVALID', 'request', cause=exc) from exc
    return encoded


def save(path, data):
    durable_write(path, (json.dumps(data, sort_keys=True) + '\n').encode())


def aws(region, *args, timeout=45):
    # AWS CLI's own retries must not duplicate an ambiguous send-command.
    return run_bounded(['aws', 'ssm', *args, '--region', region, '--output', 'json',
                        '--no-cli-pager', '--cli-connect-timeout', '10', '--cli-read-timeout', '30'],
                       timeout=timeout, max_output_bytes=64 * 1024,
                       env={**os.environ, 'AWS_MAX_ATTEMPTS': '1', 'AWS_PAGER': ''})


def checked_result(invocation, request):
    result = json.loads(invocation.get('StandardOutputContent', ''))
    if (not isinstance(result, dict) or set(result) - {'message_id', 'request_sha256', 'outcome', 'reply', 'meta', 'error', 'proposal', 'proposal_review'}
            or any(result.get(k) != request[k] for k in ('message_id', 'request_sha256'))
            or result.get('outcome') not in {'PASS', 'FAIL', 'BLOCKED', 'UNKNOWN'}
            or (invocation.get('ResponseCode') == 0) != (result['outcome'] == 'PASS')
            or (invocation.get('Status') == 'Success') != (result['outcome'] == 'PASS')):
        raise ValueError('remote receipt differs')
    for key in ('proposal', 'proposal_review'):
        if key in result and (result['outcome'] != 'PASS' or not isinstance(result[key], dict)):
            raise ValueError('invalid proposal envelope')
    if result['outcome'] == 'PASS':
        if result.get('error') or not isinstance(result.get('reply'), str) or not result['reply'].strip():
            raise ValueError('reply missing')
    elif not isinstance(result.get('error'), dict):
        raise ValueError('failure evidence missing')
    if result.get('error'):
        parsed = OperationError.from_dict(result['error'])
        if parsed.outcome != result['outcome']:
            raise ValueError('error outcome differs')
        result['error'] = parsed.as_dict()
    encoded = json.dumps(result, ensure_ascii=False).encode()
    if len(encoded) > 20 * 1024 or SECRET_TEXT.search(encoded):
        raise ValueError('remote output violates contract')
    return result


def dispatch(request, *, root, instance, region, source, timeout=330):
    encoded = validate(request, instance, region, source)
    directory = private_directory(private_directory(root) / request['message_id'])
    binding = {'request_sha256': request['request_sha256'], 'instance': instance, 'region': region, 'source_sha256': source}
    lock = os.open(directory / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise error('STATE_WRITER_CONFLICT', 'dispatch', cause=exc) from exc
        intent = directory / 'intent.json'
        if intent.exists():
            previous = json.loads(intent.read_text())
            if previous['binding'] != binding:
                raise error('STATE_BINDING_MISMATCH', 'dispatch')
            if (directory / 'result.json').is_file():
                return json.loads((directory / 'result.json').read_text())
            raise error('SDK_OUTCOME_UNKNOWN', 'dispatch', unknown=True)
        command = ('printf %s ' + shlex.quote(base64.b64encode(encoded).decode()) +
                   ' | base64 --decode | ' + shlex.join([
                       'runuser', '-u', 'railshot-agent', '--', 'env', '-u', 'CODEX_API_KEY', '-u', 'OPENAI_API_KEY',
                       'RAILSHOT_AUTH_MODE=subscription', 'RAILSHOT_AGENT_AUTH_MODE=subscription',
                       'CODEX_HOME=/var/lib/railshot-agent/.codex', 'RAILSHOT_CODEX_HOME=/var/lib/railshot-agent/.codex',
                       '/opt/railshot/codex/bin/python', f'/opt/railshot-chat/source-{source}/platform/control/chat.py',
                       'once', '--root', '/var/lib/railshot-agent/chat']))
        params = directory / 'parameters.json'
        save(params, {'DocumentName': 'AWS-RunShellScript', 'InstanceIds': [instance],
                      'TimeoutSeconds': 60, 'Parameters': {'commands': [command], 'executionTimeout': ['300']}})
        state = {'binding': binding, 'phase': 'DISPATCH_INTENT', 'command_sha256': hashlib.sha256(command.encode()).hexdigest()}
        save(intent, state)  # Persist before the potentially non-idempotent request.
        try:
            deadline = time.monotonic() + timeout
            sent = aws(region, 'send-command', '--cli-input-json', 'file://' + str(params))
            if sent.returncode:
                raise error('SDK_OUTCOME_UNKNOWN', 'send', unknown=True)
            command_id = str(uuid.UUID(json.loads(sent.stdout)['Command']['CommandId']))
            state.update(phase='DISPATCHED', command_id=command_id)
            save(intent, state)  # CommandId is durable before any observation.
            while time.monotonic() < deadline:
                observed = aws(region, 'get-command-invocation', '--command-id', command_id, '--instance-id', instance,
                               timeout=max(0.001, min(45, deadline - time.monotonic())))
                if observed.returncode:
                    # Read-only eventual-consistency polling does not redispatch.
                    if 'InvocationDoesNotExist' in observed.stderr:
                        time.sleep(1); continue
                    raise error('SDK_OUTCOME_UNKNOWN', 'observe', unknown=True)
                invocation = json.loads(observed.stdout)
                if invocation.get('Status') in {'Pending', 'InProgress', 'Delayed'}:
                    time.sleep(1); continue
                try:
                    result = checked_result(invocation, request)
                except (ValueError, KeyError, TypeError) as exc:
                    raise error('STEP_OUTPUT_INVALID', 'receipt', unknown=True, cause=exc) from exc
                save(directory / 'result.json', result)
                return result
            raise error('STEP_TIMEOUT', 'observe', unknown=True)
        except Exception as exc:
            failure = exc if isinstance(exc, OperationError) else error('SDK_OUTCOME_UNKNOWN', 'dispatch', unknown=True, cause=exc)
            result = {**{k: request[k] for k in ('message_id', 'request_sha256')}, 'outcome': failure.outcome, 'error': failure.as_dict()}
            save(directory / 'result.json', result)
            return result
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--remote-source-ref', required=True)
    args = parser.parse_args(); request = {}
    try:
        raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise error('CONTROL_CONFIG_INVALID', 'request')
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise error('CONTROL_CONFIG_INVALID', 'request', cause=exc) from exc
        result = dispatch(request, root=args.root, instance=args.instance, region=args.region, source=args.remote_source_ref)
    except Exception as exc:
        failure = exc if isinstance(exc, OperationError) else error('OBSERVATION_WRITE_FAILED', 'transport', unknown=True, cause=exc)
        ids = {k: request[k] for k in ('message_id', 'request_sha256') if isinstance(request, dict) and k in request and isinstance(request[k], str) and re.fullmatch(r'[0-9a-f-]{36,64}', request[k])}
        result = {**ids, 'outcome': failure.outcome, 'error': failure.as_dict()}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['outcome'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
