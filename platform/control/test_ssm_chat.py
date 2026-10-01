import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ssm_chat as transport


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        payload = {'workspace_id': str(uuid.uuid4()), 'messages': [{'role': 'user', 'content': 'Explain the failed stage.'}], 'context': {'status': 'FAIL'}}
        self.request = {**payload, 'message_id': str(uuid.uuid4()), 'request_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}
        self.kw = {'root': self.root, 'instance': 'i-' + '1' * 17, 'region': 'ap-northeast-2', 'source': 'a' * 64}
        self.command_id = str(uuid.uuid4())
        self.reply = {'message_id': self.request['message_id'], 'request_sha256': self.request['request_sha256'], 'outcome': 'PASS', 'reply': 'The observed unit test failed.'}

    def aws(self, region, action, *args, **kwargs):
        intent = json.loads((self.root / self.request['message_id'] / 'intent.json').read_text())
        if action == 'send-command':
            self.assertEqual(intent['phase'], 'DISPATCH_INTENT')
            return subprocess.CompletedProcess([], 0, json.dumps({'Command': {'CommandId': self.command_id}}), '')
        self.assertEqual(intent['command_id'], self.command_id)
        return subprocess.CompletedProcess([], 0, json.dumps({'Status': 'Success', 'ResponseCode': 0, 'StandardOutputContent': json.dumps(self.reply)}), '')

    def test_command_id_durable_before_poll_and_terminal_replay(self):
        with patch.object(transport, 'aws', side_effect=self.aws) as aws:
            self.assertEqual(transport.dispatch(self.request, **self.kw), self.reply)
            self.assertEqual(transport.dispatch(self.request, **self.kw), self.reply)
            self.assertEqual(aws.call_count, 2)
        self.assertEqual((self.root / self.request['message_id'] / 'intent.json').stat().st_mode & 0o777, 0o600)

    def test_ambiguous_send_is_never_repeated(self):
        with patch.object(transport, 'aws', side_effect=TimeoutError) as aws:
            result = transport.dispatch(self.request, **self.kw)
            self.assertEqual(result['outcome'], 'UNKNOWN')
            self.assertEqual(transport.dispatch(self.request, **self.kw), result)
            self.assertEqual(aws.call_count, 1)

    def test_inflight_without_result_blocks_and_binding_drift_rejects(self):
        directory = transport.private_directory(self.root / self.request['message_id'])
        binding = {'request_sha256': self.request['request_sha256'], 'instance': self.kw['instance'], 'region': self.kw['region'], 'source_sha256': self.kw['source']}
        transport.save(directory / 'intent.json', {'binding': binding, 'phase': 'DISPATCHED', 'command_id': self.command_id})
        with patch.object(transport, 'aws') as aws:
            with self.assertRaises(transport.OperationError) as caught:
                transport.dispatch(self.request, **self.kw)
            self.assertEqual(caught.exception.code, 'SDK_OUTCOME_UNKNOWN')
            with self.assertRaises(transport.OperationError) as caught:
                transport.dispatch(self.request, **{**self.kw, 'source': 'b' * 64})
            self.assertEqual(caught.exception.code, 'STATE_BINDING_MISMATCH'); aws.assert_not_called()

    def test_remote_rc_status_mismatch_is_unknown(self):
        self.reply['outcome'] = 'FAIL'
        with patch.object(transport, 'aws', side_effect=self.aws):
            result = transport.dispatch(self.request, **self.kw)
        self.assertEqual(result['outcome'], 'UNKNOWN')
        self.assertEqual(result['error']['code'], 'STEP_OUTPUT_INVALID')

    def test_invalid_input_cannot_change_target_or_command(self):
        with patch.object(transport, 'aws') as aws:
            for source in ('../other', 'a' * 64 + '; id'):
                with self.assertRaises(transport.OperationError):
                    transport.dispatch(self.request, **{**self.kw, 'source': source})
            with self.assertRaises(transport.OperationError):
                transport.dispatch({**self.request, 'command': 'id'}, **self.kw)
            aws.assert_not_called()

    def test_optional_proposal_is_bounded_object_not_authority(self):
        invocation = {'Status': 'Success', 'ResponseCode': 0}
        for proposal, valid in [({'kind': 'draft'}, True), ('execute this', False), ({'text': 'a' * (21 * 1024)}, False)]:
            invocation['StandardOutputContent'] = json.dumps({**self.reply, 'proposal': proposal})
            if valid:
                self.assertEqual(transport.checked_result(invocation, self.request)['proposal'], proposal)
            else:
                with self.assertRaises(ValueError):
                    transport.checked_result(invocation, self.request)


if __name__ == '__main__':
    unittest.main()
