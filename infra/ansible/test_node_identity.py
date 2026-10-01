"""Exercise the actual identity tasks against temporary paths; no cloud/service calls."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import yaml


class NodeIdentityTests(unittest.TestCase):
    def test_existing_identity_requires_explicit_same_machine_adoption(self):
        play = yaml.safe_load(Path(__file__).with_name('node.yml').read_text())[0]
        start = next(i for i, task in enumerate(play['tasks']) if task['name'] == 'Inspect stable node identity configuration')
        end = next(i for i, task in enumerate(play['tasks']) if task['name'] == 'Pin node identity independently of cloud hostname changes')
        original = play['tasks'][start:end + 1]
        for installed, adopt, machine_matches, recorded, desired, succeeds in [
            (False, False, True, None, 'new-node', True),
            (True, False, True, None, 'original.example.internal', False),
            (True, True, False, None, 'original.example.internal', False),
            (True, True, True, None, 'original.example.internal', True),
            (True, False, True, 'original.example.internal', 'new-node', False),
        ]:
            with self.subTest(installed=installed, adopt=adopt, matches=machine_matches, recorded=recorded), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); config = root / 'k3s/config.yaml.d/20-railshot-node-name.yaml'
                if installed: (root / 'k3s.service').touch()
                if recorded:
                    config.parent.mkdir(parents=True)
                    config.write_text(yaml.safe_dump({'node-name': recorded}))
                stub = root / 'kubectl.py'
                stub.write_text('import json; print(' + repr(json.dumps({'status': {'nodeInfo': {'systemUUID': 'machine-1' if machine_matches else 'other'}}})) + ')')
                tasks = copy.deepcopy(original)
                for task in tasks:
                    if 'ansible.builtin.command' in task:
                        task['ansible.builtin.command']['argv'] = [sys.executable, str(stub)]
                text = yaml.safe_dump([{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                    'vars': {'node_name': desired, 'adopt_existing_node_name': adopt, 'ansible_facts': {'product_uuid': 'machine-1'}}, 'tasks': tasks}])
                text = text.replace('/etc/rancher/k3s', str(root / 'k3s')).replace('/etc/systemd/system/k3s.service', str(root / 'k3s.service'))
                fixture = root / 'playbook.yml'; fixture.write_text(text)
                process = subprocess.run([shutil.which('ansible-playbook'), '-i', 'localhost,', str(fixture)],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, 'LC_ALL': 'en_US.UTF-8', 'LANG': 'en_US.UTF-8'})
                self.assertEqual(process.returncode == 0, succeeds, process.stdout + process.stderr)
                if succeeds:
                    self.assertEqual(yaml.safe_load(config.read_text()), {'node-name': desired})
                    self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                elif recorded:
                    self.assertEqual(yaml.safe_load(config.read_text()), {'node-name': recorded})
                else:
                    self.assertFalse(config.exists())


if __name__ == '__main__':
    unittest.main()
